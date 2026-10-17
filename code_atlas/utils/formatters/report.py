"""
The stored Temporal report as compact text for the chat agent.

The report (built during indexing, see semantic_ingress.report) is read in three
views so a question loads only what it needs: an overview, one process, or the
evaluation of existing Temporal code. Fact ids (F1, F2, ...) are the report's own,
so they match the report view in the UI.
"""
from typing import Any

from semantic_ingress.report.investigate import NOT_ESTABLISHED, card_facts

MAX_REFS = 3


def _refs(refs: list[str]) -> str:
    if not refs:
        return ""
    more = f" +{len(refs) - MAX_REFS}" if len(refs) > MAX_REFS else ""
    return f" [{', '.join(refs[:MAX_REFS])}{more}]"


def _freshness(doc: dict[str, Any], index_commit: str | None) -> str:
    commit = (doc.get("commit_sha") or "")[:12]
    generated = (doc.get("generated_at") or "")[:10]
    state = ""
    if index_commit and doc.get("commit_sha") and index_commit != doc["commit_sha"]:
        state = f"; STALE: the index is now at {index_commit[:12]}"
    if doc.get("status") == "failed":
        state += f"; the latest attempt failed ({doc.get('error')}), so this is the previous report"
    return f"generated {generated} for commit {commit}{state}"


def _processes(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (data.get("conversion") or {}).get("processes", []) if "error" not in p]


def _path_titles(data: dict[str, Any]) -> dict[str, str]:
    return {p["id"]: p["t"] for p in (data.get("conversion") or {}).get("paths", [])}


def report_status_line(doc: dict[str, Any] | None, index_commit: str | None) -> str:
    """One line for the system prompt: whether a report exists, and its headline verdicts."""
    if not doc or not doc.get("data"):
        if doc and doc.get("status") == "failed":
            return f"Temporal report: the last attempt failed ({doc.get('error')}); none is available."
        return "Temporal report: none yet for this index."
    data = doc["data"]
    procs = _processes(data)
    verdicts = "; ".join(f"{p['name']} {p['overall']:g} {p['verdict_class']}" for p in procs) or "no processes assessed"
    temporal = f" Temporal status: {data.get('status')}."
    return (f"Temporal report: available ({_freshness(doc, index_commit)}).{temporal} Verdicts: {verdicts}. "
            "Call get_temporal_report for details.")


def report_overview(doc: dict[str, Any], index_commit: str | None) -> str:
    data = doc["data"]
    conv = data.get("conversion") or {}
    titles = _path_titles(data)
    out = [f"TEMPORAL REPORT for {data['repo_id']} ({_freshness(doc, index_commit)})",
           f"Temporal status: {data['status']}. Knowledge pack {data['pack']['version']}, rubric {data['rubric']['version']}."]
    for w in data.get("warnings", []):
        out.append(f"Warning: {w}")
    if conv:
        hi, lo = conv["threshold"] + conv.get("margin", 0), conv["threshold"] - conv.get("margin", 0)
        out.append(f"\nPROCESSES (suitable from {hi:g}/10, borderline {lo:g} to {hi:g}):")
        for p in _processes(data):
            moved = len(p.get("moved", []))
            out.append(f"- {p['name']}: {p['overall']:g}/10 {p['verdict_class']}; strongest reason "
                       f"{titles.get(p['best_path'], p['best_path'])} {p['fit']:g}; entry {p['entry']}"
                       + (f"; the design moves {moved} files to Temporal" if moved else "")
                       + ("; LOW CONFIDENCE" if p.get("low_confidence") else ""))
            if p.get("design") and p["design"].get("gains"):
                out.append("  gains: " + "; ".join(g["text"] for g in p["design"]["gains"]))
        for p in conv.get("processes", []):
            if "error" in p:
                out.append(f"- {p['name']}: not assessed ({p['error']})")
        if not _processes(data):
            out.append("- none found to assess")
        if conv.get("not_investigated"):
            out.append("Not investigated (over the limit): " + ", ".join(x["name"] for x in conv["not_investigated"]))
    pct = data.get("pct_temporalized") or {}
    if pct:
        out.append(f"\nShare of durable-relevant files on Temporal: {pct['pct_today']}% today"
                   + (f", {pct['pct_after']}% with the designs" if pct.get("moved_files") else "") + ".")
    if data.get("gates"):
        out.append("Gates verified in the source: " + "; ".join(
            f"{g['id']} in {g['path']} {g['line']} ({g['reason']})" for g in data["gates"]))
    if data.get("evaluation"):
        out.append(f"Existing Temporal code: {data['evaluation']['verdict']}")
    out.append('\nFor one process (facts with line references, scores, design): view="process", process="<name>". '
               'For existing Temporal code: view="evaluation".')
    return "\n".join(out)


