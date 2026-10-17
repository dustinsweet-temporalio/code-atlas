"""
SyncWorkflow mechanics with stub activities registered under the real names:
the sliding window, the folder level barrier, Continue-As-New, phase order and
branching, and failure handling.
"""
import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from semantic_ingress.temporal import constants as C
from semantic_ingress.temporal import workflow as wf_module
from semantic_ingress.temporal.models import (
    FAILED_ITEMS_CAP,
    CleanupResult,
    DeleteResult,
    DiskInput,
    DiskResult,
    FileInput,
    FileResult,
    FinalizeInput,
    FinalizeResult,
    FolderInput,
    FolderResult,
    LevelInfo,
    MirrorOutcome,
    PersistBatchInput,
    PersistBatchResult,
    PlanFoldersInput,
    PlanFoldersResult,
    PlanInput,
    RepoInput,
    RepoLensResult,
    RepoResult,
    ReportInput,
    ReportOutcome,
    ResetInput,
    SyncIdInput,
    SyncParams,
    SyncPlan,
    SyncState,
    SyncWorkflowInput,
    UsageTotals,
    WorkItem,
    WorkPage,
    WorkPageInput,
)
from semantic_ingress.temporal.workflow import SyncWorkflow



@dataclass
class Stubs:
    """Async stand-ins for every activity; record calls, concurrency and order."""

    n_files: int = 12
    folders: list[tuple[int, str]] = field(default_factory=lambda: [(1, "a/b"), (1, "a/c"), (0, "a"), (-1, "")])
    mode: str = "full"
    reset_index: bool = False
    n_deleted: int = 0
    n_lens_only: int = 0
    n_docs: int = 30
    file_delay: float = 0.02
    slow_file: int | None = None
    fail_files: set[str] = field(default_factory=set)
    persist_fails: bool = False
    report_fails: bool = False
    calls: list[str] = field(default_factory=list)
    file_calls: list[str] = field(default_factory=list)
    folder_events: list[tuple[str, str]] = field(default_factory=list)
    running: int = 0
    max_running: dict[str, int] = field(default_factory=dict)
    started_before_slow_done: int = 0
    slow_done: bool = False

    def _paths(self, kind):
        if kind == "folder":
            return [WorkItem(seq=i, path=p, depth=d) for i, (d, p) in enumerate(self.folders)]
        n = self.n_files if kind == "file" else self.n_lens_only
        return [WorkItem(seq=i, path=f"{kind}{i}.py", blob_oid="b") for i in range(n)]

    async def _concurrent(self, kind, delay):
        self.running += 1
        self.max_running[kind] = max(self.max_running.get(kind, 0), self.running)
        try:
            await asyncio.sleep(delay)
        finally:
            self.running -= 1

    def activities(self):
        s = self

        @activity.defn(name=C.PLAN_SYNC)
        async def plan_sync(inp: PlanInput) -> SyncPlan:
            s.calls.append("plan")
            return SyncPlan(commit_sha="c1", mode=s.mode, reset_index=s.reset_index, reason="r",
                            n_files=s.n_files, n_lens_only=s.n_lens_only, n_deleted=s.n_deleted)

        @activity.defn(name=C.LOAD_WORK_PAGE)
        async def load_work_page(inp: WorkPageInput) -> WorkPage:
            return WorkPage(items=s._paths(inp.kind)[inp.offset:inp.offset + inp.limit])

        @activity.defn(name=C.GENERATE_FILE_SUMMARY)
        async def generate_file_summary(inp: FileInput) -> FileResult:
            s.file_calls.append(inp.path)
            if s.slow_file is not None and inp.path != f"file{s.slow_file}.py" and not s.slow_done:
                s.started_before_slow_done += 1
            slow = s.slow_file is not None and inp.path == f"file{s.slow_file}.py"
            await s._concurrent("file", 0.5 if slow else s.file_delay)
            if slow:
                s.slow_done = True
            if inp.path in s.fail_files:
                raise ApplicationError("unreadable", type=C.FILE_UNREADABLE, non_retryable=True)
            return FileResult(path=inp.path, usage=UsageTotals(input_tokens=10, output_tokens=1))

        @activity.defn(name=C.REFRESH_FILE_LENS)
        async def refresh_file_lens(inp: FileInput) -> FileResult:
            s.calls.append("lens")
            return FileResult(path=inp.path, usage=UsageTotals(input_tokens=5))

        @activity.defn(name=C.PLAN_FOLDERS)
        async def plan_folders(inp: PlanFoldersInput) -> PlanFoldersResult:
            s.calls.append(f"plan_folders:{inp.lens_refreshed}")
            levels: list[LevelInfo] = []
            for i, (depth, _) in enumerate(s.folders):
                if levels and levels[-1].depth == depth:
                    levels[-1].count += 1
                else:
                    levels.append(LevelInfo(depth=depth, offset=i, count=1))
            return PlanFoldersResult(levels=levels)

        @activity.defn(name=C.GENERATE_FOLDER_SUMMARY)
        async def generate_folder_summary(inp: FolderInput) -> FolderResult:
            s.folder_events.append(("start", inp.folder_path))
            await s._concurrent("folder", 0.02)
            s.folder_events.append(("end", inp.folder_path))
            return FolderResult(path=inp.folder_path, generated=True, usage=UsageTotals(input_tokens=20))

        @activity.defn(name=C.GENERATE_REPO_SUMMARY)
        async def generate_repo_summary(inp: RepoInput) -> RepoResult:
            s.calls.append(f"repo:{inp.folders_generated}")
            return RepoResult(generated=True, usage=UsageTotals(input_tokens=30))

        @activity.defn(name=C.GENERATE_REPO_LENS)
        async def generate_repo_lens(inp: RepoInput) -> RepoLensResult:
            s.calls.append("repo_lens")
            return RepoLensResult(status="none")

        @activity.defn(name=C.DELETE_REMOVED_DOCS)
        async def delete_removed_docs(inp: SyncIdInput) -> DeleteResult:
            s.calls.append("delete")
            return DeleteResult(files_deleted=s.n_deleted)

        @activity.defn(name=C.RESET_INDEX)
        async def reset_index(inp: ResetInput) -> None:
            s.calls.append("reset")

        @activity.defn(name=C.PERSIST_BATCH)
        async def persist_batch(inp: PersistBatchInput) -> PersistBatchResult:
            s.calls.append("persist")
            if s.persist_fails:
                raise ApplicationError("mongo down", non_retryable=True)
            n = max(0, min(inp.limit, s.n_docs - inp.offset))
            return PersistBatchResult(n=n, processed=n, inserted=n)

        @activity.defn(name=C.FINALIZE_INDEX)
        async def finalize_index(inp: FinalizeInput) -> FinalizeResult:
            s.calls.append("finalize")
            return FinalizeResult()

        @activity.defn(name=C.MIRROR_SOURCE)
        async def mirror_source(inp: SyncIdInput) -> MirrorOutcome:
            s.calls.append("mirror")
            return MirrorOutcome(uploaded=1)

        @activity.defn(name=C.BUILD_TEMPORAL_REPORT)
        async def build_temporal_report(inp: ReportInput) -> ReportOutcome:
            s.calls.append("report")
            if s.report_fails:
                raise ApplicationError("docs search down", non_retryable=True)
            return ReportOutcome(status="ok", cost_usd=0.1)

        @activity.defn(name=C.WRITE_DISK_ARTIFACTS)
        async def write_disk_artifacts(inp: DiskInput) -> DiskResult:
            s.calls.append("disk")
            return DiskResult(written=3)

        @activity.defn(name=C.CLEANUP_STAGING)
        async def cleanup_staging(inp: SyncIdInput) -> CleanupResult:
            s.calls.append("cleanup")
            return CleanupResult(documents=s.n_docs)

        return [plan_sync, load_work_page, generate_file_summary, refresh_file_lens, plan_folders,
                generate_folder_summary, generate_repo_summary, generate_repo_lens, delete_removed_docs,
                reset_index, persist_batch, finalize_index, mirror_source, build_temporal_report,
                write_disk_artifacts, cleanup_staging]


