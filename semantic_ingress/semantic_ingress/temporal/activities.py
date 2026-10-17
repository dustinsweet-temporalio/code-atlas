"""
Activities of the sync workflow.

`IndexActivities` holds what every activity needs (config, preflight result, Mongo
stores, the LLM client, the Git repository) and registers its bound methods on the
embedded worker. The activities are synchronous (the Anthropic, Mongo and pygit2
clients block) and run on the worker's thread pool. Each delegates to
core/sync_steps.py, stages what it generates, and returns a small result.

Errors are translated for Temporal's retry policy: permanent model errors and
unreadable files are non-retryable; rate limits and other transient model errors
are retried, honouring a retry-after hint; anything else is retried as-is.
"""
import contextvars
import json
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterator

import anthropic
import structlog
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core import sync_steps
from semantic_ingress.core.artifact_generator import ArtifactGenerator
from semantic_ingress.core.disk_writer import DiskWriter
from semantic_ingress.core.git_adapter import FileUnreadableError, GitAdapter
from semantic_ingress.core.llm_client import (
    LLMClient,
    LLMError,
    LLMPermanentError,
    LLMUsage,
    classify_llm_error,
)
from semantic_ingress.core.source_mirror_orchestrator import SourceMirrorOrchestrator
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.models import RepositorySnapshot
from semantic_ingress.preflight import PreflightResult
from semantic_ingress.storage import MongoStore
from semantic_ingress.storage.staging_store import StagingStore
from semantic_ingress.temporal import constants as C
from semantic_ingress.temporal.models import (
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
    SyncPlan,
    UsageTotals,
    WorkItem,
    WorkPage,
    WorkPageInput,
)

logger = structlog.get_logger()


