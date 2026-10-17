"""
Client for the Temporal Docs MCP (search over Temporal's documentation).

Authentication: `scripts/mcp_login.py` runs a one-time browser login on the host
and stores a refresh token. This client exchanges it for short-lived access
tokens headlessly, refreshing again on expiry or a 401, and writes a rotated
refresh token back if the server ever returns one.

Standard library only. The MCP endpoint speaks JSON-RPC over HTTP and may answer
with JSON or server-sent events.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SEARCH_TOOL = "search_temporal_knowledge_sources"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 30


class McpError(Exception):
    """The docs MCP is unreachable, unauthorized, or returned something unusable."""


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise McpError(f"Cannot read OAuth metadata at {url}: {e}") from e


def discover(mcp_url: str) -> dict:
    """
    OAuth endpoints for an MCP server (RFC 9728 protected-resource metadata, then
    RFC 8414 authorization-server metadata). Returns resource, token_endpoint,
    authorization_endpoint and registration_endpoint.
    """
    base = mcp_url.rstrip("/")
    prm = _get_json(f"{base}/.well-known/oauth-protected-resource")
    issuer = prm["authorization_servers"][0]
    parts = urllib.parse.urlsplit(issuer)
    as_meta = _get_json(f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{parts.path}")
    return {
        "resource": prm["resource"],
        "token_endpoint": as_meta["token_endpoint"],
        "authorization_endpoint": as_meta["authorization_endpoint"],
        "registration_endpoint": as_meta.get("registration_endpoint", ""),
    }


class TemporalDocsClient:
    def __init__(self, url: str, credentials_path: Path) -> None:
        self.url = url
        self.credentials_path = credentials_path
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires = 0.0
        self._initialized = False
        self._cred = self._load_credentials()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _load_credentials(self) -> dict:
        if not self.credentials_path.exists():
            raise McpError(
                f"No Temporal Docs MCP credentials at {self.credentials_path}. "
                "Run: python3 scripts/mcp_login.py"
            )
        try:
            cred = json.loads(self.credentials_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise McpError(f"Cannot read MCP credentials {self.credentials_path}: {e}") from e
        for key in ("client_id", "refresh_token"):
            if not cred.get(key):
                raise McpError(
                    f"MCP credentials {self.credentials_path} lack '{key}'. Run: python3 scripts/mcp_login.py"
                )
        if not cred.get("token_endpoint") or not cred.get("resource"):
            cred.update(discover(self.url))
        return cred

    def _refresh(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._cred["refresh_token"],
            "client_id": self._cred["client_id"],
            "client_secret": self._cred.get("client_secret", ""),
            "resource": self._cred["resource"],
        }).encode()
        req = urllib.request.Request(
            self._cred["token_endpoint"], data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
                tokens = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise McpError(
                f"MCP token refresh refused ({e.code}): {detail}. The login may have expired; "
                "run: python3 scripts/mcp_login.py"
            ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise McpError(f"MCP token endpoint unreachable: {e}") from e

        self._token = tokens["access_token"]
        self._token_expires = time.time() + int(tokens.get("expires_in", 3600)) - 60
        new_refresh = tokens.get("refresh_token")
        if new_refresh and new_refresh != self._cred["refresh_token"]:
            self._cred["refresh_token"] = new_refresh
            self._save_credentials()

    def _save_credentials(self) -> None:
        tmp = self.credentials_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cred, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.credentials_path)

    def _access_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires:
            self._refresh()
        return self._token

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        for attempt in (1, 2):
            req = urllib.request.Request(
                self.url, data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {self._access_token()}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
                    raw = r.read().decode()
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._token = None  # expired early: refresh once and retry
                    continue
                raise McpError(f"Temporal Docs MCP returned HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                raise McpError(f"Temporal Docs MCP unreachable: {e}") from e
        if not raw.strip():
            return {}
        if raw.lstrip().startswith("{"):
            message = json.loads(raw)
        else:  # server-sent events: the last data line carries the JSON-RPC message
            data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            if not data:
                raise McpError("Temporal Docs MCP returned an empty event stream")
            message = json.loads(data[-1])
        if "error" in message:
            raise McpError(f"Temporal Docs MCP error: {message['error']}")
        return message

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "code-atlas-indexer", "version": "1"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def search(self, query: str) -> list[dict]:
        """Search the Temporal docs. Returns [{"source_url": ..., "content": ...}, ...]."""
        with self._lock:
            self._ensure_initialized()
            message = self._post({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": SEARCH_TOOL, "arguments": {"query": query}},
            })
        result = message.get("result") or {}
        if result.get("isError"):
            raise McpError(f"Docs search failed: {result.get('content')}")
        structured = result.get("structuredContent") or {}
        chunks = structured.get("results")
        if chunks is None:
            raise McpError("Docs search returned no structured results")
        return [
            {"source_url": c.get("source_url", ""), "content": c.get("content", "")}
            for c in chunks if c.get("content")
        ]
