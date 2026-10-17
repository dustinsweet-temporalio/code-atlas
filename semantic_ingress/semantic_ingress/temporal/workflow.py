"""
SyncWorkflow: `semantic-ingress sync` as a Temporal workflow.

Phases (each resumable from `SyncState` alone):

    PLAN -> FILES -> LENS_REFRESH -> FOLDERS -> REPO
         -> (dry run: CLEANUP -> DONE)
         -> DELETE -> PERSIST -> FINALIZE -> MIRROR -> REPORT -> DISK_OUTPUT -> CLEANUP -> DONE

File, lens and folder work runs in a sliding window of `window_size` activities:
when one finishes, the next starts. Folders slide within one depth level, deepest
first, with a barrier between levels, because a folder is summarized from its
child folders. Work items are paged in from staging; summaries never pass through
the workflow (activities stage them in MongoDB).

Continue-As-New: when the server suggests it (or after `test_can_after_items`
completed items, a test hook), the window stops launching, drains what is in
flight, records it, and the run continues as new with the same state. Cursors only
advance after the work for them is recorded, so nothing is lost or run twice.

Deterministic: imports only the standard library, temporalio and the payload
models; logs through workflow.logger.
"""
import asyncio
from typing import Awaitable, Callable

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from semantic_ingress.temporal import constants as C
    from semantic_ingress.temporal.models import (
        ERROR_CHARS,
        FAILED_ITEMS_CAP,
        CleanupResult,
        DeleteResult,
        DiskInput,
        DiskResult,
        FailedItem,
        FileInput,
        FileResult,
        FinalizeInput,
        FinalizeResult,
        FolderInput,
        FolderResult,
        MirrorOutcome,
        PersistBatchInput,
        PersistBatchResult,
        PersistTotals,
        PlanFoldersInput,
        PlanFoldersResult,
        PlanInput,
        Progress,
        RepoInput,
        RepoLensResult,
        RepoResult,
        ReportInput,
        ReportOutcome,
        ResetInput,
        SyncIdInput,
        SyncOutcome,
        SyncParams,
        SyncPlan,
        SyncState,
        SyncWorkflowInput,
        WorkItem,
        WorkPage,
        WorkPageInput,
    )


def error_text(e: BaseException) -> str:
    """The message of the underlying failure of an activity error."""
    cause = e
    while isinstance(cause, ActivityError) and cause.cause is not None:
        cause = cause.cause
    if isinstance(cause, ApplicationError):
        text = cause.message
    else:
        text = str(cause) or type(cause).__name__
    return text[:ERROR_CHARS]


