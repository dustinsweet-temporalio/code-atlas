import asyncio
import uuid

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from chat import constants as C
from chat.models import ChatMessage, ChatSessionInput, SendMessageInput, TurnInput, TurnResult
from chat.workflow import ChatSessionWorkflow


@pytest_asyncio.fixture(scope="session")
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class MockTurns:
    """
    A stand-in for run_agent_turn. Each call is recorded; `behave(inp, n)` (n = call
    number) decides what happens. By default it answers "answer to <text>".
    """

    def __init__(self, behave=None):
        self.inputs: list[TurnInput] = []
        self.started = asyncio.Event()
        self.behave = behave

    def activity(self):
        mock = self

        @activity.defn(name=C.ACTIVITY_RUN_TURN)
        async def run_agent_turn(inp: TurnInput) -> TurnResult:
            mock.inputs.append(inp)
            mock.started.set()
            if mock.behave:
                return await mock.behave(inp, len(mock.inputs))
            return TurnResult(answer=f"answer to {inp.messages[-1].content}")

        return run_agent_turn


class Session:
    """One conversation: a workflow id, its seed, and helpers to send messages."""

    def __init__(self, env, queue: str, **inp):
        self.env, self.queue = env, queue
        self.session_id = str(uuid.uuid4())
        self.id = C.workflow_id("demo", self.session_id)
        self.inp = dict(session_id=self.session_id, repo_id="demo", idle_timeout_seconds=3600, **inp)

    def op(self, messages=(), dropped=0):
        return WithStartWorkflowOperation(
            ChatSessionWorkflow.run,
            ChatSessionInput(messages=list(messages), dropped=dropped, **self.inp),
            id=self.id, task_queue=self.queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )

    async def send(self, text, client_count, *, turn_id=None, update_id=None, history=None, seed=(), dropped=0):
        turn_id = turn_id or str(uuid.uuid4())
        return await self.env.client.execute_update_with_start_workflow(
            ChatSessionWorkflow.send_message,
            SendMessageInput(turn_id=turn_id, text=text, client_count=client_count, history=history),
            start_workflow_operation=self.op(seed, dropped),
            id=update_id or turn_id,
        )

    @property
    def handle(self):
        return self.env.client.get_workflow_handle(self.id)


@pytest.fixture
def queue():
    return f"chat-{uuid.uuid4().hex[:8]}"


def chat_worker(env, queue, activities):
    return Worker(env.client, task_queue=queue, workflows=[ChatSessionWorkflow], activities=activities,
                  max_heartbeat_throttle_interval=C.HEARTBEAT_THROTTLE)


def msgs(*pairs):
    return [ChatMessage(role, text) for role, text in pairs]


async def heartbeat_until_cancelled(seconds: float = 60, after_cancel: float = 0.0):
    """Heartbeat as the real activity does, until cancelled (then optionally linger)."""
    try:
        for _ in range(int(seconds * 10)):
            activity.heartbeat()
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        if after_cancel:
            await asyncio.sleep(after_cancel)
        raise
    return TurnResult(answer="finished")


