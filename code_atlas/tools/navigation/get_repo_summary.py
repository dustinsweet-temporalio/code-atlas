"""
Retrieve repository summary from semantic index.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    validate_repo_id,
    serialize_repo_summary_to_prose,
    DocumentNotFoundError,
    add_status_icon,
    index_lens_info,
    repo_lens_block,
)
from config import REPO_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


@tool
def get_repo_summary(repo_id: str, include_lens: bool = False) -> str:
    """
    Retrieve high-level overview of repository purpose and architecture.
    
    This is an OPTIONAL first step in navigation that provides:
    - Overall purpose and goals of the codebase
    - Major functional areas and responsibilities
    - Key architectural patterns and technologies
    - Important system-wide notes
    
    Use this to understand WHAT the repository does before navigating
    into specific folders and files. This helps you make better decisions
    about where to explore.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        include_lens: Also return the Temporal lens for the repository: Temporal
                      status, detected technologies, durable processes worth
                      assessing, gates, and the knowledge pack version. Set it for
                      questions about durability, orchestration, or Temporal.
        
    Returns:
        Prose-formatted repository summary
        
    Examples:
        - get_repo_summary("infra")
    
    Note:
        Not all repositories have summary documents. If missing, 
        fall back to exploring the root folder with get_folder_summary().
    """
    try:
        # Validate repo_id format
        is_valid, error = validate_repo_id(repo_id)
        if not is_valid:
            logger.warning(f"Invalid repo_id: {repo_id} - {error}")
            raise ValueError(error)
        
        # Construct database name
        database_name = construct_database_name(repo_id)
        
        # Construct document ID (empty path for repo-level)
        document_id = f"{repo_id}::repo_summary::"
        
        logger.info(f"Retrieving repo summary: {document_id}")
        
        # Retrieve document from MongoDB
        client = get_mongo_client()
        collection = client[database_name][REPO_SUMMARIES_COLLECTION]
        
        document = collection.find_one({"_id": document_id})
        
        if not document:
            logger.warning(f"Repo summary not found: {document_id}")
            raise DocumentNotFoundError(document_id, REPO_SUMMARIES_COLLECTION, database_name)
        
        # Convert to prose format
        prose = serialize_repo_summary_to_prose(document)
        if include_lens:
            prose += repo_lens_block(document)
            info = index_lens_info(client[database_name])
            if info["lens"] in ("partial", "mixed"):
                prose += f"\n- Index note: lens is {info['lens']} (pack {' + '.join(v['version'] for v in info['pack_versions'])})"
        
        logger.info(f"Successfully retrieved repo summary for: {repo_id}")
        return prose
        
    except DocumentNotFoundError as e:
        # Provide helpful context - this is expected for some repos
        error_msg = (
            f"Repository summary not found for: `{repo_id}`\n\n"
            f"This is normal - not all repositories have summary documents yet. "
            f"You can still explore this repository by:\n\n"
            f"1. Using `get_folder_summary('{repo_id}', '.')` to see the root folder\n"
            f"2. Navigating through folders and files normally\n\n"
            f"💡 The folder-based navigation will give you the same insights, "
            f"just starting from the directory structure instead of an overview."
        )
        return add_status_icon("info", error_msg)
        
    except ValueError as e:
        # Validation error
        return add_status_icon("error", f"Invalid repository ID: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error retrieving repo summary: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")
