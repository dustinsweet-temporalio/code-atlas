"""The progress line and percentage the web UI shows for a sync (temporal/progress.py)."""
from semantic_ingress.temporal.models import LevelInfo, Progress, SyncPlan
from semantic_ingress.temporal.progress import describe, percent


def _plan(n_files=40, mode="full", levels=None):
    return SyncPlan(commit_sha="abc", mode=mode, reset_index=False, reason="", n_files=n_files,
                    n_lens_only=0, n_deleted=0, levels=levels or [])


def _p(phase, done=0, total=0, plan=None, failed=0):
    return Progress(phase=phase, done=done, total=total, failed=failed, plan=plan)


def test_percent_rises_through_a_run():
    plan = _plan()
    folders = _plan(levels=[LevelInfo(depth=1, offset=0, count=8), LevelInfo(depth=0, offset=8, count=1)])
    steps = [
        _p("STARTING"), _p("PLAN"),
        _p("FILES", 0, 40, plan), _p("FILES", 20, 40, plan), _p("FILES", 40, 40, plan),
        _p("FOLDERS", 0, 9, folders), _p("FOLDERS", 9, 9, folders),
        _p("REPO", plan=folders), _p("DELETE", plan=folders), _p("PERSIST", 30, plan=folders),
        _p("FINALIZE", plan=folders), _p("MIRROR", plan=folders), _p("REPORT", plan=folders),
        _p("CLEANUP", plan=folders), _p("DONE", plan=folders),
    ]
    for report in (True, False):
        values = [percent(s, report) for s in steps]
        assert values == sorted(values), (report, values)
        assert values[0] == 0 and values[-1] == 100


def test_the_report_gets_a_band_of_its_own():
    plan = _plan()
    assert percent(_p("REPORT", plan=plan), report=True) < 90
    assert percent(_p("REPORT", plan=plan), report=False) >= 99


def test_nothing_to_generate_skips_to_the_end_of_generation():
    plan = _plan(n_files=0, mode="incremental")
    assert percent(_p("FILES", 0, 0, plan), report=False) == percent(_p("REPO", plan=plan), report=False)


def test_descriptions():
    assert describe(_p("FILES", 3, 10, _plan(mode="incremental"), failed=1)) == "Summarising changed files: 3 of 10 · 1 failed"
    assert describe(_p("FOLDERS", 1, 4, _plan())) == "Summarising folders: 1 of 4"
    assert describe(_p("REPO", plan=_plan()), lens=False) == "Writing the repository summary"
    assert describe(_p("PERSIST", 1200, plan=_plan())) == "Writing to the index: 1,200 documents"
    assert describe(_p("PLAN")) == "Comparing the repository with the index"
