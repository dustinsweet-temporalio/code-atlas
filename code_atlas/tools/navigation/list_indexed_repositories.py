"""
List all repositories with semantic indexes.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    add_status_icon,
    describe_lens_info,
    index_lens_info,
)
from config import (
    DB_PREFIX,
    FILE_SUMMARIES_COLLECTION,
    FOLDER_SUMMARIES_COLLECTION,
    REPO_SUMMARIES_COLLECTION,
)

logger = logging.getLogger(__name__)


@tool
def list_indexed_repositories() -> str:
    """
    List all repositories with semantic indexes in MongoDB.

    Returns a formatted list showing:
    - Repository ID (e.g., "infra")
    - Number of indexed files
    - Number of indexed folders
    - Commit SHA (if available)
    - Repository purpose (if available)
    - Temporal lens state and knowledge pack version

    Use this tool when:
    - User hasn't specified which repository to explore
    - You need to verify repository availability
    - User asks "what repositories are available?"

    Returns:
        Formatted list of repositories with metadata
    """
    try:
        client = get_mongo_client()
        all_dbs = client.list_database_names()
        
        # Filter for semantic index databases
        semantic_dbs = [db for db in all_dbs if db.startswith(f"{DB_PREFIX}_")]
        
        if not semantic_dbs:
            return add_status_icon("info", "No indexed repositories found.")
        
        logger.info(f"Found {len(semantic_dbs)} semantic databases")
        
        repos = []
        for db_name in semantic_dbs:
            try:
                db = client[db_name]
                
                # Get actual repo_id from documents (handles namespaced IDs correctly)
                repo_id = None
                commit_sha = None
                purpose = None
                
                # Repository summary document (the repo_summaries collection also holds
                # the directory listing, so select by document type)
                repo_summary = db[REPO_SUMMARIES_COLLECTION].find_one({"document_type": "repo_summary"})
                if repo_summary:
                    repo_id = repo_summary.get("repo_id")
                    commit_sha = repo_summary.get("commit_sha")
                    # First paragraph of the prose summary is the purpose statement
                    prose = (repo_summary.get("prose_summary") or "").strip()
                    purpose = prose.split("\n\n")[0].split("\n")[0] if prose else ""
                    if purpose and len(purpose) > 150:
                        purpose = purpose[:150] + "..."
                
                # Fall back to parsing from file/folder summary IDs
                if not repo_id:
                    sample = db[FILE_SUMMARIES_COLLECTION].find_one({})
                    if not sample:
                        sample = db[FOLDER_SUMMARIES_COLLECTION].find_one({})
                    
                    if sample:
                        # Parse repo_id from _id format: "infra::type::path"
                        if "repo_id" in sample:
                            repo_id = sample["repo_id"]
                        elif "_id" in sample:
                            repo_id = sample["_id"].split("::")[0]
                        
                        # Try to get commit_sha from sample if not from repo_summary
                        if not commit_sha:
                            commit_sha = sample.get("commit_sha")
                
                # Last resort: derive from database name
                if not repo_id:
                    repo_id = db_name[len(DB_PREFIX) + 1:]
                    logger.warning(f"Could not find repo_id in documents for {db_name}, using derived: {repo_id}")
                
                # Get counts
                file_count = db[FILE_SUMMARIES_COLLECTION].count_documents({})
                folder_count = db[FOLDER_SUMMARIES_COLLECTION].count_documents({})
                
                repos.append({
                    "lens": describe_lens_info(index_lens_info(db)),
                    "repo_id": repo_id,
                    "database": db_name,
                    "file_count": file_count,
                    "folder_count": folder_count,
                    "commit_sha": commit_sha,
                    "purpose": purpose,
                })
                
            except Exception as e:
                logger.error(f"Error processing database {db_name}: {e}")
                continue
        
        if not repos:
            return add_status_icon("warning", "Found databases but could not extract repository information.")
        
        return format_repository_list(repos)
        
    except Exception as e:
        logger.error(f"Error listing repositories: {e}", exc_info=True)
        return add_status_icon("error", f"Failed to list repositories: {e}")


def format_repository_list(repos: list) -> str:
    """
    Format repository list with optional purpose summaries.
    
    Args:
        repos: List of repository metadata dicts
        
    Returns:
        Formatted markdown string
    """
    if not repos:
        return add_status_icon("info", "No repositories found.")
    
    parts = [f"✅ **Found {len(repos)} indexed repository(ies):**\n"]
    
    for i, repo in enumerate(repos, 1):
        repo_id = repo.get("repo_id", "unknown")
        file_count = repo.get("file_count", 0)
        folder_count = repo.get("folder_count", 0)
        commit_sha = repo.get("commit_sha")
        purpose = repo.get("purpose")
        
        parts.append(f"\n{i}. **{repo_id}**")
        
        # Show purpose if available
        if purpose:
            parts.append(f"\n   📋 {purpose}")
        
        parts.append(f"\n   📄 {file_count:,} files indexed")
        parts.append(f"\n   📁 {folder_count:,} folders indexed")
        
        if commit_sha:
            commit_short = commit_sha[:8] if len(commit_sha) > 8 else commit_sha
            parts.append(f"\n   🔖 Commit: `{commit_short}`")
        if repo.get("lens"):
            parts.append(f"\n   🧭 {repo['lens']}")
    
    parts.append("\n\n💡 **Choose a repository to explore!**")
    
    return "".join(parts)
