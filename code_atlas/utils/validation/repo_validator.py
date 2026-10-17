"""
Repository name extraction and validation.
"""
import re
import logging
from typing import Tuple, Optional
from utils.mongo import (
    get_mongo_client,
    construct_database_name,
    validate_repo_id,
    DatabaseNotFoundError,
    CollectionNotFoundError,
)
from config import FILE_SUMMARIES_COLLECTION, FOLDER_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


def extract_repo_id_from_user_input(user_input: str) -> Optional[str]:
    """
    Extract repository identifier from user's natural language response.
    
    Handles various response formats:
    - "infra"
    - "I need help with infra"
    - "The infra repository"
    - "It's called infra"
    
    Args:
        user_input: User's response containing repository name
        
    Returns:
        Extracted repository ID or None if extraction fails
    """
    if not user_input or not isinstance(user_input, str):
        return None
    
    # Clean input
    user_input = user_input.strip()
    
    # Pattern 1: Direct repo name (just the ID)
    # Match: alphanumeric, hyphens, underscores
    direct_match = re.match(r'^([a-zA-Z0-9_-]+)$', user_input)
    if direct_match:
        return direct_match.group(1)
    
    # Pattern 2: Repo name in sentence
    # Match phrases like "I need help with X" or "It's called X" or "The X repository"
    sentence_patterns = [
        r'(?:with|called|named?)\s+([a-zA-Z0-9_-]+)',  # "with X" or "called X"
        r'([a-zA-Z0-9_-]+)\s+repository',  # "X repository"
        r'repository\s+([a-zA-Z0-9_-]+)',  # "repository X"
        r'repo\s+([a-zA-Z0-9_-]+)',  # "repo X"
        r'([a-zA-Z0-9_-]+)\s+repo',  # "X repo"
    ]
    
    for pattern in sentence_patterns:
        match = re.search(pattern, user_input, re.IGNORECASE)
        if match:
            return match.group(1)
    
    # Pattern 3: Last resort - look for any valid repo ID pattern
    # Find all potential repo IDs and return the longest one
    potential_ids = re.findall(r'\b([a-zA-Z0-9_-]{2,})\b', user_input)
    if potential_ids:
        # Filter out common words that aren't repo names
        common_words = {'the', 'with', 'help', 'need', 'repository', 'repo', 'called', 'named'}
        valid_ids = [pid for pid in potential_ids if pid.lower() not in common_words]
        
        if valid_ids:
            # Return the longest match (most likely to be the repo name)
            return max(valid_ids, key=len)
    
    return None


def validate_repository(repo_id: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate that repository exists in MongoDB with required collections.
    
    Args:
        repo_id: Repository identifier (e.g., "infra")
        
    Returns:
        Tuple of (is_valid, database_name, error_message)
        - is_valid: True if repository is valid and indexed
        - database_name: Constructed database name
        - error_message: Description of validation failure (None if valid)
    """
    # First validate the repo_id format
    is_valid_format, format_error = validate_repo_id(repo_id)
    if not is_valid_format:
        return False, "", format_error
    
    # Construct database name
    database_name = construct_database_name(repo_id)
    
    try:
        client = get_mongo_client()
        
        # Check if database exists
        databases = client.list_database_names()
        if database_name not in databases:
            raise DatabaseNotFoundError(database_name)
        
        # Check if required collections exist
        db = client[database_name]
        collections = db.list_collection_names()
        
        required_collections = [FILE_SUMMARIES_COLLECTION, FOLDER_SUMMARIES_COLLECTION]
        missing_collections = [col for col in required_collections if col not in collections]
        
        if missing_collections:
            raise CollectionNotFoundError(
                ", ".join(missing_collections),
                database_name
            )
        
        # Verify collections have data
        for collection_name in required_collections:
            count = db[collection_name].count_documents({})
            if count == 0:
                return (
                    False,
                    database_name,
                    f"Collection '{collection_name}' exists but is empty. "
                    f"The semantic index may be incomplete."
                )
        
        logger.info(f"Repository '{repo_id}' validated successfully")
        return True, database_name, None
        
    except DatabaseNotFoundError as e:
        logger.warning(f"Database not found: {database_name}")
        return (
            False,
            database_name,
            f"Database '{database_name}' not found. "
            f"The repository may not have been indexed yet."
        )
    
    except CollectionNotFoundError as e:
        logger.warning(f"Collections not found: {e.collection_name}")
        return (
            False,
            database_name,
            f"Required collections not found: {e.collection_name}. "
            f"The semantic index is incomplete."
        )
    
    except Exception as e:
        logger.error(f"Error validating repository: {str(e)}")
        return (
            False,
            database_name,
            f"Validation error: {str(e)}"
        )


def get_repository_info(repo_id: str) -> dict:
    """
    Get detailed information about an indexed repository.
    
    Args:
        repo_id: Repository identifier
        
    Returns:
        Dictionary containing repository information
        
    Raises:
        DatabaseNotFoundError: If repository database doesn't exist
    """
    database_name = construct_database_name(repo_id)
    
    try:
        client = get_mongo_client()
        db = client[database_name]
        
        file_count = db[FILE_SUMMARIES_COLLECTION].count_documents({})
        folder_count = db[FOLDER_SUMMARIES_COLLECTION].count_documents({})
        
        # Get a sample document to extract metadata
        sample_file = db[FILE_SUMMARIES_COLLECTION].find_one({})
        commit_sha = sample_file.get("commit_sha", "unknown") if sample_file else "unknown"
        
        return {
            "repo_id": repo_id,
            "database": database_name,
            "file_count": file_count,
            "folder_count": folder_count,
            "commit_sha": commit_sha,
            "collections": [FILE_SUMMARIES_COLLECTION, FOLDER_SUMMARIES_COLLECTION]
        }
        
    except Exception as e:
        logger.error(f"Error getting repository info: {str(e)}")
        raise DatabaseNotFoundError(database_name)