async def run(env, stubs: Stubs, **params):
    queue = f"tq-{uuid.uuid4().hex[:8]}"
    p = SyncParams(repo_id="demo", prefix="semv2", repo_path="/repos/demo", **params)
    async with Worker(env.client, task_queue=queue, workflows=[SyncWorkflow], activities=stubs.activities()):
        handle = await env.client.start_workflow(
            SyncWorkflow.run, SyncWorkflowInput(params=p), id=f"sync-{uuid.uuid4().hex[:8]}", task_queue=queue,
        )
        return await handle.result(), handle


# ----------------------------------------------------------------------
# Sliding window
# ----------------------------------------------------------------------

@pytest.mark.parametrize("window", [1, 5, 8])
async def test_window_caps_concurrency(env, window):
    stubs = Stubs(n_files=12, folders=[(1, f"a/f{i}") for i in range(10)] + [(0, "a"), (-1, "")])
    out, _ = await run(env, stubs, window_size=window, page_size=4)
    assert stubs.max_running["file"] == min(window, 12)
    assert stubs.max_running["folder"] == min(window, 10)
    assert sorted(stubs.file_calls) == sorted(f"file{i}.py" for i in range(12))
    assert out.files_done == 12 and out.folders_generated == 12


async def test_window_slides_past_a_slow_item(env):
    stubs = Stubs(n_files=20, slow_file=0)
    await run(env, stubs, window_size=5)
    # Waves of 5 would start nothing until file0 finished; a sliding window keeps going.
    assert stubs.started_before_slow_done > 4


