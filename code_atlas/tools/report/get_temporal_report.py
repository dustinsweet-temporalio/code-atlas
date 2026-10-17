"""
Read the Temporal report stored with an index.
"""
import logging

from strands import tool

from config import INDEX_META_COLLECTION, REPORTS_COLLECTION
from utils import construct_database_name, get_mongo_client, validate_repo_id
from utils.formatters.report import report_evaluation, report_overview, report_process

logger = logging.getLogger(__name__)


def load_report(repo_id: str) -> tuple[dict | None, str | None]:
    """(stored report document or None, the index's current commit or None)."""
    db = get_mongo_client()[construct_database_name(repo_id)]
    doc = db[REPORTS_COLLECTION].find_one({"_id": f"{repo_id}::temporal_report"})
    meta = db[INDEX_META_COLLECTION].find_one({}) or {}
    return doc, meta.get("commit_sha")


@tool
def get_temporal_report(repo_id: str, view: str = "overview", process: str = "") -> str:
    """
    Read the Temporal report stored with the index: which processes in the
    repository suit Temporal (durable execution) and why, built from facts
    checked against the source.

    Use it first for questions about Temporal suitability, durable execution,
    or how a part of the system would look on Temporal.

    Args:
        repo_id: Repository identifier
        view: "overview" (verdicts per process, share of work on Temporal, gates),
              "process" (one process: how it runs today with line references,
              what a crash loses, scores, and the design or why it is not a fit),
              or "evaluation" (findings and fixes for existing Temporal code)
        process: For view="process": the process name (or a distinctive part of it)

    Returns:
        The requested part of the report, as text
    """
    is_valid, error = validate_repo_id(repo_id)
    if not is_valid:
        raise ValueError(error)
    doc, index_commit = load_report(repo_id)
    if not doc or not doc.get("data"):
        reason = f" The last attempt failed: {doc.get('error')}." if doc and doc.get("error") else ""
        return (f"No Temporal report is stored for {repo_id}.{reason} It is built when the repository is "
                f"indexed with the Temporal lens on, or with ./scripts/report.sh {repo_id}.")
    logger.info(f"Temporal report for {repo_id}: view={view} process={process!r}")
    if view == "process":
        if not process:
            return "Give the process name for view=\"process\".\n\n" + report_overview(doc, index_commit)
        return report_process(doc, process)
    if view == "evaluation":
        return report_evaluation(doc)
    return report_overview(doc, index_commit)
