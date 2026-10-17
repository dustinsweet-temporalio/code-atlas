"""
Render the report data as Markdown with a fixed template, so reports read the
same way across repositories. No LLM calls; every word of prose comes from the
report data (model text that passed the checks, or fixed wording here).
"""
from typing import Any

from semantic_ingress.report.investigate import NOT_ESTABLISHED, card_facts

STATUS_TEXT = {
    "none": "no Temporal SDK in use",
    "adjacent": "another orchestrator or job system, no Temporal SDK",
    "partial": "some processes run on Temporal",
    "full": "durable processes run on Temporal",
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "n/a": 3}
MAX_REFS = 3


def bar(score: float) -> str:
    """Ten cells, floored, so a full bar always means 10."""
    n = max(0, min(10, int(score)))
    return f"`{'█' * n}{'░' * (10 - n)}`"


def fmt_score(score: float) -> str:
    return f"{score:g}"


def cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def code_list(items: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in items)


def refs_text(refs: list[str]) -> str:
    shown = code_list(refs[:MAX_REFS])
    return shown + (f" +{len(refs) - MAX_REFS}" if len(refs) > MAX_REFS else "")


class _Refs:
    """Numbers docs URLs in order of first use; pack ids stay as ids."""

    def __init__(self, sources: dict):
        self.order: list[str] = []
        self.pack_ids = {e["id"] for e in sources["pack"]}
        for e in sources["pack"]:
            for u in e["urls"]:
                self.num(u)
        for u in sources["urls"]:
            self.num(u)

    def num(self, url: str) -> int:
        if url not in self.order:
            self.order.append(url)
        return self.order.index(url) + 1

    def cite(self, refs: list[str]) -> str:
        parts = [f"`{r}`" if r in self.pack_ids or not r.startswith("http") else f"[{self.num(r)}]" for r in refs]
        return ", ".join(parts)


