"""
Retrieve file summary from semantic index.
"""
import logging
from strands import tool
from utils import (
    get_mongo_client,
    construct_database_name,
    construct_document_id,
    validate_file_path,
    serialize_file_summary_to_prose,
    DocumentNotFoundError,
    add_status_icon,
    lens_block,
)
from config import FILE_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


@tool
def get_file_summary(repo_id: str, file_path: str, include_lens: bool = False) -> str:
    """
    Retrieve semantic summary for a file.
    
    Returns a prose description of the file including its role, imports,
    public interface, semantic regions with line ranges, and metadata.
    Use this to understand what a file does WITHOUT seeing the actual source code.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        file_path: Path to file relative to repo root
                   Examples: "README.md", "src/main.py", "auth/login.py"
        include_lens: Also return the Temporal lens record (durable-execution
                      signals, Temporal pack concepts, narrative). Set it for
                      questions about durability, failure handling, retries,
                      orchestration, waits, or Temporal; leave it off otherwise.
        
    Returns:
        Prose-formatted file summary including role, interface, regions, and metadata
        
    Examples:
        - get_file_summary("infra", "README.md")
        - get_file_summary("infra", "docker/open-webui/index.html")
        - get_file_summary("infra", "tools/navigation/get_file_summary.py")
        
    Note:
        File summaries include line ranges for semantic regions. Use these ranges
        with request_file_source_lines() to retrieve specific sections of source code.
    """
    try:
        # Validate inputs
        is_valid, error = validate_file_path(file_path)
        if not is_valid:
            logger.warning(f"Invalid file path: {file_path} - {error}")
            raise ValueError(error)
        
        # Construct database and document ID
        database_name = construct_database_name(repo_id)
        document_id = construct_document_id(repo_id, "file_summary", file_path)
        
        logger.info(f"Retrieving file summary: {document_id}")
        
        # Retrieve document from MongoDB
        client = get_mongo_client()
        collection = client[database_name][FILE_SUMMARIES_COLLECTION]
        
        document = collection.find_one({"_id": document_id})
        
        if not document:
            logger.warning(f"File summary not found: {document_id}")
            raise DocumentNotFoundError(document_id, FILE_SUMMARIES_COLLECTION, database_name)
        
        # Convert to prose format
        prose = serialize_file_summary_to_prose(document)
        if include_lens:
            prose += lens_block(document)
        
        logger.info(f"Successfully retrieved file summary for: {file_path}")
        return prose
        
    except DocumentNotFoundError as e:
        # Provide helpful context for LLM
        error_msg = (
            f"File summary not found for path: `{file_path}`\n\n"
            f"This could mean:\n"
            f"- The file doesn't exist in the repository\n"
            f"- The path is incorrect (check spelling and format)\n"
            f"- The file wasn't indexed (generated files, binaries, etc.)\n\n"
        )
        
        # Suggest checking the parent folder to see what files are available
        if "/" in file_path:
            folder_path = "/".join(file_path.split("/")[:-1])
            if folder_path == "":
                folder_path = "."
            error_msg += f"💡 Try listing files in the folder: `{folder_path}` using get_folder_summary()"
        else:
            error_msg += f"💡 Try listing files in the root folder using get_folder_summary('.') "
        
        raise DocumentNotFoundError(e.document_id, e.collection, e.database) from e
        
    except ValueError as e:
        # Validation error - return formatted message
        return add_status_icon("error", f"Invalid file path: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error retrieving file summary: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")