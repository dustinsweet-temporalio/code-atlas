"""
Rubric scoring.

Suitability, per process: one structured call scores the rubric's value paths and
modifiers from the process card (evidence is card facts or indexed paths); code
checks it, sets the gates bar from the verified gates, and computes fit and the
overall score (Rubric.overall).

Evaluation: bars computed in code from confirmed findings (10 minus a deduction
per finding by severity). B-E-COVERAGE is the share of durable-relevant files
(relevance >= 2) that are already Temporal code.

% Temporalized follows the rubric's pct_temporalized metric.
"""
import re
from dataclasses import dataclass, field

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens.pack import Pack
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.report.gather import IndexData
from semantic_ingress.report.grounding import Grounding
from semantic_ingress.report.investigate import card_facts, card_text
from semantic_ingress.report.models import ConversionScores
from semantic_ingress.report.prompts import RETRY_NOTE, SCORE_SYSTEM
from semantic_ingress.report.rubric import Rubric
from semantic_ingress.report.verify import LeadResult

RECORDS_BUDGET_CHARS = 40_000
_SIG_CODE = re.compile(r"[:@]")


# --- evaluation (code only) ----------------------------------------------------

@dataclass
class EvalBar:
    id: str
    t: str
    score: float
    reason: str
    findings: list[LeadResult] = field(default_factory=list)


def coverage(data: IndexData) -> tuple[int, int]:
    """(Temporal files, all files) among relevance >= 2 files."""
    durable = [p for p, r in data.files.items() if r.rel >= 2]
    return len([p for p in durable if p in data.temporal_files]), len(durable)


def evaluation_bars(rubric: Rubric, data: IndexData, confirmed: list[LeadResult]) -> tuple[list[EvalBar], list[LeadResult]]:
    """Bars with their findings (plus coverage), and confirmed findings no bar takes."""
    deductions = rubric.deductions
    bars = [EvalBar(b["id"], b["t"], 10.0, "") for b in rubric.evaluate_bars]
    by_id = {b.id: b for b in bars}
    other = []
    for result in confirmed:
        bar_id = rubric.bar_for_check(result.lead.entry)
        if bar_id in by_id:
            by_id[bar_id].findings.append(result)
        else:
            other.append(result)
    for bar in bars:
        bar.score = max(0.0, 10 - sum(deductions[f.severity] for f in bar.findings))
        counts = {s: sum(1 for f in bar.findings if f.severity == s) for s in ("high", "medium", "low")}
        bar.reason = (
            "No confirmed findings" if not bar.findings
            else ", ".join(f"{n} {s}" for s, n in counts.items() if n) + " confirmed"
        )
    cov = rubric.data["evaluate"]["coverage"]
    temporal, durable = coverage(data)
    bars.append(EvalBar(cov["id"], cov["t"], round(10 * temporal / durable) if durable else 10,
                        f"{temporal} of {durable} durable-relevant files are Temporal code"))
    return bars, other


# --- % Temporalized ------------------------------------------------------------

def pct_temporalized(data: IndexData, moved: set[str]) -> dict:
    """
    Share of relevance >= 2 files (and their lines) that are Temporal code today,
    and after the designs move `moved` files (primary kind ORCH or ACT) into
    workflows or activities.
    """
    durable = sorted(p for p, r in data.files.items() if r.rel >= 2)
    today = {p for p in durable if p in data.temporal_files}
    after = today | {p for p in durable if p in moved and data.files[p].kind[0] in ("ORCH", "ACT")}
    lines = {p: data.line_count(p) for p in durable}
    total_lines = sum(lines.values())

    def pct(part: int, whole: int) -> int:
        return round(100 * part / whole) if whole else 0

    return {
        "files_total": len(durable),
        "files_today": len(today),
        "files_after": len(after),
        "lines_total": total_lines,
        "lines_today": sum(lines[p] for p in today),
        "lines_after": sum(lines[p] for p in after),
        "pct_today": pct(len(today), len(durable)),
        "pct_after": pct(len(after), len(durable)),
        "pct_lines_today": pct(sum(lines[p] for p in today), total_lines),
        "pct_lines_after": pct(sum(lines[p] for p in after), total_lines),
        "moved_files": sorted(after - today),
    }


# --- suitability per process (one LLM call, checked in code) ------------------------

