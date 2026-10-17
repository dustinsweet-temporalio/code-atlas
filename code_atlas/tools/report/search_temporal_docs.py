"""
Search the Temporal documentation (Temporal Docs MCP).
"""
import logging

from strands import tool

from semantic_ingress.lens.repo_pass import _format_chunks
from utils.temporal_docs import search

logger = logging.getLogger(__name__)


@tool
def search_temporal_docs(query: str) -> str:
    """
    Search the official Temporal documentation and knowledge sources.

    Use it to back statements about Temporal features, patterns and limits
    (workflows, activities, signals, timers, retries, schedules, versioning, ...)
    with the docs, and cite the source URLs it returns.

    Args:
        query: A complete natural-language question, e.g.
               "How do Temporal activity retry policies work?"

    Returns:
        The most relevant passages, each with its source URL
    """
    try:
        chunks = search(query)
    except Exception as e:
        logger.warning(f"Temporal docs search failed: {e}")
        return (f"The Temporal Docs search is unavailable ({e}). Tell the user, and say that any Temporal "
                "statement you make now is not backed by the docs.")
    logger.info(f"Temporal docs search: {query!r} -> {len(chunks)} passages")
    return (_format_chunks(chunks) + "\n\nWhen you rely on a passage, cite its SOURCE URL as a Markdown link "
            "next to the statement it supports.")
