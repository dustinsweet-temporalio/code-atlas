"""
Process discovery, investigation and card checks.

Discovery is deterministic: the repository lens's processes, plus relevance >= 2
entry points no process covers. Each process is investigated by one tool-using
call over the index and the mirrored source, which fills in a process card.
The card is then checked:

1. in code: every reference must be an indexed path, with lines in range that
   the investigator actually saw;
2. by a verification call: every fact against its cited lines; unsupported
   facts are dropped. A card whose persistence or crash-loss facts do not hold
   is marked low confidence.

Checked cards are stored with the blob ids of the files they depend on and
reused while those files are unchanged.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.report.gather import IndexData
from semantic_ingress.report.models import CardVerdicts, ProcessCard
from semantic_ingress.report.prompts import (
    INVESTIGATE_NUDGE,
    INVESTIGATE_SYSTEM,
    VERIFY_CARD_SYSTEM,
    investigate_user_prompt,
)
from semantic_ingress.report.tools import TOOLS, InvestigationTools

logger = structlog.get_logger()

MAX_PROCESSES = 8
FALLBACK_ENTRIES = 3
MAX_TOOL_CALLS = 25
MIN_TOOL_CALLS = 8
RECORDS_CHARS = 30_000
REF_LINES = 80
VERIFY_CHARS = 60_000
CARD_VERSION = 2  # 2: cards keyed on the entry point
NOT_ESTABLISHED = "Not established from the code."
_REF = re.compile(r"^(?P<path>.+)@L(?P<start>\d+)(?:-(?P<end>\d+))?$")
SINGLE_FACTS = ["trigger", "unit_of_work", "state_between_steps", "persistence", "run_length"]


@dataclass
class Candidate:
    name: str
    entry: str
    files: list[str]
    narrative: str
    origin: str  # "repository lens" or "entry point"
    shares_entry: bool = False  # another process starts at the same entry point

    @property
    def key(self) -> str:
        """Cards are keyed on the entry point, so renaming a process keeps its card."""
        ident = f"{self.entry}|{self.name}" if self.shares_entry else self.entry
        return hashlib.sha1(ident.encode()).hexdigest()[:16]


@dataclass
class CardResult:
    candidate: Candidate
    card: dict
    verdicts: list[dict] = field(default_factory=list)
    low_confidence: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    reused: bool = False
    tool_calls: int = 0
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(input_tokens=0, output_tokens=0))


# --- discovery -------------------------------------------------------------------

def discover(data: IndexData, exclude: set[str]) -> tuple[list[Candidate], list[Candidate], list[str]]:
    """(processes to investigate, processes over the limit, rel-1 entry points no process covers)."""
    lens_procs = data.repo_lens.get("processes", [])
    covered = {p for proc in lens_procs for p in [proc["entry"], *proc["files"]]}
    found = [
        Candidate(p["name"], p["entry"], p["files"], p["narrative"], "repository lens")
        for p in lens_procs if not ({p["entry"], *p["files"]} & exclude)
    ]

    def is_entry(path: str) -> bool:
        rec = data.files[path]
        return "ENTRY" in rec.kind or any(s.startswith("trigger.") for s in rec.sig)

    loose = sorted(
        (p for p in data.files if p not in covered and p not in exclude and is_entry(p)),
        key=lambda p: (-data.files[p].rel, p),
    )
    found += [Candidate(f"Entry point {p}", p, [p], data.files[p].n, "entry point")
              for p in loose if data.files[p].rel >= 2]
    uncovered = [p for p in loose if data.files[p].rel == 1]
    entries = [c.entry for c in found]
    for c in found:
        c.shares_entry = entries.count(c.entry) > 1
    if not found:  # nothing else to assess: take the relevance-1 entry points
        found = [Candidate(f"Entry point {p}", p, [p], data.files[p].n, "entry point")
                 for p in uncovered[:FALLBACK_ENTRIES]]
        uncovered = uncovered[FALLBACK_ENTRIES:]
    return found[:MAX_PROCESSES], found[MAX_PROCESSES:], uncovered


# --- card facts ------------------------------------------------------------------

def card_facts(card: dict) -> list[tuple[str, str, int | None, str, str, list[str]]]:
    """Every citable fact as (fact id, field, list index or None, label, text, refs), in a fixed order."""
    out: list[tuple[str, int | None, str, str, list[str]]] = []
    out.append(("trigger", None, "trigger", card["trigger"]["text"], card["trigger"]["refs"]))
    out.append(("unit_of_work", None, "unit of work", card["unit_of_work"]["text"], card["unit_of_work"]["refs"]))
    for i, s in enumerate(card["steps"]):
        effects = f" ({', '.join(s['effects'])})" if s["effects"] else ""
        out.append(("steps", i, f"step {i + 1}: {s['name']}", s["does"] + effects, s["refs"]))
    out.append(("state_between_steps", None, "state between steps", card["state_between_steps"]["text"],
                card["state_between_steps"]["refs"]))
    out.append(("persistence", None, "persistence", card["persistence"]["text"], card["persistence"]["refs"]))
    for i, c in enumerate(card["crash_loss"]):
        out.append(("crash_loss", i, f"crash {c['stage']}", c["loses"], c["refs"]))
    for i, f in enumerate(card["reliability"]):
        out.append(("reliability", i, "reliability", f["text"], f["refs"]))
    for i, f in enumerate(card["waits"]):
        out.append(("waits", i, "wait", f["text"], f["refs"]))
    out.append(("run_length", None, "run length", card["run_length"]["text"], card["run_length"]["refs"]))
    return [(f"F{n}", *fact) for n, fact in enumerate(out, 1)]


def card_text(card: dict) -> str:
    """The card as numbered facts, for scoring and write-up prompts."""
    lines = [f"PROCESS CARD: {card['process']}"]
    for fid, _, _, label, text, refs in card_facts(card):
        lines.append(f"{fid} {label}: {text}" + (f" [{', '.join(refs)}]" if refs else ""))
    lines.append(f"Units scale with: {card['scales_with']}")
    lines.append(f"Scale assumption: {card['scale_assumption']}")
    lines.append(f"Files: {', '.join(card['files'])}")
    return "\n".join(lines)


def _remove(card: dict, field_name: str, index: int | None) -> None:
    if index is None:
        card[field_name] = {"text": NOT_ESTABLISHED, "refs": []}
    else:
        card[field_name][index] = None


def _compact(card: dict) -> None:
    for name in ("steps", "crash_loss", "reliability", "waits"):
        card[name] = [x for x in card[name] if x is not None]


def parse_ref(ref: str) -> tuple[str, int, int] | None:
    m = _REF.match(ref.strip())
    if not m:
        return None
    start = int(m.group("start"))
    return m.group("path"), start, int(m.group("end") or start)


# --- code checks -------------------------------------------------------------------

def check_refs(card: dict, data: IndexData, seen: dict[str, set[int]] | None) -> list[str]:
    """Drop references that are not indexed, out of range or unseen; facts left without one are dropped."""
    dropped: list[str] = []

    def ok(ref: str) -> bool:
        parsed = parse_ref(ref)
        if parsed is None:
            return False
        path, start, end = parsed
        if path not in data.blobs or not (1 <= start <= end <= data.line_count(path)):
            return False
        return seen is None or (start in seen.get(path, ()) and end in seen.get(path, ()))

    for fid, field_name, index, label, text, refs in card_facts(card):
        good = [r for r in refs if ok(r)]
        for r in refs:
            if r not in good:
                dropped.append(f"{label}: reference dropped ({r})")
        target = card[field_name] if index is None else card[field_name][index]
        target["refs"] = good
        if not good and text.strip() != NOT_ESTABLISHED:
            dropped.append(f"{label}: dropped (no valid reference): {text}")
            _remove(card, field_name, index)
    _compact(card)
    card["files"] = [p for p in dict.fromkeys(card["files"]) if p in data.blobs]
    return dropped


# --- investigation and verification ----------------------------------------------------

def _records(data: IndexData, cand: Candidate) -> str:
    first = [p for p in dict.fromkeys([cand.entry, *cand.files]) if p in data.files]
    others = sorted((p for p, r in data.files.items() if r.rel >= 2 and p not in first), key=lambda p: (-data.files[p].rel, p))
    out, used = [], 0
    for p in first + others:
        line = f"{p}: {compact_file_lens(data.files[p])}"
        if used + len(line) > RECORDS_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "(none)"


def investigate(inv_llm, data: IndexData, store, repo_id: str, cand: Candidate) -> tuple[dict, dict, LLMUsage, int]:
    tools = InvestigationTools(data, store, repo_id)
    card, usage, calls = inv_llm.generate_with_tools(
        investigate_user_prompt(cand.name, cand.entry, cand.files, cand.narrative, _records(data, cand)),
        ProcessCard,
        system=INVESTIGATE_SYSTEM.format(max_calls=MAX_TOOL_CALLS),
        tools=TOOLS,
        tool_handler=tools.handle,
        max_tool_calls=MAX_TOOL_CALLS,
        min_tool_calls=MIN_TOOL_CALLS,
        nudge=INVESTIGATE_NUDGE,
    )
    data_card = card.model_dump()
    data_card["process"] = cand.name
    return data_card, tools.seen, usage, calls


def _excerpt(data: IndexData, refs: list[str]) -> str:
    parts = []
    for ref in refs:
        parsed = parse_ref(ref)
        if parsed is None:
            continue
        path, start, end = parsed
        lines = data.source_lines(path)
        end = min(end, start + REF_LINES - 1, len(lines))
        parts.append(f"--- {path} L{start}-{end}\n" + "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1)))
    return "\n".join(parts)


def verify_card(llm, data: IndexData, card: dict) -> tuple[list[dict], list[str], list[str], LLMUsage]:
    """Check each fact against its cited lines. Returns (verdicts, dropped, low-confidence reasons, usage)."""
    facts = [f for f in card_facts(card) if f[5]]
    zero = LLMUsage(input_tokens=0, output_tokens=0)
    if not facts:
        return [], [], ["no fact could be tied to the source"], zero
    blocks, used = [], 0
    for fid, _, _, label, text, refs in facts:
        block = f"{fid} ({label}): {text}\n{_excerpt(data, refs)}"
        if used + len(block) > VERIFY_CHARS:
            block = block[: max(500, VERIFY_CHARS - used)] + "\n... (truncated)"
        blocks.append(block)
        used += len(block)
    answer, usage = llm.generate("FACTS AND THEIR CITED LINES:\n\n" + "\n\n".join(blocks), CardVerdicts,
                                 system=VERIFY_CARD_SYSTEM, enforce_schema=True)
    by_id = {v.fact: v for v in answer.verdicts}

    verdicts, dropped = [], []
    for fid, field_name, index, label, text, refs in facts:
        v = by_id.get(fid)
        verdict, reason = (v.verdict, v.reason) if v else ("unclear", "Not answered by the verifier.")
        verdicts.append({"fact": label, "text": text, "verdict": verdict, "reason": reason})
        if verdict == "unsupported":
            dropped.append(f"{label}: dropped (not supported by the cited lines): {text}")
            _remove(card, field_name, index)
    _compact(card)

    low = []
    if card["persistence"]["text"] == NOT_ESTABLISHED:
        low.append("when results become durable could not be established")
    if not card["crash_loss"]:
        low.append("what a crash loses could not be established")
    return verdicts, dropped, low, usage


# --- per-process pipeline with the card cache --------------------------------------------

def _depends(data: IndexData, card: dict, cand: Candidate) -> dict[str, str]:
    paths = {cand.entry, *card["files"]}
    for *_, refs in card_facts(card):
        paths.update(p[0] for p in map(parse_ref, refs) if p)
    return {p: data.blobs[p] for p in sorted(paths) if p in data.blobs}


def process_card(llm, inv_llm, data: IndexData, store, repo_id: str, cand: Candidate, fresh: bool) -> CardResult:
    """The checked card for a process: reused if its files are unchanged, otherwise investigated and checked."""
    key = f"{repo_id}::card::{cand.key}"
    if not fresh:
        doc = store.get_process_card(repo_id, key)
        if (doc and doc.get("card_version") == CARD_VERSION
                and all(data.blobs.get(p) == b for p, b in doc["depends"].items())):
            card = {**doc["card"], "process": cand.name}  # the process may have been renamed
            return CardResult(cand, card, doc["verdicts"], doc["low_confidence"], doc["dropped"], reused=True)

    card, seen, usage, calls = investigate(inv_llm, data, store, repo_id, cand)
    dropped = check_refs(card, data, seen)
    verdicts, more, low, u = verify_card(llm, data, card)
    usage = usage + u
    dropped += more
    store.put_process_card(repo_id, {
        "_id": key, "name": cand.name, "entry": cand.entry, "card_version": CARD_VERSION,
        "card": card, "verdicts": verdicts, "low_confidence": low, "dropped": dropped,
        "depends": _depends(data, card, cand), "model": inv_llm.config.model_id,
        "created": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("process_investigated", process=cand.name, tool_calls=calls, dropped=len(dropped),
                low_confidence=bool(low), cost_usd=round(usage.total_cost_usd, 4))
    return CardResult(cand, card, verdicts, low, dropped, reused=False, tool_calls=calls, usage=usage)
