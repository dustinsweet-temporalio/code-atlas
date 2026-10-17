"""
Local source code retrieval provider.

Reads source code from the directory where the indexer mirrored it
(``<SOURCE_DIR>/<repo_id>/<file_path>``). Falls back to user request tools only
if retrieval fails catastrophically.
"""
import logging
from pathlib import Path
from strands import tool
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

from config import SOURCE_DIR


class LocalSourceProvider:
    """
    Source provider that reads code from the local source store.

    Files are stored with path structure:
    {SOURCE_DIR}/{repo_id}/{file_path}

    Example:
    /data/source/infra/run.sh
    """

    def __init__(self):
        """Resolve the source directory."""
        self.root = Path(SOURCE_DIR).resolve()

    def get_full_file(self, repo_id: str, file_path: str) -> Tuple[bool, str]:
        """
        Retrieve complete file from the source store.

        Args:
            repo_id: Repository identifier (e.g., "infra")
            file_path: File path relative to repo root

        Returns:
            Tuple of (success, content)
            - success: True if retrieved successfully
            - content: File content or error message
        """
        repo_root = (self.root / repo_id).resolve()
        target = (repo_root / file_path).resolve()

        # Never read outside the repository's directory
        if repo_root.parent != self.root or repo_root not in target.parents:
            logger.warning(f"Rejected path outside repository: {repo_id}/{file_path}")
            return False, f"Invalid file path: {file_path}"

        try:
            logger.info(f"Reading source file: {target}")

            if not target.is_file():
                logger.warning(f"File not found in source store: {repo_id}/{file_path}")
                return False, f"File not found in source store: {file_path}"

            content = target.read_bytes().decode('utf-8', errors='replace')

            logger.info(f"Successfully read {len(content)} characters")

            # Format with markdown code fence
            formatted = self._format_file_content(file_path, content)

            return True, formatted

        except PermissionError:
            logger.error(f"Permission denied reading: {target}")
            return False, f"Permission denied reading file: {file_path}"

        except Exception as e:
            logger.error(f"Unexpected error reading source file: {str(e)}", exc_info=True)
            return False, f"Failed to retrieve file: {str(e)}"

    def get_file_lines(
        self, 
        repo_id: str, 
        file_path: str, 
        start_line: int, 
        end_line: int
    ) -> Tuple[bool, str]:
        """
        Retrieve specific line range from a file in the source store.
        
        Args:
            repo_id: Repository identifier
            file_path: File path relative to repo root
            start_line: Starting line number (1-indexed, inclusive)
            end_line: Ending line number (1-indexed, inclusive)
            
        Returns:
            Tuple of (success, content)
            - success: True if retrieved successfully
            - content: Line range content with line numbers or error message
        """
        # First get the full file
        success, content = self.get_full_file(repo_id, file_path)
        
        if not success:
            return False, content  # Return error message
        
        try:
            # Extract markdown code fence content
            lines = self._extract_lines_from_formatted(content)
            
            # Validate line range
            total_lines = len(lines)
            
            if start_line < 1 or start_line > total_lines:
                logger.warning(f"Start line {start_line} out of range (1-{total_lines})")
                return False, f"Start line {start_line} out of range. File has {total_lines} lines."
            
            if end_line < start_line or end_line > total_lines:
                logger.warning(f"End line {end_line} out of range (1-{total_lines})")
                return False, f"End line {end_line} out of range. File has {total_lines} lines."
            
            # Extract requested lines (1-indexed, inclusive)
            selected_lines = lines[start_line-1:end_line]
            
            # Format with line numbers
            formatted = self._format_line_range(
                file_path, 
                selected_lines, 
                start_line
            )
            
            logger.info(f"Extracted lines {start_line}-{end_line} from {file_path}")
            
            return True, formatted
            
        except Exception as e:
            logger.error(f"Error extracting line range: {str(e)}", exc_info=True)
            return False, f"Failed to extract line range: {str(e)}"
    
    def _format_file_content(self, file_path: str, content: str) -> str:
        """
        Format file content with markdown code fence.
        
        Args:
            file_path: File path for language detection
            content: Raw file content
            
        Returns:
            Formatted content with code fence
        """
        # Detect language from file extension
        lang = self._detect_language(file_path)
        
        return f"**File: `{file_path}`**\n\n```{lang}\n{content}\n```"
    
    def _format_line_range(
        self, 
        file_path: str, 
        lines: list, 
        start_line: int
    ) -> str:
        """
        Format line range with line numbers.
        
        Args:
            file_path: File path for language detection
            lines: List of line strings
            start_line: Starting line number
            
        Returns:
            Formatted content with line numbers
        """
        lang = self._detect_language(file_path)
        
        # Add line numbers
        numbered_lines = [
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(lines)
        ]
        
        numbered_content = "\n".join(numbered_lines)
        
        return (
            f"**File: `{file_path}` (lines {start_line}-{start_line + len(lines) - 1})**\n\n"
            f"```{lang}\n{numbered_content}\n```"
        )
    
    def _extract_lines_from_formatted(self, formatted_content: str) -> list:
        """
        Extract raw lines from formatted markdown content.
        
        Args:
            formatted_content: Content with markdown code fence
            
        Returns:
            List of raw line strings
        """
        # Split by newlines
        lines = formatted_content.split('\n')
        
        # Find code fence boundaries
        fence_start = None
        fence_end = None
        
        for i, line in enumerate(lines):
            if line.startswith('```'):
                if fence_start is None:
                    fence_start = i
                else:
                    fence_end = i
                    break
        
        if fence_start is None or fence_end is None:
            # No code fence found, return all lines
            return lines
        
        # Return lines between fences
        return lines[fence_start + 1:fence_end]
    
    def _detect_language(self, file_path: str) -> str:
        """
        Detect programming language from file extension.
        
        Args:
            file_path: File path
            
        Returns:
            Language identifier for syntax highlighting
        """
        ext_map = {
            '.py': 'python',
            '.java': 'java',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.jsx': 'jsx',
            '.tsx': 'tsx',
            '.go': 'go',
            '.rs': 'rust',
            '.rb': 'ruby',
            '.php': 'php',
            '.c': 'c',
            '.cpp': 'cpp',
            '.h': 'c',
            '.hpp': 'cpp',
            '.cs': 'csharp',
            '.sh': 'bash',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.json': 'json',
            '.xml': 'xml',
            '.html': 'html',
            '.css': 'css',
            '.sql': 'sql',
            '.md': 'markdown',
            '.dockerfile': 'dockerfile',
            '.Dockerfile': 'dockerfile',
        }
        
        # Extract extension
        for ext, lang in ext_map.items():
            if file_path.endswith(ext):
                return lang
        
        # Special case: Dockerfile (no extension)
        if file_path.endswith('Dockerfile'):
            return 'dockerfile'
        
        return ''  # No language specified