def render(r: dict) -> str:
    refs = _Refs(r["sources"])
    out: list[str] = []
    w = out.append

    # --- header -------------------------------------------------------------
    sections = [s for s, key in (("suitability", "conversion"), ("evaluation", "evaluation")) if key in r]
    detected = "; ".join(
        f"{'Temporal SDK' if d['kind'] == 'temporal_sdk' else d['name']} "
        f"{'(' + d['name'] + ') ' if d['kind'] == 'temporal_sdk' else ''}in {len(d['evidence'])} files"
        for d in r["detected"]
    )
    w(f"# Temporal report: {r['repo_id']}\n")
    w("| | |\n|---|---|")
    w(f"| Repository | `{r['repo_id']}` at `{r['commit'][:12]}`, indexed {r['indexed_at']} UTC |")
    w(f"| Temporal status | **{r['status']}**: {STATUS_TEXT[r['status']]}" + (f" ({cell(detected)})" if detected else "") + " |")
    w(f"| Report | {' and '.join(sections)} |")
    w(f"| Knowledge | pack {r['pack']['id']} {r['pack']['version']}; rubric {r['rubric']['version']}; lens {r['lens_state']} |")
    w(f"| Generated | {r['generated']} with {r['model']}; cost ${r['cost_usd']:.2f} |\n")
    for warning in r["warnings"]:
        w(f"> **Warning:** {warning}\n")

    # --- verdict ------------------------------------------------------------
    w("## Verdict\n")
    conv, ev = r.get("conversion"), r.get("evaluation")
    if conv:
        procs = [p for p in conv["processes"] if "error" not in p]
        titles = {p["id"]: p["t"] for p in conv["paths"]}
        counts = {c: sum(1 for p in procs if p["verdict_class"] == c) for c in ("suitable", "borderline")}
        hi, lo = conv["threshold"] + conv["margin"], conv["threshold"] - conv["margin"]
        if procs:
            w(f"**Suitability:** of {len(procs)} investigated processes, {counts['suitable']} suit Temporal "
              f"and {counts['borderline']} are borderline. Suitable from {fmt_score(hi)}/10; "
              f"{fmt_score(lo)} to {fmt_score(hi)} is borderline, because scores can differ by up to about "
              f"{fmt_score(conv['margin'])} between runs.\n")
            w("| Process | Score | Strongest reason | Verdict |\n|---|---|---|---|")
            for p in procs:
                w(f"| [{cell(p['name'])}](#{_anchor(p['name'])}) | {bar(p['overall'])} **{fmt_score(p['overall'])}** | "
                  f"{titles[p['best_path']]} {fmt_score(p['fit'])} | {p['verdict_class']} |")
            for p in conv["processes"]:
                if "error" in p:
                    w(f"| {cell(p['name'])} | | | not assessed: {cell(p['error'])} |")
            w("")
        else:
            w("**Suitability:** no process with multi-step or failure-sensitive work was found to assess.\n")
    if ev:
        w(f"**Existing Temporal code.** {ev['verdict']}\n")
    pct = r["pct_temporalized"]
    designed = bool(conv) and any(p.get("design") for p in conv["processes"])
    if designed:
        w(f"**Temporalized:** {pct['pct_today']}% of durable-relevant files today, "
          f"{pct['pct_after']}% with the designs below ({pct['pct_lines_after']}% of their lines).\n")
    else:
        w(f"**Temporalized:** {pct['pct_today']}% of durable-relevant files today.\n")

    if conv:
        _render_conversion(r, conv, refs, w)
    if ev:
        _render_evaluation(ev, refs, w)

    # --- % Temporalized ---------------------------------------------------------
    w("## Share of durable work on Temporal\n")
    w("Durable-relevant code means files the index rates relevance 2 or 3: they implement a step, "
      "or the orchestration, of failure-sensitive or multi-step work.\n")
    w("| | Files | Lines |\n|---|---|---|")
    w(f"| Durable-relevant code | {pct['files_total']} | {pct['lines_total']:,} |")
    w(f"| On Temporal today | {pct['files_today']} ({pct['pct_today']}%) | {pct['lines_today']:,} ({pct['pct_lines_today']}%) |")
    if designed:
        w(f"| On Temporal with the designs | {pct['files_after']} ({pct['pct_after']}%) | "
          f"{pct['lines_after']:,} ({pct['pct_lines_after']}%) |")
    w("")
    if pct["moved_files"]:
        w(f"Files the designs move into workflows or activities: {code_list(pct['moved_files'])}.\n")

    # --- questions, sources, appendix ------------------------------------------
    w("## Open questions\n")
    w("The index cannot answer these; they decide scope and priority.\n")
    for q in r["questions"]:
        w(f"- {q['t']}")
    w("")

    w("## Sources\n")
    w("Every Temporal statement above cites one of these. Pack entries were checked against the "
      "Temporal docs through the Temporal Docs MCP; numbered links are docs pages. Facts about the code "
      "cite source lines (`path@Lstart-end`).\n")
    for e in r["sources"]["pack"]:
        links = ", ".join(f"[{refs.num(u)}]" for u in e["urls"])
        w(f"- `{e['id']}` {e['t']}" + (f" ({links})" if links else ""))
    w("")
    for i, url in enumerate(refs.order, 1):
        w(f"{i}. <{url}>")
    w("")

    _render_appendix(r, w)
    return "\n".join(out).rstrip() + "\n"


def _anchor(name: str) -> str:
    return "".join(c for c in name.lower().replace(" ", "-") if c.isalnum() or c == "-")


def _render_card(card: dict, w) -> None:
    facts = {(f[1], f[2]): f for f in card_facts(card)}

    def fact(field: str, index: int | None = None) -> str:
        fid, _, _, _, text, refs = facts[(field, index)]
        if text == NOT_ESTABLISHED:
            return f"*{text}*"
        return f"**{fid}** {text}" + (f" ({refs_text(refs)})" if refs else "")

    w("**How it runs today** (established from the source):\n")
    w(f"- Trigger: {fact('trigger')}")
    w(f"- Unit of work: {fact('unit_of_work')} Scales with {card['scales_with'].rstrip('.')}. "
      f"*Assumption: {card['scale_assumption'].rstrip('.')}.*")
    if card["steps"]:
        w("- Steps:")
        for i in range(len(card["steps"])):
            w(f"  {i + 1}. {card['steps'][i]['name']}: {fact('steps', i)}")
    w(f"- State between steps: {fact('state_between_steps')}")
    w(f"- Persisted: {fact('persistence')}")
    if card["reliability"]:
        w("- Reliability code today: " + "; ".join(fact("reliability", i) for i in range(len(card["reliability"]))))
    if card["waits"]:
        w("- Waits: " + "; ".join(fact("waits", i) for i in range(len(card["waits"]))))
    w(f"- Run length: {fact('run_length')}\n")
    if card["crash_loss"]:
        w("**What a crash loses:**\n")
        w("| | When | Lost or repeated | Source |\n|---|---|---|---|")
        for i, c in enumerate(card["crash_loss"]):
            fid = facts[("crash_loss", i)][0]
            w(f"| {fid} | {cell(c['stage'])} | {cell(c['loses'])} | {refs_text(c['refs'])} |")
        w("")


