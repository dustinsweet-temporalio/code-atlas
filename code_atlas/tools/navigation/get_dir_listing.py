"""
Retrieve directory listing from semantic index.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    construct_document_id,
    serialize_dir_listing_to_prose,
    DocumentNotFoundError,
    add_status_icon,
)
from config import DIR_LIST_COLLECTION

logger = logging.getLogger(__name__)


@tool
def get_dir_listing(repo_id: str) -> str:
    """
    Retrieve complete directory listing for a repository.
    
    Returns a newline-separated list of ALL files in the repository.
    This is extremely useful when the user provides an exact filename -
    you can quickly find the full path without navigating folder structure.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        
    Returns:
        Newline-separated list of all file paths in the repository
        
    Examples:
        - get_dir_listing("infra")
          Returns: ".env\n.gitignore\nrun.sh\nscripts/setup.sh\n..."
        
    Use Cases:
        - User says: "Show me run.sh" → get dir listing → find "run.sh" → get file summary/source
        - User says: "Where is the Docker configuration?" → get dir listing → search for "Dockerfile"
        - Quick file path lookup without navigating folders
    """
    try:
        # Construct database and document ID
        database_name = construct_database_name(repo_id)
        document_id = construct_document_id(repo_id, "dir_list", "")
        
        logger.info(f"Retrieving directory listing: {document_id}")
        
        # Retrieve document from MongoDB
        client = get_mongo_client()
        collection = client[database_name][DIR_LIST_COLLECTION]
        
        document = collection.find_one({"_id": document_id})
        
        if not document:
            logger.warning(f"Directory listing not found: {document_id}")
            raise DocumentNotFoundError(document_id, DIR_LIST_COLLECTION, database_name)
        
        # Convert to prose format (just returns the prose_summary field)
        prose = serialize_dir_listing_to_prose(document)
        
        logger.info(f"Successfully retrieved directory listing for: {repo_id}")
        return prose
        
    except DocumentNotFoundError as e:
        # Provide helpful context for LLM
        error_msg = (
            f"Directory listing not found for repository: `{repo_id}`\n\n"
            f"This could mean:\n"
            f"- The repository hasn't been fully indexed\n"
            f"- The directory listing wasn't generated during indexing\n"
            f"- The repository ID is incorrect\n\n"
            f"💡 Try using folder navigation instead:\n"
            f"- `get_folder_summary('{repo_id}', '.')` to see the root folder\n"
            f"- Navigate through folders to find the file you're looking for"
        )
        raise DocumentNotFoundError(e.document_id, e.collection, e.database) from e
        
    except Exception as e:
        logger.error(f"Error retrieving directory listing: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")
