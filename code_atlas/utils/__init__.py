"""
Top-level utility exports.
"""
from .mongo import (
    get_mongo_client,
    close_mongo_client,
    DocumentNotFoundError,
    DatabaseNotFoundError,
    CollectionNotFoundError,
    construct_document_id,
    construct_database_name,
    validate_file_path,
    validate_folder_path,
    validate_line_range,
    validate_search_query,
    validate_search_limit,
    validate_repo_id,
)
from .formatters import (
    serialize_document_to_prose,
    serialize_file_summary_to_prose,
    serialize_folder_summary_to_prose,
    serialize_repo_summary_to_prose,
    serialize_dir_listing_to_prose, 
    format_search_results,
    format_source_request,
    add_status_icon,
)
from .formatters.lens import (
    describe_lens_info,
    index_lens_info,
    lens_block,
    repo_lens_block,
)
from .validation import (
    extract_repo_id_from_user_input,
    validate_repository,
    get_repository_info,
)

__all__ = [
    # MongoDB
    'get_mongo_client',
    'close_mongo_client',
    'DocumentNotFoundError',
    'DatabaseNotFoundError',
    'CollectionNotFoundError',
    'construct_document_id',
    'construct_database_name',
    'validate_file_path',
    'validate_folder_path',
    'validate_line_range',
    'validate_search_query',
    'validate_search_limit',
    'validate_repo_id',
    
    # Formatters
    'serialize_document_to_prose',
    'serialize_file_summary_to_prose',
    'serialize_folder_summary_to_prose',
    'serialize_repo_summary_to_prose',
    'serialize_dir_listing_to_prose',
    'format_search_results',
    'format_source_request',
    'add_status_icon',
    'describe_lens_info',
    'index_lens_info',
    'lens_block',
    'repo_lens_block',
    
    # Validation
    'extract_repo_id_from_user_input',
    'validate_repository',
    'get_repository_info',
]
