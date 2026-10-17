"""
Source code request tools with pluggable provider interface.
"""
from .source_provider import (
    SourceProvider,
    UserInputProvider,
    get_source_provider,
    set_source_provider,
)
from .request_full_file_source import request_full_file_source
from .request_file_source_lines import request_file_source_lines
from .local_provider import (
    LocalSourceProvider,
    get_file_source,
    get_file_source_lines,
)

ALL_SOURCE_TOOLS = [
    # PRIMARY: automatic retrieval from the source store (use these first)
    get_file_source,
    get_file_source_lines,
    
    # LAST RESORT: User manual input (only if the source store catastrophically fails)
    request_full_file_source,
    request_file_source_lines,
]

__all__ = [
    # Provider interface
    'SourceProvider',
    'UserInputProvider',
    'LocalSourceProvider',
    'get_source_provider',
    'set_source_provider',
    
    # Source store tools (primary)
    'get_file_source',
    'get_file_source_lines',
    
    # User request tools (last resort)
    'request_full_file_source',
    'request_file_source_lines',
    
    # Tool registry
    'ALL_SOURCE_TOOLS',
]
