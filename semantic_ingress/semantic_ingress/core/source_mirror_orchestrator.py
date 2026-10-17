"""
Source mirror orchestrator for repository file synchronization.

Coordinates local source-store copy/delete operations for full and incremental sync.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import NamedTuple

import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.source_store import SourceStore
from semantic_ingress.models import RepositorySnapshot, SnapshotDelta

logger = structlog.get_logger()


class SourceOperation(NamedTuple):
    """Result of a single source store operation."""
    path: str
    operation: str  # "upload" or "delete"
    success: bool
    error: str | None


class SourceMirrorResult(NamedTuple):
    """Result of source store mirroring operation."""
    uploaded: int
    deleted: int
    failed_uploads: list[tuple[str, str]]  # (path, error)
    failed_deletes: list[tuple[str, str]]  # (path, error)

    @property
    def has_failures(self) -> bool:
        """Check if any operations failed."""
        return len(self.failed_uploads) > 0 or len(self.failed_deletes) > 0

    @property
    def total_failures(self) -> int:
        """Total number of failed operations."""
        return len(self.failed_uploads) + len(self.failed_deletes)


class SourceMirrorOrchestrator:
    """Orchestrates source store file mirroring operations."""

    def __init__(self, config: IngestionConfig) -> None:
        """
        Initialize source store mirror orchestrator.

        Args:
            config: Ingestion configuration
        """
        self.config = config
        self.repo_path = config.repo_path
        self.log = logger.bind(component="source_mirror_orchestrator")

        # Initialize source store
        self.store = SourceStore(config.source, config.repo_id or "unknown")

        # Thread-safe tracking
        self.progress_lock = Lock()

    def mirror_full(
        self,
        snapshot: RepositorySnapshot,
        dry_run: bool = False,
    ) -> SourceMirrorResult:
        """
        Mirror all files from repository to source store.

        Args:
            snapshot: Repository snapshot with all files
            dry_run: If True, only show what would be uploaded

        Returns:
            SourceMirrorResult with operation summary
        """
        self.log.info("starting_full_source_mirror", file_count=len(snapshot.files), dry_run=dry_run)

        print("\n" + "=" * 80)
        print(f"SOURCE MIRROR - FULL SYNC {'(DRY-RUN)' if dry_run else ''}")
        print("=" * 80)
        print(f"Files to upload: {len(snapshot.files):,}")
        print("=" * 80 + "\n")

        if dry_run:
            # Just show what would be uploaded
            for idx, (path, file_snap) in enumerate(list(snapshot.files.items())[:10], 1):
                print(f"  [{idx}] {path} (blob: {file_snap.blob_oid[:8]}...)")
            if len(snapshot.files) > 10:
                print(f"  ... and {len(snapshot.files) - 10} more")
            print()
            return SourceMirrorResult(
                uploaded=len(snapshot.files),
                deleted=0,
                failed_uploads=[],
                failed_deletes=[],
            )

        # Perform actual uploads
        failed_uploads: list[tuple[str, str]] = []

        with ThreadPoolExecutor(max_workers=self.config.source.max_workers) as executor:
            futures = {
                executor.submit(
                    self._upload_file,
                    path,
                    file_snap.blob_oid,
                ): path
                for path, file_snap in snapshot.files.items()
            }

            completed = 0
            for future in as_completed(futures):
                path = futures[future]
                try:
                    operation = future.result()
                    if not operation.success:
                        failed_uploads.append((path, operation.error or "Unknown error"))
                        self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                        self._print_progress(f"  Error: {operation.error}")
                    else:
                        self._print_progress(f"[{completed + 1}/{len(futures)}] ✓ {path}")

                except Exception as e:
                    error_msg = str(e)
                    failed_uploads.append((path, error_msg))
                    self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                    self._print_progress(f"  Error: {error_msg}")

                completed += 1

        uploaded_count = len(snapshot.files) - len(failed_uploads)

        # A full mirror reflects HEAD exactly: remove mirrored files that are no
        # longer in the repository (left behind by an earlier index of it).
        stale = sorted(set(self.store.list_files()) - set(snapshot.files))
        deleted_count, failed_deletes = (
            self.store.delete_files_bulk(stale) if stale else (0, [])
        )

        print("\n" + "=" * 80)
        print("SOURCE FULL MIRROR COMPLETE")
        print("=" * 80)
        print(f"Uploaded: {uploaded_count:,}")
        print(f"Removed:  {deleted_count:,} (no longer in the repository)")
        print(f"Failed:   {len(failed_uploads) + len(failed_deletes):,}")
        print("=" * 80 + "\n")

        self.log.info(
            "full_source_mirror_complete",
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed=len(failed_uploads) + len(failed_deletes),
        )

        return SourceMirrorResult(
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed_uploads=failed_uploads,
            failed_deletes=failed_deletes,
        )

    def mirror_incremental(
        self,
        delta: SnapshotDelta,
        snapshot: RepositorySnapshot,
        dry_run: bool = False,
    ) -> SourceMirrorResult:
        """
        Mirror only changed files to source store.

        Args:
            delta: Snapshot delta with changes
            snapshot: Current repository snapshot
            dry_run: If True, only show what would be changed

        Returns:
            SourceMirrorResult with operation summary
        """
        files_to_upload = [c.path for c in delta.added] + [c.path for c in delta.modified]
        files_to_delete = [c.path for c in delta.deleted]

        self.log.info(
            "starting_incremental_source_mirror",
            uploads=len(files_to_upload),
            deletes=len(files_to_delete),
            dry_run=dry_run,
        )

        print("\n" + "=" * 80)
        print(f"SOURCE MIRROR - INCREMENTAL {'(DRY-RUN)' if dry_run else ''}")
        print("=" * 80)
        print(f"Files to upload: {len(files_to_upload):,}")
        print(f"Files to delete: {len(files_to_delete):,}")
        print("=" * 80 + "\n")

        if dry_run:
            if files_to_upload:
                print("Would upload:")
                for path in files_to_upload[:10]:
                    file_snap = snapshot.get_file(path)
                    blob_oid = file_snap.blob_oid if file_snap else "unknown"
                    print(f"  • {path} (blob: {blob_oid[:8]}...)")
                if len(files_to_upload) > 10:
                    print(f"  ... and {len(files_to_upload) - 10} more")

            if files_to_delete:
                print("\nWould delete:")
                for path in files_to_delete[:10]:
                    print(f"  • {path}")
                if len(files_to_delete) > 10:
                    print(f"  ... and {len(files_to_delete) - 10} more")

            print()
            return SourceMirrorResult(
                uploaded=len(files_to_upload),
                deleted=len(files_to_delete),
                failed_uploads=[],
                failed_deletes=[],
            )

        # Perform actual operations
        failed_uploads: list[tuple[str, str]] = []
        failed_deletes: list[tuple[str, str]] = []

        # Upload added/modified files
        if files_to_upload:
            print("UPLOADING FILES")
            print("-" * 80)

            with ThreadPoolExecutor(max_workers=self.config.source.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._upload_file,
                        path,
                        snapshot.get_file(path).blob_oid,
                    ): path
                    for path in files_to_upload
                }

                completed = 0
                for future in as_completed(futures):
                    path = futures[future]
                    try:
                        operation = future.result()
                        if not operation.success:
                            failed_uploads.append((path, operation.error or "Unknown error"))
                            self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                            self._print_progress(f"  Error: {operation.error}")
                        else:
                            self._print_progress(f"[{completed + 1}/{len(futures)}] ✓ {path}")

                    except Exception as e:
                        error_msg = str(e)
                        failed_uploads.append((path, error_msg))
                        self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                        self._print_progress(f"  Error: {error_msg}")

                    completed += 1

            uploaded_count = len(files_to_upload) - len(failed_uploads)
            print(f"\n✓ Uploaded {uploaded_count:,} files")
            if failed_uploads:
                print(f"❌ Failed {len(failed_uploads):,} uploads")
            print()

        # Delete removed files
        if files_to_delete:
            print("DELETING FILES")
            print("-" * 80)

            deleted_count, failures = self.store.delete_files_bulk(files_to_delete)
            failed_deletes.extend(failures)

            print(f"✓ Deleted {deleted_count:,} files")
            if failed_deletes:
                print(f"❌ Failed {len(failed_deletes):,} deletions")
            print()

        uploaded_count = len(files_to_upload) - len(failed_uploads)
        deleted_count = len(files_to_delete) - len(failed_deletes)

        print("=" * 80)
        print("SOURCE INCREMENTAL MIRROR COMPLETE")
        print("=" * 80)
        print(f"Uploaded: {uploaded_count:,}")
        print(f"Deleted:  {deleted_count:,}")
        if failed_uploads or failed_deletes:
            print(f"Failed:   {len(failed_uploads) + len(failed_deletes):,}")
        print("=" * 80 + "\n")

        self.log.info(
            "incremental_source_mirror_complete",
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed_uploads=len(failed_uploads),
            failed_deletes=len(failed_deletes),
        )

        return SourceMirrorResult(
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed_uploads=failed_uploads,
            failed_deletes=failed_deletes,
        )

    def mirror_exact(
        self,
        snapshot: RepositorySnapshot,
        dry_run: bool = False,
    ) -> SourceMirrorResult:
        """
        Force exact mirror of repository to source store.

        Compares local snapshot vs source store state, uploads missing/changed files,
        deletes extra files.

        Args:
            snapshot: Current repository snapshot
            dry_run: If True, only show what would be changed

        Returns:
            SourceMirrorResult with operation summary
        """
        self.log.info("starting_exact_source_mirror", dry_run=dry_run)

        print("\n" + "=" * 80)
        print(f"SOURCE MIRROR - EXACT SYNC {'(DRY-RUN)' if dry_run else ''}")
        print("=" * 80)

        # List existing source store files
        print("Comparing repository vs source store...")
        stored_files = self.store.list_files()
        print(f"Local files:  {len(snapshot.files):,}")
        print(f"Stored files: {len(stored_files):,}")
        print()

        # Determine what needs to change
        local_paths = set(snapshot.files.keys())
        stored_paths = set(stored_files.keys())

        # Files to upload: new or changed (by blob OID)
        files_to_upload = []
        for path in local_paths:
            local_blob_oid = snapshot.get_file(path).blob_oid
            stored_blob_oid = stored_files.get(path)

            if stored_blob_oid is None:
                # New file
                files_to_upload.append(path)
            elif local_blob_oid != stored_blob_oid:
                # Changed file (blob OID mismatch)
                files_to_upload.append(path)

        # Files to delete: exist in source store but not locally
        files_to_delete = list(stored_paths - local_paths)

        print(f"Files to upload: {len(files_to_upload):,}")
        print(f"Files to delete: {len(files_to_delete):,}")
        print("=" * 80 + "\n")

        if dry_run:
            if files_to_upload:
                print("Would upload:")
                for path in files_to_upload[:10]:
                    file_snap = snapshot.get_file(path)
                    blob_oid = file_snap.blob_oid if file_snap else "unknown"
                    print(f"  • {path} (blob: {blob_oid[:8]}...)")
                if len(files_to_upload) > 10:
                    print(f"  ... and {len(files_to_upload) - 10} more")

            if files_to_delete:
                print("\nWould delete:")
                for path in files_to_delete[:10]:
                    print(f"  • {path}")
                if len(files_to_delete) > 10:
                    print(f"  ... and {len(files_to_delete) - 10} more")

            print()
            return SourceMirrorResult(
                uploaded=len(files_to_upload),
                deleted=len(files_to_delete),
                failed_uploads=[],
                failed_deletes=[],
            )

        # Perform actual operations
        failed_uploads: list[tuple[str, str]] = []
        failed_deletes: list[tuple[str, str]] = []

        # Upload files
        if files_to_upload:
            print("UPLOADING FILES")
            print("-" * 80)

            with ThreadPoolExecutor(max_workers=self.config.source.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._upload_file,
                        path,
                        snapshot.get_file(path).blob_oid,
                    ): path
                    for path in files_to_upload
                }

                completed = 0
                for future in as_completed(futures):
                    path = futures[future]
                    try:
                        operation = future.result()
                        if not operation.success:
                            failed_uploads.append((path, operation.error or "Unknown error"))
                            self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                            self._print_progress(f"  Error: {operation.error}")
                        else:
                            self._print_progress(f"[{completed + 1}/{len(futures)}] ✓ {path}")

                    except Exception as e:
                        error_msg = str(e)
                        failed_uploads.append((path, error_msg))
                        self._print_progress(f"[{completed + 1}/{len(futures)}] ❌ FAILED: {path}")
                        self._print_progress(f"  Error: {error_msg}")

                    completed += 1

            uploaded_count = len(files_to_upload) - len(failed_uploads)
            print(f"\n✓ Uploaded {uploaded_count:,} files")
            if failed_uploads:
                print(f"❌ Failed {len(failed_uploads):,} uploads")
            print()

        # Delete files
        if files_to_delete:
            print("DELETING FILES")
            print("-" * 80)

            deleted_count, failures = self.store.delete_files_bulk(files_to_delete)
            failed_deletes.extend(failures)

            print(f"✓ Deleted {deleted_count:,} files")
            if failed_deletes:
                print(f"❌ Failed {len(failed_deletes):,} deletions")
            print()

        uploaded_count = len(files_to_upload) - len(failed_uploads)
        deleted_count = len(files_to_delete) - len(failed_deletes)

        print("=" * 80)
        print("SOURCE EXACT MIRROR COMPLETE")
        print("=" * 80)
        print(f"Uploaded: {uploaded_count:,}")
        print(f"Deleted:  {deleted_count:,}")
        if failed_uploads or failed_deletes:
            print(f"Failed:   {len(failed_uploads) + len(failed_deletes):,}")
        print("=" * 80 + "\n")

        self.log.info(
            "exact_source_mirror_complete",
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed_uploads=len(failed_uploads),
            failed_deletes=len(failed_deletes),
        )

        return SourceMirrorResult(
            uploaded=uploaded_count,
            deleted=deleted_count,
            failed_uploads=failed_uploads,
            failed_deletes=failed_deletes,
        )

    def _upload_file(self, path: str, blob_oid: str) -> SourceOperation:
        """Copy a single file into the source store."""
        local_path = self.repo_path / path
        success, error = self.store.upload_file(local_path, path, blob_oid)
        return SourceOperation(
            path=path,
            operation="upload",
            success=success,
            error=error,
        )

    def _print_progress(self, message: str) -> None:
        """Thread-safe progress printing."""
        with self.progress_lock:
            print(message)
