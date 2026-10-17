"""
The report's written sections: the conversion design (or why the repository is
not suitable) and the evaluation remediations.

Each call may search the Temporal docs. Answers are checked in code (process
names, paths and citations); a failed check gets one retry without tools, and
whatever still fails is dropped and recorded.
"""
import json
from typing import Callable

from pydantic import BaseModel

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens.pack import Pack
from semantic_ingress.lens.repo_pass import SEARCH_TOOL, _format_chunks
from semantic_ingress.report.grounding import Grounding
from semantic_ingress.report.models import ConversionWriteup, EvaluationWriteup
from semantic_ingress.report.prompts import (
    DESIGN_TASK,
    EVAL_SYSTEM,
    GROUNDING,
    RETRY_NOTE,
    UNSUITABLE_TASK,
    WRITEUP_SYSTEM,
)
from semantic_ingress.report.verify import LeadResult

DocsSearch = Callable[[str], list[dict]]

DESIGN_SEARCHES = 3
OTHER_SEARCHES = 2
DESIGNED_KINDS = {"workflow", "activity", "child_workflow"}


def entries_text(pack: Pack, ids: list[str] | None = None, sections: list[str] | None = None) -> str:
    """Pack entries as 'id | title | statement | docs URLs', by id list or by section."""
    if ids is not None:
        entries = [pack.entries[i] for i in dict.fromkeys(ids) if i in pack.entries]
    else:
        entries = [e for s in sections or [] for e in pack.data.get(s) or []]
    lines = ["PACK ENTRIES (id | title | statement | docs URLs)"]
    for e in entries:
        if pack.usable(e):
            lines.append(f"{e['id']} | {e['t']} | {e.get('s', '')} | {' '.join(e.get('url') or [])}")
    return "\n".join(lines)


def _call(llm, prompt: str, model: type[BaseModel], system: str, docs_search: DocsSearch | None,
          max_searches: int, grounding: Grounding) -> tuple[BaseModel, LLMUsage, int]:
    if docs_search is None:
        answer, usage = llm.generate(prompt, model, system=system, enforce_schema=True)
        return answer, usage, 0

    def handle(name: str, tool_input: dict) -> str:
        chunks = docs_search(tool_input["query"])
        grounding.returned_urls.update(c["source_url"] for c in chunks if c.get("source_url"))
        return _format_chunks(chunks)

    return llm.generate_with_tools(
        prompt, model, system=system, tools=[SEARCH_TOOL], tool_handler=handle, max_tool_calls=max_searches
    )


def _retry(llm, prompt: str, model: type[BaseModel], system: str, answer: BaseModel,
           problems: list[str], grounding: Grounding) -> tuple[BaseModel, LLMUsage]:
    note = RETRY_NOTE.format(
        previous=answer.model_dump_json(), problems="\n".join(problems),
        urls="\n".join(sorted(grounding.returned_urls)) or "(none)",
    )
    return llm.generate(prompt + note, model, system=system, enforce_schema=True)


# --- conversion ------------------------------------------------------------------

def _gain_cite_ok(ref: str, fact_ids: set[str], g: Grounding) -> bool:
    return ref in fact_ids or g.pack_id_ok(ref)


def _conversion_problems(w: ConversionWriteup, suitable: bool, process_names: set[str], g: Grounding,
                         fact_ids: set[str] = frozenset()) -> list[str]:
    problems = []
    if suitable and not w.designs:
        problems.append("the process is suitable but no design was given")
    if not suitable and w.designs:
        problems.append("the process is not suitable, so designs must be empty")
    for d in w.designs:
        where = f"design {d.process!r}"
        if d.process not in process_names:
            problems.append(f"{where}: not a process name from the input")
        problems += g.problems_cites(d.cites, where)
        for i, gain in enumerate(d.gains, 1):
            bad = [r for r in gain.cites if not _gain_cite_ok(r, fact_ids, g)]
            if bad or not gain.cites:
                problems.append(f"{where} gain {i}: needs card fact ids or pack ids as citations (invalid: {bad})")
        for el in d.elements:
            ewhere = f"{where} element {el.name!r}"
            problems += g.problems_paths(el.files, ewhere) + g.problems_cites(el.cites, ewhere)
            if not el.cites:
                problems.append(f"{ewhere}: needs at least one citation")
    for i, r in enumerate(w.reasons, 1):
        problems += g.problems_paths(r.evidence, f"reason {i}") + g.problems_cites(r.cites, f"reason {i}")
        if not r.cites:
            problems.append(f"reason {i}: needs at least one citation")
    return problems


