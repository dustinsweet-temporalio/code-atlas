"""
Repository-level lens pass: one LLM call (with an optional Temporal Docs search
tool) over the Tier 0 facts, the root roll-up and the file lens records.

The answer is then checked in code against the inputs: detected technologies come
from Tier 0 only, status must agree with the Temporal file count, paths must be
indexed files, gates must be pack gates, and citations must be pack ids or URLs
the docs search actually returned in this run.
"""
from dataclasses import dataclass, field
from typing import Callable

import structlog

from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.lens.models import DetectedTech, RepoLens
from semantic_ingress.lens.pack import Pack, repo_view
from semantic_ingress.lens.prompts import (
    REPO_LENS_SYSTEM,
    REPO_NO_SEARCH_INSTRUCTIONS,
    REPO_SEARCH_INSTRUCTIONS,
    repo_user_prompt,
)
from semantic_ingress.lens.tier0 import Tier0Facts

logger = structlog.get_logger()

# A docs search returns chunks: [{"source_url": ..., "content": ...}, ...]
DocsSearch = Callable[[str], list[dict]]

MAX_SEARCHES = 6
RECORDS_BUDGET_CHARS = 240_000  # about 60k tokens of compact records
CHUNKS_PER_SEARCH = 5
CHUNK_CHARS = 1500

SEARCH_TOOL = {
    "name": "search_temporal_docs",
    "description": (
        "Search the official Temporal documentation and knowledge sources. Returns the most "
        "relevant passages with their source URLs. Use a complete natural-language question."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A complete question"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}


@dataclass
class RepoPassResult:
    lens: RepoLens
    usage: LLMUsage
    searches: int
    urls_returned: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)


def _records_text(records: dict[str, tuple[int, str]]) -> tuple[str, int]:
    """Compact records, highest relevance first, within the budget. Returns (text, omitted)."""
    ordered = sorted(records.items(), key=lambda kv: (-kv[1][0], kv[0]))
    lines, used, omitted = [], 0, 0
    for path, (rel, compact) in ordered:
        line = f"{path}: {compact}"
        if used + len(line) > RECORDS_BUDGET_CHARS:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), omitted


def _format_chunks(chunks: list[dict]) -> str:
    official = [c for c in chunks if "docs.temporal.io" in c.get("source_url", "")]
    others = [c for c in chunks if c not in official]
    picked = (official + others)[:CHUNKS_PER_SEARCH]
    if not picked:
        return "No results."
    return "\n\n".join(f"SOURCE: {c['source_url']}\n{c['content'][:CHUNK_CHARS]}" for c in picked)


def run_repo_pass(
    llm: LLMClient,
    pack: Pack,
    tier0: Tier0Facts,
    root_rollup_text: str,
    records: dict[str, tuple[int, str]],
    known_paths: set[str],
    docs_search: DocsSearch | None,
    previous: list[dict] | None = None,
    changed: list[str] | None = None,
) -> RepoPassResult:
    """
    Args:
        records: path -> (rel, compact lens line) for files with rel >= 1 (rel 3 first within the budget)
        known_paths: every indexed file path (to validate paths the model names)
        docs_search: Temporal Docs search, or None to rely on pack entries only
        previous: the last run's processes (incremental runs), kept stable unless the changes call for more
        changed: files regenerated in this run
    """
    view, pack_ids = repo_view(pack)
    search_instructions = (
        REPO_SEARCH_INSTRUCTIONS.format(max_searches=MAX_SEARCHES) if docs_search else REPO_NO_SEARCH_INSTRUCTIONS
    )
    system = REPO_LENS_SYSTEM.format(search_instructions=search_instructions) + "\n\n" + view
    records_text, omitted = _records_text(records)
    prompt = repo_user_prompt(tier0.to_text(), root_rollup_text, records_text or "(none)", omitted,
                              previous=previous, changed=changed)

    urls_returned: list[str] = []
    if docs_search is None:
        lens, usage = llm.generate(prompt, RepoLens, system=system, enforce_schema=True)
        searches = 0
    else:
        def handle(name: str, tool_input: dict) -> str:
            chunks = docs_search(tool_input["query"])
            urls_returned.extend(c["source_url"] for c in chunks if c.get("source_url"))
            return _format_chunks(chunks)

        lens, usage, searches = llm.generate_with_tools(
            prompt, RepoLens, system=system, tools=[SEARCH_TOOL], tool_handler=handle,
            max_tool_calls=MAX_SEARCHES,
        )

    lens, corrections = _check(lens, pack, pack_ids, tier0, known_paths, set(urls_returned))
    if docs_search is not None and searches == 0 and lens.processes:
        corrections.append("no docs search was made although one was available")
    for c in corrections:
        logger.info("repo_lens_corrected", correction=c)
    return RepoPassResult(lens, usage, searches, sorted(set(urls_returned)), corrections)


