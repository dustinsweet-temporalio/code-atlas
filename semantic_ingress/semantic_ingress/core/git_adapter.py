"""
Git repository adapter for snapshot generation and file enumeration.
Uses pygit2 for efficient Git operations.
"""
import fnmatch
from pathlib import Path
from typing import NamedTuple

import pygit2
import structlog

from semantic_ingress.config import FileFilterConfig
from semantic_ingress.models import FileSnapshot, RepositorySnapshot

logger = structlog.get_logger()

# Repositories are typically bind-mounted into a container and owned by a different
# uid than the one running the indexer. Reading them is intended, so skip libgit2's
# ownership ("safe.directory") check.
try:
    pygit2.option(pygit2.GIT_OPT_SET_OWNER_VALIDATION, 0)
except Exception:  # older pygit2/libgit2 without the option
    pass


class FileUnreadableError(ValueError):
    """A file's blob is missing or is not UTF-8 text; retrying will not help."""


class FileEntry(NamedTuple):
    """A file entry from the Git tree."""
    name: str  # Full path
    id: str    # Blob OID
    filemode: int


class GitAdapter:
    """Adapter for Git repository operations."""

    def __init__(self, repo_path: Path, filter_config: FileFilterConfig) -> None:
        """
        Initialize Git adapter.

        Args:
            repo_path: Path to the Git repository
            filter_config: Configuration for file filtering

        Raises:
            ValueError: If path is not a valid Git repository
        """
        self.repo_path = repo_path
        self.filter_config = filter_config
        self.log = logger.bind(repo_path=str(repo_path))

        try:
            self.repo = pygit2.Repository(str(repo_path))
        except pygit2.GitError as e:
            raise ValueError(
                f"Not a valid Git repository: {repo_path}. "
                "Point --repo-path at the repository root (the folder containing .git)."
            ) from e

        self.log.info("git_adapter_initialized", head=self.get_head_sha())

    def get_head_sha(self) -> str:
        """Get the current HEAD commit SHA."""
        if self.repo.head_is_unborn:
            raise ValueError("Repository HEAD is unborn (no commits)")
        return str(self.repo.head.target)

    def get_snapshot(self, commit_sha: str | None = None) -> RepositorySnapshot:
        """
        Generate a complete repository snapshot at a specific commit.

        Args:
            commit_sha: Commit SHA to snapshot (defaults to HEAD)

        Returns:
            RepositorySnapshot containing all eligible files
        """
        if commit_sha is None:
            commit_sha = self.get_head_sha()

        self.log.info("generating_snapshot", commit_sha=commit_sha)

        commit = self.repo.get(commit_sha)
        if commit is None:
            raise ValueError(f"Commit not found: {commit_sha}")

        tree = commit.tree
        files: dict[str, FileSnapshot] = {}

        # Walk the tree and collect eligible files
        for entry in self._walk_tree(tree):
            # ✅ UPDATED: Pass blob_oid to should_include_file
            if self._should_include_file(entry.name, entry.filemode, entry.id):
                path = entry.name
                blob_oid = entry.id
                files[path] = FileSnapshot(path=path, blob_oid=blob_oid)

        self.log.info("snapshot_generated", commit_sha=commit_sha, file_count=len(files))
        return RepositorySnapshot(commit_sha=commit_sha, files=files)

    def get_file_content(self, blob_oid: str) -> str:
        """
        Retrieve file content by blob OID.

        Args:
            blob_oid: Git blob object ID

        Returns:
            File content as string

        Raises:
            ValueError: If blob not found or not decodable
        """
        blob = self.repo.get(blob_oid)
        if blob is None:
            raise FileUnreadableError(f"Blob not found: {blob_oid}")

        try:
            return blob.data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise FileUnreadableError(f"Cannot decode blob as UTF-8: {blob_oid}") from e

    def _walk_tree(self, tree: pygit2.Tree, prefix: str = "") -> list[FileEntry]:
        """
        Recursively walk a Git tree and yield all file entries.

        Args:
            tree: Git tree object
            prefix: Path prefix for nested entries

        Returns:
            List of file entries with full paths
        """
        entries = []
        for entry in tree:
            full_path = f"{prefix}{entry.name}" if prefix else entry.name
            if entry.type_str == "tree":
                # Recurse into subdirectory
                subtree = self.repo.get(entry.id)
                entries.extend(self._walk_tree(subtree, f"{full_path}/"))
            else:
                # Add file entry
                entries.append(FileEntry(
                    name=full_path,
                    id=str(entry.id),
                    filemode=entry.filemode
                ))
        return entries

    def _is_binary_file(self, blob_oid: str) -> bool:
        """
        Check if a file is binary by sampling the first 8KB.
        
        Args:
            blob_oid: Git blob object ID
        
        Returns:
            True if file appears to be binary
        """
        try:
            blob = self.repo.get(blob_oid)
            if blob is None:
                return True
            
            # Sample first 8KB
            sample_size = min(8192, blob.size)
            sample = blob.data[:sample_size]
            
            # Check for null bytes (strong indicator of binary)
            if b'\x00' in sample:
                return True
            
            # Check for high ratio of non-text bytes
            try:
                sample.decode('utf-8')
                return False  # Successfully decoded as UTF-8
            except UnicodeDecodeError:
                return True  # Failed to decode
            
        except Exception:
            return True  # Assume binary on error

    def _matching_exclude_pattern(self, path: str) -> str | None:
        """
        Return the first exclude pattern that matches ``path``, or None.

        Pattern rules:
          - ``name/``     a folder with that name at any depth (``node_modules/``)
          - ``a/b/``      a folder path, at the repo root or nested (``docs/build/``)
          - ``*.ext``     a pattern without a slash is matched against the file name
                          at any depth (``*.png``, ``Makefile``)
          - ``a/*.txt``   a pattern with a slash is matched against the whole
                          repo-relative path; ``*`` also crosses ``/``
        """
        parts = path.split("/")
        folders, name = parts[:-1], parts[-1]

        for pattern in self.filter_config.exclude_patterns:
            if pattern.endswith("/"):
                folder = pattern.rstrip("/")
                if "/" in folder:
                    if f"/{folder}/" in f"/{path}":
                        return pattern
                elif any(fnmatch.fnmatch(part, folder) for part in folders):
                    return pattern
            elif "/" in pattern:
                if fnmatch.fnmatch(path, pattern.lstrip("/")):
                    return pattern
            elif fnmatch.fnmatch(name, pattern):
                return pattern
        return None

    def _should_include_file(self, path: str, filemode: int, blob_oid: str) -> bool:
        """
        Determine if a file should be included in the snapshot.

        Args:
            path: Repository-relative file path
            filemode: Git file mode
            blob_oid: Git blob object ID (NEW)

        Returns:
            True if file should be included
        """
        # Check if it's a regular file or executable
        if filemode not in (pygit2.GIT_FILEMODE_BLOB, pygit2.GIT_FILEMODE_BLOB_EXECUTABLE):
            return False

        # Check against exclude patterns
        pattern = self._matching_exclude_pattern(path)
        if pattern is not None:
            self.log.debug("excluded_by_pattern", path=path, pattern=pattern)
            return False
        
        # Check file size
        if self._is_file_too_large(blob_oid):
            self.log.debug(
                "skipping_large_file",
                path=path,
                max_size=self.filter_config.max_file_size_bytes,
            )
            return False
        
        # ✅ NEW: Check if binary
        if self._is_binary_file(blob_oid):
            self.log.debug("skipping_binary_file", path=path)
            return False

        return True

    def _is_file_too_large(self, blob_oid: str) -> bool:
        """
        Check if a file exceeds the maximum size threshold.

        Args:
            blob_oid: Git blob object ID

        Returns:
            True if file is too large
        """
        blob = self.repo.get(blob_oid)
        if blob is None:
            return True
        return blob.size > self.filter_config.max_file_size_bytes