def records_text(data: IndexData, paths: set[str], min_rel: int = 1) -> str:
    rows = sorted(((r.rel, p) for p, r in data.files.items() if p in paths and r.rel >= min_rel), key=lambda x: (-x[0], x[1]))
    out, used = [], 0
    for _, path in rows:
        line = f"{path}: {compact_file_lens(data.files[path])}"
        if used + len(line) > RECORDS_BUDGET_CHARS:
            out.append(f"({len(rows) - len(out)} lower-relevance records omitted)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "(none)"


def signal_hits(data: IndexData, paths: set[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for path in sorted(paths):
        if path in data.files:
            for sig in data.files[path].sig:
                hits.setdefault(_SIG_CODE.split(sig, 1)[0], []).append(path)
    return hits


def rubric_text(rubric: Rubric, data: IndexData, paths: set[str], gates: dict[str, list[str]],
                rejected: dict[str, str]) -> str:
    """The value paths and modifiers with anchors, and the index's hits for each among the process files."""
    hits = signal_hits(data, paths)
    out = []
    for section, bars in (("VALUE PATHS (fit is the best of these)", rubric.value_paths),
                          ("MODIFIERS", rubric.modifiers)):
        out.append(section)
        for bar in bars:
            anchors = "; ".join(f"{k}: {v}" for k, v in bar["anchors"].items())
            out.append(f"{bar['id']} | {bar['t']} | {bar['q']}\n  anchors: {anchors}")
            if bar.get("consider"):
                out.append(f"  consider: {bar['consider']}")
            found = []
            for code in bar.get("signals") or []:
                files = sorted({p for c, ps in hits.items() if (c.startswith(code[:-1]) if code.endswith(".*") else c == code) for p in ps})
                if files:
                    found.append(f"{code}: {', '.join(files[:4])}")
            for gate in bar.get("gates") or []:
                status = (f"verified in {', '.join(gates[gate])}" if gate in gates
                          else "not confirmed by verification" if gate in rejected else "not raised")
                found.append(f"{gate}: {status}")
            out.append("  index: " + ("; ".join(found) or "no signals in the process files"))
    return "\n".join(out)


def _problems(answer: ConversionScores, rubric: Rubric, fact_ids: set[str], grounding: Grounding) -> list[str]:
    ids = [b["id"] for b in rubric.scored]
    given = [b.id for b in answer.bars]
    problems = [f"bar {i} must be scored exactly once (given {given.count(i)} times)" for i in ids if given.count(i) != 1]
    for bar in answer.bars:
        if bar.id not in ids:
            problems.append(f"bar {bar.id} is not a rubric bar")
            continue
        if not 0 <= bar.score <= 10:
            problems.append(f"{bar.id}: score {bar.score} is outside 0-10")
        valid = [e for e in bar.evidence if e in fact_ids or e in grounding.paths]
        if bar.score > 0 and bar.id != rubric.gates_bar and not valid:
            problems.append(f"{bar.id}: a score above 0 needs evidence (card fact ids or indexed paths)")
        problems += [f"{bar.id}: evidence {e!r} is not a card fact id or an indexed path"
                     for e in bar.evidence if e not in valid]
        problems += [f"{bar.id}: citation {c!r} is not a pack id, or is an unconfirmed gate"
                     for c in bar.cites if not grounding.pack_id_ok(c)]
    return problems


def score_process(
    llm, rubric: Rubric, pack: Pack, data: IndexData, grounding: Grounding, card: dict,
    paths: set[str], gates: dict[str, list[str]], entries_text: str,
) -> tuple[dict[str, dict], LLMUsage, list[str]]:
    """Bar id -> {score, reason, evidence, signals, cites}; usage; notes (corrections)."""
    system = SCORE_SYSTEM + "\n\n" + entries_text
    verified = "\n".join(f"{g}: {', '.join(p)}" for g, p in gates.items()) or "(none verified for this process)"
    prompt = (
        f"RUBRIC (with the index's hits among this process's files):\n"
        f"{rubric_text(rubric, data, paths, gates, grounding.rejected_gates)}\n\n"
        f"VERIFIED GATES FOR THIS PROCESS:\n{verified}\n\n"
        f"GATES NOT CONFIRMED BY VERIFICATION (do not rely on them):\n{grounding.rejected_text()}\n\n"
        f"{card_text(card)}\n\n"
        f"FILE LENS RECORDS OF THE PROCESS FILES (path: record):\n{records_text(data, paths, min_rel=0)}"
    )
    fact_ids = {f[0] for f in card_facts(card)}
    answer, usage = llm.generate(prompt, ConversionScores, system=system, enforce_schema=True)
    problems = _problems(answer, rubric, fact_ids, grounding)
    if problems:
        retry = prompt + RETRY_NOTE.format(previous=answer.model_dump_json(), problems="\n".join(problems), urls="(none)")
        answer, more = llm.generate(retry, ConversionScores, system=system, enforce_schema=True)
        usage = usage + more

    notes: list[str] = []
    known = {b["id"]: b for b in rubric.scored}
    bars: dict[str, dict] = {}
    for bar in answer.bars:
        if bar.id in bars or bar.id not in known:
            continue
        evidence = [e for e in dict.fromkeys(bar.evidence) if e in fact_ids or e in grounding.paths]
        score = min(10, max(0, bar.score))
        if score > 0 and bar.id != rubric.gates_bar and not evidence:
            notes.append(f"{bar.id}: score {score} -> 0 (no evidence)")
            score = 0
        bars[bar.id] = {
            "score": score, "reason": bar.reason.strip(), "evidence": evidence,
            "signals": [s for s in bar.signals if _SIG_CODE.split(s, 1)[0] in pack.signal_codes],
            "cites": [c for c in dict.fromkeys(bar.cites) if grounding.pack_id_ok(c)],
        }
    for bar_id in known:
        if bar_id not in bars:
            notes.append(f"{bar_id}: not scored by the model; set to 0")
            bars[bar_id] = {"score": 0, "reason": "Not scored.", "evidence": [], "signals": [], "cites": []}

    # The gates bar reflects only gates verified for this process.
    g = bars[rubric.gates_bar]
    if not gates and g["score"] != 10:
        notes.append(f"{rubric.gates_bar}: {g['score']} -> 10 (no verified gates for this process)")
        g.update(score=10, reason="No gates verified for this process.")
    elif gates and g["score"] == 10:
        notes.append(f"{rubric.gates_bar}: 10 -> 5 (verified gates apply)")
        g.update(score=5, reason="Verified gates apply to part of the process: " + ", ".join(gates))
    if gates:
        g["cites"] = list(dict.fromkeys(g["cites"] + list(gates)))
        g["evidence"] = list(dict.fromkeys(g["evidence"] + [p for ps in gates.values() for p in ps]))
    return bars, usage, notes
