"""
System information tools.
"""
from .get_agent_version import get_agent_version

ALL_SYSTEM_TOOLS = [
    get_agent_version,
]

__all__ = [
    'get_agent_version',
    'ALL_SYSTEM_TOOLS',
]