def find_process(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    procs = _processes(data)
    wanted = name.strip().lower()
    for p in procs:
        if p["name"].lower() == wanted:
            return p
    matches = [p for p in procs if wanted and (wanted in p["name"].lower() or wanted in p["entry"].lower())]
    return matches[0] if len(matches) == 1 else None


def report_process(doc: dict[str, Any], name: str) -> str:
    data = doc["data"]
    p = find_process(data, name)
    if p is None:
        names = ", ".join(f'"{x["name"]}"' for x in _processes(data)) or "none"
        return f"No single process matches {name!r}. Processes in the report: {names}."
    titles = _path_titles(data)
    pack_titles = {e["id"]: e["t"] for e in data.get("sources", {}).get("pack", [])}
    card = p["card"]
    out = [f"PROCESS: {p['name']} (entry {p['entry']}): {p['overall']:g}/10 {p['verdict_class']}; "
           f"fit {p['fit']:g} on {titles.get(p['best_path'], p['best_path'])}"
           + (f"; capped by gates (weighted {p['weighted']:g})" if p.get("gate_capped") else ""),
           f"Verdict: {p['verdict']}"]
    if p.get("low_confidence"):
        out.append("LOW CONFIDENCE: " + "; ".join(p["low_confidence"]))

    out.append("\nHOW IT RUNS TODAY (facts checked against the cited source lines):")
    for fid, _, _, label, text, refs in card_facts(card):
        if text != NOT_ESTABLISHED:
            out.append(f"{fid} {label}: {text}{_refs(refs)}")
    out.append(f"Units scale with: {card['scales_with']}. Assumption: {card['scale_assumption']}")

    out.append("\nSCORES (evidence is fact ids or paths):")
    for b in p["bars"]:
        cites = f"; cites {', '.join(b['cites'])}" if b["cites"] else ""
        out.append(f"- {b['t']} {b['score']:g}: {b['reason']} (evidence {', '.join(b['evidence']) or 'none'}{cites})")
    for g in p.get("gates", []):
        out.append(f"- Gate {g['id']} verified in {', '.join(g['paths'])}")

    d = p.get("design")
    if d:
        out.append(f"\nDESIGN\nWhy Temporal: {d['why']}")
        if d.get("gains"):
            out.append("What you gain: " + "; ".join(f"{g['text']} ({', '.join(g['cites'])})" for g in d["gains"]))
        for el in d["elements"]:
            out.append(f"- {el['kind'].replace('_', ' ')} {el['name']} (from {', '.join(el['files']) or 'n/a'}): "
                       f"{el['does']} [{', '.join(el['cites'])}]")
        if d.get("removes"):
            out.append("Removes: " + "; ".join(d["removes"]))
        if d.get("stays"):
            out.append("Stays outside Temporal: " + "; ".join(d["stays"]))
    else:
        if p.get("reasons"):
            out.append("\nWHY IT IS NOT A FIT")
            out += [f"- {r['text']} [{', '.join(r['cites'])}]" for r in p["reasons"]]
        if p.get("would_change"):
            out.append("Would change the verdict: " + "; ".join(p["would_change"]))

    cited = sorted({c for b in p["bars"] for c in b["cites"]} | {c for c in (d or {}).get("cites", [])}
                   | {c for el in (d or {}).get("elements", []) for c in el["cites"]})
    named = [f"{c} ({pack_titles[c]})" for c in cited if c in pack_titles]
    urls = [c for c in cited if c.startswith("http")]
    if named or urls:
        out.append("\nSOURCES: " + "; ".join(named + urls))
    return "\n".join(out)


def report_evaluation(doc: dict[str, Any]) -> str:
    ev = doc["data"].get("evaluation")
    if not ev:
        return "This repository has no existing Temporal code, so the report has no evaluation section."
    out = [f"EXISTING TEMPORAL CODE ({ev['temporal_files']} files): {ev['verdict']}", "BARS:"]
    out += [f"- {b['t']} {b['score']:g}: {b['reason']}" for b in ev["bars"]]
    findings = [f for b in ev["bars"] for f in b.get("findings", [])] + ev.get("other_findings", [])
    if findings:
        out.append("FINDINGS (confirmed against the source):")
        for f in findings:
            out.append(f"- {f['severity']} {f['entry']} at {f['path']} {f['line']}: {f['reason']} "
                       f"Fix: {f['fix'] or '(none written)'} [{', '.join(f['cites'])}]")
    return "\n".join(out)
