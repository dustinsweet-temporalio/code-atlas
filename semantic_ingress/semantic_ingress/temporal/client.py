"""
Start (or attach to) the sync workflow for a repository, run the embedded worker
until it finishes, and print progress while it runs.

One sync per repository: the workflow id is `sync-<repo_id>` with
USE_EXISTING, so re-running `sync` after a crash or Ctrl-C attaches to the
running workflow and the new worker resumes it from its history.
"""
import asyncio
from dataclasses import dataclass
from typing import Callable

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.llm_client import LLMClient
from semantic_ingress.preflight import PreflightResult
from semantic_ingress.storage import MongoStore
from semantic_ingress.storage.staging_store import StagingStore
from semantic_ingress.temporal.activities import IndexActivities
from semantic_ingress.temporal.constants import ORIGIN_CLI, sync_memo, task_queue, workflow_id
from semantic_ingress.temporal.models import (
    Progress,
    SyncOutcome,
    SyncParams,
    SyncPlan,
    SyncWorkflowInput,
)
from semantic_ingress.temporal.progress import usage_cost
from semantic_ingress.temporal.worker import build_worker
from semantic_ingress.temporal.workflow import SyncWorkflow

PROGRESS_POLL_SECONDS = 5.0


def progress_line(p: Progress) -> str:
    counts = f" {p.done:,}/{p.total:,}" if p.total else (f" {p.done:,}" if p.done else "")
    failed = f", {p.failed} failed" if p.failed else ""
    runs = f" (run {p.continued_as_new_count + 1})" if p.continued_as_new_count else ""
    return f"⏱  {p.phase}{counts}{failed} | ${usage_cost(p.usage):.4f}{runs}"


@dataclass
class SyncRun:
    outcome: SyncOutcome
    attached: bool


async def _is_running(client: Client, wid: str) -> bool:
    try:
        description = await client.get_workflow_handle(wid).describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return description.status == WorkflowExecutionStatus.RUNNING


async def start_or_attach(
    config: IngestionConfig,
    preflight: PreflightResult,
    params: SyncParams,
    on_plan: Callable[[SyncPlan], None] = lambda plan: None,
    poll_seconds: float = PROGRESS_POLL_SECONDS,
) -> SyncRun:
    """
    Run the sync workflow to completion with an embedded worker, printing progress.
    `on_plan` is called once with the plan as soon as it is known. Raises
    WorkflowFailureError when the workflow fails.
    """
    repo_id = params.repo_id
    wid, queue = workflow_id(repo_id), task_queue(repo_id)
    client = await Client.connect(config.temporal.address, namespace=config.temporal.namespace)

    store = MongoStore(config.mongo, config.prefix)
    staging = StagingStore(store.client)
    staging.ensure_indexes()
    activities = IndexActivities(config, preflight, store, staging, LLMClient(config.llm, internal_retries=False))
    worker, executor = build_worker(client, queue, activities.all(), params.window_size)

    attached = await _is_running(client, wid)
    try:
        async with worker:
            handle = await client.start_workflow(
                SyncWorkflow.run,
                SyncWorkflowInput(params=params),
                id=wid,
                task_queue=queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                memo=sync_memo(repo_id, params.repo_path, ORIGIN_CLI,
                               report=params.lens_enabled and not params.no_report),
            )
            if attached:
                print(f"Attached to the running sync for {repo_id} (the original run's options apply)")
            print(f"Workflow: {config.temporal.workflow_url(wid)}\n")

            result = asyncio.create_task(handle.result())
            latest = client.get_workflow_handle(wid)  # the current run, across Continue-As-New
            last_line = None
            plan_seen = False
            while True:
                # Poll quickly until the plan is known (to print the mode header early).
                done, _ = await asyncio.wait({result}, timeout=poll_seconds if plan_seen else 1.0)
                if done:
                    break
                try:
                    progress = await latest.query(SyncWorkflow.progress)
                except Exception:  # between runs of a Continue-As-New chain, or the worker is busy
                    continue
                if progress.plan is not None and not plan_seen:
                    on_plan(progress.plan)
                    plan_seen = True
                line = progress_line(progress)
                if line != last_line:
                    print(line)
                    last_line = line
            outcome = await result
            if outcome.plan is not None and not plan_seen:
                on_plan(outcome.plan)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return SyncRun(outcome=outcome, attached=attached)
