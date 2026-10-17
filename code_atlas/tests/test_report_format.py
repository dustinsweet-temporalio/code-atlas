"""The stored Temporal report as text for the chat agent (code_atlas/utils/formatters/report.py)."""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "report_format", Path(__file__).resolve().parents[1] / "utils" / "formatters" / "report.py"
)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

CARD = {
    "process": "Indexing sync",
    "trigger": {"text": "The sync CLI command.", "refs": ["cli.py@L10-20"]},
    "unit_of_work": {"text": "One LLM call per file.", "refs": ["orch.py@L5"]},
    "scales_with": "files in the repository", "scale_assumption": "Assumes 2,000 files.",
    "steps": [{"name": "summarise", "does": "Calls the LLM.", "effects": ["paid_call"], "refs": ["orch.py@L30-40"]}],
    "state_between_steps": {"text": "In memory.", "refs": ["orch.py@L50"]},
    "persistence": {"text": "Written at the end.", "refs": ["cli.py@L90"]},
    "crash_loss": [{"stage": "during generation", "loses": "Everything generated so far.", "refs": ["orch.py@L50"]}],
    "reliability": [], "waits": [], "run_length": {"text": "Not established from the code.", "refs": []},
    "files": ["cli.py", "orch.py"],
}
PROC = {
    "name": "Indexing sync", "entry": "orch.py", "overall": 7.2, "verdict_class": "suitable", "fit": 8,
    "best_path": "PATH-PIPELINE", "weighted": 7.2, "gate_capped": False, "low_confidence": [],
    "verdict": "Suitable.", "card": CARD, "moved": ["orch.py"], "gates": [],
    "bars": [{"id": "PATH-PIPELINE", "t": "Durable pipeline", "score": 8, "reason": "Long paid runs.",
              "evidence": ["F2", "F6"], "cites": ["V-DURABLE"]}],
    "design": {"why": "A crash re-pays every call.", "cites": [],
               "gains": [{"text": "Crashed runs resume instead of re-paying", "cites": ["F6"]}],
               "elements": [{"kind": "workflow", "name": "SyncWorkflow", "files": ["orch.py"], "does": "Orchestrates.",
                             "cites": ["V-DURABLE"]}], "removes": ["orch.py: pickle cache"], "stays": []},
    "reasons": [], "would_change": [],
}
DOC = {
    "status": "ok", "commit_sha": "abc123def4567890", "generated_at": "2026-10-17T10:00:00",
    "data": {
        "repo_id": "demo", "status": "none", "pack": {"version": "0.1.0"}, "rubric": {"version": "2.0.0"},
        "warnings": [], "gates": [], "pct_temporalized": {"pct_today": 0, "pct_after": 50, "moved_files": ["orch.py"]},
        "conversion": {"threshold": 5, "margin": 0.75, "paths": [{"id": "PATH-PIPELINE", "t": "Durable pipeline"}],
                       "processes": [PROC], "not_investigated": []},
        "sources": {"pack": [{"id": "V-DURABLE", "t": "Durable execution", "urls": []}], "urls": []},
    },
}


def test_status_line_gives_headline_verdicts_and_flags_a_stale_report():
    line = rf.report_status_line(DOC, "abc123def4567890")
    assert "Indexing sync 7.2 suitable" in line and "STALE" not in line
    assert "STALE: the index is now at fffffff" in rf.report_status_line(DOC, "fffffff00000")
    assert rf.report_status_line(None, None) == "Temporal report: none yet for this index."


def test_overview_and_process_views():
    overview = rf.report_overview(DOC, "abc123def4567890")
    assert "- Indexing sync: 7.2/10 suitable; strongest reason Durable pipeline 8" in overview
    assert "gains: Crashed runs resume instead of re-paying" in overview
    detail = rf.report_process(DOC, "indexing")  # a distinctive part of the name is enough
    assert "F1 trigger: The sync CLI command. [cli.py@L10-20]" in detail
    assert "F6 crash during generation: Everything generated so far." in detail
    assert "Not established" not in detail
    assert "SOURCES: V-DURABLE (Durable execution)" in detail
    assert "No single process matches" in rf.report_process(DOC, "chat")
