"""
Local filesystem source store.

Mirrors repository files under ``<SOURCE_DIR>/<repo_id>/<path>`` so the query
agent can retrieve implementation details later. The Git blob OID of every
mirrored file is kept in a sidecar tree (``<SOURCE_DIR>/.oids/<repo_id>/<path>``)
so incremental runs can tell which files changed without reading file contents.
"""
import shutil
from pathlib import Path

import structlog

from semantic_ingress.config import SourceStoreConfig

logger = structlog.get_logger()

OID_DIR_NAME = ".oids"


class SourceStoreError(Exception):
    """Base exception for source store errors."""
    pass


class SourceStore:
    """Filesystem adapter for source mirroring operations."""

    def __init__(self, config: SourceStoreConfig, repo_id: str) -> None:
        """
        Initialize the source store.

        Args:
            config: Source store configuration
            repo_id: Repository identifier (used as the directory name)
        """
        self.config = config
        self.repo_id = repo_id
        self.log = logger.bind(component="source_store", root=str(config.root), repo_id=repo_id)

        if not repo_id or repo_id in (".", "..") or "/" in repo_id or "\\" in repo_id:
            raise SourceStoreError(f"Invalid repo_id for source storage: {repo_id!r}")

        self.repo_root = (config.root / repo_id).resolve()
        self.oid_root = (config.root / OID_DIR_NAME / repo_id).resolve()

        try:
            self.repo_root.mkdir(parents=True, exist_ok=True)
            self.oid_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SourceStoreError(f"Cannot create source directory {config.root}: {e}") from e

        self.log.info("source_store_initialized")

    def _safe_path(self, base: Path, repo_relative_path: str) -> Path:
        """Resolve a repo-relative path under ``base``, refusing traversal."""
        candidate = (base / repo_relative_path).resolve()
        if base != candidate and base not in candidate.parents:
            raise SourceStoreError(f"Path escapes repository root: {repo_relative_path}")
        return candidate

    def upload_file(
        self,
        local_path: Path,
        repo_relative_path: str,
        blob_oid: str,
    ) -> tuple[bool, str | None]:
        """
        Copy a file into the store and record its Git blob OID.

        Returns:
            Tuple of (success: bool, error_message: str | None)
        """
        try:
            dest = self._safe_path(self.repo_root, repo_relative_path)
            oid_file = self._safe_path(self.oid_root, repo_relative_path)

            dest.parent.mkdir(parents=True, exist_ok=True)
            oid_file.parent.mkdir(parents=True, exist_ok=True)

            # Write to a temp name then rename so readers never see a partial file.
            tmp = dest.with_name(dest.name + ".tmp-copy")
            shutil.copyfile(local_path, tmp)
            tmp.replace(dest)
            oid_file.write_text(blob_oid)

            self.log.debug("file_stored", path=repo_relative_path, blob_oid=blob_oid)
            return True, None

        except Exception as e:
            self.log.error("store_failed", path=repo_relative_path, error=str(e))
            return False, f"Failed to store file: {e}"

    def delete_file(self, repo_relative_path: str) -> tuple[bool, str | None]:
        """
        Delete a file from the store.

        Returns:
            Tuple of (success: bool, error_message: str | None)
        """
        try:
            for base in (self.repo_root, self.oid_root):
                target = self._safe_path(base, repo_relative_path)
                target.unlink(missing_ok=True)
                self._prune_empty_parents(target.parent, base)
            return True, None
        except Exception as e:
            self.log.error("delete_failed", path=repo_relative_path, error=str(e))
            return False, f"Failed to delete file: {e}"

    def delete_files_bulk(self, repo_relative_paths: list[str]) -> tuple[int, list[tuple[str, str]]]:
        """
        Delete multiple files from the store.

        Returns:
            Tuple of (success_count: int, failures: list[(path, error)])
        """
        if not repo_relative_paths:
            return 0, []

        self.log.info("bulk_deleting_files", count=len(repo_relative_paths))

        success_count = 0
        failures: list[tuple[str, str]] = []
        for path in repo_relative_paths:
            ok, error = self.delete_file(path)
            if ok:
                success_count += 1
            else:
                failures.append((path, error or "Unknown error"))

        self.log.info(
            "bulk_delete_complete",
            success_count=success_count,
            failure_count=len(failures),
        )
        return success_count, failures

    def list_files(self) -> dict[str, str]:
        """
        List all mirrored files for this repository.

        Returns:
            Dictionary mapping repo-relative paths to blob OIDs
        """
        files: dict[str, str] = {}
        for oid_file in self.oid_root.rglob("*"):
            if not oid_file.is_file():
                continue
            rel = oid_file.relative_to(self.oid_root).as_posix()
            if not (self.repo_root / rel).is_file():
                continue  # sidecar without a file: treat as missing so it is re-uploaded
            try:
                blob_oid = oid_file.read_text().strip()
            except OSError as e:
                self.log.warning("failed_to_read_blob_oid", path=rel, error=str(e))
                continue
            if blob_oid:
                files[rel] = blob_oid

        self.log.info("source_files_listed", count=len(files))
        return files

    def get_blob_oid(self, repo_relative_path: str) -> str | None:
        """Return the recorded Git blob OID for a mirrored file, if any."""
        try:
            oid_file = self._safe_path(self.oid_root, repo_relative_path)
            return oid_file.read_text().strip() or None
        except (OSError, SourceStoreError):
            return None

    @staticmethod
    def _prune_empty_parents(directory: Path, stop_at: Path) -> None:
        """Remove now-empty directories up to (not including) ``stop_at``."""
        while directory != stop_at and stop_at in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent
