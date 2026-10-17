"""
JSON to prose conversion for semantic summaries.

All semantic documents now contain pre-computed prose_summary fields.
No structured JSON fallback - prose is always present.
"""
from typing import Dict, Any


def serialize_file_summary_to_prose(data: dict) -> str:
    """
    Return pre-computed prose summary for a file.
    
    Args:
        data: File summary document from MongoDB
        
    Returns:
        Pre-computed prose description
        
    Raises:
        KeyError: If prose_summary field is missing (should never happen)
    """
    if "prose_summary" not in data:
        raise KeyError(
            f"File summary document missing 'prose_summary' field. "
            f"Document ID: {data.get('_id', 'unknown')}. "
            f"This indicates the semantic index is corrupted or outdated."
        )
    
    return data["prose_summary"]


def serialize_folder_summary_to_prose(data: dict) -> str:
    """
    Return pre-computed prose summary for a folder.
    
    Args:
        data: Folder summary document from MongoDB
        
    Returns:
        Pre-computed prose description
        
    Raises:
        KeyError: If prose_summary field is missing (should never happen)
    """
    if "prose_summary" not in data:
        raise KeyError(
            f"Folder summary document missing 'prose_summary' field. "
            f"Document ID: {data.get('_id', 'unknown')}. "
            f"This indicates the semantic index is corrupted or outdated."
        )
    
    return data["prose_summary"]


def serialize_repo_summary_to_prose(data: dict) -> str:
    """
    Return pre-computed prose summary for a repository.
    
    Args:
        data: Repository summary document from MongoDB
        
    Returns:
        Pre-computed prose description
        
    Raises:
        KeyError: If prose_summary field is missing (should never happen)
    """
    if "prose_summary" not in data:
        raise KeyError(
            f"Repository summary document missing 'prose_summary' field. "
            f"Document ID: {data.get('_id', 'unknown')}. "
            f"This indicates the semantic index is corrupted or outdated."
        )
    
    return data["prose_summary"]


def serialize_dir_listing_to_prose(data: dict) -> str:
    """
    Return pre-computed directory listing.
    
    Args:
        data: Directory listing document from MongoDB
        
    Returns:
        Newline-separated list of all files in repository
        
    Raises:
        KeyError: If prose_summary field is missing (should never happen)
    """
    if "prose_summary" not in data:
        raise KeyError(
            f"Directory listing document missing 'prose_summary' field. "
            f"Document ID: {data.get('_id', 'unknown')}. "
            f"This indicates the semantic index is corrupted or outdated."
        )
    
    return data["prose_summary"]


def serialize_document_to_prose(document: Dict[str, Any]) -> str:
    """
    Auto-detect document type and return prose summary.
    
    Args:
        document: Document from MongoDB (any summary type)
        
    Returns:
        Prose-formatted string
        
    Raises:
        ValueError: If document type cannot be determined
        KeyError: If prose_summary field is missing
    """
    document_type = document.get("document_type")
    
    if document_type == "file_summary":
        return serialize_file_summary_to_prose(document)
    elif document_type == "folder_summary":
        return serialize_folder_summary_to_prose(document)
    elif document_type == "repo_summary":
        return serialize_repo_summary_to_prose(document)
    elif document_type == "dir_list":
        return serialize_dir_listing_to_prose(document)
    else:
        raise ValueError(
            f"Unknown document type: {document_type}. "
            f"Document ID: {document.get('_id', 'unknown')}"
        )