def _check(
    lens: RepoLens,
    pack: Pack,
    pack_ids: set[str],
    tier0: Tier0Facts,
    known_paths: set[str],
    urls: set[str],
) -> tuple[RepoLens, list[str]]:
    corrections: list[str] = []
    data = lens.model_dump()

    # Detected technologies: Tier 0 is the only source.
    detected = [DetectedTech(kind="temporal_sdk", name=lang, evidence=paths).model_dump()
                for lang, paths in sorted(tier0.temporal_sdk.items())]
    detected += [DetectedTech(kind="adjacent", name=name, evidence=paths).model_dump()
                 for name, paths in sorted(tier0.adjacent.items())]
    if data["detected"] != detected:
        corrections.append("detected replaced with Tier 0 facts")
    data["detected"] = detected

    # Status must agree with the deterministic facts.
    if tier0.temporal_file_count == 0 and data["status"] in ("partial", "full"):
        status = "adjacent" if tier0.adjacent else "none"
        corrections.append(f"status {data['status']} -> {status} (no Temporal SDK files)")
        data["status"] = status
    elif tier0.temporal_file_count > 0 and data["status"] in ("none", "adjacent"):
        corrections.append(f"status {data['status']} -> partial (Temporal SDK files present)")
        data["status"] = "partial"

    # Processes: only indexed paths, only pack ids.
    processes = []
    for proc in data["processes"][:8]:
        if proc["entry"] not in known_paths:
            corrections.append(f"process {proc['name']!r} dropped (unknown entry {proc['entry']})")
            continue
        proc["files"] = [f for f in proc["files"] if f in known_paths]
        proc["concepts"] = [c for c in proc["concepts"] if c in pack_ids or c in pack.entries]
        processes.append(proc)
    data["processes"] = processes

    # Anti-signals: gate ids only, indexed paths only.
    anti = []
    for a in data["anti"]:
        if not a["id"].startswith("G-") or a["id"] not in pack_ids:
            corrections.append(f"anti {a['id']} dropped (not a pack gate)")
            continue
        a["evidence"] = [p for p in a["evidence"] if p in known_paths]
        anti.append(a)
    data["anti"] = anti

    # Citations: pack ids, or URLs the search returned in this run.
    cites = []
    for c in data["cites"]:
        ref = c["ref"]
        if ref in pack_ids or ref in urls:
            cites.append(c)
        else:
            corrections.append(f"citation dropped (not a pack id or returned URL): {ref}")
    data["cites"] = cites

    return RepoLens.model_validate(data), corrections


def compact_repo_lens(lens: RepoLens) -> str:
    """One-line serving form of the repository lens."""
    parts = [f"status {lens.status} | archetype {lens.archetype}"]
    if lens.detected:
        parts.append("detected " + " ".join(f"{d.kind}:{d.name}" for d in lens.detected))
    if lens.processes:
        parts.append("processes " + "; ".join(f"{p.name} ({p.entry})" for p in lens.processes))
    if lens.anti:
        parts.append("anti " + " ".join(a.id for a in lens.anti))
    parts.append(lens.n)
    return " | ".join(parts)
