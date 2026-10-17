"""
Models for Git snapshots and comparison results.

These structures support incremental update logic and change detection.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FileChangeType(Enum):
    """Type of change detected for a file."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class FileSnapshot:
    """Snapshot of a single file at a point in time."""

    path: str
    blob_oid: str

    @property
    def relative_path(self) -> Path:
        """Return path as a Path object."""
        return Path(self.path)


@dataclass(frozen=True)
class RepositorySnapshot:
    """Complete snapshot of a repository at a specific commit."""

    commit_sha: str
    files: dict[str, FileSnapshot]  # keyed by path

    def get_file(self, path: str) -> FileSnapshot | None:
        """Retrieve a file snapshot by path."""
        return self.files.get(path)

    def all_paths(self) -> set[str]:
        """Return all file paths in this snapshot."""
        return set(self.files.keys())


@dataclass(frozen=True)
class FileChange:
    """A detected change to a file between snapshots."""

    path: str
    change_type: FileChangeType
    old_blob_oid: str | None
    new_blob_oid: str | None

    @property
    def is_semantic_candidate(self) -> bool:
        """
        Whether this change is a candidate for semantic analysis.
        
        Added and modified files require semantic processing.
        Deleted and unchanged files do not.
        """
        return self.change_type in (FileChangeType.ADDED, FileChangeType.MODIFIED)


@dataclass(frozen=True)
class SnapshotDelta:
    """Result of comparing two repository snapshots."""

    old_commit: str
    new_commit: str
    changes: list[FileChange]

    def by_type(self, change_type: FileChangeType) -> list[FileChange]:
        """Filter changes by type."""
        return [c for c in self.changes if c.change_type == change_type]

    @property
    def added(self) -> list[FileChange]:
        """All added files."""
        return self.by_type(FileChangeType.ADDED)

    @property
    def modified(self) -> list[FileChange]:
        """All modified files."""
        return self.by_type(FileChangeType.MODIFIED)

    @property
    def deleted(self) -> list[FileChange]:
        """All deleted files."""
        return self.by_type(FileChangeType.DELETED)

    @property
    def unchanged(self) -> list[FileChange]:
        """All unchanged files."""
        return self.by_type(FileChangeType.UNCHANGED)

    @property
    def semantic_candidates(self) -> list[FileChange]:
        """All changes that may require semantic processing."""
        return [c for c in self.changes if c.is_semantic_candidate]