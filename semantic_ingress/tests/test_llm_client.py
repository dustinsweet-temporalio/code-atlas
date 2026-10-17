"""
LLMClient retry modes: internal retries (every existing caller) and single-attempt
classified errors (Temporal activities, where the activity retry policy owns retries).
"""
import anthropic

try:  # the HTTP library the installed anthropic SDK builds its errors from
    import httpx2 as httpx
except ImportError:
    import httpx
import pytest

from semantic_ingress.config import LLMConfig
from semantic_ingress.core import llm_client
from semantic_ingress.core.llm_client import (
    LLMClient,
    LLMError,
    LLMPermanentError,
    LLMTransientError,
    LLMUsage,
)
from semantic_ingress.models import FileSemanticSummary

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
OK = '{"role": "A file."}'


@pytest.fixture
def client_factory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)

    def make(responses, internal_retries):
        client = LLMClient(LLMConfig(), internal_retries=internal_retries)
        calls = []

        def invoke(prompt, system=None, schema=None):
            calls.append(prompt)
            item = responses[min(len(calls), len(responses)) - 1]
            if isinstance(item, Exception):
                raise item
            return item, LLMUsage(input_tokens=1, output_tokens=1)

        monkeypatch.setattr(client, "_invoke_model", invoke)
        return client, calls

    return make


def test_activity_mode_makes_one_attempt_and_turns_off_sdk_retries(client_factory):
    client, calls = client_factory([anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, headers={"retry-after": "4"}, request=REQUEST), body=None)],
        internal_retries=False)
    assert client.client.max_retries == 0
    with pytest.raises(LLMTransientError) as e:
        client.generate("p", FileSemanticSummary)
    assert len(calls) == 1 and e.value.retry_after == 4.0


def test_activity_mode_classifies_errors(client_factory):
    client, _ = client_factory(["not json at all"], internal_retries=False)
    with pytest.raises(LLMTransientError):
        client.generate("p", FileSemanticSummary)

    client, _ = client_factory([anthropic.BadRequestError(
        "bad", response=httpx.Response(400, request=REQUEST), body=None)], internal_retries=False)
    with pytest.raises(LLMPermanentError):
        client.generate("p", FileSemanticSummary)

    client, _ = client_factory([OK], internal_retries=False)
    summary, usage = client.generate("p", FileSemanticSummary)
    assert summary.role == "A file." and usage.input_tokens == 1


def test_default_mode_still_retries_internally(client_factory):
    client, calls = client_factory(["not json", "still not json", OK], internal_retries=True)
    summary, _ = client.generate("p", FileSemanticSummary)
    assert summary.role == "A file." and len(calls) == 3
    assert client.client.max_retries == LLMConfig().max_retries

    client, calls = client_factory(["nope"], internal_retries=True)
    with pytest.raises(LLMError):
        client.generate("p", FileSemanticSummary)
    assert len(calls) == 3


def test_activity_mode_says_when_a_response_hit_max_tokens(monkeypatch, client_factory):
    from types import SimpleNamespace

    client, _ = client_factory([OK], internal_retries=False)
    truncated = SimpleNamespace(stop_reason="max_tokens",
                                content=[SimpleNamespace(type="text", text='{"role": "A fi')])
    monkeypatch.setattr(client, "_invoke_model", LLMClient._invoke_model.__get__(client))
    monkeypatch.setattr(client, "_call", lambda *a, **k: (truncated, LLMUsage(input_tokens=1, output_tokens=1)))
    with pytest.raises(LLMTransientError) as e:
        client.generate("p", FileSemanticSummary, enforce_schema=True)
    assert f"hit max_tokens ({client.config.max_tokens})" in str(e.value)
    assert "Unterminated string" in str(e.value)

    # A complete response that fails validation for another reason is reported as before.
    complete = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"role": "A fi')])
    monkeypatch.setattr(client, "_call", lambda *a, **k: (complete, LLMUsage(input_tokens=1, output_tokens=1)))
    with pytest.raises(LLMTransientError) as e:
        client.generate("p", FileSemanticSummary, enforce_schema=True)
    assert "max_tokens" not in str(e.value)
