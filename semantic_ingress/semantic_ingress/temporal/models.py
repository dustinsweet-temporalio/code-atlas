"""
Payloads of the sync workflow and its activities.

Dataclasses of primitives only (no pydantic, no heavy imports), so the workflow
sandbox can import this module and payloads can gain fields without breaking
history. Every workflow and activity takes one dataclass and returns one.
Summaries never travel through here: activities stage them in MongoDB.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FAILED_ITEMS_CAP = 1000
ERROR_CHARS = 300


# ----------------------------------------------------------------------
# Workflow input, state and result
# ----------------------------------------------------------------------

@dataclass
class SyncParams:
    """Options from the CLI; fixed for the whole run (an attached re-run keeps the original's)."""

    repo_id: str
    prefix: str
    repo_path: str
    dry_run: bool = False
    force: bool = False
    refresh_lens: bool = False
    no_report: bool = False
    output_dir: str | None = None
    window_size: int = 5
    page_size: int = 100                      # work items loaded from staging at a time
    lens_enabled: bool = True
    test_can_after_items: int | None = None   # test hook: Continue-As-New after N completed items


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: UsageTotals | None) -> None:
        if other is None:
            return
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class FailedItem:
    path: str
    error: str


@dataclass
class LevelInfo:
    """A run of folder work items at one depth (deepest level first; the root is last)."""

    depth: int
    offset: int
    count: int


@dataclass
class SyncPlan:
    commit_sha: str
    mode: str                  # "full" | "incremental"
    reset_index: bool          # the existing index is replaced after generation
    reason: str
    n_files: int               # file summaries to generate
    n_lens_only: int           # unchanged files whose lens is refreshed (--refresh-lens)
    n_deleted: int
    warnings: list[str] = field(default_factory=list)
    levels: list[LevelInfo] = field(default_factory=list)   # filled when the folder phase starts
    n_added: int = 0
    n_modified: int = 0
    n_unchanged: int = 0
    n_snapshot_files: int = 0


@dataclass
class PersistTotals:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    no_change: int = 0
    errors: int = 0


@dataclass
class MirrorOutcome:
    uploaded: int = 0
    deleted: int = 0
    failed_uploads: list[list[str]] = field(default_factory=list)   # [path, error]
    failed_deletes: list[list[str]] = field(default_factory=list)
    error: str | None = None                                         # the mirror activity itself failed

    @property
    def total_failures(self) -> int:
        return len(self.failed_uploads) + len(self.failed_deletes) + (1 if self.error else 0)


@dataclass
class ReportOutcome:
    status: str                 # "ok" | "skipped" | "failed"
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class SyncState:
    """Everything that must survive Continue-As-New."""

    sync_id: str
    phase: str = "PLAN"
    plan: SyncPlan | None = None
    file_cursor: int = 0
    lens_cursor: int = 0
    folders_planned: bool = False
    folder_level: int = 0
    folder_cursor: int = 0          # absolute position in the folder work items
    deleted: DeleteResult | None = None
    delete_error: str | None = None
    reset_done: bool = False
    persist_offset: int = 0
    persist_done: bool = False
    usage: UsageTotals = field(default_factory=UsageTotals)
    failed: list[FailedItem] = field(default_factory=list)
    failed_overflow: int = 0
    files_done: int = 0
    lens_done: int = 0
    lens_refreshed_any: bool = False
    folders_done: int = 0
    folders_generated: int = 0
    repo_summary_generated: bool = False
    repo_lens: RepoLensResult | None = None
    persist: PersistTotals | None = None
    persist_error: str | None = None
    finalize: FinalizeResult | None = None
    finalize_error: str | None = None
    mirror: MirrorOutcome | None = None
    report: ReportOutcome | None = None
    disk_written: int | None = None
    disk_error: str | None = None
    would_upsert: int | None = None
    would_update_lens: int | None = None
    items_since_can: int = 0
    continued_as_new_count: int = 0