# Tool functions using LocalSourceProvider
@tool
def get_file_source(repo_id: str, file_path: str) -> str:
    """
    Retrieve complete source code for a file from the source store.
    
    This is the PRIMARY method for retrieving source code.
    Falls back to request_full_file_source() only if the source store is unavailable.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        file_path: Path to file relative to repository root
                   Examples: "README.MD", "run.sh", "scripts/setup.sh"
        
    Returns:
        File source code with markdown formatting, or error message with fallback suggestion
        
    Examples:
        - get_file_source("infra", "README.MD")
        - get_file_source("infra", "run.sh")
    """
    from utils import validate_file_path, add_status_icon
    
    try:
        # Validate file path
        is_valid, error = validate_file_path(file_path)
        if not is_valid:
            logger.warning(f"Invalid file path: {file_path} - {error}")
            return add_status_icon("error", f"Invalid file path: {error}")
        
        # Attempt source store retrieval
        provider = LocalSourceProvider()
        success, content = provider.get_full_file(repo_id, file_path)
        
        if success:
            return content
        else:
            # Retrieval failed - suggest fallback ONLY if catastrophic
            logger.error(f"Source retrieval failed for {file_path}: {content}")
            
            fallback_msg = (
                f"⚠️ **Source retrieval failed**\n\n"
                f"**File:** `{file_path}`\n"
                f"**Error:** {content}\n\n"
                f"💡 **This should almost never happen.** If the source store is unavailable:\n"
                f"Use `request_full_file_source('{file_path}')` to ask the user to provide the file."
            )
            
            return fallback_msg
            
    except Exception as e:
        logger.error(f"Error in get_file_source: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")

@tool
def get_file_source_lines(
    repo_id: str, 
    file_path: str, 
    start_line: int, 
    end_line: int
) -> str:
    """
    Retrieve specific line range from a file in the source store.
    
    This is the PRIMARY method for retrieving line ranges.
    Falls back to request_file_source_lines() only if the source store is unavailable.
    
    **PREFERRED** over get_file_source() for cost efficiency.
    
    **Smart Widening Strategy:**
    If the line range is insufficient:
    1. Widen the range by ~20 lines on each side
    2. If still insufficient, request the full file
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        file_path: Path to file relative to repository root
        start_line: Starting line number (1-indexed, inclusive)
        end_line: Ending line number (1-indexed, inclusive)
        
    Returns:
        File source code lines with line numbers, or error message with fallback suggestion
        
    Examples:
        - get_file_source_lines("infra", "run.sh", 23, 45)
        - get_file_source_lines("infra", "scripts/setup.sh", 1, 30)
    """
    from utils import validate_file_path, validate_line_range, add_status_icon
    
    try:
        # Validate inputs
        is_valid, error = validate_file_path(file_path)
        if not is_valid:
            logger.warning(f"Invalid file path: {file_path} - {error}")
            return add_status_icon("error", f"Invalid file path: {error}")
        
        is_valid, error = validate_line_range(start_line, end_line)
        if not is_valid:
            logger.warning(f"Invalid line range: {start_line}-{end_line} - {error}")
            return add_status_icon("error", f"Invalid line range: {error}")
        
        # Attempt source store retrieval
        provider = LocalSourceProvider()
        success, content = provider.get_file_lines(repo_id, file_path, start_line, end_line)
        
        if success:
            return content
        else:
            # Retrieval failed - suggest fallback ONLY if catastrophic
            logger.error(f"Source line range retrieval failed for {file_path}: {content}")
            
            fallback_msg = (
                f"⚠️ **Source retrieval failed**\n\n"
                f"**File:** `{file_path}`\n"
                f"**Lines:** {start_line}-{end_line}\n"
                f"**Error:** {content}\n\n"
                f"💡 **This should almost never happen.** If the source store is unavailable:\n"
                f"Use `request_file_source_lines('{file_path}', {start_line}, {end_line})` "
                f"to ask the user to provide these lines."
            )
            
            return fallback_msg
            
    except Exception as e:
        logger.error(f"Error in get_file_source_lines: {str(e)}", exc_info=True)
        return add_status_icon("error", f"Unexpected error: {str(e)}")
