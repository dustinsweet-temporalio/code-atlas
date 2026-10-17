"""
The worker `sync` embeds for the duration of a run.

It runs where the repository is on disk (bind-mounted into the indexer container by
index.sh, or under REPOS_ROOT in the `index-worker` service, which builds one per sync
started from the web UI; see service.py) and needs the source-data volume. It polls a
per-repository task queue, so a worker started for another repository never picks up
this one's activities.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable, Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from semantic_ingress.temporal.workflow import SyncWorkflow


def worker_slots(window_size: int) -> int:
    """Activity slots: the window, plus room for page loads and the odd sequential activity."""
    return max(window_size, 4)


def build_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable],
    window_size: int,
) -> tuple[Worker, ThreadPoolExecutor]:
    """
    A worker for SyncWorkflow and the given (synchronous) activities. The caller owns
    the returned executor and shuts it down after the worker stops.
    """
    slots = worker_slots(window_size)
    executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="sync-activity")
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SyncWorkflow],
        activities=list(activities),
        activity_executor=executor,
        max_concurrent_activities=slots,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    return worker, executor
