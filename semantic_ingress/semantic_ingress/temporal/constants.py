"""
Names, retry policies and timeouts shared by the sync workflow, its activities and the CLI.

Safe to import in the workflow sandbox (standard library and temporalio only).
The timeout and attempt values are starting points; tune them against real runs.
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

WORKFLOW_NAME = "SyncWorkflow"
PROGRESS_QUERY = "progress"


def workflow_id(repo_id: str) -> str:
    """One sync per repository: a re-run attaches to the running one."""
    return f"sync-{repo_id}"


def task_queue(repo_id: str) -> str:
    """Per-repository queue, so only the worker with this repository mounted picks up its tasks."""
    return f"code-atlas-sync-{repo_id}"


# Memo `origin` of a run: started by `sync` (index.sh, which runs its own worker) or by
# the web UI (run by the `index-worker` service, which looks for running "ui" syncs).
ORIGIN_CLI = "cli"
ORIGIN_UI = "ui"


def sync_memo(repo_id: str, repo_path: str, origin: str, report: bool) -> dict:
    """What a run records about itself for the UI and the worker service (`report`: it builds the report)."""
    return {"repo_id": repo_id, "mode": "sync", "origin": origin, "repo_path": repo_path, "report": report}


# Activity names (explicit, so renaming a method never breaks a running workflow).
PLAN_SYNC = "plan_sync"
LOAD_WORK_PAGE = "load_work_page"
GENERATE_FILE_SUMMARY = "generate_file_summary"
REFRESH_FILE_LENS = "refresh_file_lens"
PLAN_FOLDERS = "plan_folders"
GENERATE_FOLDER_SUMMARY = "generate_folder_summary"
GENERATE_REPO_SUMMARY = "generate_repo_summary"
GENERATE_REPO_LENS = "generate_repo_lens"
DELETE_REMOVED_DOCS = "delete_removed_docs"
RESET_INDEX = "reset_index"
PERSIST_BATCH = "persist_batch"
FINALIZE_INDEX = "finalize_index"
MIRROR_SOURCE = "mirror_source"
BUILD_TEMPORAL_REPORT = "build_temporal_report"
WRITE_DISK_ARTIFACTS = "write_disk_artifacts"
CLEANUP_STAGING = "cleanup_staging"

# ApplicationError types raised by activities.
LLM_PERMANENT = "LLM_PERMANENT"
LLM_TRANSIENT = "LLM_TRANSIENT"
FILE_UNREADABLE = "FILE_UNREADABLE"
INVALID_OPTIONS = "INVALID_OPTIONS"
PERSIST_ERRORS = "PERSIST_ERRORS"

LOCAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[INVALID_OPTIONS],
)

LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
    non_retryable_error_types=[LLM_PERMANENT, FILE_UNREADABLE],
)

REPO_LENS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
    non_retryable_error_types=[LLM_PERMANENT, FILE_UNREADABLE],
)

MIRROR_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

PERSIST_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

REPORT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_attempts=2,
    non_retryable_error_types=[LLM_PERMANENT],
)

# start_to_close timeouts
PLAN_TIMEOUT = timedelta(minutes=5)
WORK_PAGE_TIMEOUT = timedelta(seconds=30)
FILE_TIMEOUT = timedelta(minutes=10)
FOLDER_TIMEOUT = timedelta(minutes=20)       # very large folders are summarised in chunks
REPO_SUMMARY_TIMEOUT = timedelta(minutes=10)
REPO_LENS_TIMEOUT = timedelta(minutes=30)    # tool loop with docs searches
DELETE_TIMEOUT = timedelta(minutes=2)
RESET_TIMEOUT = timedelta(minutes=2)
PERSIST_TIMEOUT = timedelta(minutes=2)
FINALIZE_TIMEOUT = timedelta(minutes=5)
MIRROR_TIMEOUT = timedelta(minutes=30)
REPORT_TIMEOUT = timedelta(minutes=90)
DISK_TIMEOUT = timedelta(minutes=5)
CLEANUP_TIMEOUT = timedelta(minutes=1)

# File, lens and folder activities heartbeat too, so a killed worker's in-flight items
# are retried within ITEM_HEARTBEAT_TIMEOUT when the run resumes.
ITEM_HEARTBEAT_TIMEOUT = timedelta(seconds=30)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)
HEARTBEAT_INTERVAL_SECONDS = 10.0

PERSIST_BATCH_SIZE = 500