@dataclass
class SyncWorkflowInput:
    params: SyncParams
    state: SyncState | None = None   # None on the first run


@dataclass
class SyncOutcome:
    """The workflow result; the CLI renders it like the end of the old in-process sync."""

    sync_id: str
    dry_run: bool
    plan: SyncPlan | None
    usage: UsageTotals
    failed: list[FailedItem]
    failed_overflow: int
    deleted: DeleteResult | None
    delete_error: str | None
    persist: PersistTotals | None
    persist_error: str | None
    finalize: FinalizeResult | None
    finalize_error: str | None
    mirror: MirrorOutcome | None
    report: ReportOutcome | None
    disk_written: int | None
    disk_error: str | None
    would_upsert: int | None
    would_update_lens: int | None
    files_done: int
    folders_generated: int
    repo_summary_generated: bool
    repo_lens: RepoLensResult | None
    continued_as_new_count: int


@dataclass
class Progress:
    phase: str
    done: int
    total: int
    failed: int
    usage: UsageTotals = field(default_factory=UsageTotals)
    continued_as_new_count: int = 0
    plan: SyncPlan | None = None


# ----------------------------------------------------------------------
# Activity payloads
# ----------------------------------------------------------------------

@dataclass
class PlanInput:
    sync_id: str
    params: SyncParams


@dataclass
class WorkPageInput:
    sync_id: str
    kind: str          # "file" | "lens" | "folder"
    offset: int
    limit: int


@dataclass
class WorkItem:
    seq: int
    path: str
    blob_oid: str | None = None
    depth: int | None = None


@dataclass
class WorkPage:
    items: list[WorkItem] = field(default_factory=list)


@dataclass
class FileInput:
    sync_id: str
    commit_sha: str
    path: str
    blob_oid: str


@dataclass
class FileResult:
    path: str
    usage: UsageTotals = field(default_factory=UsageTotals)
    rel: int | None = None
    failed: bool = False
    error: str | None = None


@dataclass
class PlanFoldersInput:
    sync_id: str
    lens_refreshed: bool


@dataclass
class PlanFoldersResult:
    levels: list[LevelInfo] = field(default_factory=list)


@dataclass
class FolderInput:
    sync_id: str
    commit_sha: str
    folder_path: str


@dataclass
class FolderResult:
    path: str
    usage: UsageTotals = field(default_factory=UsageTotals)
    skipped: bool = False
    generated: bool = False
    failed: bool = False
    error: str | None = None


@dataclass
class RepoInput:
    sync_id: str
    commit_sha: str
    incremental: bool
    folders_generated: int


@dataclass
class RepoResult:
    usage: UsageTotals = field(default_factory=UsageTotals)
    skipped: bool = False
    generated: bool = False


@dataclass
class RepoLensResult:
    usage: UsageTotals = field(default_factory=UsageTotals)
    status: str = ""
    archetype: str = ""
    n_processes: int = 0
    searches: int = 0


@dataclass
class SyncIdInput:
    sync_id: str


@dataclass
class DeleteResult:
    files_deleted: int = 0
    folders_deleted: int = 0


@dataclass
class ResetInput:
    repo_id: str
    prefix: str


@dataclass
class PersistBatchInput:
    sync_id: str
    offset: int
    limit: int


@dataclass
class PersistBatchResult:
    n: int = 0
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    no_change: int = 0
    errors: int = 0


@dataclass
class FinalizeInput:
    sync_id: str
    incremental: bool


@dataclass
class FinalizeResult:
    lens_refreshed: int | None = None
    index_messages: list[str] = field(default_factory=list)


@dataclass
class ReportInput:
    sync_id: str
    index_changed: bool


@dataclass
class DiskInput:
    sync_id: str
    output_dir: str


@dataclass
class DiskResult:
    written: int = 0


@dataclass
class CleanupResult:
    documents: int = 0
    lens_updates: int = 0
