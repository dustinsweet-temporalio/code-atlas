"""
Lead verification: check the index's leads against the source before a report
uses them.

Leads are the repository lens's gates (each with its evidence paths) and, for
existing Temporal code, the review-check findings in file lens records (`f`,
e.g. E-AO-STC@L88). Calibration showed about half of the findings are accurate,
so only confirmed leads reach the report body. One call per file, gates first,
then findings by relevance, at most MAX_LEADS in total.
"""
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import structlog

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens.pack import Pack
from semantic_ingress.report.cache import ReportCache, prompt_hash
from semantic_ingress.report.gather import IndexData
from semantic_ingress.report.models import FileVerdicts
from semantic_ingress.report.prompts import VERIFY_SYSTEM, verify_user_prompt

logger = structlog.get_logger()

MAX_LEADS = 60
WHOLE_FILE_LINES = 800
WINDOW = 60
HEAD_LINES = 40
SOURCE_CHARS = 60_000
WORKERS = 6
_ANCHOR = re.compile(r"^L(\d+)(?:-(\d+))?$")


@dataclass
class Lead:
    path: str
    key: str  # the gate id, or the finding as stored (E-AO-STC@L88)
    entry: str  # pack id
    anchor: tuple[int, int] | None
    rel: int

    @property
    def is_gate(self) -> bool:
        return self.entry.startswith("G-")


@dataclass
class LeadResult:
    lead: Lead
    verdict: str
    line: str
    severity: str
    reason: str


def parse_anchor(text: str) -> tuple[int, int] | None:
    m = _ANCHOR.match(text)
    if not m:
        return None
    start = int(m.group(1))
    return start, int(m.group(2) or start)


def collect_leads(data: IndexData, include_findings: bool) -> tuple[list[Lead], int]:
    """Leads to verify (gates first, then findings by relevance) and how many were left out by the cap."""
    gates = [
        Lead(path, a["id"], a["id"], None, data.files[path].rel)
        for a in data.repo_lens.get("anti", [])
        for path in a.get("evidence", [])
        if path in data.files
    ]
    findings = []
    if include_findings:
        for path, rec in data.files.items():
            for f in rec.f:
                entry, _, anchor = f.partition("@")
                findings.append(Lead(path, f, entry, parse_anchor(anchor), rec.rel))
        findings.sort(key=lambda lead: (-lead.rel, lead.path, lead.key))
    leads = gates + findings
    return leads[:MAX_LEADS], max(0, len(leads) - MAX_LEADS)


def source_text(lines: list[str], anchors: list[tuple[int, int]]) -> str:
    """Numbered source: the whole file if short, else the head plus windows around the anchors."""
    if len(lines) <= WHOLE_FILE_LINES:
        keep = set(range(len(lines)))
    elif anchors:
        keep = set(range(HEAD_LINES))
        for start, end in anchors:
            keep.update(range(max(0, start - 1 - WINDOW), min(len(lines), end + WINDOW)))
    else:
        keep = set(range(WHOLE_FILE_LINES))
    out, prev, used = [], -1, 0
    for i in sorted(keep):
        if prev != -1 and i != prev + 1:
            out.append("...")
        line = f"{i + 1}: {lines[i]}"
        used += len(line) + 1
        if used > SOURCE_CHARS:
            out.append("... (truncated)")
            break
        out.append(line)
        prev = i
    return "\n".join(out)


def _verify_file(llm, pack: Pack, path: str, leads: list[Lead], lines: list[str]) -> tuple[list[LeadResult], LLMUsage]:
    zero = LLMUsage(input_tokens=0, output_tokens=0)
    if not lines:
        return [LeadResult(lead, "unclear", "", "n/a", "Source not available in the mirror.") for lead in leads], zero

    leads_text = "\n".join(
        f"{lead.key} | {lead.entry} | {pack.entries[lead.entry]['t']} | {pack.entries[lead.entry]['s']}"
        if lead.entry in pack.entries else f"{lead.key} | {lead.entry} | (not in pack) |"
        for lead in leads
    )
    anchors = [lead.anchor for lead in leads if lead.anchor]
    answer, usage = llm.generate(
        verify_user_prompt(path, leads_text, source_text(lines, anchors)),
        FileVerdicts, system=VERIFY_SYSTEM, enforce_schema=True,
    )
    by_key = {v.lead: v for v in answer.verdicts}

    results = []
    for lead in leads:
        v = by_key.get(lead.key)
        if v is None:
            results.append(LeadResult(lead, "unclear", "", "n/a", "Not answered by the verifier."))
            continue
        verdict, line, severity, reason = v.verdict, v.line.strip(), v.severity, v.reason.strip()
        anchor = parse_anchor(line)
        if line and (anchor is None or anchor[0] < 1 or anchor[1] > len(lines)):
            line = ""
        if verdict == "confirmed" and not line:
            verdict, reason = "unclear", f"No valid line anchor given. {reason}"
        if verdict != "confirmed" or lead.is_gate:
            severity = "n/a"
        elif severity == "n/a":
            severity = "medium"
        results.append(LeadResult(lead, verdict, line, severity, reason))
    return results, usage


def verify_leads(llm, pack: Pack, data: IndexData, leads: list[Lead],
                 cache: ReportCache | None = None) -> tuple[list[LeadResult], LLMUsage]:
    """Verdicts for every lead; a file's verdicts are cached on its blob, its leads, pack, model and prompt."""
    by_path: dict[str, list[Lead]] = defaultdict(list)
    for lead in leads:
        by_path[lead.path].append(lead)

    def cache_parts(path: str, group: list[Lead]) -> tuple:
        return (path, data.blobs.get(path), sorted(lead.key for lead in group), pack.version,
                llm.config.model_id, prompt_hash(VERIFY_SYSTEM))

    total = LLMUsage(input_tokens=0, output_tokens=0)
    results: list[LeadResult] = []
    todo: dict[str, list[Lead]] = {}
    cached: dict[str, list[LeadResult]] = {}
    for path, group in sorted(by_path.items()):
        hit = cache.get("leads", *cache_parts(path, group)) if cache else None
        if hit is None:
            todo[path] = group
        else:
            by_key = {v["lead"]: v for v in hit}
            cached[path] = [LeadResult(lead, by_key[lead.key]["verdict"], by_key[lead.key]["line"],
                                       by_key[lead.key]["severity"], by_key[lead.key]["reason"]) for lead in group]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            path: pool.submit(_verify_file, llm, pack, path, group, data.source_lines(path))
            for path, group in todo.items()
        }
        fresh: dict[str, list[LeadResult]] = {}
        for path, future in futures.items():
            try:
                file_results, usage = future.result()
                if cache:
                    cache.put("leads", *cache_parts(path, todo[path]), value=[
                        {"lead": r.lead.key, "verdict": r.verdict, "line": r.line, "severity": r.severity,
                         "reason": r.reason} for r in file_results
                    ])
            except Exception as e:  # one failed file must not sink the report (and is not cached)
                logger.warning("lead_verification_failed", path=path, error=str(e))
                file_results = [LeadResult(lead, "unclear", "", "n/a", f"Verification failed: {e}") for lead in todo[path]]
                usage = LLMUsage(input_tokens=0, output_tokens=0)
            fresh[path] = file_results
            total = total + usage
    for path in sorted(by_path):
        results.extend(cached.get(path) or fresh[path])
    return results, total
