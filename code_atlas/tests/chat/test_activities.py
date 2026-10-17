"""
The real run_agent_turn activity (with a fake stream_answer and fakeredis), its
error mapping, and the Redis Stream publisher.
"""
import asyncio
import uuid

import anthropic
import fakeredis
import pytest
from strands.types.exceptions import (
    ContextWindowOverflowException,
    EventLoopException,
    MaxTokensReachedException,
    ModelThrottledException,
    StructuredOutputException,
)
from temporalio.client import WorkflowUpdateFailedError

import agent
from chat import constants as C
from chat import streaming
from chat.activities import ChatActivities, _to_activity_error
from chat.streaming import TurnPublisher, turn_key
from tests.chat.conftest import Session, chat_worker

try:  # the HTTP library the installed anthropic SDK builds its errors from
    import httpx2 as httpx
except ImportError:
    import httpx

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(cls, code):
    return cls("error", response=httpx.Response(code, request=REQUEST), body=None)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def _events(redis, turn_id):
    return [(f["t"], f.get("d", "")) for _, f in await redis.xrange(turn_key(turn_id))]


# ---------------------------------------------------------------------- error mapping

@pytest.mark.parametrize("error, kind, retryable", [
    (ContextWindowOverflowException("prompt is too long"), C.CONTEXT_OVERFLOW, False),
    (MaxTokensReachedException("max tokens"), C.LLM_PERMANENT, False),
    (ModelThrottledException("slow down"), "ModelThrottledException", True),
    (StructuredOutputException("bad output"), "StructuredOutputException", True),
    (_status(anthropic.AuthenticationError, 401), C.LLM_PERMANENT, False),
    (_status(anthropic.BadRequestError, 400), C.LLM_PERMANENT, False),
    (_status(anthropic.PermissionDeniedError, 403), C.LLM_PERMANENT, False),
    (_status(anthropic.InternalServerError, 500), "InternalServerError", True),
    (_status(anthropic.APIStatusError, 529), "APIStatusError", True),
    (anthropic.APIConnectionError(request=REQUEST), "APIConnectionError", True),
    (anthropic.APITimeoutError(request=REQUEST), "APITimeoutError", True),
    (EventLoopException(_status(anthropic.BadRequestError, 400)), C.LLM_PERMANENT, False),
    (EventLoopException(_status(anthropic.InternalServerError, 500)), "InternalServerError", True),
    (RuntimeError("something else"), "RuntimeError", True),
])
def test_error_mapping(error, kind, retryable):
    mapped = _to_activity_error(error)
    assert mapped.type == kind and mapped.non_retryable is (not retryable)
    assert (kind in C.TURN_RETRY.non_retryable_error_types) is (not retryable)


# ---------------------------------------------------------------------- publisher

async def test_publisher_adds_each_event_with_a_ttl(redis):
    pub = TurnPublisher(redis, "t1", ttl=600)
    await pub.chunk("hello ")
    await pub.done()
    assert await _events(redis, "t1") == [("chunk", "hello "), ("done", "")]
    assert 0 < await redis.ttl(turn_key("t1")) <= 600


async def test_publisher_caps_the_stream_and_refreshes_the_ttl_in_one_round_trip():
    calls = []

    class Pipeline:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def xadd(self, key, fields, **kw):
            calls.append(("xadd", key, fields, kw))

        def expire(self, key, ttl):
            calls.append(("expire", key, ttl))

        async def execute(self):
            calls.append(("execute",))

    class Redis:
        def pipeline(self, transaction=True):
            calls.append(("pipeline", transaction))
            return Pipeline()

    await TurnPublisher(Redis(), "t1", ttl=600).chunk("x")
    assert calls == [
        ("pipeline", False),
        ("xadd", turn_key("t1"), {"t": "chunk", "d": "x"}, {"maxlen": streaming.MAX_EVENTS, "approximate": True}),
        ("expire", turn_key("t1"), 600),
        ("execute",),
    ]


async def test_a_redis_failure_never_propagates(caplog):
    class Broken:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis is down")

    pub = TurnPublisher(Broken(), "t2", ttl=60)
    await pub.chunk("a")
    await pub.chunk("b")
    await pub.done(stopped=True)
    assert pub._broken
    assert sum("streaming degraded" in r.message for r in caplog.records) == 1  # logged once


# ---------------------------------------------------------------------- the real activity

def _fake_stream(monkeypatch, gen):
    calls = []

    async def stream_answer(payload, raise_errors=False):
        calls.append(payload)
        async for chunk in gen(len(calls)):
            yield chunk

    monkeypatch.setattr(agent, "stream_answer", stream_answer)
    return calls


async def test_retries_restart_the_streamed_answer(env, queue, redis, monkeypatch):
    async def gen(attempt):
        yield f"try {attempt} "
        if attempt < 3:
            raise RuntimeError("connection reset")
        yield "done"

    calls = _fake_stream(monkeypatch, gen)
    async with chat_worker(env, queue, ChatActivities(redis, 600).all()):
        s = Session(env, queue)
        turn = str(uuid.uuid4())
        result = await s.send("q", client_count=0, turn_id=turn)
    assert result.answer == "try 3 done" and len(calls) == 3
    assert calls[0]["messages"] == [{"role": "user", "content": "q"}] and calls[0]["repo_id"] == "demo"
    assert await _events(redis, turn) == [
        ("chunk", "try 1 "), ("reset", ""), ("chunk", "try 2 "), ("reset", ""),
        ("chunk", "try 3 "), ("chunk", "done"), ("done", "")]


async def test_stop_reaches_an_activity_whose_heartbeat_is_a_child_task(env, queue, redis, monkeypatch):
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def gen(attempt):
        yield "thinking… "
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield "never"

    _fake_stream(monkeypatch, gen)
    async with chat_worker(env, queue, ChatActivities(redis, 600).all()):
        s = Session(env, queue)
        turn = str(uuid.uuid4())
        pending = asyncio.create_task(s.send("q", client_count=0, turn_id=turn))
        await asyncio.wait_for(started.wait(), 10)
        await s.handle.signal(C.CANCEL_TURN_SIGNAL, turn)
        result = await asyncio.wait_for(pending, 15)
    assert result.stopped and cancelled.is_set()
    assert await _events(redis, turn) == [("chunk", "thinking… "), ("done", "stopped")]


async def test_an_unavailable_repository_fails_the_turn_without_retries(env, queue, redis, monkeypatch):
    async def gen(attempt):
        raise agent.AgentInputError("repository 'demo' is not available (not found)")
        yield  # pragma: no cover

    calls = _fake_stream(monkeypatch, gen)
    async with chat_worker(env, queue, ChatActivities(redis, 600).all()):
        s = Session(env, queue)
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await s.send("q", client_count=0)
    assert e.value.cause.type == C.TURN_FAILED and "not available" in e.value.cause.message
    assert len(calls) == 1