async def test_folder_levels_have_a_barrier(env):
    stubs = Stubs(folders=[(2, "a/b/c"), (2, "a/b/d"), (2, "a/e/f"), (1, "a/b"), (1, "a/e"), (0, "a"), (-1, "")])
    await run(env, stubs, window_size=8)
    depth = {p: d for d, p in stubs.folders}
    for i, (event, path) in enumerate(stubs.folder_events):
        if event == "start":
            ended_before = {p for e, p in stubs.folder_events[:i] if e == "end"}
            deeper = {p for p, d in depth.items() if d > depth[path]}
            assert deeper <= ended_before, f"{path or '.'} started before its deeper level finished"


# ----------------------------------------------------------------------
# Continue-As-New
# ----------------------------------------------------------------------

async def test_continue_as_new_carries_cursors_usage_and_failures(env):
    plain, _ = await run(env, Stubs(n_files=12, fail_files={"file7.py"}), window_size=5)
    stubs = Stubs(n_files=12, fail_files={"file7.py"})
    out, handle = await run(env, stubs, window_size=5, test_can_after_items=3)

    assert out.continued_as_new_count > 0
    assert sorted(stubs.file_calls) == sorted(f"file{i}.py" for i in range(12))  # each exactly once
    assert out.usage == plain.usage and out.failed == plain.failed
    assert out.files_done == plain.files_done == 12 and out.folders_generated == plain.folders_generated

    # The first run of the chain ended in Continue-As-New, and the chain replays.
    first = await env.client.get_workflow_handle(handle.id, run_id=handle.first_execution_run_id).fetch_history()
    assert first.events[-1].HasField("workflow_execution_continued_as_new_event_attributes")
    await Replayer(workflows=[SyncWorkflow]).replay_workflow(first)


def test_can_due_on_the_server_suggestion(monkeypatch):
    wf = SyncWorkflow()
    wf._params = SyncParams(repo_id="demo", prefix="semv2", repo_path="/r")
    state = SyncState(sync_id="s")

    class Info:
        def __init__(self, suggested):
            self.suggested = suggested

        def is_continue_as_new_suggested(self):
            return self.suggested

    monkeypatch.setattr(wf_module.workflow, "info", lambda: Info(False))
    assert not wf._can_due(state)
    monkeypatch.setattr(wf_module.workflow, "info", lambda: Info(True))
    assert wf._can_due(state)


