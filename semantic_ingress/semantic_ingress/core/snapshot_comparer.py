"""
Snapshot comparison logic for incremental updates.

Compares two repository snapshots and produces a delta of changes.
"""

import structlog

from semantic_ingress.models import (
    FileChange,
    FileChangeType,
    RepositorySnapshot,
    SnapshotDelta,
)

logger = structlog.get_logger()


class SnapshotComparer:
    """Compares repository snapshots to detect changes."""

    def __init__(self) -> None:
        """Initialize snapshot comparer."""
        self.log = logger.bind(component="snapshot_comparer")

    def compare(
        self, old_snapshot: RepositorySnapshot, new_snapshot: RepositorySnapshot
    ) -> SnapshotDelta:
        """
        Compare two snapshots and produce a delta.

        Args:
            old_snapshot: Previous repository state
            new_snapshot: Current repository state

        Returns:
            SnapshotDelta describing all changes
        """
        self.log.info(
            "comparing_snapshots",
            old_commit=old_snapshot.commit_sha,
            new_commit=new_snapshot.commit_sha,
        )

        old_paths = old_snapshot.all_paths()
        new_paths = new_snapshot.all_paths()

        changes: list[FileChange] = []

        # Detect added files
        added_paths = new_paths - old_paths
        for path in sorted(added_paths):
            new_file = new_snapshot.get_file(path)
            assert new_file is not None
            changes.append(
                FileChange(
                    path=path,
                    change_type=FileChangeType.ADDED,
                    old_blob_oid=None,
                    new_blob_oid=new_file.blob_oid,
                )
            )

        # Detect deleted files
        deleted_paths = old_paths - new_paths
        for path in sorted(deleted_paths):
            old_file = old_snapshot.get_file(path)
            assert old_file is not None
            changes.append(
                FileChange(
                    path=path,
                    change_type=FileChangeType.DELETED,
                    old_blob_oid=old_file.blob_oid,
                    new_blob_oid=None,
                )
            )

        # Detect modified and unchanged files
        common_paths = old_paths & new_paths
        for path in sorted(common_paths):
            old_file = old_snapshot.get_file(path)
            new_file = new_snapshot.get_file(path)
            assert old_file is not None
            assert new_file is not None

            if old_file.blob_oid != new_file.blob_oid:
                changes.append(
                    FileChange(
                        path=path,
                        change_type=FileChangeType.MODIFIED,
                        old_blob_oid=old_file.blob_oid,
                        new_blob_oid=new_file.blob_oid,
                    )
                )
            else:
                changes.append(
                    FileChange(
                        path=path,
                        change_type=FileChangeType.UNCHANGED,
                        old_blob_oid=old_file.blob_oid,
                        new_blob_oid=new_file.blob_oid,
                    )
                )

        delta = SnapshotDelta(
            old_commit=old_snapshot.commit_sha,
            new_commit=new_snapshot.commit_sha,
            changes=changes,
        )

        self.log.info(
            "snapshot_comparison_complete",
            added=len(delta.added),
            modified=len(delta.modified),
            deleted=len(delta.deleted),
            unchanged=len(delta.unchanged),
        )

        return delta