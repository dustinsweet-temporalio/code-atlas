import io
import json
from pathlib import Path

import pytest

from semantic_ingress import preflight as pf
from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens import mcp_docs
from semantic_ingress.lens.models import RepoLens
from semantic_ingress.lens.pack import load_pack
from semantic_ingress.lens.repo_pass import run_repo_pass
from semantic_ingress.lens.tier0 import Tier0Facts

PACK_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "temporal-pack.yaml"
CHUNKS = [{"source_url": "https://docs.temporal.io/saga", "content": "Saga..."},
          {"source_url": "https://community.temporal.io/x", "content": "Forum..."}]


# --- MCP client ---------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(tmp_path, monkeypatch, bodies):
    cred = tmp_path / "c.json"
    cred.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": "r",
                                "token_endpoint": "https://auth/token", "resource": "https://mcp/"}))
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        calls.append(url)
        if url == "https://auth/token":
            return _Resp(json.dumps({"access_token": "a", "expires_in": 3600}).encode())
        return _Resp(bodies.pop(0).encode())

    monkeypatch.setattr(mcp_docs.urllib.request, "urlopen", fake_urlopen)
    return mcp_docs.TemporalDocsClient("https://mcp/", cred), calls


def test_search_parses_structured_results_from_event_stream(tmp_path, monkeypatch):
    result = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"results": CHUNKS}, "content": []}}
    client, calls = _client(tmp_path, monkeypatch, [
        '{"jsonrpc": "2.0", "id": 1, "result": {}}',  # initialize (JSON)
        "",  # initialized notification
        "event: message\ndata: " + json.dumps(result) + "\n\n",  # tools/call (SSE)
    ])
    assert client.search("What is a saga?") == CHUNKS
    assert calls[0] == "https://auth/token"


def test_missing_credentials_names_the_fix(tmp_path):
    with pytest.raises(mcp_docs.McpError, match="mcp_login.py"):
        mcp_docs.TemporalDocsClient("https://mcp/", tmp_path / "missing.json")


# --- preflight ----------------------------------------------------------------

def test_preflight_stops_at_pack_before_any_api_call(monkeypatch, tmp_path):
    from semantic_ingress.config import IngestionConfig, LensConfig

    called = []
    monkeypatch.setattr(pf.anthropic, "Anthropic", lambda **kw: called.append("anthropic"))
    bad = tmp_path / "pack.yaml"
    bad.write_text("meta: {id: x}\n")
    config = IngestionConfig(repo_path=tmp_path, output_dir=tmp_path, prefix="semv2")
    config.lens = LensConfig(enabled=True, pack_path=bad, mcp_url="https://mcp/", mcp_credentials=tmp_path / "c.json")
    with pytest.raises(pf.PreflightError, match="Knowledge pack"):
        pf.run_preflight(config)
    assert called == []


# --- repository pass with a docs search ----------------------------------------

class FakeLLM:
    """Calls the docs-search tool once, then answers citing a returned URL and an invented one."""

    def __init__(self, search: bool):
        self.search = search

    def generate_with_tools(self, prompt, response_model, system, tools, tool_handler, max_tool_calls):
        assert "Docs search is required" in system
        n = 0
        if self.search:
            tool_handler("search_temporal_docs", {"query": "What is the saga pattern?"})
            n = 1
        lens = RepoLens(status="none", archetype="service", detected=[], processes=[], anti=[], n="x",
                        cites=[{"ref": "https://docs.temporal.io/saga", "supports": "saga"},
                               {"ref": "https://docs.temporal.io/made-up", "supports": "x"},
                               {"ref": "V-DURABLE", "supports": "durable"}])
        return lens, LLMUsage(input_tokens=0, output_tokens=0), n


@pytest.mark.parametrize("search", [True, False])
def test_repo_pass_keeps_only_returned_urls(search):
    pack = load_pack(PACK_PATH)
    result = run_repo_pass(FakeLLM(search), pack, Tier0Facts(), "(none)", {}, set(), lambda q: CHUNKS)
    refs = [c.ref for c in result.lens.cites]
    if search:
        assert refs == ["https://docs.temporal.io/saga", "V-DURABLE"]
        assert result.urls_returned == sorted({c["source_url"] for c in CHUNKS})
    else:
        assert refs == ["V-DURABLE"]
        # no processes in this fake answer, so a missing search is not flagged
        assert "no docs search was made although one was available" not in result.corrections
