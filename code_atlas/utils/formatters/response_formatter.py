"""
Response formatting utilities for tool outputs.
"""
from typing import List, Dict, Any


def add_status_icon(status: str, message: str) -> str:
    """
    Add status icon to message.
    
    Args:
        status: Status type ('success', 'error', 'info', 'warning', 'pause')
        message: Message text
        
    Returns:
        Message with status icon
    """
    icons = {
        'success': '✅',
        'error': '❌',
        'info': 'ℹ️',
        'warning': '⚠️',
        'pause': '⏸️'
    }
    
    icon = icons.get(status.lower(), 'ℹ️')
    return f"{icon} {message}"


def format_error_message(error_type: str, details: str, suggestion: str = None) -> str:
    """
    Format error message with optional suggestion.
    
    Args:
        error_type: Type of error (e.g., "Document Not Found")
        details: Error details
        suggestion: Optional suggestion for recovery
        
    Returns:
        Formatted error message
    """
    parts = [f"❌ **{error_type}**\n\n{details}"]
    
    if suggestion:
        parts.append(f"\n\n💡 **Suggestion:** {suggestion}")
    
    return "".join(parts)


def format_search_results(results: List[Dict[str, Any]]) -> str:
    """
    Format text search results as a list with metadata.
    
    Args:
        results: List of search result dictionaries containing:
                 - document_id: Full document ID
                 - document_type: "file_summary" or "folder_summary"
                 - path: File or folder path
                 
    Returns:
        Formatted search results string
    """
    if not results:
        return add_status_icon("info", "No results found for your search query.")
    
    parts = [f"✅ **Found {len(results)} matching document(s):**\n"]
    
    for i, result in enumerate(results, 1):
        doc_type = result.get("document_type", "unknown")
        path = result.get("path", "unknown")
        doc_id = result.get("document_id", "unknown")
        
        # Determine icon based on type
        type_icon = "📄" if doc_type == "file_summary" else "📁"
        
        parts.append(f"\n{i}. {type_icon} **{path}**")
        parts.append(f"\n   Type: `{doc_type}`")
        parts.append(f"\n   ID: `{doc_id}`")
    
    return "".join(parts)


def format_validation_success(repo_id: str, database: str, collections: List[str]) -> str:
    """
    Format repository validation success message.
    
    Args:
        repo_id: Repository identifier
        database: Database name
        collections: List of validated collection names
        
    Returns:
        Formatted success message
    """
    parts = [
        f"✅ **Repository validated successfully!**\n\n",
        f"**Repository:** `{repo_id}`\n",
        f"**Database:** `{database}`\n",
        f"**Collections found:** {', '.join(f'`{c}`' for c in collections)}\n\n",
        f"I'm ready to help you explore this codebase. What would you like to know?"
    ]
    
    return "".join(parts)


def format_validation_failure(repo_id: str, database: str, reason: str) -> str:
    """
    Format repository validation failure message.
    
    Args:
        repo_id: Repository identifier
        database: Database name that was checked
        reason: Reason for validation failure
        
    Returns:
        Formatted failure message
    """
    parts = [
        f"❌ **Repository validation failed**\n\n",
        f"**Repository:** `{repo_id}`\n",
        f"**Database checked:** `{database}`\n",
        f"**Issue:** {reason}\n\n",
        f"💡 **This usually means:**\n",
        f"- The repository hasn't been indexed yet\n",
        f"- The repository name is incorrect\n",
        f"- The semantic index is incomplete\n\n",
        f"Please verify the repository name or contact your administrator to index this repository."
    ]
    
    return "".join(parts)


def format_source_request(file_path: str, start_line: int = None, end_line: int = None) -> str:
    """
    Format source code request message for user.
    
    Args:
        file_path: Path to the file
        start_line: Optional starting line number
        end_line: Optional ending line number
        
    Returns:
        Formatted request message
    """
    if start_line is not None and end_line is not None:
        return (
            f"⏸️ **Source code needed**\n\n"
            f"Please provide **lines {start_line}-{end_line}** from:\n\n"
            f"**{file_path}**\n\n"
            f"Paste the code in your next message and I'll continue my analysis."
        )
    else:
        return (
            f"⏸️ **Source code needed**\n\n"
            f"Please provide the complete source code for:\n\n"
            f"**{file_path}**\n\n"
            f"Paste the code in your next message and I'll continue my analysis."
        )


def format_index_creation_status(database: str, collections: List[str], status: str) -> str:
    """
    Format search index creation status message.
    
    Args:
        database: Database name
        collections: List of collections being indexed
        status: "created", "exists", or "error"
        
    Returns:
        Formatted status message
    """
    if status == "created":
        return (
            f"✅ **Search indexes created successfully**\n\n"
            f"**Database:** `{database}`\n"
            f"**Collections indexed:** {', '.join(f'`{c}`' for c in collections)}\n\n"
            f"Text search is now available."
        )
    elif status == "exists":
        return (
            f"ℹ️ **Search indexes already exist**\n\n"
            f"**Database:** `{database}`\n"
            f"**Collections:** {', '.join(f'`{c}`' for c in collections)}\n\n"
            f"Text search is ready to use."
        )
    else:
        return (
            f"⚠️ **Search index creation failed**\n\n"
            f"**Database:** `{database}`\n"
            f"**Collections:** {', '.join(f'`{c}`' for c in collections)}\n\n"
            f"Text search may not work correctly. Using fallback navigation methods."
        )
    
def format_repository_list(repos: List[Dict[str, Any]]) -> str:
    """
    Format list of indexed repositories.
    
    Args:
        repos: List of repository dictionaries containing:
               - repo_id: Repository identifier
               - database: Database name
               - file_count: Number of indexed files
               - folder_count: Number of indexed folders
               - commit_sha: Commit SHA (truncated)
    
    Returns:
        Formatted repository list string
    """
    if not repos:
        return add_status_icon("info", "No repositories found.")
    
    parts = [f"✅ **Found {len(repos)} indexed repository(ies):**\n"]
    
    for i, repo in enumerate(repos, 1):
        repo_id = repo.get("repo_id", "unknown")
        file_count = repo.get("file_count", 0)
        folder_count = repo.get("folder_count", 0)
        commit_sha = repo.get("commit_sha", "unknown")
        
        parts.append(f"\n{i}. **{repo_id}**")
        parts.append(f"\n   📄 {file_count:,} files indexed")
        parts.append(f"\n   📁 {folder_count:,} folders indexed")
        parts.append(f"\n   🔖 Commit: `{commit_sha}`")
    
    parts.append("\n\n💡 **To explore a repository, just tell me its name from the list above.**")
    
    return "".join(parts)    