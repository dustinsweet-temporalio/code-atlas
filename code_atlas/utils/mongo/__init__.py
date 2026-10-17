"""
MongoDB utility exports.
"""
from .client import get_mongo_client, close_mongo_client, test_connection
from .exceptions import (
    DocumentNotFoundError,
    DatabaseNotFoundError,
    CollectionNotFoundError,
    SearchIndexMissingError,
    InvalidRepositoryNameError,
    DocumentValidationError,
)
from .validators import (
    validate_repo_id,  # NEW!
    validate_database_name,
    validate_collection_name,
    validate_file_path,
    validate_folder_path,
    validate_line_range,
    validate_search_query,
    validate_search_limit,
    construct_document_id,
    construct_database_name,
    parse_document_id,
)

__all__ = [
    # Client
    'get_mongo_client',
    'close_mongo_client',
    'test_connection',
    
    # Exceptions
    'DocumentNotFoundError',
    'DatabaseNotFoundError',
    'CollectionNotFoundError',
    'SearchIndexMissingError',
    'InvalidRepositoryNameError',
    'DocumentValidationError',
    
    # Validators
    'validate_repo_id',  # NEW!
    'validate_database_name',
    'validate_collection_name',
    'validate_file_path',
    'validate_folder_path',
    'validate_line_range',
    'validate_search_query',
    'validate_search_limit',
    'construct_document_id',
    'construct_database_name',
    'parse_document_id',
]
