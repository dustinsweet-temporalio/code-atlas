#!/usr/bin/env python3
"""
One-time login to the Temporal Docs MCP for the indexer's Temporal lens.

Opens your browser for the provider's sign-in, then stores a refresh token at
~/.config/code-atlas/temporal-docs-mcp.json (readable only by you). The indexer
container mounts that folder and refreshes access tokens itself, so indexing
runs need no browser. Run this again if indexing reports the login expired.

    python3 scripts/mcp_login.py [--url https://temporal.mcp.kapa.ai]

Standard library only; runs on the host (not in Docker).
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_URL = "https://temporal.mcp.kapa.ai"
STORE = Path.home() / ".config" / "code-atlas" / "temporal-docs-mcp.json"
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/callback"


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(url: str, data: dict, as_json: bool = False) -> dict:
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    ctype = "application/json" if as_json else "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def discover(mcp_url: str) -> dict:
    """OAuth endpoints via RFC 9728 (protected resource) and RFC 8414 (authorization server) metadata."""
    prm = get_json(mcp_url.rstrip("/") + "/.well-known/oauth-protected-resource")
    issuer = urllib.parse.urlsplit(prm["authorization_servers"][0])
    meta = get_json(f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server{issuer.path}")
    return {
        "resource": prm["resource"],
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": meta["registration_endpoint"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help="Temporal Docs MCP URL")
    args = parser.parse_args()

    endpoints = discover(args.url)
    client = post(endpoints["registration_endpoint"], {
        "client_name": "code-atlas-indexer",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
        "scope": "openid",
    }, as_json=True)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    auth_url = endpoints["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "scope": "openid", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": endpoints["resource"],
    })

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Login received. You can close this tab.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print("Opening your browser to sign in to the Temporal Docs MCP...")
    print(f"If it does not open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if "error" in result or result.get("state") != state:
        print(f"Login failed: {result}", file=sys.stderr)
        return 1

    tokens = post(endpoints["token_endpoint"], {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
        "code_verifier": verifier, "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""), "resource": endpoints["resource"],
    })
    if not tokens.get("refresh_token"):
        print("The server did not issue a refresh token; headless indexing would not work.", file=sys.stderr)
        return 1

    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps({
        "mcp_url": args.url,
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "refresh_token": tokens["refresh_token"],
        "token_endpoint": endpoints["token_endpoint"],
        "resource": endpoints["resource"],
        "obtained_at": int(time.time()),
    }, indent=2))
    os.chmod(STORE, 0o600)
    print(f"Signed in. Credentials stored at {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
