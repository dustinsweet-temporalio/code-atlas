"""
Durability smoke test against a real Temporal server and Redis (the compose stack, or
any reachable pair): a turn survives its worker being killed mid-answer.

    CHAT_INTEGRATION=1 TEMPORAL_ADDRESS=localhost:7233 REDIS_URL=redis://localhost:6379/0 \\
        python -m pytest tests/chat/test_integration.py

Takes about a minute: the killed worker's activity is retried once its heartbeat
times out (30 seconds).
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
from temporalio.client import Client

import app as app_module
from chat.streaming import turn_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("CHAT_INTEGRATION"), reason="set CHAT_INTEGRATION=1 (needs Temporal and Redis)"),
]

CODE_ATLAS = Path(__file__).resolve().parents[2]
ANSWER = "".join(f"chunk{i} " for i in range(20))


def start_worker(queue: str) -> subprocess.Popen:
    env = {**os.environ, "CHAT_TASK_QUEUE": queue, "STUB_CHUNKS": "20", "STUB_DELAY": "0.3"}
    return subprocess.Popen([sys.executable, "tests/chat/stub_worker.py"], cwd=CODE_ATLAS, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def browser_answer(text: str) -> tuple[str, list]:
    """What the browser ends up showing, and the control frames it saw."""
    answer, controls = "", []
    for frame in text.split("\n\n"):
        if not frame.startswith("data: ") or frame == "data: [DONE]":
            continue
        event = json.loads(frame[6:])
        if isinstance(event, str):
            answer += event
        else:
            controls.append(event)
            if event.get("reset"):
                answer = ""
    return answer, controls


async def test_a_turn_survives_its_worker_being_killed(monkeypatch):
    queue = f"chat-it-{uuid.uuid4().hex[:6]}"
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    monkeypatch.setattr(app_module, "_temporal", client)
    monkeypatch.setattr(app_module, "CHAT_TASK_QUEUE", queue)
    monkeypatch.setattr(app_module, "validate_repository", lambda repo_id: (True, f"semv2_{repo_id}", None))
    app_module.app.state.redis = redis

    worker = start_worker(queue)
    try:
        turn = str(uuid.uuid4())
        body = {"repo_id": "demo", "session_id": str(uuid.uuid4()), "turn_id": turn, "prior_count": 0,
                "messages": [{"role": "user", "content": "tell me everything"}]}
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=180) as http:
            request = asyncio.create_task(http.post("/api/chat", json=body))

            for _ in range(300):  # wait until the answer is streaming
                if await redis.xlen(turn_key(turn)) >= 3:
                    break
                await asyncio.sleep(0.1)
            assert await redis.xlen(turn_key(turn)) >= 3, "the turn never started streaming"
            worker.kill()  # mid-answer
            worker.wait()
            worker = start_worker(queue)

            response = await request
        answer, controls = browser_answer(response.text)
        assert response.status_code == 200
        assert answer == ANSWER
        assert {"reset": True} in controls  # the retried attempt restarted the answer
        events = [f["t"] for _, f in await redis.xrange(turn_key(turn))]
        assert events.count("reset") >= 1 and events[-1] == "done"
    finally:
        worker.kill()
        await redis.aclose()
