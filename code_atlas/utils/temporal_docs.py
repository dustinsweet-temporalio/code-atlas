"""
Temporal Docs MCP for chat: search, and a connection check for the status light.

Reuses the indexer's client (same image) and the sign-in from scripts/mcp_login.py,
mounted at /run/code-atlas. "Connected" means a token refresh and a real search
returning at least one docs.temporal.io result; the check is cached so that
health polling does not search continuously.
"""
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from semantic_ingress.lens.mcp_docs import McpError, TemporalDocsClient

MCP_URL = os.environ.get("TEMPORAL_DOCS_MCP_URL", "").strip() or "https://temporal.mcp.kapa.ai"
CREDENTIALS = Path(
    os.environ.get("TEMPORAL_DOCS_MCP_CREDENTIALS", "").strip() or "/run/code-atlas/temporal-docs-mcp.json"
)
CHECK_SECONDS = 600
CANARY_QUERY = "What is a Temporal Workflow?"
FIX = "Run python3 scripts/mcp_login.py on the host, then restart the app (docker compose up -d app)."

_lock = threading.Lock()
_client: TemporalDocsClient | None = None
_status: dict | None = None
_checked = 0.0


def _get_client() -> TemporalDocsClient:
    global _client
    with _lock:
        if _client is None:
            _client = TemporalDocsClient(MCP_URL, CREDENTIALS)
        return _client


def search(query: str) -> list[dict]:
    """Passages from the Temporal docs: [{"source_url": ..., "content": ...}, ...]. Raises McpError."""
    return _get_client().search(query)


def status(recheck: bool = False) -> dict:
    """{"status": connected | unavailable | not_configured, "detail", "fix", "checked_at"} (cached)."""
    global _status, _checked, _client
    if not recheck and _status is not None and time.time() - _checked < CHECK_SECONDS:
        return _status
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not CREDENTIALS.exists():
        result = {"status": "not_configured", "detail": f"No sign-in found at {CREDENTIALS}.", "fix": FIX}
    else:
        try:
            chunks = search(CANARY_QUERY)
            official = [c for c in chunks if "docs.temporal.io" in c.get("source_url", "")]
            if official:
                result = {"status": "connected",
                          "detail": f"Search returned {len(chunks)} passages, {len(official)} from docs.temporal.io.",
                          "fix": ""}
            else:
                result = {"status": "unavailable",
                          "detail": f"Search returned {len(chunks)} passages, none from docs.temporal.io.",
                          "fix": "The service may be degraded; try again later."}
        except McpError as e:
            with _lock:
                _client = None  # build a fresh client next time (credentials may have been renewed)
            result = {"status": "unavailable", "detail": str(e), "fix": FIX}
        except Exception as e:  # network errors and the like
            result = {"status": "unavailable", "detail": f"{type(e).__name__}: {e}", "fix": ""}
    _status, _checked = {**result, "checked_at": checked_at}, time.time()
    return _status