# ----------------------------------------------------------------------
# Phases and branching
# ----------------------------------------------------------------------

def _phases(calls):
    return [c.split(":")[0] for c in calls]


async def test_full_run_phase_order(env):
    stubs = Stubs(reset_index=True, n_docs=1200)
    out, _ = await run(env, stubs, window_size=5, lens_enabled=True, output_dir="/out")
    assert _phases(stubs.calls) == ["plan", "plan_folders", "repo", "repo_lens", "reset", "persist", "persist",
                                    "persist", "finalize", "mirror", "report", "disk", "cleanup"]
    assert out.persist.processed == 1200 and out.disk_written == 3 and out.report.status == "ok"


async def test_incremental_deletes_before_persisting(env):
    stubs = Stubs(mode="incremental", n_deleted=2)
    out, _ = await run(env, stubs, window_size=5)
    phases = _phases(stubs.calls)
    assert "reset" not in phases
    assert phases.index("delete") < phases.index("persist")
    assert out.deleted.files_deleted == 2


async def test_dry_run_skips_every_write(env):
    stubs = Stubs(mode="incremental", n_deleted=2, n_docs=7)
    out, _ = await run(env, stubs, window_size=5, dry_run=True, lens_enabled=True)
    assert _phases(stubs.calls) == ["plan", "plan_folders", "repo", "repo_lens", "cleanup"]
    assert out.would_upsert == 7 and out.persist is None


async def test_refresh_lens_and_report_switches(env):
    stubs = Stubs(mode="incremental", n_lens_only=3)
    await run(env, stubs, window_size=5, refresh_lens=True, lens_enabled=True, no_report=True)
    assert stubs.calls.count("lens") == 3 and "plan_folders:True" in stubs.calls
    assert "report" not in stubs.calls

    stubs = Stubs(mode="incremental", n_lens_only=3)
    await run(env, stubs, window_size=5, lens_enabled=False)  # lens off: no lens work, no report
    assert "lens" not in stubs.calls and "repo_lens" not in stubs.calls and "report" not in stubs.calls
    assert "plan_folders:False" in stubs.calls


# ----------------------------------------------------------------------
# Failures
# ----------------------------------------------------------------------

async def test_failures_are_recorded_and_the_run_continues(env):
    stubs = Stubs(fail_files={"file3.py"}, persist_fails=True, report_fails=True)
    out, _ = await run(env, stubs, window_size=5, lens_enabled=True)
    assert [(f.path, f.error) for f in out.failed] == [("file3.py", "unreadable")]
    assert out.persist_error == "mongo down"
    assert "finalize" not in stubs.calls  # index metadata only follows a successful persist
    assert "mirror" in stubs.calls and "report" in stubs.calls
    assert out.report.status == "failed" and out.report.error == "docs search down"


def test_failed_items_are_truncated_and_capped():
    wf = SyncWorkflow()
    state = SyncState(sync_id="s")
    for i in range(FAILED_ITEMS_CAP + 5):
        wf._fail(state, f"f{i}", "x" * 1000)
    assert len(state.failed) == FAILED_ITEMS_CAP and state.failed_overflow == 5
    assert len(state.failed[0].error) == 300


async def test_workflow_module_loads_in_the_sandbox(env):
    # Creating a worker validates the workflow inside the sandbox.
    Worker(env.client, task_queue="validate", workflows=[SyncWorkflow], activities=[])


async def test_captured_histories_replay():
    """Histories captured from real runs (tests/temporal/histories; one ends in Continue-As-New)."""
    from pathlib import Path

    paths = sorted((Path(__file__).parent / "histories").glob("*.json"))
    assert paths
    for path in paths:
        history = WorkflowHistory.from_json(path.stem, path.read_text())
        await Replayer(workflows=[SyncWorkflow]).replay_workflow(history)