def write_process(
    llm, pack: Pack, grounding: Grounding, docs_search: DocsSearch | None,
    bars: dict[str, dict], result: dict, gates: dict[str, list[str]],
    process: str, card: str, records: str, pack_text: str, fact_ids: set[str] = frozenset(),
) -> tuple[ConversionWriteup, LLMUsage, int, list[str]]:
    """One process's write-up (a design, or why it is not a fit), usage, docs searches made, and notes."""
    overall, suitable = result["overall"], result["design"]
    if suitable:
        task = DESIGN_TASK.format(label=result["verdict_class"], overall=overall, max_searches=DESIGN_SEARCHES)
        max_searches = DESIGN_SEARCHES
    else:
        gate_note = ", capped by verified gates" if result["gate_capped"] else ""
        task = UNSUITABLE_TASK.format(overall=overall, gate_note=gate_note, max_searches=OTHER_SEARCHES)
        max_searches = OTHER_SEARCHES
    system = WRITEUP_SYSTEM.format(grounding=GROUNDING, task=task) + "\n\n" + pack_text
    bar_lines = "\n".join(f"{b} {v['score']}/10: {v['reason']}" for b, v in bars.items())
    verified = "\n".join(f"{g}: {', '.join(p)}" for g, p in gates.items()) or "(none verified for this process)"
    prompt = (
        f"PROCESS: {process}\nOVERALL: {overall}/10 (fit {result['fit']} on {result['best_path']})\n\n"
        f"BARS:\n{bar_lines}\n\nVERIFIED GATES:\n{verified}\n\n"
        f"GATES NOT CONFIRMED BY VERIFICATION (do not rely on or mention them):\n{grounding.rejected_text()}\n\n"
        f"{card}\n\nFILE LENS RECORDS (path: record):\n{records}"
    )

    answer, usage, searches = _call(llm, prompt, ConversionWriteup, system, docs_search, max_searches, grounding)
    names = {process}
    problems = _conversion_problems(answer, suitable, names, grounding, fact_ids)
    if problems:
        answer, more = _retry(llm, prompt, ConversionWriteup, system, answer, problems, grounding)
        usage = usage + more

    notes = []
    if suitable and docs_search is not None and searches == 0:
        notes.append(f"{process}: no docs search was made although one was required")
    if not suitable:
        answer.designs = []
    designs = []
    for d in answer.designs[:1]:
        where = f"design {d.process!r}"
        if d.process not in names:
            grounding.dropped.append(f"{where}: dropped (not the process being designed)")
            continue
        elements = []
        for el in d.elements:
            ewhere = f"{where} element {el.name!r}"
            el.files = grounding.keep_paths(el.files, ewhere)
            el.cites = grounding.cites(el.cites, ewhere)
            if not el.cites:
                grounding.dropped.append(f"{ewhere}: dropped (no valid citation)")
                continue
            elements.append(el)
        d.elements = elements
        d.cites = grounding.cites(d.cites, where)
        gains = []
        for gain in d.gains[:3]:
            gain.cites = [r for r in dict.fromkeys(gain.cites) if _gain_cite_ok(r, fact_ids, grounding)]
            if gain.cites:
                gains.append(gain)
            else:
                grounding.dropped.append(f"{where} gain dropped (no valid citation): {gain.text}")
        d.gains = gains
        designs.append(d)
    answer.designs = designs
    reasons = []
    for i, r in enumerate(answer.reasons, 1):
        where = f"{process} reason {i}"
        r.evidence = grounding.keep_paths(r.evidence, where)
        r.cites = grounding.cites(r.cites, where)
        if not r.cites:
            grounding.dropped.append(f"{where} dropped (no valid citation): {r.text}")
            continue
        reasons.append(r)
    answer.reasons = reasons
    return answer, usage, searches, notes


def moved_files(writeup: ConversionWriteup) -> set[str]:
    """Files the design builds workflows or activities from."""
    return {f for d in writeup.designs for el in d.elements if el.kind in DESIGNED_KINDS for f in el.files}


# --- evaluation ------------------------------------------------------------------

def finding_key(result: LeadResult) -> str:
    return f"{result.lead.path}#{result.lead.key}"


def write_evaluation(
    llm, pack: Pack, grounding: Grounding, docs_search: DocsSearch | None,
    findings: list[LeadResult], temporal_files: int,
) -> tuple[EvaluationWriteup, LLMUsage, int]:
    """Remediations for confirmed findings, and the evaluation verdict."""
    zero = LLMUsage(input_tokens=0, output_tokens=0)
    if not findings:
        verdict = (
            f"No review-check finding was confirmed in the {temporal_files} Temporal files. "
            "The review covers the checks the index flagged; it is not a full code review."
        )
        return EvaluationWriteup(verdict=verdict, remediations=[]), zero, 0

    system = EVAL_SYSTEM.format(grounding=GROUNDING, max_searches=OTHER_SEARCHES) + "\n\n" + entries_text(
        pack, ids=[f.lead.entry for f in findings]
    )
    rows = "\n".join(
        f"{finding_key(f)} | {f.severity} | {f.lead.entry} | line {f.line} | {f.reason}" for f in findings
    )
    prompt = f"CONFIRMED FINDINGS (key | severity | pack id | line | verifier's reason):\n{rows}"
    answer, usage, searches = _call(llm, prompt, EvaluationWriteup, system, docs_search, OTHER_SEARCHES, grounding)

    keys = {finding_key(f) for f in findings}
    problems = [f"remediation for unknown finding {r.finding!r}" for r in answer.remediations if r.finding not in keys]
    problems += [f"no remediation for {k}" for k in sorted(keys - {r.finding for r in answer.remediations})]
    for r in answer.remediations:
        problems += grounding.problems_cites(r.cites, r.finding)
    if problems:
        answer, more = _retry(llm, prompt, EvaluationWriteup, system, answer, problems, grounding)
        usage = usage + more

    remediations = []
    for r in answer.remediations:
        if r.finding not in keys:
            grounding.dropped.append(f"remediation for unknown finding dropped ({r.finding})")
            continue
        r.cites = grounding.cites(r.cites, r.finding)
        remediations.append(r)
    answer.remediations = remediations
    return answer, usage, searches
