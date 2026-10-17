"""
Activities: error classification for the retry policy, idempotent staging, strict
planning, and heartbeating with cancellation of a threaded activity.
"""
import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import anthropic

try:  # the HTTP library the installed anthropic SDK builds its errors from
    import httpx2 as httpx
except ImportError:
    import httpx
import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.workflow import ActivityCancellationType

from semantic_ingress.core.git_adapter import FileUnreadableError
from semantic_ingress.core.incremental_loader import IncrementalLoader
from semantic_ingress.core.llm_client import LLMPermanentError, LLMTransientError
from semantic_ingress.storage.mongo_store import MongoStoreError
from semantic_ingress.temporal import constants as C
from semantic_ingress.temporal.activities import heartbeating, to_activity_error, translated_errors
from semantic_ingress.temporal.models import FileInput, PlanInput
from tests.temporal.conftest import commit_files

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status: int, headers: dict | None = None):
    return cls("error", response=httpx.Response(status, headers=headers or {}, request=REQUEST), body=None)


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

def test_rate_limit_is_retried_after_the_servers_hint():
    error = to_activity_error(_status_error(anthropic.RateLimitError, 429, {"retry-after": "7"}))
    assert isinstance(error, ApplicationError) and not error.non_retryable
    assert error.type == C.LLM_TRANSIENT and error.next_retry_delay == timedelta(seconds=7)


@pytest.mark.parametrize("make", [
    lambda: _status_error(anthropic.InternalServerError, 500),
    lambda: _status_error(anthropic.APIStatusError, 529),
    lambda: anthropic.APIConnectionError(request=REQUEST),
    lambda: anthropic.APITimeoutError(request=REQUEST),
])
def test_transient_model_errors_are_retryable(make):
    error = to_activity_error(make())
    assert isinstance(error, ApplicationError) and not error.non_retryable and error.next_retry_delay is None


@pytest.mark.parametrize("make", [
    lambda: _status_error(anthropic.BadRequestError, 400),
    lambda: _status_error(anthropic.AuthenticationError, 401),
    lambda: LLMPermanentError("Model refused the request: None"),
])
def test_permanent_model_errors_are_not_retried(make):
    error = to_activity_error(make())
    assert error.type == C.LLM_PERMANENT and error.non_retryable


def test_unreadable_files_are_not_retried_and_other_errors_pass_through():
    error = to_activity_error(FileUnreadableError("Cannot decode blob as UTF-8: abc"))
    assert error.type == C.FILE_UNREADABLE and error.non_retryable
    mongo = to_activity_error(MongoStoreError("connection refused"))
    assert mongo.type == "MongoStoreError" and not mongo.non_retryable  # retried by the activity's policy
    assert C.FILE_UNREADABLE in C.LLM_RETRY.non_retryable_error_types
    assert C.LLM_PERMANENT in C.LLM_RETRY.non_retryable_error_types


def _failure(error):
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DataConverter

    failure = Failure()
    DataConverter.default.failure_converter.to_failure(error, DataConverter.default.payload_converter, failure)
    return failure


def test_a_long_error_chain_is_trimmed_to_fit_the_failure_size_limit():
    # A connection error arrives wrapped in the SDK's and the HTTP library's exceptions,
    # each with a long stack trace; serialised whole, it exceeded the server's limit.
    # Distinct frames (as in the HTTP library's stack), so the trace does not collapse.
    namespace: dict = {}
    code = "def frame_0():\n    raise OSError(104, 'Connection reset by peer')\n" + "".join(
        f"def frame_{i}():\n    frame_{i - 1}()\n" for i in range(1, 61))
    exec(compile(code, "/usr/local/lib/python3.12/site-packages/httpcore/_sync/connection_pool.py", "exec"),
         namespace)

    def deep(n):
        namespace[f"frame_{n}"]()

    def generate():
        try:
            try:
                deep(60)
            except OSError as e:
                raise anthropic.APIConnectionError(request=REQUEST) from e
        except anthropic.APIConnectionError as e:
            raise LLMTransientError("Anthropic API not reachable: Connection error.") from e

    try:
        generate()
    except LLMTransientError as e:
        untrimmed = _failure(e)

    with pytest.raises(ApplicationError) as info:
        with translated_errors():
            generate()
    error = info.value
    assert error.type == C.LLM_TRANSIENT and not error.non_retryable
    assert error.message == ("Anthropic API not reachable: Connection error. "
                             "(root cause: OSError: [Errno 104] Connection reset by peer)")
    assert isinstance(error.__cause__, LLMTransientError) and error.__cause__.__cause__ is None

    failure = _failure(error)
    assert failure.cause.message == "Anthropic API not reachable: Connection error."
    assert not failure.cause.HasField("cause")
    assert failure.ByteSize() < 4096 < untrimmed.ByteSize()


