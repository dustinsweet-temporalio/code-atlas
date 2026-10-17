"""
Code Atlas web server.

Serves the chat UI and a small API:

    GET  /api/health   service status (database, API key, model)
    GET  /api/config   UI settings (the Temporal UI link)
    GET  /api/repos    indexed repositories
    GET  /api/report/{repo_id}  the stored Temporal report (data for the report view)
    GET  /api/report/{repo_id}/markdown  the detailed report as Markdown (as ./scripts/report.sh writes it)
    GET  /api/compass/{repo_id}  the Compass Web URL that opens this index's database
    POST /api/chat     streaming (SSE) answer for a conversation about one index ({repo_id, messages})

Run with: uvicorn app:app --host 0.0.0.0 --port 8080
"""
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import List, Literal
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import (
    COMPASS_PUBLIC_URL,
    COMPASS_URL,
    DB_PREFIX,
    FILE_SUMMARIES_COLLECTION,
    FOLDER_SUMMARIES_COLLECTION,
    INDEX_META_COLLECTION,
    LOG_FORMAT,
    LOG_LEVEL,
    MODEL_ID,
    REPORTS_COLLECTION,
    SOURCE_DIR,
)
from utils.mongo import get_mongo_client
from utils.formatters.lens import index_lens_info
from utils.validation import validate_repository

logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
logger = logging.getLogger("code_atlas.app")

WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent.parent / "web"))

app = FastAPI(title="Code Atlas", docs_url=None, redoc_url=None)


