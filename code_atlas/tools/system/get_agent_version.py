"""
Get agent version information.
"""
import logging
from strands import tool
from config import AGENT_VERSION

logger = logging.getLogger(__name__)


@tool
def get_agent_version() -> str:
    """
    Get the current version of the Semantic Code Agent.
    
    Returns the semantic version number (e.g., "0.2.0").
    Useful for debugging, troubleshooting, and verifying deployments.
    
    Returns:
        Version string in semantic versioning format (MAJOR.MINOR.PATCH)
        
    Examples:
        - get_agent_version() -> "0.2.0"
    """
    logger.info(f"Agent version requested: {AGENT_VERSION}")
    return f"🤖 **Semantic Code Agent**\n\nVersion: `{AGENT_VERSION}`"