def usage_totals(usage: LLMUsage) -> UsageTotals:
    return UsageTotals(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def to_activity_error(e: Exception) -> BaseException:
    """The exception an activity should raise for `e`, classified for the retry policy."""
    if isinstance(e, (ApplicationError, CancelledError)):
        return e
    if isinstance(e, FileUnreadableError):
        return ApplicationError(str(e), type=C.FILE_UNREADABLE, non_retryable=True)
    if isinstance(e, (anthropic.APIError, LLMError, ValidationError, json.JSONDecodeError)):
        error = classify_llm_error(e)
        if isinstance(error, LLMPermanentError):
            return ApplicationError(str(error), type=C.LLM_PERMANENT, non_retryable=True)
        delay = timedelta(seconds=error.retry_after) if error.retry_after else None
        return ApplicationError(str(error), type=C.LLM_TRANSIENT, next_retry_delay=delay)
    return e


@contextmanager
def translated_errors() -> Iterator[None]:
    try:
        yield
    except Exception as e:
        error = to_activity_error(e)
        if error is e:
            raise
        raise error from e


@contextmanager
def heartbeating(interval: float = C.HEARTBEAT_INTERVAL_SECONDS,
                 details: Callable[[], tuple] = lambda: ()) -> Iterator[None]:
    """
    Heartbeat from a helper thread while a long synchronous body runs, so the server
    notices a dead worker within the heartbeat timeout and can deliver cancellation
    (the SDK then raises CancelledError in the activity thread). The thread runs
    activity calls in a copy of the activity's context, which they need.
    """
    stop = threading.Event()
    ctx = contextvars.copy_context()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                ctx.run(activity.heartbeat, *details())
            except Exception as e:  # never let the helper thread die noisily
                logger.warning("heartbeat_failed", error=str(e))

    ctx.run(activity.heartbeat, *details())
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


class IndexActivities:
    """The sync workflow's activities, bound to one repository's run."""

    def __init__(
        self,
        config: IngestionConfig,
        preflight: PreflightResult,
        store: MongoStore,
        staging: StagingStore,
        llm: LLMClient,
        git: GitAdapter | None = None,
    ) -> None:
        self.config = config
        self.repo_id = config.repo_id or "unknown"
        self.preflight = preflight
        self.pack = preflight.pack if config.lens.enabled else None
        self.docs_search = preflight.docs.search if preflight.docs is not None else None
        self.store = store
        self.staging = staging
        self.llm = llm
        self.git = git or GitAdapter(config.repo_path, config.file_filter)
        self._lock = threading.Lock()
        self._snapshots: dict[str, RepositorySnapshot] = {}
        self._trees: dict[str, sync_steps.FolderTree] = {}
        self._generators: dict[str, ArtifactGenerator] = {}
        self._plans: dict[str, dict] = {}

    def all(self) -> list[Callable]:
        return [
            self.plan_sync, self.load_work_page, self.generate_file_summary, self.refresh_file_lens,
            self.plan_folders, self.generate_folder_summary, self.generate_repo_summary,
            self.generate_repo_lens, self.delete_removed_docs, self.reset_index, self.persist_batch,
            self.finalize_index, self.mirror_source, self.build_temporal_report,
            self.write_disk_artifacts, self.cleanup_staging,
        ]

    # ------------------------------------------------------------------
    # Per-commit caches (the commit is pinned by plan_sync)
    # ------------------------------------------------------------------

    def _snapshot(self, commit_sha: str) -> RepositorySnapshot:
        with self._lock:
            if commit_sha not in self._snapshots:
                self._snapshots[commit_sha] = self.git.get_snapshot(commit_sha)
            return self._snapshots[commit_sha]

    def _tree(self, commit_sha: str) -> sync_steps.FolderTree:
        snapshot = self._snapshot(commit_sha)
        with self._lock:
            if commit_sha not in self._trees:
                self._trees[commit_sha] = sync_steps.FolderTree.of(snapshot)
            return self._trees[commit_sha]

    def _generator(self, commit_sha: str) -> ArtifactGenerator:
        with self._lock:
            if commit_sha not in self._generators:
                self._generators[commit_sha] = ArtifactGenerator(self.git, self.llm, commit_sha, pack=self.pack)
            return self._generators[commit_sha]

    def _plan(self, sync_id: str) -> dict:
        with self._lock:
            cached = self._plans.get(sync_id)
        if cached is not None:
            return cached
        plan = self.staging.get_plan(sync_id)
        if plan is None:
            raise ApplicationError(f"No staged plan for sync {sync_id} (staging expired or was cleaned up)",
                                   type="STAGING_MISSING", non_retryable=True)
        with self._lock:
            self._plans[sync_id] = plan
        return plan

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    @activity.defn(name=C.PLAN_SYNC)
    def plan_sync(self, inp: PlanInput) -> SyncPlan:
        if inp.params.repo_id != self.repo_id:
            raise ApplicationError(
                f"This worker indexes {self.repo_id}, not {inp.params.repo_id}",
                type=C.INVALID_OPTIONS, non_retryable=True,
            )
        with translated_errors():
            try:
                return sync_steps.plan_sync(
                    self.config, self.pack, self.store, self.staging, self.git, inp.params, inp.sync_id
                )
            except FileUnreadableError:
                raise
            except ValueError as e:  # e.g. --refresh-lens without the lens, or no commits
                raise ApplicationError(str(e), type=C.INVALID_OPTIONS, non_retryable=True) from e

    @activity.defn(name=C.LOAD_WORK_PAGE)
    def load_work_page(self, inp: WorkPageInput) -> WorkPage:
        with translated_errors():
            rows = self.staging.load_work_items(inp.sync_id, inp.kind, inp.offset, inp.limit)
        return WorkPage(items=[
            WorkItem(seq=r["seq"], path=r["path"], blob_oid=r.get("blob_oid"), depth=r.get("depth"))
            for r in rows
        ])

    @activity.defn(name=C.PLAN_FOLDERS)
    def plan_folders(self, inp: PlanFoldersInput) -> PlanFoldersResult:
        with translated_errors():
            return PlanFoldersResult(
                levels=sync_steps.plan_folders(self.staging, self.git, inp.sync_id, inp.lens_refreshed)
            )

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    @activity.defn(name=C.GENERATE_FILE_SUMMARY)
    def generate_file_summary(self, inp: FileInput) -> FileResult:
        with heartbeating(), translated_errors():
            doc, usage, rel = sync_steps.generate_file(
                self.repo_id, self._generator(inp.commit_sha), inp.commit_sha, inp.path, inp.blob_oid
            )
            self.staging.stage_document(inp.sync_id, doc)
        print(f"  ✓ {inp.path} | {usage.input_tokens:,} in / {usage.output_tokens:,} out | "
              f"${usage.total_cost_usd:.4f}")
        return FileResult(path=inp.path, usage=usage_totals(usage), rel=rel)

    @activity.defn(name=C.REFRESH_FILE_LENS)
    def refresh_file_lens(self, inp: FileInput) -> FileResult:
        with heartbeating(), translated_errors():
            record, usage = sync_steps.refresh_lens(self._generator(inp.commit_sha), inp.path, inp.blob_oid)
            self.staging.stage_lens_update(inp.sync_id, inp.path, record.model_dump(), compact_file_lens(record))
        print(f"  ✓ {inp.path} rel={record.rel} | ${usage.total_cost_usd:.4f}")
        return FileResult(path=inp.path, usage=usage_totals(usage), rel=record.rel)

    @activity.defn(name=C.GENERATE_FOLDER_SUMMARY)
    def generate_folder_summary(self, inp: FolderInput) -> FolderResult:
        display = inp.folder_path or "."
        with heartbeating(), translated_errors():
            plan = self._plan(inp.sync_id)
            children = sync_steps.load_children(
                self.store, self.staging, inp.sync_id, self.repo_id, inp.folder_path,
                self._tree(inp.commit_sha), incremental=plan["mode"] == "incremental",
            )
            out = sync_steps.generate_folder_from_children(
                self._generator(inp.commit_sha), self.pack, inp.folder_path,
                children.files, children.folders, children.file_lenses, children.rollups,
            )
            if out is None:
                return FolderResult(path=inp.folder_path, skipped=True)
            summary, rollup, usage = out
            self.staging.stage_document(
                inp.sync_id,
                sync_steps.folder_document(self.repo_id, inp.commit_sha, inp.folder_path, summary, rollup),
            )
        print(f"  ✓ {display}/ | {usage.input_tokens:,} in / {usage.output_tokens:,} out | "
              f"${usage.total_cost_usd:.4f}")
        return FolderResult(path=inp.folder_path, usage=usage_totals(usage), generated=True)

    @activity.defn(name=C.GENERATE_REPO_SUMMARY)
    def generate_repo_summary(self, inp: RepoInput) -> RepoResult:
        with translated_errors():
            out = sync_steps.generate_repo(
                self.store, self.staging, self._generator(inp.commit_sha), inp.sync_id, self.repo_id,
                inp.commit_sha, inp.incremental, inp.folders_generated, need_lens=self.pack is not None,
            )
        return RepoResult(usage=usage_totals(out.usage), skipped=out.skipped, generated=out.generated)

    @activity.defn(name=C.GENERATE_REPO_LENS)
    def generate_repo_lens(self, inp: RepoInput) -> RepoLensResult:
        if self.pack is None:
            raise ApplicationError("The repository lens needs LENS_ENABLED=true",
                                   type=C.INVALID_OPTIONS, non_retryable=True)
        with heartbeating(), translated_errors():
            out = sync_steps.generate_repo_lens(
                self.pack, self.store, self.staging, self.git, self.llm, self.docs_search,
                inp.sync_id, self.repo_id, inp.commit_sha, inp.incremental,
            )
        return RepoLensResult(usage=usage_totals(out.usage), status=out.status, archetype=out.archetype,
                              n_processes=out.n_processes, searches=out.searches)

    # ------------------------------------------------------------------
    # Index writes
    # ------------------------------------------------------------------

    @activity.defn(name=C.DELETE_REMOVED_DOCS)
    def delete_removed_docs(self, inp: SyncIdInput) -> DeleteResult:
        with translated_errors():
            plan = self._plan(inp.sync_id)
            result = DeleteResult()
            if plan["deleted_ids"]:
                result.files_deleted = self.store.delete_documents_bulk(plan["deleted_ids"], self.repo_id)["total_deleted"]
            if plan["orphaned_folder_ids"]:
                result.folders_deleted = self.store.delete_documents_bulk(
                    plan["orphaned_folder_ids"], self.repo_id
                )["total_deleted"]
        return result

    @activity.defn(name=C.RESET_INDEX)
    def reset_index(self, inp: ResetInput) -> None:
        if inp.repo_id != self.repo_id:
            raise ApplicationError(f"This worker indexes {self.repo_id}, not {inp.repo_id}",
                                   type=C.INVALID_OPTIONS, non_retryable=True)
        with translated_errors():
            self.store.reset_repository(inp.repo_id)

    @activity.defn(name=C.PERSIST_BATCH)
    def persist_batch(self, inp: PersistBatchInput) -> PersistBatchResult:
        with translated_errors():
            docs = self.staging.staged_page(inp.sync_id, inp.offset, inp.limit)
            if not docs:
                return PersistBatchResult(n=0)
            result = self.store.upsert_documents_bulk(docs)
        summary = result["summary"]
        if summary["errors"]:
            # Upserts are idempotent by _id, so the whole batch is simply retried.
            raise ApplicationError(
                f"{summary['errors']} of {len(docs)} documents failed to persist", type=C.PERSIST_ERRORS
            )
        return PersistBatchResult(
            n=len(docs), processed=result["total_processed"], inserted=summary["inserted"],
            updated=summary["updated"], no_change=summary["no_change"], errors=summary["errors"],
        )

    @activity.defn(name=C.FINALIZE_INDEX)
    def finalize_index(self, inp: FinalizeInput) -> FinalizeResult:
        with translated_errors():
            plan = self._plan(inp.sync_id)
            lens_refreshed, lines = sync_steps.finalize_index(
                self.config, self.pack, self.store, self.staging, inp.sync_id, plan["commit_sha"], inp.incremental
            )
        return FinalizeResult(lens_refreshed=lens_refreshed, index_messages=lines)

    # ------------------------------------------------------------------
    # After the index
    # ------------------------------------------------------------------

    @activity.defn(name=C.MIRROR_SOURCE)
    def mirror_source(self, inp: SyncIdInput) -> MirrorOutcome:
        with heartbeating(), translated_errors():
            plan = self._plan(inp.sync_id)
            snapshot = self._snapshot(plan["commit_sha"])
            delta = sync_steps.delta_from_plan(plan)
            mirror = SourceMirrorOrchestrator(self.config)
            if delta is None:
                result = mirror.mirror_full(snapshot)
            else:
                result = mirror.mirror_incremental(delta, snapshot)
        return MirrorOutcome(
            uploaded=result.uploaded,
            deleted=result.deleted,
            failed_uploads=[[p, e] for p, e in result.failed_uploads],
            failed_deletes=[[p, e] for p, e in result.failed_deletes],
        )

    @activity.defn(name=C.BUILD_TEMPORAL_REPORT)
    def build_temporal_report(self, inp: ReportInput) -> ReportOutcome:
        from semantic_ingress.report.service import build_and_store_report

        with heartbeating(), translated_errors():
            stored = self.store.get_report(self.repo_id)
            if not (inp.index_changed or not stored or stored.get("status") != "ok"):
                return ReportOutcome(status="skipped")
            print("=" * 80)
            print("TEMPORAL REPORT")
            print("=" * 80)
            result = build_and_store_report(self.config, self.preflight, self.store, self.repo_id)
        return ReportOutcome(status="ok", cost_usd=result.usage.total_cost_usd)

    @activity.defn(name=C.WRITE_DISK_ARTIFACTS)
    def write_disk_artifacts(self, inp: DiskInput) -> DiskResult:
        with translated_errors():
            documents = sync_steps.stored_documents(self.staging, inp.sync_id)
            DiskWriter(Path(inp.output_dir)).write_documents(documents)
        return DiskResult(written=len(documents))

    @activity.defn(name=C.CLEANUP_STAGING)
    def cleanup_staging(self, inp: SyncIdInput) -> CleanupResult:
        with translated_errors():
            counts = self.staging.cleanup(inp.sync_id)
        with self._lock:
            self._plans.pop(inp.sync_id, None)
        return CleanupResult(documents=counts["documents"], lens_updates=counts["lens_updates"])
