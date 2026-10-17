# semantic_ingress/core/dir_list_generator.py
"""
Generator for directory listing document.
Creates a sorted list of all files in the repository.
"""
from pathlib import Path

import structlog

from semantic_ingress.models import RepositorySnapshot

logger = structlog.get_logger()


class DirListGenerator:
    """Generates directory listing for a repository."""

    def __init__(self):
        """Initialize dir list generator."""
        self.log = logger.bind(component="dir_list_generator")

    def generate(self, snapshot: RepositorySnapshot) -> str:
        """
        Generate directory listing from repository snapshot.
        
        Produces a simple text file with one file path per line, sorted alphabetically.
        Format: One file path per line, relative to repository root.
        
        Args:
            snapshot: Repository snapshot containing all files
            
        Returns:
            String content of dir_list.txt
        """
        self.log.info("generating_dir_list", file_count=len(snapshot.files))
        
        # Extract and sort all file paths
        file_paths = sorted(snapshot.files.keys())
        
        # Create the content
        content = "\n".join(file_paths)
        
        # Ensure trailing newline (standard Unix convention)
        if content and not content.endswith("\n"):
            content += "\n"
        
        self.log.info("dir_list_generated", line_count=len(file_paths))
        
        return content
