"""
The chat API (app.py) with a fake Temporal client and fakeredis: request
validation, the SSE relay and its reconciliation, retries, stop and close.
"""
import asyncio
import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

import app as app_module
from chat import constants as C
from chat.models import SendMessageResult
from chat.streaming import turn_key


class FakeHandle:
    def __init__(self, client, workflow_id):
        self.client, self.workflow_id = client, workflow_id

    async def signal(self, name, *args):
        self.client.signals.append((self.workflow_id, name, args))
        if self.client.signal_error:
            raise self.client.signal_error


class FakeTemporal:
    """Records Update-with-Start calls; `script` decides each call's outcome."""

    def __init__(self, redis):
        self.redis = redis
        self.calls = []
        self.signals = []
        self.signal_error = None
        self.script = []  # per call: (events to stream, result or exception)

    async def execute_update_with_start_workflow(self, update, arg, *, start_workflow_operation, id):
        self.calls.append({"update_id": id, "input": arg, "start": start_workflow_operation})
        events, outcome = self.script[len(self.calls) - 1]
        for fields in events:
            await self.redis.xadd(turn_key(arg.turn_id), fields)
            await asyncio.sleep(0.01)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get_workflow_handle(self, workflow_id):
        return FakeHandle(self, workflow_id)


@pytest.fixture
def api(monkeypatch):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    temporal = FakeTemporal(redis)
    monkeypatch.setattr(app_module, "_redis_factory", lambda: redis)
    monkeypatch.setattr(app_module, "_temporal", temporal)
    monkeypatch.setattr(app_module, "validate_repository", lambda repo_id: (True, f"semv2_{repo_id}", None))
    with TestClient(app_module.app) as client:
        yield client, temporal, redis


def body(**over):
    base = {
        "repo_id": "demo", "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()), "prior_count": 2,
        "messages": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}],
    }
    base.update(over)
    return base


