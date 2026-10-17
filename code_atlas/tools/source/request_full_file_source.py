"""
Request complete source code for a file.
"""
import logging
from strands import tool
from utils import validate_file_path, add_status_icon
from .source_provider import get_source_provider

logger = logging.getLogger(__name__)


@tool
def request_full_file_source(file_path: str) -> str:
    """
    Request the complete source code for a file.
    
    **IMPORTANT**: This tool does NOT return source code directly. It returns
    an instruction message asking the USER to provide the code. You must wait
    for the user to paste the code in their next message before continuing.
    
    Use this when:
    - The file is small (check file size metadata from get_file_summary first)
    - You need to see the entire implementation
    - Line ranges aren't sufficient
    
    PREFER request_file_source_lines() when possible to reduce token usage.
    
    Args:
        file_path: Path to file relative to repository root
                   Examples: "README.md", "src/main.py", "auth/login.py"
        
    Returns:
        Instruction message for the user to provide source code
        
    Examples:
        - request_full_file_source("README.md")
        - request_full_file_source("src/config.py")
        - request_full_file_source("auth/token_validator.py")
        
    Cost Awareness:
        Check the file size from get_file_summary() metadata before requesting.
        Large files (>500 lines) consume significant tokens. Consider using
        request_file_source_lines() to get only the relevant sections.
    """
    try:
        # Validate file path
        is_valid, error = validate_file_path(file_path)
        if not is_valid:
            logger.warning(f"Invalid file path for source request: {file_path} - {error}")
            return add_status_icon("error", f"Invalid file path: {error}")
        
        logger.info(f"Requesting full source for file: {file_path}")
        
        # Get source provider and delegate
        provider = get_source_provider()
        return provider.request_full_file(file_path)
        
    except Exception as e:
        logger.error(f"Error requesting full file source: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Failed to request source: {str(e)}")