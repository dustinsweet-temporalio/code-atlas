"""
ChatSessionWorkflow on the time-skipping test server, with a mock run_agent_turn
(tests 1-16 of the implementation guide).
"""
import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chat import constants as C
from chat.models import TurnResult
from tests.chat.conftest import MockTurns, Session, chat_worker, heartbeat_until_cancelled, msgs


def failure_type(e: WorkflowUpdateFailedError) -> str | None:
    return getattr(e.cause, "type", None)


async def status(session: Session) -> WorkflowExecutionStatus:
    return (await session.handle.describe()).status


# 1 ---------------------------------------------------------------------

async def test_first_message_is_seeded_from_the_browser(env, queue):
    mock = MockTurns()
    seed = msgs(("user", "earlier question"), ("assistant", "earlier answer"))
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        result = await s.send("new question", client_count=2, seed=seed)
    assert [(m.role, m.content) for m in mock.inputs[0].messages] == [
        ("user", "earlier question"), ("assistant", "earlier answer"), ("user", "new question")]
    assert result.answer == "answer to new question" and not result.stopped
    assert result.total_count == len(seed) + 2


# 2, 3 ------------------------------------------------------------------

async def test_count_mismatch_then_resync(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        await s.send("q1", client_count=0)
        turn = str(uuid.uuid4())
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await s.send("q2", client_count=5, turn_id=turn)  # the session holds 2
        assert failure_type(e.value) == C.RESYNC_REQUIRED
        assert await status(s) == WorkflowExecutionStatus.RUNNING  # the Update failed, not the workflow

        history = msgs(("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d"))
        result = await s.send("q2", client_count=5, turn_id=turn, update_id=f"{turn}:resync", history=history)
    assert [m.content for m in mock.inputs[-1].messages] == ["a", "b", "c", "d", "q2"]  # context replaced
    assert result.total_count == 7 and len(mock.inputs) == 2


# 4 ---------------------------------------------------------------------

async def test_a_resent_turn_returns_the_recorded_answer(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        turn = str(uuid.uuid4())
        first = await s.send("q", client_count=0, turn_id=turn, update_id="u1")
        second = await s.send("q", client_count=0, turn_id=turn, update_id="u2")
    assert len(mock.inputs) == 1
    assert first.answer == second.answer == "answer to q" and second.total_count == 2


# 5 ---------------------------------------------------------------------

async def test_turns_run_one_at_a_time(env, queue):
    async def slow(inp, n):
        await asyncio.sleep(0.5)
        return TurnResult(answer=f"answer {n}")

    mock = MockTurns(slow)
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        first = asyncio.create_task(s.send("q1", client_count=0))
        await mock.started.wait()
        second = asyncio.create_task(s.send("q2", client_count=2))
        r1, r2 = await asyncio.gather(first, second)
    assert [m.content for m in mock.inputs[1].messages] == ["q1", "answer 1", "q2"]
    assert (r1.total_count, r2.total_count) == (2, 4)


# 6 ---------------------------------------------------------------------

async def test_the_validator_rejects_bad_messages_without_history_events(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        await s.send("q", client_count=0)
        for text in ("   ", "x" * (C.MAX_TEXT_CHARS + 1)):
            with pytest.raises(WorkflowUpdateFailedError) as e:
                await s.send(text, client_count=2, update_id=f"bad-{len(text)}")
            assert failure_type(e.value) == C.INVALID_INPUT
        history = await s.handle.fetch_history()
    accepted = [ev.workflow_execution_update_accepted_event_attributes.accepted_request.meta.update_id
                for ev in history.events if ev.HasField("workflow_execution_update_accepted_event_attributes")]
    assert len(accepted) == 1 and not any(u.startswith("bad-") for u in accepted)
    assert len(mock.inputs) == 1


# 7, 8 ------------------------------------------------------------------

async def test_stop_during_a_turn(env, queue):
    mock = MockTurns(lambda inp, n: heartbeat_until_cancelled())
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        turn = str(uuid.uuid4())
        pending = asyncio.create_task(s.send("long question", client_count=0, turn_id=turn))
        await mock.started.wait()
        await s.handle.signal(C.CANCEL_TURN_SIGNAL, turn)
        result = await asyncio.wait_for(pending, 15)
        recorded = await s.handle.query(C.GET_TURN_QUERY, turn, result_type=None)
    assert result.stopped and result.answer == "" and result.total_count == 2
    assert recorded["found"] and recorded["stopped"] and recorded["answer"] == C.STOPPED_TEXT


async def test_stop_before_the_turn_starts(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        await s.send("q1", client_count=0)
        turn = str(uuid.uuid4())
        await s.handle.signal(C.CANCEL_TURN_SIGNAL, turn)
        result = await s.send("q2", client_count=2, turn_id=turn)
    assert result.stopped and result.total_count == 4
    assert len(mock.inputs) == 1  # the stopped turn never ran


# 9 ---------------------------------------------------------------------

@workflow.defn(name="CancelProbe", sandboxed=False)
class CancelProbe:
    @workflow.run
    async def run(self) -> list[str]:
        handle = workflow.start_activity(
            C.ACTIVITY_RUN_TURN, args=[], start_to_close_timeout=timedelta(minutes=1),
            heartbeat_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=1),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        await workflow.sleep(1)
        handle.cancel()
        try:
            await handle
            return ["completed"]
        except ActivityError as e:
            return [type(e).__name__, type(e.cause).__name__, str(isinstance(e.cause, CancelledError))]


async def test_a_cancelled_activity_handle_raises_activity_error_caused_by_cancelled_error(env, queue):
    stopped = asyncio.Event()

    @activity.defn(name=C.ACTIVITY_RUN_TURN)
    async def run_agent_turn() -> None:
        try:
            await heartbeat_until_cancelled()
        finally:
            stopped.set()

    async with Worker(env.client, task_queue=queue, workflows=[CancelProbe], activities=[run_agent_turn],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      max_heartbeat_throttle_interval=C.HEARTBEAT_THROTTLE):
        outcome = await env.client.execute_workflow(CancelProbe.run, id=f"probe-{queue}", task_queue=queue)
    assert outcome == ["ActivityError", "CancelledError", "True"]
    assert stopped.is_set()


# 10 --------------------------------------------------------------------

async def test_a_failed_turn_fails_only_its_update(env, queue):
    async def fail_once(inp, n):
        if n == 1:
            raise ApplicationError("bad request", type=C.LLM_PERMANENT, non_retryable=True)
        return TurnResult(answer="ok")

    mock = MockTurns(fail_once)
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await s.send("q1", client_count=0)
        assert failure_type(e.value) == C.TURN_FAILED and "bad request" in e.value.cause.message
        assert await status(s) == WorkflowExecutionStatus.RUNNING
        result = await s.send("q2", client_count=0)  # the failed turn was not recorded
    assert result.answer == "ok" and result.total_count == 2


# 12 --------------------------------------------------------------------

async def test_idle_timeout_ends_the_session_and_a_new_message_rehydrates(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        s.inp["idle_timeout_seconds"] = 60
        await s.send("q1", client_count=0)
        first_run = (await s.handle.describe()).run_id
        await env.sleep(50)
        await s.send("q2", client_count=2)          # t=50 restarts the 60s timer
        await env.sleep(30)                          # t=80: past the first deadline
        assert await status(s) == WorkflowExecutionStatus.RUNNING
        await env.sleep(40)                          # t=120: past 50 + 60
        assert await status(s) == WorkflowExecutionStatus.COMPLETED

        seed = msgs(("user", "q1"), ("assistant", "answer to q1"), ("user", "q2"), ("assistant", "answer to q2"))
        result = await s.send("q3", client_count=4, seed=seed)
        second_run = (await s.handle.describe()).run_id
    assert second_run != first_run
    assert [m.content for m in mock.inputs[-1].messages] == [m.content for m in seed] + ["q3"]
    assert result.total_count == 6


# 13 --------------------------------------------------------------------

async def test_continue_as_new_carries_the_context(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue, continue_as_new_after_turns=2)
        await s.send("q1", client_count=0)
        first_run = (await s.handle.describe()).run_id
        await s.send("q2", client_count=2)
        for _ in range(50):  # the run continues as new once turn 2 is recorded
            if (await s.handle.describe()).run_id != first_run:
                break
            await asyncio.sleep(0.1)
        assert (await s.handle.describe()).run_id != first_run
        result = await s.send("q3", client_count=4)
    assert [m.content for m in mock.inputs[-1].messages] == ["q1", "answer to q1", "q2", "answer to q2", "q3"]
    assert result.total_count == 6


async def test_no_continue_as_new_while_a_turn_waits(env, queue):
    async def slow(inp, n):
        await asyncio.sleep(0.5)
        return TurnResult(answer=f"answer {n}")

    mock = MockTurns(slow)
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue, continue_as_new_after_turns=1)
        first = asyncio.create_task(s.send("q1", client_count=0))
        await mock.started.wait()
        second = asyncio.create_task(s.send("q2", client_count=2))
        r1, r2 = await asyncio.gather(first, second)  # q2 would fail if the run ended under it
    assert (r1.total_count, r2.total_count) == (2, 4)
    assert [m.content for m in mock.inputs[1].messages] == ["q1", "answer 1", "q2"]


# 14 --------------------------------------------------------------------

async def test_the_context_is_trimmed_to_the_budget(env, queue):
    async def long_answer(inp, n):
        return TurnResult(answer="a" * 40)

    mock = MockTurns(long_answer)
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue, context_char_budget=100)
        count = 0
        for i in range(5):
            result = await s.send("q" * 40, client_count=count)
            count = result.total_count
    assert count == 10  # every message counts, kept or dropped
    assert [len(t.messages) for t in mock.inputs] == [1, 3, 3, 3, 3]  # older messages dropped
    assert all(t.messages[0].role == "user" for t in mock.inputs)


# 15, 16 ----------------------------------------------------------------

async def test_close_while_idle_completes_the_workflow(env, queue):
    mock = MockTurns()
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        await s.send("q", client_count=0)
        await s.handle.signal(C.CLOSE_SIGNAL)
        await asyncio.wait_for(s.handle.result(), 10)

        # Exact failures after the workflow closed (what the API's retry relies on):
        with pytest.raises(RPCError) as e:  # a plain Update: NOT_FOUND
            await s.handle.execute_update(C.SEND_MESSAGE_UPDATE, None, id="after-close")
        assert e.value.status == RPCStatusCode.NOT_FOUND
        # Update-with-Start starts a new run instead (the rehydration path).
        result = await s.send("q2", client_count=2, seed=msgs(("user", "q"), ("assistant", "answer to q")))
    assert result.total_count == 4


async def test_close_during_a_turn_stops_it_and_rejects_new_messages(env, queue):
    mock = MockTurns(lambda inp, n: heartbeat_until_cancelled(after_cancel=1.0))
    async with chat_worker(env, queue, [mock.activity()]):
        s = Session(env, queue)
        pending = asyncio.create_task(s.send("q", client_count=0))
        await mock.started.wait()
        await s.handle.signal(C.CLOSE_SIGNAL)
        await asyncio.sleep(0.3)  # the activity is still winding down
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await s.handle.execute_update(C.SEND_MESSAGE_UPDATE, {"turn_id": "t2", "text": "q2", "client_count": 2},
                                          id="t2")
        assert failure_type(e.value) == C.SESSION_CLOSING
        result = await asyncio.wait_for(pending, 15)
        await asyncio.wait_for(s.handle.result(), 10)
    assert result.stopped