def _render_conversion(r: dict, conv: dict, refs: _Refs, w) -> None:
    w("## Suitability for Temporal\n")
    w("Each process was investigated in the source and scored on its own. Its fit is its strongest reason "
      "to use Temporal (value path); reliability burden, separability and migration ease adjust the score, "
      "and verified gates cap it.\n")
    if r["status"] == "partial":
        w("Processes that already use Temporal are reviewed under *Existing Temporal code* instead.\n")
    weights = ", ".join(f"{k} {v}" for k, v in conv["weights"].items())
    gate_titles = {e["id"]: e["t"] for e in r["sources"]["pack"]}
    if r["gates"]:
        w("**Gates verified in the source** (reasons Temporal is a poor fit, where they apply):\n")
        for g in r["gates"]:
            w(f"- `{g['id']}` {gate_titles.get(g['id'], '')}: `{g['path']}` {g['line']}: {g['reason']}")
        w("")

    for p in conv["processes"]:
        if "error" in p:
            continue
        label = p["verdict_class"]
        w(f"### {p['name']}\n")
        source = "reused from an earlier report (its files are unchanged)" if p["card_reused"] else "investigated for this report"
        w(f"**{fmt_score(p['overall'])}/10, {label}.** Entry point `{p['entry']}`; process card {source}.\n")
        if p["low_confidence"]:
            w(f"> **Low confidence:** {'; '.join(p['low_confidence'])}.\n")
        w(f"{p['verdict']}\n")
        _render_card(p["card"], w)

        w("**Scores:**\n")
        w("| | Score | Why | Evidence |\n|---|---|---|---|")
        for b in p["bars"]:
            if b["kind"] == "path":
                w(_bar_row(b, refs))
        w(f"| **Fit** (strongest reason) | {bar(p['fit'])} **{fmt_score(p['fit'])}** | | |")
        for b in p["bars"]:
            if b["kind"] == "modifier":
                w(_bar_row(b, refs))
        capped = f"; capped by verified gates (weighted {fmt_score(p['weighted'])})" if p["gate_capped"] else ""
        w(f"| **Overall** | {bar(p['overall'])} **{fmt_score(p['overall'])}** | Weighted: {weights}{capped}. | |\n")

        for g in p["gates"]:
            w(f"- Gate `{g['id']}` {gate_titles.get(g['id'], '')}: verified in {code_list(g['paths'])}.")
        if p["gates"]:
            w("")

        if p["design"]:
            d = p["design"]
            w(f"**Why Temporal:** {d['why']}" + (f" ({refs.cite(d['cites'])})" if d["cites"] else "") + "\n")
            if d.get("gains"):
                w("**What you gain:**\n" + "\n".join(f"- {g['text']} ({refs.cite(g['cites'])})" for g in d["gains"]) + "\n")
            w("| Element | Kind | Built from | Does | Sources |\n|---|---|---|---|---|")
            for el in d["elements"]:
                w(f"| {cell(el['name'])} | {el['kind'].replace('_', ' ')} | {code_list(el['files'])} | "
                  f"{cell(el['does'])} | {refs.cite(el['cites'])} |")
            w("")
            if d["removes"]:
                w("Removes:\n" + "\n".join(f"- {x}" for x in d["removes"]) + "\n")
            if d["stays"]:
                w("Stays outside Temporal:\n" + "\n".join(f"- {x}" for x in d["stays"]) + "\n")
        elif not p["design"]:
            if p["reasons"]:
                w("**Why it is not a fit:**\n")
                for reason in p["reasons"]:
                    ev = f" ({code_list(reason['evidence'])})" if reason["evidence"] else ""
                    w(f"- {reason['text']}{ev} {refs.cite(reason['cites'])}")
                w("")
            if p["would_change"]:
                w("What would change the verdict:\n" + "\n".join(f"- {x}" for x in p["would_change"]) + "\n")

    if conv["not_investigated"]:
        w("**Not investigated** (over the limit of processes per report): "
          + "; ".join(f"{x['name']} (`{x['entry']}`)" for x in conv["not_investigated"]) + ".\n")
    if conv["uncovered_entries"]:
        w("**Entry points no process covers** (relevance 1, not investigated): "
          + code_list(conv["uncovered_entries"]) + ".\n")


