"""
Abstract interface for source code retrieval with pluggable providers.

This module implements the Open/Closed Principle - the interface is stable,
but implementations can be swapped without changing tool code.

Current: UserInputProvider (asks user to paste code)
Future: BitbucketAPIProvider (fetches code directly from Bitbucket API)
"""
from abc import ABC, abstractmethod
from typing import Optional


class SourceProvider(ABC):
    """
    Abstract base class for source code retrieval.
    
    Implementations must provide methods to request full files or line ranges.
    The return value is a message string (either instructions to user or the code itself).
    """
    
    @abstractmethod
    def request_full_file(self, file_path: str) -> str:
        """
        Request complete source code for a file.
        
        Args:
            file_path: Path to file relative to repository root
            
        Returns:
            Instruction message or source code
        """
        pass
    
    @abstractmethod
    def request_file_lines(self, file_path: str, start_line: int, end_line: int) -> str:
        """
        Request specific line range from a file.
        
        Args:
            file_path: Path to file relative to repository root
            start_line: Starting line number (1-indexed, inclusive)
            end_line: Ending line number (inclusive)
            
        Returns:
            Instruction message or source code lines
        """
        pass


class UserInputProvider(SourceProvider):
    """
    Source provider that requests code from the user via conversation.
    
    This is the current implementation. The tool returns an instruction message,
    and the LLM waits for the user to paste the code in their next message.
    """
    
    def request_full_file(self, file_path: str) -> str:
        """
        Ask user to provide complete file source.
        
        Args:
            file_path: Path to file relative to repository root
            
        Returns:
            Instruction message for user
        """
        from utils.formatters import format_source_request
        return format_source_request(file_path)
    
    def request_file_lines(self, file_path: str, start_line: int, end_line: int) -> str:
        """
        Ask user to provide specific line range.
        
        Args:
            file_path: Path to file relative to repository root
            start_line: Starting line number (1-indexed)
            end_line: Ending line number (inclusive)
            
        Returns:
            Instruction message for user
        """
        from utils.formatters import format_source_request
        return format_source_request(file_path, start_line, end_line)


# Global provider instance (can be swapped for testing or future implementations)
_current_provider: Optional[SourceProvider] = None


def get_source_provider() -> SourceProvider:
    """
    Get the current source provider instance.
    
    Returns:
        SourceProvider: Active source provider (default: UserInputProvider)
    """
    global _current_provider
    
    if _current_provider is None:
        _current_provider = UserInputProvider()
    
    return _current_provider


def set_source_provider(provider: SourceProvider) -> None:
    """
    Set a custom source provider (for testing or future implementations).
    
    Args:
        provider: SourceProvider instance to use
    """
    global _current_provider
    _current_provider = provider


# Future implementation example (not used yet):
# 
# class BitbucketAPIProvider(SourceProvider):
#     """
#     Source provider that fetches code directly from Bitbucket API.
#     """
#     
#     def __init__(self, workspace: str, repo_slug: str, api_token: str):
#         self.workspace = workspace
#         self.repo_slug = repo_slug
#         self.api_token = api_token
#         self.base_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}"
#     
#     def request_full_file(self, file_path: str) -> str:
#         """Fetch complete file from Bitbucket API."""
#         import requests
#         
#         url = f"{self.base_url}/src/main/{file_path}"
#         headers = {"Authorization": f"Bearer {self.api_token}"}
#         
#         response = requests.get(url, headers=headers)
#         response.raise_for_status()
#         
#         return f"```\n{response.text}\n```"
#     
#     def request_file_lines(self, file_path: str, start_line: int, end_line: int) -> str:
#         """Fetch file and extract line range."""
#         full_content = self.request_full_file(file_path)
#         lines = full_content.split('\n')
#         
#         # Extract lines (1-indexed, inclusive)
#         extracted = lines[start_line-1:end_line]
#         
#         return f"```\n" + "\n".join(extracted) + "\n```"