@app.middleware("http")
async def revalidate_ui_files(request, call_next):
    """Browsers revalidate the UI files (cheap, via ETag) so a rebuilt app is picked up at once."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    repo_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", description="The index this conversation is about")
    messages: List[ChatMessage] = Field(min_length=1)


def _sse(payload) -> bytes:
    """Encode one server-sent event. JSON encoding keeps newlines out of the frame."""
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


@app.get("/api/health")
def health(recheck: bool = False):
    """App status. temporal_docs is the Temporal Docs MCP check (cached; recheck=true forces it)."""
    from utils.temporal_docs import status as docs_status

    status = {
        "model": MODEL_ID,
        "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "source_dir": SOURCE_DIR,
        "database": "ok",
    }
    try:
        get_mongo_client().admin.command("ping")
    except Exception as e:
        status["database"] = f"unavailable: {e}"
    status["temporal_docs"] = docs_status(recheck=recheck)
    ok = status["database"] == "ok" and status["api_key_configured"]
    return JSONResponse(status, status_code=200 if ok else 503)


@app.get("/api/config")
def ui_config():
    """Settings the UI needs: where the Temporal UI (indexing runs) is, as the browser reaches it."""
    return {
        "temporal_ui_url": os.environ.get("TEMPORAL_UI_URL", "").strip() or "http://localhost:8233",
        "temporal_namespace": os.environ.get("TEMPORAL_NAMESPACE", "").strip() or "default",
    }


@app.get("/api/repos")
def list_repos():
    """Indexed repositories with file and folder counts."""
    try:
        client = get_mongo_client()
        prefix = f"{DB_PREFIX}_"
        repos = []
        for db_name in sorted(client.list_database_names()):
            if not db_name.startswith(prefix):
                continue
            db = client[db_name]
            info = index_lens_info(db)
            repos.append({
                "repo_id": db_name[len(prefix):],
                "files": db[FILE_SUMMARIES_COLLECTION].estimated_document_count(),
                "folders": db[FOLDER_SUMMARIES_COLLECTION].estimated_document_count(),
                "pack_version": info["pack_version"],
                "pack_versions": info["pack_versions"],
                "lens": info["lens"],
                "indexed_at": info["indexed_at"],
                "prompt_versions": info["prompt_versions"],
            })
        return {"repos": repos}
    except Exception as e:
        logger.error(f"Failed to list repositories: {e}")
        return JSONResponse({"error": str(e)}, status_code=503)


def _stored_report(repo_id: str) -> tuple[dict, dict] | JSONResponse:
    """The stored report document and the index's meta document, or the error response to send."""
    if not re.match(r"^[A-Za-z0-9_-]+$", repo_id):
        return JSONResponse({"error": "Invalid index name."}, status_code=400)
    try:
        db = get_mongo_client()[f"{DB_PREFIX}_{repo_id}"]
        doc = db[REPORTS_COLLECTION].find_one({"_id": f"{repo_id}::temporal_report"})
        meta = db[INDEX_META_COLLECTION].find_one({}) or {}
    except Exception as e:
        logger.error(f"Failed to read the report for {repo_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=503)
    if not doc or not doc.get("data"):
        detail = f" The last attempt failed: {doc.get('error')}" if doc and doc.get("error") else ""
        return JSONResponse({"error": f"No Temporal report for this index yet.{detail}"}, status_code=404)
    return doc, meta


@app.get("/api/report/{repo_id}")
def get_report(repo_id: str):
    """The Temporal report stored with an index (built during indexing), with its freshness."""
    found = _stored_report(repo_id)
    if isinstance(found, JSONResponse):
        return found
    doc, meta = found
    index_commit = meta.get("commit_sha")
    return {
        "status": doc.get("status"), "error": doc.get("error"),
        "generated_at": doc.get("generated_at"), "commit_sha": doc.get("commit_sha"),
        "index_commit": index_commit,
        "stale": bool(index_commit and doc.get("commit_sha") and index_commit != doc["commit_sha"]),
        "data": doc["data"],
    }


@app.get("/api/report/{repo_id}/markdown")
def get_report_markdown(repo_id: str):
    """The detailed Temporal report: the full Markdown document ./scripts/report.sh writes."""
    found = _stored_report(repo_id)
    if isinstance(found, JSONResponse):
        return found
    doc, _ = found
    markdown = doc.get("markdown")
    if not markdown:
        # A report stored without its Markdown: render it from the data, as the indexer does.
        from semantic_ingress.report.render import render

        try:
            markdown = render(doc["data"])
        except Exception as e:
            logger.error(f"Failed to render the detailed report for {repo_id}: {e}", exc_info=True)
            return JSONResponse({"error": f"Could not render the detailed report: {e}"}, status_code=500)
    return PlainTextResponse(markdown, media_type="text/markdown")


@app.get("/api/compass/{repo_id}")
def compass_link(repo_id: str):
    """
    Where Compass Web shows this index's database: <public url>/<connection id>/<database>.

    Compass Web gives its connection a new random id each time it starts, so the id is
    looked up from the service on every request rather than built into the UI.
    """
    if not re.match(r"^[A-Za-z0-9_-]+$", repo_id):
        return JSONResponse({"error": "Invalid index name."}, status_code=400)
    if not (COMPASS_URL and COMPASS_PUBLIC_URL):
        return JSONResponse({"error": "Compass Web is not configured (COMPASS_URL, COMPASS_PUBLIC_URL)."}, status_code=404)
    is_valid, _, error = validate_repository(repo_id)
    if not is_valid:
        return JSONResponse({"error": f"Index '{repo_id}' is not available: {error}"}, status_code=404)
    try:
        with urllib.request.urlopen(f"{COMPASS_URL.rstrip('/')}/api/connection-info", timeout=5) as res:
            connections = json.load(res)
        connection_id = connections[0]["id"]
    except Exception as e:
        logger.error(f"Compass Web lookup failed: {e}")
        return JSONResponse({"error": f"Compass Web is not reachable ({e}). Is the compass service running? "
                                      "docker compose up -d compass"}, status_code=503)
    database = f"{DB_PREFIX}_{repo_id}"
    return {"url": f"{COMPASS_PUBLIC_URL.rstrip('/')}/{quote(connection_id, safe='')}/{quote(database, safe='')}",
            "database": database}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Stream an answer as server-sent events: `data: "<text chunk>"` ... `data: [DONE]`."""
    from agent import stream_answer

    # Reject an unknown index before any model call.
    is_valid, _, error = validate_repository(request.repo_id)
    if not is_valid:
        return JSONResponse({"error": f"Index '{request.repo_id}' is not available: {error}"}, status_code=400)

    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    async def events():
        try:
            async for chunk in stream_answer({"messages": messages, "repo_id": request.repo_id}):
                yield _sse(chunk)
        except Exception as e:  # stream_answer reports its own errors; this is a backstop
            logger.error(f"Chat stream failed: {e}", exc_info=True)
            yield _sse({"error": str(e)})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