def frames(response):
    out = []
    for frame in response.text.split("\n\n"):
        if frame.startswith("data: "):
            data = frame[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def result(answer, stopped=False, total=4):
    return SendMessageResult(turn_id="t", answer=answer, stopped=stopped, total_count=total)


def chunk(text):
    return {"t": "chunk", "d": text}


# ---------------------------------------------------------------------- validation

def test_request_validation(api):
    client, temporal, _ = api
    assert client.post("/api/chat", json=body(session_id="not-a-uuid")).status_code == 422
    r = client.post("/api/chat", json=body(messages=[{"role": "user", "content": "q"},
                                                     {"role": "assistant", "content": "a"}]))
    assert r.status_code == 400 and "user" in r.json()["error"]
    half = "x" * (app_module.CHAT_CONTEXT_CHARS // 2 + 1)
    big = [{"role": "user", "content": half}, {"role": "assistant", "content": half}, {"role": "user", "content": "q"}]
    assert client.post("/api/chat", json=body(messages=big)).status_code == 400
    too_long = [{"role": "user", "content": "x" * (C.MAX_TEXT_CHARS + 1)}]
    assert client.post("/api/chat", json=body(messages=too_long, prior_count=0)).status_code == 400
    assert temporal.calls == []

    # One older message over the budget is a valid tail (the newest message is always kept).
    temporal.script = [([], result("a"))]
    one = [{"role": "assistant", "content": "x" * (app_module.CHAT_CONTEXT_CHARS + 1)}, {"role": "user", "content": "q"}]
    assert client.post("/api/chat", json=body(messages=one, prior_count=1)).status_code == 200


def test_the_update_with_start_request(api):
    client, temporal, _ = api
    temporal.script = [([], result("a2"))]
    b = body()
    client.post("/api/chat", json=b)
    call = temporal.calls[0]
    assert call["update_id"] == b["turn_id"]
    inp = call["input"]
    assert (inp.turn_id, inp.text, inp.client_count, inp.history) == (b["turn_id"], "q2", 2, None)
    start = call["start"]
    assert start._start_workflow_input.id == C.workflow_id("demo", b["session_id"])
    seed = start._start_workflow_input.args[0]
    assert [m.content for m in seed.messages] == ["q1", "a1"] and seed.dropped == 0


# ---------------------------------------------------------------------- relay

def test_streamed_text_equal_to_the_answer(api):
    client, temporal, _ = api
    temporal.script = [([chunk("Hello "), chunk("world"), {"t": "done", "d": ""}], result("Hello world"))]
    assert frames(client.post("/api/chat", json=body())) == ["Hello ", "world", "[DONE]"]


def test_the_answer_extends_the_streamed_text(api):
    client, temporal, _ = api
    temporal.script = [([chunk("Hello ")], result("Hello world"))]
    assert frames(client.post("/api/chat", json=body())) == ["Hello ", "world", "[DONE]"]


def test_the_answer_diverges_from_the_streamed_text(api):
    client, temporal, _ = api
    temporal.script = [([chunk("Hi")], result("Hello"))]
    assert frames(client.post("/api/chat", json=body())) == ["Hi", {"reset": True}, "Hello", "[DONE]"]


def test_a_retry_resets_the_streamed_text(api):
    client, temporal, _ = api
    temporal.script = [([chunk("partial "), {"t": "reset"}, chunk("full")], result("full"))]
    assert frames(client.post("/api/chat", json=body())) == ["partial ", {"reset": True}, "full", "[DONE]"]


def test_stopped_and_failed_turns(api):
    client, temporal, _ = api
    failure = WorkflowUpdateFailedError(ApplicationError("model exploded", type=C.TURN_FAILED))
    temporal.script = [([chunk("so far ")], result("", stopped=True)), ([], failure)]
    assert frames(client.post("/api/chat", json=body())) == ["so far ", {"stopped": True}, "[DONE]"]
    assert frames(client.post("/api/chat", json=body())) == [{"error": "model exploded"}, "[DONE]"]


def test_redis_down_still_ends_with_the_full_answer(api, monkeypatch):
    client, temporal, redis = api

    async def broken(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis, "xread", broken)
    monkeypatch.setattr(redis, "xadd", lambda *a, **k: asyncio.sleep(0))
    temporal.script = [([chunk("lost")], result("The full answer"))]
    assert frames(client.post("/api/chat", json=body())) == ["The full answer", "[DONE]"]


# ---------------------------------------------------------------------- retries

def test_resync_required_is_retried_once_with_the_tail(api):
    client, temporal, _ = api
    resync = WorkflowUpdateFailedError(ApplicationError("out of sync", type=C.RESYNC_REQUIRED))
    temporal.script = [([], resync), ([], result("a2"))]
    b = body()
    assert frames(client.post("/api/chat", json=b)) == ["a2", "[DONE]"]
    assert [c["update_id"] for c in temporal.calls] == [b["turn_id"], f"{b['turn_id']}:resync"]
    assert [m.content for m in temporal.calls[1]["input"].history] == ["q1", "a1"]

    temporal.calls.clear()
    temporal.script = [([], resync), ([], resync)]
    assert frames(client.post("/api/chat", json=body())) == [{"error": "out of sync"}, "[DONE]"]
    assert len(temporal.calls) == 2  # exactly one retry


@pytest.mark.parametrize("closed", [
    WorkflowUpdateFailedError(ApplicationError("completed", type=C.UPDATE_WORKFLOW_COMPLETED)),
    RPCError("workflow execution already completed", RPCStatusCode.NOT_FOUND, b""),
])
def test_a_workflow_that_closed_under_the_update_is_retried_once(api, closed):
    client, temporal, _ = api
    temporal.script = [([], closed), ([], result("a2"))]
    b = body()
    assert frames(client.post("/api/chat", json=b)) == ["a2", "[DONE]"]
    assert [c["update_id"] for c in temporal.calls] == [b["turn_id"], f"{b['turn_id']}:retry"]


async def test_a_disconnect_does_not_cancel_the_turn():
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    turn = str(uuid.uuid4())
    finish = asyncio.Event()

    async def update():
        await finish.wait()
        return result("done")

    task = asyncio.create_task(update())
    app_module._keep(task)
    await redis.xadd(turn_key(turn), chunk("first "))
    stream = app_module.relay(redis, turn, task)
    assert json.loads((await stream.__anext__())[6:]) == "first "
    await stream.aclose()  # the browser went away
    assert not task.cancelled() and task in app_module._turn_tasks
    finish.set()
    assert (await task).answer == "done"
    await asyncio.sleep(0)
    assert task not in app_module._turn_tasks


# ---------------------------------------------------------------------- stop and close

def test_stop_and_close(api):
    client, temporal, _ = api
    sid, turn = str(uuid.uuid4()), str(uuid.uuid4())
    assert client.post(f"/api/chat/demo/{sid}/stop", json={"turn_id": turn}).status_code == 202
    assert client.delete(f"/api/chat/demo/{sid}").status_code == 202
    wid = C.workflow_id("demo", sid)
    assert temporal.signals == [(wid, C.CANCEL_TURN_SIGNAL, (turn,)), (wid, C.CLOSE_SIGNAL, ())]

    temporal.signal_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    assert client.post(f"/api/chat/demo/{sid}/stop", json={"turn_id": turn}).status_code == 202
    assert client.delete(f"/api/chat/demo/{sid}").status_code == 202
    assert client.delete("/api/chat/demo/not-a-uuid").status_code == 400


def test_config_reports_the_context_budget(api):
    client, _, _ = api
    assert client.get("/api/config").json()["chat_context_chars"] == app_module.CHAT_CONTEXT_CHARS
