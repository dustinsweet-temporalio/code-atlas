"""
Text search across semantic summaries.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    validate_search_query,
    validate_search_limit,
    format_search_results,
    add_status_icon,
)
from config import FILE_SUMMARIES_COLLECTION, FOLDER_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


@tool
def text_search_summaries(repo_id: str, query: str, limit: int = 10) -> str:
    """
    Search for files and folders by text query across semantic summaries.
    
    This is a FALLBACK tool - prefer deliberate navigation using get_folder_summary()
    when possible. Use this when you need to quickly jump to relevant areas of the
    codebase without knowing the exact structure.
    
    Searches across:
    - File roles, imports, public interface names/descriptions, notes
    - Folder summaries, file descriptions, subfolder descriptions
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        query: Search query string (e.g., "authentication", "database connection")
        limit: Maximum number of results to return (default: 10, max: 20)
        
    Returns:
        List of matching document IDs with metadata (type, path)
        Use get_folder_summary() or get_file_summary() to retrieve full details
        
    Examples:
        - text_search_summaries("infra", "authentication")
        - text_search_summaries("infra", "docker configuration")
        - text_search_summaries("infra", "JWT token validation")
        
    Note:
        This returns document IDs and metadata only. You must retrieve the actual
        summaries using get_folder_summary() or get_file_summary() afterward.
    """
    try:
        # Validate inputs
        is_valid, error = validate_search_query(query)
        if not is_valid:
            logger.warning(f"Invalid search query: {query} - {error}")
            return add_status_icon("error", f"Invalid search query: {error}")
        
        normalized_limit, warning = validate_search_limit(limit)
        if warning:
            logger.info(warning)
        
        # Construct database name
        database_name = construct_database_name(repo_id)
        
        logger.info(f"Searching summaries in {database_name} for: '{query}' (limit: {normalized_limit})")
        
        client = get_mongo_client()
        db = client[database_name]
        
        results = []
        
        # Search file summaries
        file_collection = db[FILE_SUMMARIES_COLLECTION]
        file_results = file_collection.find(
            {"$text": {"$search": query}},
            {"_id": 1, "path": 1, "document_type": 1, "score": {"$meta": "textScore"}}
        ).sort([("score", {"$meta": "textScore"})]).limit(normalized_limit)
        
        for doc in file_results:
            results.append({
                "document_id": doc["_id"],
                "document_type": doc.get("document_type", "file_summary"),
                "path": doc.get("path", "unknown"),
                "score": doc.get("score", 0)
            })
        
        # Search folder summaries
        folder_collection = db[FOLDER_SUMMARIES_COLLECTION]
        folder_results = folder_collection.find(
            {"$text": {"$search": query}},
            {"_id": 1, "path": 1, "document_type": 1, "score": {"$meta": "textScore"}}
        ).sort([("score", {"$meta": "textScore"})]).limit(normalized_limit)
        
        for doc in folder_results:
            results.append({
                "document_id": doc["_id"],
                "document_type": doc.get("document_type", "folder_summary"),
                "path": doc.get("path", "unknown"),
                "score": doc.get("score", 0)
            })
        
        # Sort combined results by score and limit
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        results = results[:normalized_limit]
        
        # Remove score from output (internal only)
        for result in results:
            result.pop("score", None)
        
        logger.info(f"Search found {len(results)} results for query: '{query}'")
        
        # Format results for LLM
        formatted = format_search_results(results)
        
        if warning:
            formatted = add_status_icon("warning", warning) + "\n\n" + formatted
        
        return formatted
        
    except Exception as e:
        logger.error(f"Error performing text search: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Search failed: {str(e)}")