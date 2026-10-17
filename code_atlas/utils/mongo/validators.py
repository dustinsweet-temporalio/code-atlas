"""
Input validation helpers for MongoDB operations.
"""
import re
from typing import Tuple, Optional


def validate_repo_id(repo_id: str) -> Tuple[bool, Optional[str]]:
    """
    Validate repository ID format.
    
    Args:
        repo_id: Repository identifier (e.g., "infra", "infra")
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not repo_id:
        return False, "Repository ID cannot be empty"
    
    if not isinstance(repo_id, str):
        return False, "Repository ID must be a string"
    
    # Allow alphanumeric, hyphens, underscores (NO forward slashes for namespaces)
    if not re.match(r'^[a-zA-Z0-9_-]+$', repo_id):
        return False, "Repository ID can only contain letters, numbers, hyphens, and underscores"
    
    if len(repo_id) > 100:
        return False, "Repository ID cannot exceed 100 characters"
    
    return True, None


def validate_database_name(database_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate database name format.
    
    Args:
        database_name: Database name (e.g., "semv2_infra")
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not database_name:
        return False, "Database name cannot be empty"
    
    if not isinstance(database_name, str):
        return False, "Database name must be a string"
    
    # MongoDB database name restrictions
    if len(database_name) > 64:
        return False, "Database name cannot exceed 64 characters"
    
    # Check for invalid characters: /\. "$*<>:|?
    invalid_chars = r'[/\\. "$*<>:|?]'
    if re.search(invalid_chars, database_name):
        return False, f"Database name contains invalid characters"
    
    return True, None


def validate_collection_name(collection_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate collection name format.
    
    Args:
        collection_name: Collection name (e.g., "file_summaries")
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not collection_name:
        return False, "Collection name cannot be empty"
    
    if not isinstance(collection_name, str):
        return False, "Collection name must be a string"
    
    # MongoDB collection name restrictions
    if collection_name.startswith("system."):
        return False, "Collection name cannot start with 'system.'"
    
    if '$' in collection_name:
        return False, "Collection name cannot contain '$'"
    
    if len(collection_name) > 255:
        return False, "Collection name cannot exceed 255 characters"
    
    return True, None


def validate_file_path(file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Validate file path format.
    
    Args:
        file_path: File path relative to repo root (e.g., "src/main.py")
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not file_path:
        return False, "File path cannot be empty"
    
    if not isinstance(file_path, str):
        return False, "File path must be a string"
    
    # Disallow absolute paths
    if file_path.startswith('/'):
        return False, "File path must be relative to repository root (no leading slash)"
    
    # Disallow parent directory references
    if '..' in file_path:
        return False, "File path cannot contain '..' (parent directory references)"
    
    # Disallow path traversal attempts
    if file_path.startswith('.'):
        return False, "File path cannot start with '.'"
    
    return True, None


def validate_folder_path(folder_path: str) -> Tuple[bool, Optional[str]]:
    """
    Validate folder path format.
    
    Args:
        folder_path: Folder path relative to repo root (e.g., "src" or ".")
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if folder_path is None or folder_path == "":
        return False, "Folder path cannot be empty (use '.' for root)"
    
    if not isinstance(folder_path, str):
        return False, "Folder path must be a string"
    
    # Allow "." for root
    if folder_path == ".":
        return True, None
    
    # Disallow absolute paths
    if folder_path.startswith('/'):
        return False, "Folder path must be relative to repository root (no leading slash)"
    
    # Disallow parent directory references
    if '..' in folder_path:
        return False, "Folder path cannot contain '..' (parent directory references)"
    
    # Disallow starting with '.' except for root
    if folder_path.startswith('.') and folder_path != '.':
        return False, "Folder path cannot start with '.' (use '.' for root only)"
    
    return True, None


def validate_line_range(start_line: int, end_line: int) -> Tuple[bool, Optional[str]]:
    """
    Validate source code line range.
    
    Args:
        start_line: Starting line number (1-indexed)
        end_line: Ending line number (inclusive)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return False, "Line numbers must be integers"
    
    if start_line < 1:
        return False, "Start line must be >= 1"
    
    if end_line < start_line:
        return False, f"End line ({end_line}) must be >= start line ({start_line})"
    
    if end_line - start_line > 1000:
        return False, "Line range cannot exceed 1000 lines (too large for context window)"
    
    return True, None


def validate_search_query(query: str) -> Tuple[bool, Optional[str]]:
    """
    Validate text search query.
    
    Args:
        query: Search query string
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not query:
        return False, "Search query cannot be empty"
    
    if not isinstance(query, str):
        return False, "Search query must be a string"
    
    if len(query.strip()) < 2:
        return False, "Search query must be at least 2 characters"
    
    if len(query) > 500:
        return False, "Search query cannot exceed 500 characters"
    
    return True, None


def validate_search_limit(limit: int) -> Tuple[int, Optional[str]]:
    """
    Validate and normalize search result limit.
    
    Args:
        limit: Requested limit
        
    Returns:
        Tuple of (normalized_limit, warning_message)
    """
    from config import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT
    
    if limit is None or limit < 1:
        return DEFAULT_SEARCH_LIMIT, None
    
    if limit > MAX_SEARCH_LIMIT:
        return MAX_SEARCH_LIMIT, f"Limit capped at maximum of {MAX_SEARCH_LIMIT}"
    
    return limit, None


def construct_document_id(repo_id: str, document_type: str, path: str) -> str:
    """
    Construct MongoDB document ID following the standard format.
    
    Format: {repo_id}::{document_type}::{path}
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        document_type: Type of document ("file_summary", "folder_summary", "repo_summary", "dir_list")
        path: File or folder path relative to repo root (empty string for repo-level documents)
        
    Returns:
        Formatted document ID string
        
    Examples:
        construct_document_id("infra", "file_summary", "run.sh")
        -> "infra::file_summary::run.sh"
        
        construct_document_id("infra", "repo_summary", "")
        -> "infra::repo_summary::"
        
        construct_document_id("infra", "dir_list", "")
        -> "infra::dir_list::"
    """
    return f"{repo_id}::{document_type}::{path}"


def construct_database_name(repo_id: str) -> str:
    """
    Construct database name from repository ID.
    
    Format: {DB_PREFIX}_{repo_id}
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        
    Returns:
        Database name (e.g., "semv2_infra")
    """
    from config import DB_PREFIX
    return f"{DB_PREFIX}_{repo_id}"


def parse_document_id(document_id: str) -> Tuple[str, str, str]:
    """
    Parse document ID into its components.
    
    Args:
        document_id: Document ID (e.g., "infra::dir_list::")
        
    Returns:
        Tuple of (repo_id, document_type, path)
        
    Raises:
        ValueError: If document ID format is invalid
    """
    parts = document_id.split("::")
    
    if len(parts) != 3:
        raise ValueError(
            f"Invalid document ID format: '{document_id}'. "
            f"Expected format: 'repo_id::document_type::path'"
        )
    
    repo_id, document_type, path = parts
    
    if not repo_id or not document_type:
        raise ValueError(f"Document ID cannot have empty repo_id or document_type: '{document_id}'")
    
    # Note: path can be empty for repo-level documents (e.g., "infra::repo_summary::")
    
    return repo_id, document_type, path
