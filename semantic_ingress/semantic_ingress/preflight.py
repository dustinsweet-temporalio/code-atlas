"""
Preflight: check everything an index run depends on before any LLM spend.

Order: knowledge pack and report rubric (lens on) -> Anthropic API -> MongoDB ->
Temporal server -> Temporal Docs MCP (lens on: token refresh, then a canary search). The first failure stops the run
with the fix to apply. The docs client that passed is returned for reuse.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta

import anthropic

from semantic_ingress.config import IngestionConfig
from semantic_ingress.lens.mcp_docs import McpError, TemporalDocsClient
from semantic_ingress.lens.pack import Pack, PackError, load_pack
from semantic_ingress.report.rubric import Rubric, RubricError, load_rubric
from semantic_ingress.storage import MongoStore

CANARY_QUERY = "What are the Event History limits for a Temporal Workflow Execution?"
CANARY_MIN_CHUNKS = 5
CANARY_MAX_SECONDS = 30
EXIT_CODE = 2


class PreflightError(Exception):
    """A dependency of the index run is not usable; the message says how to fix it."""


@dataclass
class PreflightResult:
    checks: list[tuple[str, str]] = field(default_factory=list)  # (check, detail)
    pack: Pack | None = None
    rubric: Rubric | None = None
    docs: TemporalDocsClient | None = None


def run_preflight(config: IngestionConfig, check_temporal: bool = True) -> PreflightResult:
    """Run the checks in order; `check_temporal` is off for commands that do not use Temporal (`report`)."""
    result = PreflightResult()
    lens = config.lens.enabled

    # 1. Knowledge pack
    if lens:
        try:
            result.pack = load_pack(config.lens.pack_path)
        except PackError as e:
            raise PreflightError(f"Knowledge pack: {e}") from e
        result.checks.append(("knowledge pack", f"{result.pack.id} {result.pack.version} ({result.pack.hash})"))
        try:
            result.rubric = load_rubric(config.report.rubric_path, result.pack)
        except RubricError as e:
            raise PreflightError(f"Report rubric: {e}") from e
        result.checks.append(("report rubric", f"{result.rubric.id} {result.rubric.version} ({result.rubric.hash})"))
    else:
        result.checks.append(("knowledge pack", "skipped (LENS_ENABLED=false)"))

    # 2. Anthropic API (token counting is free)
    try:
        client = anthropic.Anthropic(timeout=30, max_retries=1)
        client.messages.count_tokens(model=config.llm.model_id, messages=[{"role": "user", "content": "ping"}])
    except anthropic.AuthenticationError as e:
        raise PreflightError("Anthropic API key rejected. Check ANTHROPIC_API_KEY in .env.") from e
    except anthropic.NotFoundError as e:
        raise PreflightError(f"INDEX_MODEL {config.llm.model_id!r} not found. Check INDEX_MODEL in .env.") from e
    except anthropic.APIError as e:
        raise PreflightError(f"Anthropic API not reachable: {e}") from e
    result.checks.append(("anthropic api", config.llm.model_id))

    # 3. MongoDB (the store pings on construction)
    try:
        MongoStore(config.mongo, config.prefix)
    except Exception as e:
        raise PreflightError(f"MongoDB not reachable at MONGO_URL: {e}") from e
    result.checks.append(("mongodb", config.mongo.url))

    # 4. Temporal server (sync runs as a workflow on it)
    if check_temporal:
        try:
            version = asyncio.run(_temporal_server_version(config))
        except Exception as e:
            raise PreflightError(
                f"Temporal server not reachable at TEMPORAL_ADDRESS ({config.temporal.address}): {e}. "
                "Run `docker compose up -d temporal` and check TEMPORAL_ADDRESS."
            ) from e
        result.checks.append(("temporal server", f"{config.temporal.address} ({version})"))

    # 5. Temporal Docs MCP
    if lens:
        start = time.time()
        try:
            docs = TemporalDocsClient(config.lens.mcp_url, config.lens.mcp_credentials)
            chunks = docs.search(CANARY_QUERY)
        except McpError as e:
            raise PreflightError(f"Temporal Docs MCP: {e}") from e
        elapsed = time.time() - start
        official = [c for c in chunks if "docs.temporal.io" in c["source_url"]]
        if len(chunks) < CANARY_MIN_CHUNKS or not official:
            raise PreflightError(
                f"Temporal Docs MCP canary search returned {len(chunks)} results "
                f"({len(official)} from docs.temporal.io); expected at least {CANARY_MIN_CHUNKS}, "
                "including docs.temporal.io. The service may be degraded; try again later."
            )
        if elapsed > CANARY_MAX_SECONDS:
            raise PreflightError(f"Temporal Docs MCP canary search took {elapsed:.0f}s (limit {CANARY_MAX_SECONDS}s).")
        result.docs = docs
        result.checks.append(("temporal docs mcp", f"{len(chunks)} results, {len(official)} official, {elapsed:.1f}s"))
    else:
        result.checks.append(("temporal docs mcp", "skipped (LENS_ENABLED=false)"))

    return result


async def _temporal_server_version(config: IngestionConfig) -> str:
    """Connect to the Temporal server and make one cheap call; returns the server version."""
    from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
    from temporalio.client import Client

    client = await asyncio.wait_for(
        Client.connect(config.temporal.address, namespace=config.temporal.namespace), timeout=10
    )
    info = await client.workflow_service.get_system_info(GetSystemInfoRequest(), timeout=timedelta(seconds=10))
    return f"server {info.server_version}" if info.server_version else "connected"