def _bar_row(b: dict, refs: _Refs) -> str:
    cites = f" ({refs.cite(b['cites'])})" if b["cites"] else ""
    evidence = ", ".join(f"`{e}`" if "/" in e or "." in e else e for e in b["evidence"][:5])
    return f"| {b['t']} | {bar(b['score'])} {fmt_score(b['score'])} | {cell(b['reason'])}{cites} | {evidence} |"


def _finding_rows(findings: list[dict], refs: _Refs, w) -> None:
    w("| Severity | Where | Check | What the code does | Fix | Sources |\n|---|---|---|---|---|---|")
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["path"])):
        w(f"| {f['severity']} | `{f['path']}` {f['line']} | `{f['entry']}` | {cell(f['reason'])} | "
          f"{cell(f['fix']) or '(none written)'} | {refs.cite(f['cites'])} |")
    w("")


def _render_evaluation(ev: dict, refs: _Refs, w) -> None:
    w("## Existing Temporal code\n")
    w(f"Review of the {ev['temporal_files']} Temporal files. Each bar starts at 10 and loses points per "
      "confirmed finding (high 3, medium 1.5, low 0.5). Only findings confirmed against the source count.\n")
    w("| Bar | Score | Basis |\n|---|---|---|")
    for b in ev["bars"]:
        w(f"| {b['t']} | {bar(b['score'])} {fmt_score(b['score'])} | {cell(b['reason'])} |")
    w("")
    with_findings = [b for b in ev["bars"] if b["findings"]]
    if with_findings or ev["other_findings"]:
        w("### Findings\n")
    for b in with_findings:
        w(f"#### {b['t']}\n")
        _finding_rows(b["findings"], refs, w)
    if ev["other_findings"]:
        w("#### Other findings\n")
        w("These checks are not inputs of any rubric bar, so they do not change a score.\n")
        _finding_rows(ev["other_findings"], refs, w)


def _render_appendix(r: dict, w) -> None:
    w("## Appendix\n")
    leads = r["leads"]
    not_confirmed = [x for x in leads if x["verdict"] != "confirmed"]
    counts = {v: sum(1 for x in leads if x["verdict"] == v) for v in ("confirmed", "rejected", "unclear")}
    procs = [p for p in (r.get("conversion") or {}).get("processes", []) if "error" not in p]
    w("### Method\n")
    w("- Inputs: the code index (summaries and lens records), the mirrored source, the knowledge pack, "
      "the report rubric and the Temporal Docs MCP. No other Temporal knowledge.")
    if procs:
        fresh = [p for p in procs if not p["card_reused"]]
        w(f"- Processes: each was investigated by reading the index and the source (effort "
          f"{r['investigate_effort']}), giving a process card of facts with line references. "
          f"{len(fresh)} cards were built for this report and {len(procs) - len(fresh)} reused. "
          "Every fact was checked against its cited lines; unsupported facts were dropped.")
    w(f"- Leads checked against the source: {len(leads)} ({counts['confirmed']} confirmed, "
      f"{counts['rejected']} rejected, {counts['unclear']} unclear)"
      + (f"; {r['leads_skipped']} more were over the limit and not checked" if r["leads_skipped"] else "") + ".")
    w(f"- Docs searches while writing: {r['docs_searches']}.")
    w("- Scores: bars are chosen from the rubric anchors by the model, with evidence, and checked in code; "
      "fit, the overall score, the gate cap and the evaluation bars are computed in code.\n")

    checks = [(p["name"], c) for p in procs for c in p["card_checks"] if c["verdict"] != "supported"]
    if checks:
        w("### Card facts not supported\n")
        w("| Process | Fact | Verdict | Reason |\n|---|---|---|---|")
        for name, c in checks:
            w(f"| {cell(name)} | {cell(c['fact'])}: {cell(c['text'])} | {c['verdict']} | {cell(c['reason'])} |")
        w("")
    if not_confirmed:
        w("### Leads not confirmed\n")
        w("| File | Lead | Verdict | Reason |\n|---|---|---|---|")
        for x in not_confirmed:
            w(f"| `{x['path']}` | `{x['lead']}` | {x['verdict']} | {cell(x['reason'])} |")
        w("")
    dropped = [d for p in procs for d in p["card_dropped"]] + r["notes"] + r["dropped"]
    if dropped:
        w("### Dropped claims and corrections\n")
        for x in dropped:
            w(f"- {x}")
        w("")
