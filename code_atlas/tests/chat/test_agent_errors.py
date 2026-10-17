"""agent.stream_answer: raise_errors=True raises; the default still yields errors as text."""
import pytest

import agent


async def _collect(gen):
    return [chunk async for chunk in gen]


PAYLOAD = {"repo_id": "demo", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def valid_repo(monkeypatch):
    monkeypatch.setattr(agent, "validate_repository", lambda repo_id: (True, f"semv2_{repo_id}", None))
    monkeypatch.setattr(agent, "ensure_repository_indexes", lambda *a: None)
    monkeypatch.setattr(agent, "load_repo_overview", lambda db: None)
    monkeypatch.setattr(agent, "load_report_line", lambda repo_id: "")
    monkeypatch.setattr(agent, "build_model", lambda: None)


async def test_unknown_repository(monkeypatch):
    monkeypatch.setattr(agent, "validate_repository", lambda repo_id: (False, None, "not found"))
    with pytest.raises(agent.AgentInputError, match="not available"):
        await _collect(agent.stream_answer(PAYLOAD, raise_errors=True))


async def test_unknown_repository_default_yields_text(monkeypatch):
    monkeypatch.setattr(agent, "validate_repository", lambda repo_id: (False, None, "not found"))
    chunks = await _collect(agent.stream_answer(PAYLOAD))
    assert chunks and chunks[0].startswith("Error: repository 'demo' is not available")


class _FailingAgent:
    def __init__(self, **kwargs):
        pass

    async def stream_async(self, prompt):
        yield {"data": "partial "}
        raise RuntimeError("model exploded")


async def test_model_errors_are_reraised(valid_repo, monkeypatch):
    monkeypatch.setattr(agent, "Agent", _FailingAgent)
    with pytest.raises(RuntimeError, match="model exploded"):
        await _collect(agent.stream_answer(PAYLOAD, raise_errors=True))


async def test_model_errors_default_yields_text(valid_repo, monkeypatch):
    monkeypatch.setattr(agent, "Agent", _FailingAgent)
    chunks = await _collect(agent.stream_answer(PAYLOAD))
    assert "".join(chunks).startswith("partial ") and "model exploded" in chunks[-1]
