"""
Retrieve folder summary from semantic index.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    construct_document_id,
    validate_folder_path,
    serialize_folder_summary_to_prose,
    DocumentNotFoundError,
    add_status_icon,
    lens_block,
)
from config import FOLDER_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


@tool
def get_folder_summary(repo_id: str, folder_path: str, include_lens: bool = False) -> str:
    """
    Retrieve semantic summary for a folder.
    
    Returns a prose description of the folder including its purpose,
    contained files with descriptions, and subfolders with descriptions.
    This is the primary navigation tool for exploring repository structure.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        folder_path: Path to folder relative to repo root
                     Use "." for repository root
                     Examples: ".", "src", "src/auth", "tools/navigation"
        include_lens: Also return the Temporal lens roll-up for the folder's whole subtree (durable-execution
                      signals, Temporal pack concepts, narrative). Set it for
                      questions about durability, failure handling, retries,
                      orchestration, waits, or Temporal; leave it off otherwise.
        
    Returns:
        Prose-formatted folder summary including files and subfolders
        
    Examples:
        - get_folder_summary("infra", ".") - Get root folder
        - get_folder_summary("infra", "docker") - Get docker folder
        - get_folder_summary("infra", "docker/open-webui") - Get nested folder
    """
    try:
        # Validate inputs
        is_valid, error = validate_folder_path(folder_path)
        if not is_valid:
            logger.warning(f"Invalid folder path: {folder_path} - {error}")
            raise ValueError(error)
        
        # Construct database and document ID
        database_name = construct_database_name(repo_id)
        document_id = construct_document_id(repo_id, "folder_summary", folder_path)
        
        logger.info(f"Retrieving folder summary: {document_id}")
        
        # Retrieve document from MongoDB
        client = get_mongo_client()
        collection = client[database_name][FOLDER_SUMMARIES_COLLECTION]
        
        document = collection.find_one({"_id": document_id})
        
        if not document:
            logger.warning(f"Folder summary not found: {document_id}")
            raise DocumentNotFoundError(document_id, FOLDER_SUMMARIES_COLLECTION, database_name)
        
        # Convert to prose format
        prose = serialize_folder_summary_to_prose(document)
        if include_lens:
            prose += lens_block(document)
        
        logger.info(f"Successfully retrieved folder summary for: {folder_path}")
        return prose
        
    except DocumentNotFoundError as e:
        # Provide helpful context for LLM
        error_msg = (
            f"Folder summary not found for path: `{folder_path}`\n\n"
            f"This could mean:\n"
            f"- The folder doesn't exist in the repository\n"
            f"- The path is incorrect (check spelling and format)\n"
            f"- The folder wasn't indexed\n\n"
        )
        
        # Suggest checking parent folder if this is a nested path
        if folder_path != "." and "/" in folder_path:
            parent_path = "/".join(folder_path.split("/")[:-1])
            if parent_path == "":
                parent_path = "."
            error_msg += f"💡 Try checking the parent folder: `{parent_path}`"
        elif folder_path != ".":
            error_msg += f"💡 Try checking the root folder: `.`"
        
        raise DocumentNotFoundError(e.document_id, e.collection, e.database) from e
        
    except ValueError as e:
        # Validation error - return formatted message
        return add_status_icon("error", f"Invalid folder path: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error retrieving folder summary: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")