@workflow.defn(name=C.WORKFLOW_NAME)
class SyncWorkflow:
    def __init__(self) -> None:
        self._state: SyncState | None = None
        self._params: SyncParams | None = None
        self._pages: dict[str, tuple[int, list[WorkItem]]] = {}   # kind -> (offset, items); not carried

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @workflow.query(name=C.PROGRESS_QUERY)
    def progress(self) -> Progress:
        s = self._state
        if s is None:
            return Progress(phase="STARTING", done=0, total=0, failed=0)
        done = total = 0
        if s.plan is not None:
            if s.phase == "FILES":
                done, total = s.files_done, s.plan.n_files
            elif s.phase == "LENS_REFRESH":
                done, total = s.lens_done, s.plan.n_lens_only
            elif s.phase == "FOLDERS":
                done, total = s.folders_done, sum(level.count for level in s.plan.levels)
            elif s.phase == "PERSIST":
                done = s.persist_offset
        return Progress(
            phase=s.phase, done=done, total=total, failed=len(s.failed) + s.failed_overflow,
            usage=s.usage, continued_as_new_count=s.continued_as_new_count, plan=s.plan,
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, inp: SyncWorkflowInput) -> SyncOutcome:
        self._params = inp.params
        self._state = s = inp.state or SyncState(sync_id=str(workflow.uuid4()))
        workflow.logger.info("sync %s: phase %s (run %d)", s.sync_id, s.phase, s.continued_as_new_count + 1)

        if s.phase == "PLAN":
            await self._plan(s)
        if s.phase == "FILES":
            await self._files(s)
        if s.phase == "LENS_REFRESH":
            await self._lens_refresh(s)
        if s.phase == "FOLDERS":
            await self._folders(s)
        if s.phase == "REPO":
            await self._repo(s)
        if s.phase == "DELETE":
            await self._delete(s)
        if s.phase == "PERSIST":
            await self._persist(s)
        if s.phase == "FINALIZE":
            await self._finalize(s)
        if s.phase == "MIRROR":
            await self._mirror(s)
        if s.phase == "REPORT":
            await self._report(s)
        if s.phase == "DISK_OUTPUT":
            await self._disk_output(s)
        if s.phase == "CLEANUP":
            await self._cleanup(s)
        return self._outcome(s)

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def _plan(self, s: SyncState) -> None:
        s.plan = await workflow.execute_activity(
            C.PLAN_SYNC, PlanInput(sync_id=s.sync_id, params=self._params), result_type=SyncPlan,
            start_to_close_timeout=C.PLAN_TIMEOUT, retry_policy=C.LOCAL_RETRY,
        )
        await self._advance(s, "FILES")

    async def _files(self, s: SyncState) -> None:
        plan = s.plan
        prefix = "Regeneration: " if plan.mode == "incremental" else ""

        async def launch(item: WorkItem) -> FileResult:
            try:
                return await workflow.execute_activity(
                    C.GENERATE_FILE_SUMMARY,
                    FileInput(sync_id=s.sync_id, commit_sha=plan.commit_sha, path=item.path, blob_oid=item.blob_oid),
                    result_type=FileResult, start_to_close_timeout=C.FILE_TIMEOUT,
                    heartbeat_timeout=C.ITEM_HEARTBEAT_TIMEOUT, retry_policy=C.LLM_RETRY,
                )
            except ActivityError as e:
                return FileResult(path=item.path, failed=True, error=error_text(e))

        def record(r: FileResult) -> None:
            s.files_done += 1
            s.usage.add(r.usage)
            if r.failed:
                self._fail(s, r.path, prefix + (r.error or ""))

        while s.file_cursor < plan.n_files:
            s.file_cursor = await self._slide(s, "file", s.file_cursor, plan.n_files, launch, record)
            if s.file_cursor < plan.n_files:
                await self._maybe_continue_as_new(s)
        await self._advance(s, "LENS_REFRESH")

    async def _lens_refresh(self, s: SyncState) -> None:
        plan = s.plan

        async def launch(item: WorkItem) -> FileResult:
            try:
                return await workflow.execute_activity(
                    C.REFRESH_FILE_LENS,
                    FileInput(sync_id=s.sync_id, commit_sha=plan.commit_sha, path=item.path, blob_oid=item.blob_oid),
                    result_type=FileResult, start_to_close_timeout=C.FILE_TIMEOUT,
                    heartbeat_timeout=C.ITEM_HEARTBEAT_TIMEOUT, retry_policy=C.LLM_RETRY,
                )
            except ActivityError as e:
                return FileResult(path=item.path, failed=True, error=error_text(e))

        def record(r: FileResult) -> None:
            s.lens_done += 1
            s.usage.add(r.usage)
            if r.failed:
                self._fail(s, r.path, "Lens refresh: " + (r.error or ""))
            else:
                s.lens_refreshed_any = True

        if self._params.refresh_lens and self._params.lens_enabled:
            while s.lens_cursor < plan.n_lens_only:
                s.lens_cursor = await self._slide(s, "lens", s.lens_cursor, plan.n_lens_only, launch, record)
                if s.lens_cursor < plan.n_lens_only:
                    await self._maybe_continue_as_new(s)
        await self._advance(s, "FOLDERS")

    async def _folders(self, s: SyncState) -> None:
        plan = s.plan
        if not s.folders_planned:
            planned = await workflow.execute_activity(
                C.PLAN_FOLDERS, PlanFoldersInput(sync_id=s.sync_id, lens_refreshed=s.lens_refreshed_any),
                result_type=PlanFoldersResult, start_to_close_timeout=C.PLAN_TIMEOUT, retry_policy=C.LOCAL_RETRY,
            )
            plan.levels = planned.levels
            s.folders_planned = True
            s.folder_level = 0
            s.folder_cursor = 0

        async def launch(item: WorkItem) -> FolderResult:
            try:
                return await workflow.execute_activity(
                    C.GENERATE_FOLDER_SUMMARY,
                    FolderInput(sync_id=s.sync_id, commit_sha=plan.commit_sha, folder_path=item.path),
                    result_type=FolderResult, start_to_close_timeout=C.FOLDER_TIMEOUT,
                    heartbeat_timeout=C.ITEM_HEARTBEAT_TIMEOUT, retry_policy=C.LLM_RETRY,
                )
            except ActivityError as e:
                return FolderResult(path=item.path, failed=True, error=error_text(e))

        def record(r: FolderResult) -> None:
            s.folders_done += 1
            s.usage.add(r.usage)
            if r.failed:
                self._fail(s, f"FOLDER SUMMARY: {r.path or '.'}", r.error or "")
            elif r.generated:
                s.folders_generated += 1

        # Deepest level first; every folder of a level is done before the next level starts.
        while s.folder_level < len(plan.levels):
            level = plan.levels[s.folder_level]
            end = level.offset + level.count
            s.folder_cursor = max(s.folder_cursor, level.offset)
            while s.folder_cursor < end:
                s.folder_cursor = await self._slide(s, "folder", s.folder_cursor, end, launch, record)
                if s.folder_cursor < end:
                    await self._maybe_continue_as_new(s)
            s.folder_level += 1
            await self._maybe_continue_as_new(s)
        await self._advance(s, "REPO")

    async def _repo(self, s: SyncState) -> None:
        plan = s.plan
        repo_input = RepoInput(
            sync_id=s.sync_id, commit_sha=plan.commit_sha,
            incremental=plan.mode == "incremental", folders_generated=s.folders_generated,
        )
        try:
            result = await workflow.execute_activity(
                C.GENERATE_REPO_SUMMARY, repo_input, result_type=RepoResult,
                start_to_close_timeout=C.REPO_SUMMARY_TIMEOUT, retry_policy=C.LLM_RETRY,
            )
            s.usage.add(result.usage)
            s.repo_summary_generated = result.generated
        except ActivityError as e:
            self._fail(s, "REPOSITORY SUMMARY", error_text(e))

        if s.repo_summary_generated and self._params.lens_enabled:
            try:
                s.repo_lens = await workflow.execute_activity(
                    C.GENERATE_REPO_LENS, repo_input, result_type=RepoLensResult,
                    start_to_close_timeout=C.REPO_LENS_TIMEOUT, heartbeat_timeout=C.HEARTBEAT_TIMEOUT,
                    retry_policy=C.REPO_LENS_RETRY,
                )
                s.usage.add(s.repo_lens.usage)
            except ActivityError as e:  # the repo summary stands on its own
                self._fail(s, "REPOSITORY LENS", error_text(e))

        await self._advance(s, "CLEANUP" if self._params.dry_run else "DELETE")

    async def _delete(self, s: SyncState) -> None:
        # Incremental runs remove documents of deleted files (and folders left empty)
        # just before persisting; a full run replaces the index instead.
        if s.plan.mode == "incremental" and s.plan.n_deleted and s.deleted is None:
            try:
                s.deleted = await workflow.execute_activity(
                    C.DELETE_REMOVED_DOCS, SyncIdInput(sync_id=s.sync_id), result_type=DeleteResult,
                    start_to_close_timeout=C.DELETE_TIMEOUT, retry_policy=C.LOCAL_RETRY,
                )
            except ActivityError as e:
                s.delete_error = error_text(e)
        await self._advance(s, "PERSIST")

    async def _persist(self, s: SyncState) -> None:
        if s.persist is None:
            s.persist = PersistTotals()
        try:
            # A full re-index drops the old index only now, after generation succeeded.
            if s.plan.reset_index and not s.reset_done:
                await workflow.execute_activity(
                    C.RESET_INDEX, ResetInput(repo_id=self._params.repo_id, prefix=self._params.prefix),
                    start_to_close_timeout=C.RESET_TIMEOUT, retry_policy=C.LOCAL_RETRY,
                )
                s.reset_done = True
            while not s.persist_done:
                batch = await workflow.execute_activity(
                    C.PERSIST_BATCH,
                    PersistBatchInput(sync_id=s.sync_id, offset=s.persist_offset, limit=C.PERSIST_BATCH_SIZE),
                    result_type=PersistBatchResult, start_to_close_timeout=C.PERSIST_TIMEOUT,
                    retry_policy=C.PERSIST_RETRY,
                )
                s.persist_offset += batch.n
                s.persist.processed += batch.processed
                s.persist.inserted += batch.inserted
                s.persist.updated += batch.updated
                s.persist.no_change += batch.no_change
                s.persist.errors += batch.errors
                s.items_since_can += 1
                if batch.n < C.PERSIST_BATCH_SIZE:
                    s.persist_done = True
                else:
                    await self._maybe_continue_as_new(s)
        except ActivityError as e:
            s.persist_error = error_text(e)
        # Index metadata and text indexes follow only a successful persist.
        await self._advance(s, "MIRROR" if s.persist_error else "FINALIZE")

    async def _finalize(self, s: SyncState) -> None:
        try:
            s.finalize = await workflow.execute_activity(
                C.FINALIZE_INDEX, FinalizeInput(sync_id=s.sync_id, incremental=s.plan.mode == "incremental"),
                result_type=FinalizeResult, start_to_close_timeout=C.FINALIZE_TIMEOUT, retry_policy=C.LOCAL_RETRY,
            )
        except ActivityError as e:
            s.finalize_error = error_text(e)
        await self._advance(s, "MIRROR")

    async def _mirror(self, s: SyncState) -> None:
        try:
            s.mirror = await workflow.execute_activity(
                C.MIRROR_SOURCE, SyncIdInput(sync_id=s.sync_id), result_type=MirrorOutcome,
                start_to_close_timeout=C.MIRROR_TIMEOUT, heartbeat_timeout=C.HEARTBEAT_TIMEOUT,
                retry_policy=C.MIRROR_RETRY,
            )
        except ActivityError as e:
            s.mirror = MirrorOutcome(error=error_text(e))
        await self._advance(s, "REPORT")

    async def _report(self, s: SyncState) -> None:
        # After persistence and the source mirror, which it reads. It runs when the
        # index changed or no good report is stored; a failure never fails the sync.
        # The staged documents always include the dir_list document, so the index
        # always counts as changed (as in the in-process sync this replaces).
        if self._params.lens_enabled and not self._params.no_report:
            try:
                s.report = await workflow.execute_activity(
                    C.BUILD_TEMPORAL_REPORT, ReportInput(sync_id=s.sync_id, index_changed=True),
                    result_type=ReportOutcome, start_to_close_timeout=C.REPORT_TIMEOUT,
                    heartbeat_timeout=C.HEARTBEAT_TIMEOUT, retry_policy=C.REPORT_RETRY,
                )
            except ActivityError as e:
                s.report = ReportOutcome(status="failed", error=error_text(e))
        await self._advance(s, "DISK_OUTPUT")

    async def _disk_output(self, s: SyncState) -> None:
        if self._params.output_dir:
            try:
                written = await workflow.execute_activity(
                    C.WRITE_DISK_ARTIFACTS, DiskInput(sync_id=s.sync_id, output_dir=self._params.output_dir),
                    result_type=DiskResult, start_to_close_timeout=C.DISK_TIMEOUT, retry_policy=C.LOCAL_RETRY,
                )
                s.disk_written = written.written
            except ActivityError as e:
                s.disk_error = error_text(e)
        await self._advance(s, "CLEANUP")

    async def _cleanup(self, s: SyncState) -> None:
        try:
            counts = await workflow.execute_activity(
                C.CLEANUP_STAGING, SyncIdInput(sync_id=s.sync_id), result_type=CleanupResult,
                start_to_close_timeout=C.CLEANUP_TIMEOUT, retry_policy=C.LOCAL_RETRY,
            )
            if self._params.dry_run:
                s.would_upsert, s.would_update_lens = counts.documents, counts.lens_updates
        except ActivityError as e:  # the staging TTL removes it later
            workflow.logger.warning("sync %s: staging cleanup failed: %s", s.sync_id, error_text(e))
        s.phase = "DONE"

    # ------------------------------------------------------------------
    # Sliding window and Continue-As-New
    # ------------------------------------------------------------------

    async def _slide(
        self,
        s: SyncState,
        kind: str,
        cursor: int,
        end: int,
        launch: Callable[[WorkItem], Awaitable],
        record: Callable[[object], None],
    ) -> int:
        """
        Keep up to `window_size` activities in flight over items [cursor, end). When one
        completes, record it and start the next. Returns the index of the next item not
        yet launched; on return nothing is in flight. Stops launching, drains what is in
        flight and returns early when Continue-As-New is due.

        `launch` never raises ActivityError (it returns a failed result instead).
        """
        window = max(1, self._params.window_size)
        in_flight: dict[asyncio.Task, int] = {}
        try:
            while True:
                while len(in_flight) < window and cursor < end and not self._can_due(s):
                    item = await self._item(s, kind, cursor)
                    in_flight[asyncio.create_task(launch(item))] = cursor
                    cursor += 1
                if not in_flight:
                    return cursor
                done, _ = await workflow.wait(list(in_flight), return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(done, key=in_flight.__getitem__):  # by item index: replay-stable
                    in_flight.pop(task)
                    record(task.result())
                    s.items_since_can += 1
        finally:
            for task in in_flight:  # cancellation or an unexpected error: no orphaned activities
                task.cancel()

    async def _item(self, s: SyncState, kind: str, index: int) -> WorkItem:
        """Work item `index` of a kind, paged in from staging (the page buffer is not carried over CaN)."""
        offset, items = self._pages.get(kind, (0, []))
        if not offset <= index < offset + len(items):
            page = await workflow.execute_activity(
                C.LOAD_WORK_PAGE,
                WorkPageInput(sync_id=s.sync_id, kind=kind, offset=index, limit=self._params.page_size),
                result_type=WorkPage, start_to_close_timeout=C.WORK_PAGE_TIMEOUT, retry_policy=C.LOCAL_RETRY,
            )
            if not page.items or page.items[0].seq != index:
                raise ApplicationError(f"Work item {kind} #{index} is missing from staging", non_retryable=True)
            offset, items = index, page.items
            self._pages[kind] = (offset, items)
        return items[index - offset]

    def _can_due(self, s: SyncState) -> bool:
        if workflow.info().is_continue_as_new_suggested():
            return True
        n = self._params.test_can_after_items
        return bool(n) and s.items_since_can >= n

    async def _maybe_continue_as_new(self, s: SyncState) -> None:
        if not self._can_due(s):
            return
        await workflow.wait_condition(workflow.all_handlers_finished)
        s.items_since_can = 0
        s.continued_as_new_count += 1
        workflow.logger.info("sync %s: continuing as new in phase %s", s.sync_id, s.phase)
        workflow.continue_as_new(SyncWorkflowInput(params=self._params, state=s))

    async def _advance(self, s: SyncState, phase: str) -> None:
        s.phase = phase
        workflow.logger.info("sync %s: phase %s", s.sync_id, phase)
        await self._maybe_continue_as_new(s)

    # ------------------------------------------------------------------

    def _fail(self, s: SyncState, path: str, error: str) -> None:
        if len(s.failed) < FAILED_ITEMS_CAP:
            s.failed.append(FailedItem(path=path, error=error[:ERROR_CHARS]))
        else:
            s.failed_overflow += 1

    def _outcome(self, s: SyncState) -> SyncOutcome:
        return SyncOutcome(
            sync_id=s.sync_id,
            dry_run=self._params.dry_run,
            plan=s.plan,
            usage=s.usage,
            failed=s.failed,
            failed_overflow=s.failed_overflow,
            deleted=s.deleted,
            delete_error=s.delete_error,
            persist=s.persist,
            persist_error=s.persist_error,
            finalize=s.finalize,
            finalize_error=s.finalize_error,
            mirror=s.mirror,
            report=s.report,
            disk_written=s.disk_written,
            disk_error=s.disk_error,
            would_upsert=s.would_upsert,
            would_update_lens=s.would_update_lens,
            files_done=s.files_done,
            folders_generated=s.folders_generated,
            repo_summary_generated=s.repo_summary_generated,
            repo_lens=s.repo_lens,
            continued_as_new_count=s.continued_as_new_count,
        )
