"""Temporal Docs MCP status for the UI light (code_atlas/utils/temporal_docs.py)."""
import importlib.util
from pathlib import Path

from semantic_ingress.lens.mcp_docs import McpError

_spec = importlib.util.spec_from_file_location(
    "temporal_docs", Path(__file__).resolve().parents[1] / "utils" / "temporal_docs.py"
)
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


def reset(monkeypatch, creds: Path, search):
    monkeypatch.setattr(td, "CREDENTIALS", creds)
    monkeypatch.setattr(td, "search", search)
    monkeypatch.setattr(td, "_status", None)
    monkeypatch.setattr(td, "_checked", 0.0)


def test_no_sign_in_is_not_configured_without_any_call(tmp_path, monkeypatch):
    def never(q):
        raise AssertionError("no search without credentials")
    reset(monkeypatch, tmp_path / "missing.json", never)
    s = td.status()
    assert s["status"] == "not_configured" and "mcp_login.py" in s["fix"]


def test_connected_needs_an_official_docs_result_and_is_cached(tmp_path, monkeypatch):
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    calls = []
    def search(q):
        calls.append(q)
        return [{"source_url": "https://docs.temporal.io/workflows", "content": "x"}]
    reset(monkeypatch, creds, search)
    assert td.status()["status"] == "connected"
    assert td.status()["status"] == "connected" and len(calls) == 1  # cached
    td.status(recheck=True)
    assert len(calls) == 2


def test_a_refused_token_is_unavailable_with_the_fix(tmp_path, monkeypatch):
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    def refused(q):
        raise McpError("token refresh refused (400)")
    reset(monkeypatch, creds, refused)
    s = td.status()
    assert s["status"] == "unavailable" and "refused" in s["detail"] and "mcp_login.py" in s["fix"]
