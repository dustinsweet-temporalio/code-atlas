"""
Request specific line range from a file.
"""
import logging
from strands import tool
from utils import validate_file_path, validate_line_range, add_status_icon
from .source_provider import get_source_provider

logger = logging.getLogger(__name__)


@tool
def request_file_source_lines(file_path: str, start_line: int, end_line: int) -> str:
    """
    Request a specific line range from a file's source code.
    
    **IMPORTANT**: This tool does NOT return source code directly. It returns
    an instruction message asking the USER to provide the lines. You must wait
    for the user to paste the code in their next message before continuing.
    
    **PREFERRED** over request_full_file_source() for cost efficiency.
    
    Use file summaries to find semantic regions with line ranges, then request
    only those specific sections. This dramatically reduces token consumption.
    
    Args:
        file_path: Path to file relative to repository root
        start_line: Starting line number (1-indexed, inclusive)
        end_line: Ending line number (1-indexed, inclusive)
        
    Returns:
        Instruction message for the user to provide source code lines
        
    Examples:
        - request_file_source_lines("auth/login.py", 23, 45)
        - request_file_source_lines("src/database.py", 100, 150)
        - request_file_source_lines("utils/validator.py", 1, 30)
        
    Best Practice:
        1. Use get_file_summary() to see semantic regions
        2. Identify the region containing relevant code
        3. Request only those lines using this tool
        4. Avoid requesting entire files when only a section is needed
        
    Line Range Limit:
        Maximum 1000 lines per request to prevent context window overflow.
    """
    try:
        # Validate file path
        is_valid, error = validate_file_path(file_path)
        if not is_valid:
            logger.warning(f"Invalid file path for source request: {file_path} - {error}")
            return add_status_icon("error", f"Invalid file path: {error}")
        
        # Validate line range
        is_valid, error = validate_line_range(start_line, end_line)
        if not is_valid:
            logger.warning(f"Invalid line range for {file_path}: {start_line}-{end_line} - {error}")
            return add_status_icon("error", f"Invalid line range: {error}")
        
        logger.info(f"Requesting lines {start_line}-{end_line} from file: {file_path}")
        
        # Get source provider and delegate
        provider = get_source_provider()
        return provider.request_file_lines(file_path, start_line, end_line)
        
    except Exception as e:
        logger.error(f"Error requesting file source lines: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Failed to request source lines: {str(e)}")