def test_a_short_chain_keeps_its_message():
    import json

    def parse():
        try:
            json.loads('{"role": "unterminated')
        except json.JSONDecodeError as e:
            raise LLMTransientError(f"Invalid model response: {e}") from e

    with pytest.raises(ApplicationError) as info:
        with translated_errors():
            parse()
    assert info.value.message.startswith("Invalid model response: Unterminated string")
    assert "root cause" not in info.value.message  # the root says nothing new


# ----------------------------------------------------------------------
# Activities in an ActivityEnvironment
# ----------------------------------------------------------------------

def _plan(harness):
    sync_id = "s1"
    plan = ActivityEnvironment().run(harness.activities().plan_sync, PlanInput(sync_id=sync_id, params=harness.params()))
    return sync_id, plan


def test_generate_file_raises_a_classified_error(harness):
    commit_files(harness.repo_path, {"a.py": "x = 1\n"})
    sync_id, plan = _plan(harness)
    harness.llm.fail_paths = {"a.py"}
    harness.llm.error = _status_error(anthropic.RateLimitError, 429, {"retry-after": "3"})
    acts = harness.activities()
    blob = acts.git.get_snapshot(plan.commit_sha).files["a.py"].blob_oid
    with pytest.raises(ApplicationError) as e:
        ActivityEnvironment().run(acts.generate_file_summary, FileInput(sync_id, plan.commit_sha, "a.py", blob))
    assert e.value.next_retry_delay == timedelta(seconds=3) and not e.value.non_retryable


def test_a_retried_activity_stages_one_record(harness):
    commit_files(harness.repo_path, {"a.py": "x = 1\n"})
    sync_id, plan = _plan(harness)
    blob = harness.activities().git.get_snapshot(plan.commit_sha).files["a.py"].blob_oid
    acts = harness.activities()
    for _ in range(2):
        ActivityEnvironment().run(acts.generate_file_summary, FileInput(sync_id, plan.commit_sha, "a.py", blob))
    file_docs = [k for k in harness.staging.docs if "::file_summary::" in k]
    assert file_docs == [f"s1:{harness.repo_id}::file_summary::a.py"]


def test_plan_sync_retries_a_database_error_instead_of_reindexing(harness):
    commit_files(harness.repo_path, {"a.py": "x = 1\n"})
    harness.store.fail_queries = 1
    with pytest.raises(ApplicationError) as e:
        _plan(harness)
    assert e.value.type == "MongoStoreError" and not e.value.non_retryable  # Temporal retries it
    assert harness.staging.plans == {}

    harness.store.fail_queries = 1  # the old, lenient behaviour is kept for other callers
    assert IncrementalLoader(harness.store).load_previous_snapshot(harness.repo_id) is None


def test_refresh_lens_without_the_lens_is_an_invalid_option(harness):
    commit_files(harness.repo_path, {"a.py": "x = 1\n"})
    from semantic_ingress.models import StoredDocument

    # An existing (current-layout) index makes the run incremental, where the option is checked.
    doc = StoredDocument.for_file_summary(harness.repo_id, "a.py", "c0", "old", "prose", {"role": "r"})
    harness.store.upsert_documents_bulk([doc])
    harness.store.dbs[harness.repo_id]["index_meta"][f"{harness.repo_id}::index_meta"] = {"schema_version": 2}
    acts = harness.activities()
    with pytest.raises(ApplicationError) as e:
        ActivityEnvironment().run(acts.plan_sync, PlanInput("s1", harness.params(refresh_lens=True)))
    assert e.value.type == C.INVALID_OPTIONS and e.value.non_retryable


def test_heartbeating_sends_heartbeats():
    beats: list = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)

    def body() -> None:
        with heartbeating(interval=0.05, details=lambda: ("working",)):
            time.sleep(0.3)

    env.run(body)
    assert len(beats) >= 3 and beats[-1] == ("working",)


# ----------------------------------------------------------------------
# Cancellation of a long threaded activity
# ----------------------------------------------------------------------

started = threading.Event()
stopped = threading.Event()
saw_cancel = threading.Event()


@activity.defn(name="long_running_for_test")
def long_running() -> None:
    started.set()
    try:
        with heartbeating(interval=0.1):
            for _ in range(600):
                time.sleep(0.05)
    except CancelledError:
        saw_cancel.set()
        raise
    finally:
        stopped.set()


@workflow.defn(name="LongRunningForTest", sandboxed=False)
class LongRunningWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            "long_running_for_test", start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=2), retry_policy=RetryPolicy(maximum_attempts=1),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )


async def test_cancelling_the_workflow_stops_the_activity_thread(env):
    queue = f"cancel-{uuid.uuid4().hex[:6]}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with Worker(env.client, task_queue=queue, workflows=[LongRunningWorkflow],
                          activities=[long_running], activity_executor=executor,
                          workflow_runner=UnsandboxedWorkflowRunner()):
            handle = await env.client.start_workflow(LongRunningWorkflow.run, id=queue, task_queue=queue)
            assert await asyncio.to_thread(started.wait, 10)
            await handle.cancel()
            assert await asyncio.to_thread(stopped.wait, 15), "the activity thread kept running"
            assert saw_cancel.is_set()
