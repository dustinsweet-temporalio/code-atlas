# semantic_ingress/core/generation_orchestrator.py
"""
Orchestrates the complete semantic generation process.
Handles unified sync operation with automatic mode detection.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import NamedTuple

import pickle
import structlog
import tempfile

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.artifact_generator import ArtifactGenerator
from semantic_ingress.core.folder_tree import (
    ancestors_inclusive,
    compute_subtree,
    direct_child_pattern,
    folders_to_regenerate,
    parent_of,
    subtree_of_child,
)
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.incremental_loader import IncrementalLoader
from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.lens.models import FileLens, FileLensRecord, FolderLensRollup
from semantic_ingress.lens.pack import Pack, load_pack
from semantic_ingress.lens.repo_pass import DocsSearch, compact_repo_lens, run_repo_pass
from semantic_ingress.lens.rollup import compact_rollup, compute_rollup, max_rel
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.lens.tier0 import scan as tier0_scan
from semantic_ingress.core.snapshot_comparer import SnapshotComparer
from semantic_ingress.core.prose_serializer import (
    serialize_file_summary,
    serialize_folder_summary,
    serialize_repo_summary,
)
from semantic_ingress.models import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    DocumentType,
    FileSemanticSummary,
    FolderSemanticSummary,
    IndexMeta,
    PackRef,
    RepositorySemanticSummary,
    RepositorySnapshot,
    SnapshotDelta,
    StoredDocument,
)
from semantic_ingress.core.source_mirror_orchestrator import SourceMirrorOrchestrator, SourceMirrorResult
from semantic_ingress.core.dir_list_generator import DirListGenerator

logger = structlog.get_logger()

FOLDER_LENS_WORDS = 40


def _cap_words(text: str, limit: int) -> str:
    """Keep whole sentences up to `limit` words (20% slack); otherwise cut at the limit."""
    words = text.split()
    if len(words) <= limit * 1.2:
        return text.strip()
    kept, count = [], 0
    for sentence in text.replace("\n", " ").split(". "):
        n = len(sentence.split())
        if count + n > limit:
            break
        kept.append(sentence.rstrip("."))
        count += n
    return (". ".join(kept) + ".") if kept else " ".join(words[:limit])


class FileArtifacts(NamedTuple):
    """Container for file-level artifacts."""
    path: str
    summary: FileSemanticSummary | None
    summary_usage: LLMUsage
    skipped: bool
    lens: FileLensRecord | None = None


class SyncResult(NamedTuple):
    """Result of sync operation."""
    documents: list[StoredDocument]
    usage: LLMUsage
    failed_items: list[tuple[str, str]]  # (path, error_message)
    delta: SnapshotDelta | None  # None for full sync
    current_snapshot: RepositorySnapshot  # Current Git state
    reset_index: bool  # True when the existing index must be dropped before persisting
    lens_updates: list[tuple[str, dict, str]] = []  # (path, lens, lens_prose) for --refresh-lens


class GenerationOrchestrator:
    """Orchestrates semantic artifact generation with unified sync operation."""

    def __init__(self, config: IngestionConfig, max_workers: int = 8) -> None:
        """
        Initialize generation orchestrator.

        Args:
            config: Ingestion configuration
            max_workers: Maximum number of parallel workers (default: 8)
        """
        self.config = config
        self.max_workers = max_workers
        self.log = logger.bind(component="generation_orchestrator")

        # Initialize components
        self.git = GitAdapter(config.repo_path, config.file_filter)
        self.llm = LLMClient(config.llm)
        self.comparer = SnapshotComparer()

        # Thread-safe usage tracking
        self.usage_lock = Lock()
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)

        # Thread-safe progress tracking
        self.progress_lock = Lock()
        
        # Thread-safe error tracking
        self.error_lock = Lock()
        self.failed_items: list[tuple[str, str]] = []

        # Temporal lens: the pack (None when the lens is off) and an optional docs
        # search for the repository pass (set by the caller; see lens/repo_pass.py).
        self.pack: Pack | None = load_pack(config.lens.pack_path) if config.lens.enabled else None
        self.docs_search: DocsSearch | None = None
        self._reset_lens_state()

    def _reset_lens_state(self) -> None:
        """Per-sync lens state: fresh records, and stored ones loaded for unchanged children."""
        self._file_lenses: dict[str, FileLensRecord] = {}
        self._folder_rollups: dict[str, FolderLensRollup] = {}
        self._stored_file_lenses: dict[str, FileLensRecord] = {}
        self._stored_rollups: dict[str, FolderLensRollup] = {}
        self._repo_lens: tuple[dict, str] | None = None  # (record, compact line)
        self._lens_only_paths: set[str] = set()

    def sync(
        self,
        agent_client,
        dry_run: bool = False,
        force: bool = False,
        refresh_lens: bool = False,
    ) -> SyncResult:
        """
        Unified sync operation with automatic mode detection.

        1. Queries the index for existing state
        2. Auto-detects full vs incremental mode
        3. Generates artifacts in-memory
        4. Returns list of StoredDocuments ready for persistence

        Args:
            agent_client: Mongo store for index operations
            dry_run: If True, don't perform actual deletions/upserts
            force: If True, run a full re-index and replace the existing index
            refresh_lens: If True, regenerate the lens of unchanged files with the installed pack

        Returns:
            SyncResult containing documents, usage, and failed items
        """
        self.log.info("starting_unified_sync", repo_id=self.config.repo_id, dry_run=dry_run, force=force)

        # Reset tracking
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)
        self.failed_items = []
        self._reset_lens_state()

        # Load previous snapshot from the index
        loader = IncrementalLoader(agent_client)
        old_snapshot = loader.load_previous_snapshot(self.config.repo_id or "unknown")

        # A full re-index replaces an existing index when forced, or when the
        # stored layout is older than this code can serve incrementally.
        reset_reason = self._full_reindex_reason(agent_client, old_snapshot, force)
        if reset_reason:
            old_snapshot = None
        self._incremental = old_snapshot is not None

        # Auto-detect mode
        if old_snapshot is None:
            reason = reset_reason or "index empty or collection doesn't exist"
            self.log.info("mode_detected", mode="FULL INDEX", reason=reason)
            print("\n" + "=" * 80)
            print(f"MODE: FULL INDEX ({reason})")
            print("=" * 80 + "\n")

            file_summaries, folder_summaries, repo_summary = self._sync_full()
        else:
            self.log.info("mode_detected", mode="INCREMENTAL")
            print("\n" + "=" * 80)
            print("MODE: INCREMENTAL (comparing index vs Git state)")
            print("=" * 80 + "\n")
            
            self._check_pack_version(agent_client, refresh_lens)
            self._check_prompt_version(agent_client)
            file_summaries, folder_summaries, repo_summary = self._sync_incremental(
                old_snapshot,
                agent_client,
                dry_run,
                refresh_lens,
            )
        
        # Convert to StoredDocuments with prose serialization
        current_snapshot = self.git.get_snapshot()
        documents = self._convert_to_stored_documents(
            file_summaries,
            folder_summaries,
            repo_summary,
            current_snapshot
        )

        # ✅ CACHE TO DISK immediately after generation
        cache_path = Path(tempfile.gettempdir()) / f"semv2_cache_{self.config.repo_id}.pkl"
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump(documents, f)
            self.log.info("documents_cached_to_disk", path=str(cache_path), count=len(documents))
            print(f"\n✅ Documents cached to: {cache_path}")
            print(f"   If persistence fails, resume with: --resume-cache {cache_path}\n")
        except Exception as e:
            self.log.warning("cache_write_failed", error=str(e))        
        
        final_usage = self._get_total_usage()
        
        # Get current snapshot for source mirroring
        current_snapshot = self.git.get_snapshot()
        
        # Determine delta for source mirroring
        if old_snapshot is None:
            final_delta = None  # Full sync
        else:
            final_delta = self.comparer.compare(old_snapshot, current_snapshot)
              
        return SyncResult(
            documents=documents,
            usage=final_usage,
            failed_items=self.failed_items,
            delta=final_delta,
            current_snapshot=current_snapshot,
            reset_index=reset_reason is not None,
            lens_updates=[
                (path, rec.model_dump(), compact_file_lens(rec))
                for path, rec in sorted(self._file_lenses.items())
                if path in self._lens_only_paths
            ],
        )

    def _full_reindex_reason(
        self,
        agent_client,
        old_snapshot: RepositorySnapshot | None,
        force: bool,
    ) -> str | None:
        """
        Why an existing index must be rebuilt from scratch, or None to proceed normally.

        A missing or older index_meta means documents lack the structured `content`
        that incremental folder regeneration needs, so they cannot be reused.
        """
        if old_snapshot is None:
            return None  # nothing to replace; a normal full index
        if force:
            return "--force: replacing the existing index"
        meta = agent_client.get_index_meta(self.config.repo_id or "unknown")
        stored_version = (meta or {}).get("schema_version", 1)
        if stored_version < SCHEMA_VERSION:
            return (
                f"index layout v{stored_version} is older than v{SCHEMA_VERSION}; "
                "rebuilding once so folder summaries can be regenerated incrementally"
            )
        return None

    def build_index_meta(self, commit_sha: str, lens_pack_versions: list[dict] | None = None,
                         prompt_versions: list[dict] | None = None) -> IndexMeta:
        """Describe how this index was built (written after successful persistence)."""
        repo_id = self.config.repo_id or "unknown"
        return IndexMeta(
            _id=IndexMeta.make_id(repo_id),
            repo_id=repo_id,
            index_model=self.config.llm.model_id,
            commit_sha=commit_sha,
            lens_enabled=self.pack is not None,
            pack=PackRef(id=self.pack.id, version=self.pack.version, hash=self.pack.hash) if self.pack else None,
            lens_pack_versions=lens_pack_versions or [],
            prompt_versions=prompt_versions or [],
        )


    def _sync_full(self) -> tuple[dict[str, FileSemanticSummary], dict[str, FolderSemanticSummary], RepositorySemanticSummary]:
        """
        Perform full semantic generation for the entire repository.
        Returns in-memory artifacts without writing to disk.
        
        Returns:
            Tuple of (file_summaries, folder_summaries, repo_summary)
        """
        self.log.info("starting_full_sync", repo_id=self.config.repo_id)

        # Get current repository snapshot
        snapshot = self.git.get_snapshot()
        self.log.info("snapshot_acquired", commit_sha=snapshot.commit_sha, file_count=len(snapshot.files))

        # Initialize generator
        semantic_generator = ArtifactGenerator(self.git, self.llm, snapshot.commit_sha, pack=self.pack)

        # Generate file summaries
        file_summaries: dict[str, FileSemanticSummary] = {}
        file_count = len(snapshot.files)

        print("\n" + "=" * 80)
        print(f"GENERATING FILE SUMMARIES ({self.max_workers} workers)")
        print("=" * 80)

        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._process_file,
                    path,
                    file_snapshot.blob_oid,
                    semantic_generator,
                ): path
                for path, file_snapshot in snapshot.files.items()
            }

            for future in as_completed(futures):
                path = futures[future]
                try:
                    artifacts = future.result()
                    
                    if artifacts.summary:
                        file_summaries[path] = artifacts.summary
                    if artifacts.lens is not None:
                        self._file_lenses[path] = artifacts.lens
                    
                    self._accumulate_usage(artifacts.summary_usage)
                    
                    completed += 1
                    current_usage = self._get_total_usage()
                    
                    status_lines = [f"[{completed}/{file_count}] {path}"]
                    
                    if artifacts.summary_usage.input_tokens > 0:
                        status_lines.append(
                            f"  📝 {artifacts.summary_usage.input_tokens:,} in / "
                            f"{artifacts.summary_usage.output_tokens:,} out | "
                            f"${artifacts.summary_usage.total_cost_usd:.4f} | "
                            f"Total: ${current_usage.total_cost_usd:.4f}"
                        )
                    
                    self._print_progress("\n".join(status_lines))
                    
                except Exception as e:
                    error_msg = str(e)
                    self.log.error("file_processing_failed", path=path, error=error_msg)
                    with self.error_lock:
                        self.failed_items.append((path, error_msg))
                    self._print_progress(f"[{completed + 1}/{file_count}] ❌ FAILED: {path}")
                    self._print_progress(f"  Error: {error_msg}")
                    completed += 1

        self.log.info("file_artifacts_generated", summary_count=len(file_summaries))

        # Generate folder summaries
        print("\n" + "=" * 80)
        print("GENERATING FOLDER SUMMARIES")
        print("=" * 80)
        folder_summaries = self._generate_folder_artifacts(semantic_generator, file_summaries)
        self.log.info("folder_artifacts_generated", summary_count=len(folder_summaries))

        # Generate repository summary (from the root and top-level folder summaries)
        repo_summary = self._generate_repo_summary(semantic_generator, folder_summaries)

        final_usage = self._get_total_usage()
        print("\n" + "=" * 80)
        print("FULL SYNC COMPLETE")
        print("=" * 80)
        print(f"Total Input Tokens:  {final_usage.input_tokens:,}")
        if final_usage.cache_read_tokens or final_usage.cache_write_tokens:
            print(f"Cache Read Tokens:   {final_usage.cache_read_tokens:,}")
            print(f"Cache Write Tokens:  {final_usage.cache_write_tokens:,}")
        print(f"Total Output Tokens: {final_usage.output_tokens:,}")
        print(f"Total Cost:          ${final_usage.total_cost_usd:.4f}")
        
        if self.failed_items:
            print(f"\n⚠️  FAILED ITEMS: {len(self.failed_items)}")
            print("-" * 80)
            for path, error in self.failed_items:
                print(f"  • {path}")
                print(f"    {error}")
        
        print("=" * 80 + "\n")

        self.log.info("full_sync_complete", repo_id=self.config.repo_id, failed_count=len(self.failed_items))
        
        return file_summaries, folder_summaries, repo_summary

    def _sync_incremental(
        self,
        old_snapshot: RepositorySnapshot,
        agent_client,
        dry_run: bool,
        refresh_lens: bool = False,
    ) -> tuple[dict[str, FileSemanticSummary], dict[str, FolderSemanticSummary], RepositorySemanticSummary]:
        """
        Perform incremental semantic generation.
        
        Args:
            old_snapshot: Previous repository snapshot from the index
            agent_client: Mongo store for deletions
            dry_run: If True, don't perform actual deletions
            refresh_lens: Regenerate the lens of unchanged files, then every folder and the repo lens
        
        Returns:
            Tuple of (file_summaries, folder_summaries, repo_summary)
        """
        self.log.info("starting_incremental_sync", repo_id=self.config.repo_id)

        # Get current snapshot
        new_snapshot = self.git.get_snapshot()
        semantic_generator = ArtifactGenerator(self.git, self.llm, new_snapshot.commit_sha, pack=self.pack)

        # Compare snapshots
        delta = self.comparer.compare(old_snapshot, new_snapshot)

        self.log.info(
            "delta_computed",
            added=len(delta.added),
            modified=len(delta.modified),
            deleted=len(delta.deleted),
        )

        print("Delta Summary:")
        print(f"  Added:     {len(delta.added):,} files")
        print(f"  Modified:  {len(delta.modified):,} files")
        print(f"  Deleted:   {len(delta.deleted):,} files")
        print(f"  Unchanged: {len(delta.unchanged):,} files")
        print()

        # Handle deletions FIRST
        if delta.deleted:
            print("=" * 80)
            print(f"DELETING {len(delta.deleted)} FILES FROM INDEX")
            print("=" * 80)
            
            deleted_paths = [change.path for change in delta.deleted]
            
            if not dry_run:
                # Delete file summaries
                file_summary_ids = [
                    StoredDocument.generate_id(
                        self.config.repo_id or "unknown",
                        DocumentType.FILE_SUMMARY,
                        path
                    )
                    for path in deleted_paths
                ]
                
                try:
                    result = agent_client.delete_documents_bulk(
                        file_summary_ids,
                        self.config.repo_id or "unknown"
                    )
                    print(f"✓ Deleted {result['total_deleted']} file summary artifacts")
                    self.log.info("deleted_file_summaries", **result)
                except Exception as e:
                    self.log.error("failed_to_delete_file_summaries", error=str(e))
                    print(f"❌ Failed to delete file summaries: {e}")
                
                # Clean up orphaned folders
                print("\nCleaning up orphaned folders...")
                self._cleanup_orphaned_folders(deleted_paths, agent_client)
            else:
                print("DRY-RUN: Would delete file summaries for:")
                for path in deleted_paths[:10]:
                    print(f"  • {path}")
                if len(deleted_paths) > 10:
                    print(f"  ... and {len(deleted_paths) - 10} more")
            
            print("=" * 80 + "\n")

        # Any file whose git blob changed is regenerated (added or modified).
        files_to_regenerate: set[str] = {c.path for c in delta.added} | {c.path for c in delta.modified}
        deleted_paths_set: set[str] = {c.path for c in delta.deleted}

        # Generate summaries for files that need it
        file_summaries: dict[str, FileSemanticSummary] = {}

        if files_to_regenerate:
            print(f"REGENERATING FILE SUMMARIES ({self.max_workers} workers)")
            print("-" * 80)
            regen_count = len(files_to_regenerate)
            
            completed = 0
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._regenerate_file,
                        path,
                        new_snapshot.get_file(path).blob_oid,
                        semantic_generator,
                    ): path
                    for path in files_to_regenerate
                }

                for future in as_completed(futures):
                    path = futures[future]
                    try:
                        artifacts = future.result()
                        
                        if artifacts.summary:
                            file_summaries[path] = artifacts.summary
                        if artifacts.lens is not None:
                            self._file_lenses[path] = artifacts.lens
                        
                        self._accumulate_usage(artifacts.summary_usage)
                        
                        completed += 1
                        current_usage = self._get_total_usage()
                        
                        status_lines = [f"[{completed}/{regen_count}] {path}"]
                        status_lines.append(
                            f"  📝 {artifacts.summary_usage.input_tokens:,} in / "
                            f"{artifacts.summary_usage.output_tokens:,} out | "
                            f"${artifacts.summary_usage.total_cost_usd:.4f} | "
                            f"Total: ${current_usage.total_cost_usd:.4f}"
                        )
                        
                        self._print_progress("\n".join(status_lines))
                        
                    except Exception as e:
                        error_msg = str(e)
                        self.log.error("file_regeneration_failed", path=path, error=error_msg)
                        with self.error_lock:
                            self.failed_items.append((path, f"Regeneration: {error_msg}"))
                        self._print_progress(f"[{completed + 1}/{regen_count}] ❌ FAILED: {path}")
                        self._print_progress(f"  Error: {error_msg}")
                        completed += 1

            self.log.info("file_artifacts_regenerated", summary_count=len(file_summaries))

        # --refresh-lens: lens only (no re-summarising) for every unchanged file.
        if refresh_lens and self.pack is not None:
            self._refresh_unchanged_lenses(
                semantic_generator, new_snapshot, set(new_snapshot.files) - files_to_regenerate
            )

        # Folders to rebuild: every surviving ancestor of a changed or deleted file
        # (every folder when the lens is refreshed, so roll-ups use the new records).
        existing_folders = self._get_all_folders_from_files(new_snapshot.files.keys())
        if self._lens_only_paths:
            affected_folders = set(existing_folders)
        else:
            affected_folders = folders_to_regenerate(files_to_regenerate, deleted_paths_set, existing_folders)

        # Regenerate affected folders from ALL their children: fresh file summaries plus
        # the stored summaries of unchanged sibling files and unchanged child folders.
        folder_summaries: dict[str, FolderSemanticSummary] = {}

        if affected_folders:
            print("\n" + "=" * 80)
            print("REGENERATING FOLDER SUMMARIES")
            print("=" * 80)
            stored_files, stored_folders = self._load_children_for_folders(
                agent_client,
                affected_folders,
                skip_files=set(file_summaries) | deleted_paths_set,
            )
            print(
                f"Loaded {len(stored_files):,} unchanged file summaries and "
                f"{len(stored_folders):,} unchanged folder summaries from the index\n"
            )
            folder_summaries = self._generate_folder_artifacts(
                semantic_generator,
                {**stored_files, **file_summaries},
                affected_folders,
                known_folder_summaries=stored_folders,
            )
            self.log.info("folder_artifacts_regenerated", summary_count=len(folder_summaries))

        # Regenerate the repo summary if any folder changed, or if none is stored yet
        # (this also adds one to indexes created before repo summaries were enabled).
        repo_summary: RepositorySemanticSummary | None = None
        if folder_summaries or not self._repo_summary_exists(agent_client, need_lens=self.pack is not None):
            repo_summary = self._generate_repo_summary(
                semantic_generator,
                folder_summaries,
                agent_client,
            )
        else:
            self.log.info("skipping_repo_summary_no_changes")

        final_usage = self._get_total_usage()
        print("\n" + "=" * 80)
        print("INCREMENTAL SYNC COMPLETE")
        print("=" * 80)
        print(f"Total Input Tokens:  {final_usage.input_tokens:,}")
        if final_usage.cache_read_tokens or final_usage.cache_write_tokens:
            print(f"Cache Read Tokens:   {final_usage.cache_read_tokens:,}")
            print(f"Cache Write Tokens:  {final_usage.cache_write_tokens:,}")
        print(f"Total Output Tokens: {final_usage.output_tokens:,}")
        print(f"Total Cost:          ${final_usage.total_cost_usd:.4f}")
        
        if self.failed_items:
            print(f"\n⚠️  FAILED ITEMS: {len(self.failed_items)}")
            print("-" * 80)
            for path, error in self.failed_items:
                print(f"  • {path}")
                print(f"    {error}")
        
        print("=" * 80 + "\n")

        self.log.info("incremental_sync_complete", repo_id=self.config.repo_id, failed_count=len(self.failed_items))
        
        return file_summaries, folder_summaries, repo_summary

    def _convert_to_stored_documents(
        self,
        file_summaries: dict[str, FileSemanticSummary],
        folder_summaries: dict[str, FolderSemanticSummary],
        repo_summary: RepositorySemanticSummary,
        current_snapshot: RepositorySnapshot
    ) -> list[StoredDocument]:
        """Convert in-memory summaries to StoredDocuments with prose serialization."""
        documents = []
        commit_sha = current_snapshot.commit_sha
        
        # File summaries (SERIALIZE TO PROSE)
        for path, summary in file_summaries.items():
            file_snapshot = current_snapshot.files.get(path)
            if file_snapshot:
                prose = serialize_file_summary(summary)
                doc = StoredDocument.for_file_summary(
                    repo_id=self.config.repo_id or "unknown",
                    path=path,
                    commit_sha=commit_sha,
                    blob_oid=file_snapshot.blob_oid,
                    prose_summary=prose,
                    content=summary.model_dump(),
                )
                lens = self._file_lenses.get(path)
                if lens is not None:
                    doc.lens, doc.lens_prose = lens.model_dump(), compact_file_lens(lens)
                documents.append(doc)
        
        # Folder summaries (SERIALIZE TO PROSE)
        for path, summary in folder_summaries.items():
            prose = serialize_folder_summary(summary)
            # FIX: Ensure root folder uses "." instead of empty string
            normalized_path = "." if path == "" else path
            doc = StoredDocument.for_folder_summary(
                repo_id=self.config.repo_id or "unknown",
                path=normalized_path,  # Use normalized path
                commit_sha=commit_sha,
                prose_summary=prose,
                content=summary.model_dump(),
            )
            rollup = self._folder_rollups.get(path)
            if rollup is not None:
                doc.lens, doc.lens_prose = rollup.model_dump(), compact_rollup(rollup)
            documents.append(doc)
        
        # Repo summary (SERIALIZE TO PROSE) - only if one was generated
        if repo_summary is not None:
            prose = serialize_repo_summary(repo_summary)
            doc = StoredDocument.for_repo_summary(
                repo_id=self.config.repo_id or "unknown",
                commit_sha=commit_sha,
                prose_summary=prose,
                content=repo_summary.model_dump(),
            )
            if self._repo_lens is not None:
                doc.lens, doc.lens_prose = self._repo_lens
            documents.append(doc)

        # ✅ DIR LIST DOCUMENT
        # Always generate dir_list at the end
        dir_list_doc = self._generate_dir_list_document(current_snapshot)
        documents.append(dir_list_doc)            
        
        return documents
    
    def _generate_dir_list_document(
        self,
        current_snapshot: RepositorySnapshot,
    ) -> StoredDocument:
        """
        Generate directory listing document.
        
        Args:
            current_snapshot: Current repository snapshot
            
        Returns:
            StoredDocument for dir_list
        """
        self.log.info("generating_dir_list_document")
        
        generator = DirListGenerator()
        dir_list_content = generator.generate(current_snapshot)
        
        doc = StoredDocument.for_dir_list(
            repo_id=self.config.repo_id or "unknown",
            commit_sha=current_snapshot.commit_sha,
            dir_list_content=dir_list_content,
        )
        
        self.log.info("dir_list_document_generated", line_count=len(dir_list_content.splitlines()))
        
        return doc

    def _validate_and_ensure_indexes(self, agent_client) -> None:
        """
        Validate text indexes exist and work properly.
        Create indexes if missing. Skip repo_summaries collection.
        
        Args:
            agent_client: Mongo store for index operations
        """
        self.log.info("validating_text_indexes", repo_id=self.config.repo_id)
              
        # Collections to validate (skip repo_summaries per requirements)
        collections = ["file_summaries", "folder_summaries"]
        
        # Generic search terms that should exist in most repos
        validation_terms = ["file", "class", "function", "import"]
        
        database = agent_client._get_database_name(self.config.repo_id or "unknown")
        for collection in collections:
            print(f"\nValidating {collection}...")

            try:
                # Make sure the index covers prose_summary and lens_prose (replaces older indexes)
                if agent_client.ensure_text_index(database, collection) == "created":
                    print(f"  ✓ Created text index on {collection} (prose_summary, lens_prose)")

                if self._validate_index_search(agent_client, collection, validation_terms):
                    print(f"  ✓ Text index on {collection} is working")
                else:
                    raise Exception(f"Text index search returned no results for {collection}")

            except Exception as e:
                self.log.error("index_validation_failed", collection=collection, error=str(e))
                print(f"  ❌ FAILED: {e}")
                # Don't fail the entire sync, just warn
                print(f"  ⚠️  Text search may not work properly for {collection}")

    def _create_indexes_without_validation(self, agent_client) -> None:
        """
        Create text indexes without validation (for full index mode).
        
        Args:
            agent_client: Mongo store for index operations
        """
        self.log.info("creating_text_indexes_without_validation", repo_id=self.config.repo_id)
        
        # Collections to create indexes for (skip repo_summaries per requirements)
        collections = ["file_summaries", "folder_summaries"]
        
        database = agent_client._get_database_name(self.config.repo_id or "unknown")
        for collection in collections:
            print(f"\nCreating index for {collection}...")

            try:
                status = agent_client.ensure_text_index(database, collection)
                if status == "created":
                    print(f"  ✓ Created text index on {collection} (prose_summary, lens_prose)")
                else:
                    print(f"  ℹ️  Text index already present on {collection}")

            except Exception as e:
                self.log.error("index_creation_failed", collection=collection, error=str(e))
                print(f"  ⚠️  FAILED: {e}")
                # Don't fail the entire sync, just warn
                print(f"  ⚠️  Text search may not work properly for {collection}")

    def _validate_index_search(
        self,
        agent_client,
        collection: str,
        search_terms: list[str]
    ) -> bool:
        """
        Validate text index by performing test searches.
        Returns True if at least one search term returns results.
        
        Args:
            agent_client: Mongo store
            collection: Collection name to search
            search_terms: List of terms to search for
        
        Returns:
            True if validation passes (at least one term found)
        """
        database = agent_client._get_database_name(self.config.repo_id or "unknown")
        
        for term in search_terms:
            try:
                results = agent_client.text_search(
                    database=database,
                    collection=collection,
                    search_text=term,
                    limit=1,
                )
                
                if results:
                    self.log.debug(
                        "validation_search_success",
                        collection=collection,
                        term=term,
                        result_count=len(results),
                    )
                    return True
            
            except Exception as e:
                self.log.debug(
                    "validation_search_failed",
                    collection=collection,
                    term=term,
                    error=str(e),
                )
                continue
        
        # All searches returned 0 results
        return False

    def _accumulate_usage(self, usage: LLMUsage) -> None:
        """Thread-safe usage accumulation."""
        with self.usage_lock:
            self.total_usage = self.total_usage + usage

    def _get_total_usage(self) -> LLMUsage:
        """Thread-safe usage retrieval."""
        with self.usage_lock:
            return self.total_usage

    def _print_progress(self, message: str) -> None:
        """Thread-safe progress printing."""
        with self.progress_lock:
            print(message)

    def _process_file(
        self,
        path: str,
        blob_oid: str,
        semantic_generator: ArtifactGenerator,
    ) -> FileArtifacts:
        """Process a single file: generate summary."""
        summary, lens, summary_usage = semantic_generator.generate_file_summary(path, blob_oid)
        return FileArtifacts(
            path=path,
            summary=summary,
            summary_usage=summary_usage,
            skipped=False,
            lens=lens,
        )

    def _regenerate_file(
        self,
        path: str,
        blob_oid: str,
        semantic_generator: ArtifactGenerator,
    ) -> FileArtifacts:
        """Regenerate summary for a single file."""
        summary, lens, summary_usage = semantic_generator.generate_file_summary(path, blob_oid)
        return FileArtifacts(
            path=path,
            summary=summary,
            summary_usage=summary_usage,
            skipped=False,
            lens=lens,
        )

    def _get_all_folders_from_files(self, file_paths: set[str] | list[str]) -> set[str]:
        """Extract all unique folder paths from file paths."""
        folders: set[str] = set()
        for file_path in file_paths:
            path_obj = Path(file_path)
            for parent in path_obj.parents:
                parent_str = str(parent)
                if parent_str == ".":
                    parent_str = ""
                folders.add(parent_str)
        return folders

    def _cleanup_orphaned_folders(self, deleted_file_paths: list[str], agent_client) -> None:
        """Delete folder artifacts for folders that became empty."""
        if not deleted_file_paths:
            return

        self.log.info("checking_for_orphaned_folders", deleted_files=len(deleted_file_paths))

        # Get current folder structure from Git
        current_snapshot = self.git.get_snapshot()
        existing_folders = self._get_all_folders_from_files(current_snapshot.files.keys())

        # Get folders affected by deletions
        affected_folders = self._get_affected_folders(set(deleted_file_paths))

        # Find orphaned folders
        orphaned_folders = affected_folders - existing_folders

        if not orphaned_folders:
            self.log.info("no_orphaned_folders_found")
            print("  ✓ No orphaned folders")
            return

        self.log.info("found_orphaned_folders", count=len(orphaned_folders))
        print(f"  Found {len(orphaned_folders)} orphaned folders")

        # Delete folder summaries
        folder_summary_ids = [
            StoredDocument.generate_id(
                self.config.repo_id or "unknown",
                DocumentType.FOLDER_SUMMARY,
                folder
            )
            for folder in orphaned_folders
        ]

        try:
            result = agent_client.delete_documents_bulk(
                folder_summary_ids,
                self.config.repo_id or "unknown"
            )
            print(f"  ✓ Deleted {result['total_deleted']} folder summary artifacts")
            self.log.info("deleted_orphaned_folder_summaries", **result)
        except Exception as e:
            self.log.error("failed_to_delete_folder_summaries", error=str(e))
            print(f"  ⚠️  Failed to delete folder summaries: {e}")

    def _generate_folder_artifacts(
        self,
        semantic_generator: ArtifactGenerator,
        file_summaries: dict[str, FileSemanticSummary],
        specific_folders: set[str] | None = None,
        known_folder_summaries: dict[str, FolderSemanticSummary] | None = None,
    ) -> dict[str, FolderSemanticSummary]:
        """
        Generate folder summaries bottom-up.

        Each folder is summarised from all of its direct children: the file summaries
        passed in, and child folder summaries generated in this run or, for child
        folders not regenerated, taken from `known_folder_summaries` (stored ones).
        Child folder summaries already cover their own subtrees, so every folder
        summary describes everything beneath it. Subtree counts are computed here.

        Args:
            file_summaries: Summaries of every file to consider (fresh and stored)
            specific_folders: Only regenerate these folders (None = all)
            known_folder_summaries: Stored summaries of child folders that are not regenerated
        """
        known = known_folder_summaries or {}

        folders_with_files: dict[str, list[str]] = defaultdict(list)
        for file_path in file_summaries:
            folders_with_files[parent_of(file_path)].append(file_path)

        all_folders: set[str] = set()
        for path in list(file_summaries) + list(known):
            all_folders.update(ancestors_inclusive(path, is_folder=path in known))

        folder_children: dict[str, set[str]] = defaultdict(set)
        for folder in all_folders:
            if folder != "":
                folder_children[parent_of(folder)].add(folder)

        folder_paths = sorted(
            all_folders,
            key=lambda p: p.count("/") if p else -1,
            reverse=True,
        )

        if specific_folders is not None:
            folder_paths = [p for p in folder_paths if p in specific_folders]

        folder_summaries: dict[str, FolderSemanticSummary] = {}
        folder_count = len(folder_paths)

        for idx, folder_path in enumerate(folder_paths, 1):
            display_path = folder_path if folder_path else "."

            child_files_summaries = {
                Path(p).name: file_summaries[p]
                for p in sorted(folders_with_files.get(folder_path, []))
            }

            child_subfolders_summaries: dict[str, FolderSemanticSummary] = {}
            for child_folder_path in sorted(folder_children.get(folder_path, set())):
                child = folder_summaries.get(child_folder_path) or known.get(child_folder_path)
                if child is not None:
                    child_subfolders_summaries[Path(child_folder_path).name] = child

            if not child_files_summaries and not child_subfolders_summaries:
                continue

            print(f"[{idx}/{folder_count}] {display_path}")

            try:
                rollup = None
                if self.pack is not None:
                    rollup, file_lines, folder_lines = self._folder_lens_inputs(
                        folder_path, folders_with_files.get(folder_path, []), folder_children.get(folder_path, set())
                    )
                if rollup is not None and max_rel(rollup) >= 2:
                    summary, summary_usage, lens_n = semantic_generator.generate_folder_summary_with_lens(
                        display_path,
                        child_files_summaries,
                        child_subfolders_summaries,
                        file_lines,
                        folder_lines,
                    )
                    rollup.n = _cap_words(lens_n, FOLDER_LENS_WORDS)
                else:
                    summary, summary_usage = semantic_generator.generate_folder_summary(
                        display_path,
                        child_files_summaries,
                        child_subfolders_summaries,
                    )
                if rollup is not None:
                    self._folder_rollups[folder_path] = rollup
                summary.subtree = compute_subtree(
                    len(child_files_summaries),
                    {
                        name: subtree_of_child(child.subtree, len(child.files))
                        for name, child in child_subfolders_summaries.items()
                    },
                )
                folder_summaries[folder_path] = summary
                self._accumulate_usage(summary_usage)
                current_usage = self._get_total_usage()
                print(f"  📝 {summary_usage.input_tokens:,} in / {summary_usage.output_tokens:,} out | "
                      f"${summary_usage.total_cost_usd:.4f} | Total: ${current_usage.total_cost_usd:.4f}")
            except Exception as e:
                error_msg = str(e)
                self.log.error("folder_summary_generation_failed", path=display_path, error=error_msg)
                with self.error_lock:
                    self.failed_items.append((f"FOLDER SUMMARY: {display_path}", error_msg))
                print(f"  ❌ FAILED: {error_msg}")

        return folder_summaries

    def _load_children_for_folders(
        self,
        agent_client,
        folders: set[str],
        skip_files: set[str],
    ) -> tuple[dict[str, FileSemanticSummary], dict[str, FolderSemanticSummary]]:
        """
        Load the stored summaries of the direct children of `folders`.

        Returns (files, folders): stored file summaries of direct child files not in
        `skip_files` (those were regenerated or deleted), and stored folder summaries
        of direct child folders that are not themselves being regenerated.
        """
        repo_id = self.config.repo_id or "unknown"
        patterns = [direct_child_pattern(f) for f in sorted(folders)]

        file_docs = agent_client.query_documents(
            repo_id=repo_id,
            collection="file_summaries",
            filter_query={"path": {"$in": patterns}},
            projection={"path": 1, "content": 1, "lens": 1},
        )
        folder_docs = agent_client.query_documents(
            repo_id=repo_id,
            collection="folder_summaries",
            filter_query={"path": {"$in": patterns}},
            projection={"path": 1, "content": 1, "lens": 1},
        )

        stored_files: dict[str, FileSemanticSummary] = {}
        for doc in file_docs:
            path = doc.get("path")
            if not path or path in skip_files:
                continue
            if not doc.get("content"):
                self.log.warning("stored_file_summary_without_content", path=path)
                continue
            stored_files[path] = FileSemanticSummary.model_validate(doc["content"])
            if doc.get("lens"):
                self._stored_file_lenses[path] = FileLensRecord.model_validate(doc["lens"])

        stored_folders: dict[str, FolderSemanticSummary] = {}
        for doc in folder_docs:
            path = doc.get("path")
            if not path or path == "." or path in folders:
                continue  # the root is never a child; regenerated folders come from this run
            if not doc.get("content"):
                self.log.warning("stored_folder_summary_without_content", path=path)
                continue
            stored_folders[path] = FolderSemanticSummary.model_validate(doc["content"])
            if doc.get("lens"):
                self._stored_rollups[path] = FolderLensRollup.model_validate(doc["lens"])

        return stored_files, stored_folders

    # ------------------------------------------------------------------
    # Temporal lens
    # ------------------------------------------------------------------

    def _check_pack_version(self, agent_client, refresh_lens: bool) -> None:
        """Warn when the index's lens was built with another pack version (never regenerates on its own)."""
        if self.pack is None:
            if refresh_lens:
                raise ValueError("--refresh-lens needs LENS_ENABLED=true")
            return
        meta = agent_client.get_index_meta(self.config.repo_id or "unknown") or {}
        built_with = (meta.get("pack") or {}).get("version")
        if not meta.get("lens_enabled"):
            if not refresh_lens:
                print("ℹ️  This index has no Temporal lens yet. Changed files get one now; "
                      "run with --refresh-lens to add it to every file.\n")
        elif built_with and built_with != self.pack.version and not refresh_lens:
            print(f"⚠️  Index lens built with pack {built_with}; installed pack is {self.pack.version}. "
                  f"Changed files will use {self.pack.version}. Run with --refresh-lens to update all files.\n")

    def _check_prompt_version(self, agent_client) -> None:
        """Warn when stored summaries were written by an older prompt version (never regenerates on its own)."""
        versions = agent_client.prompt_versions(self.config.repo_id or "unknown")
        older = sum(v["files"] for v in versions if v["version"] != PROMPT_VERSION)
        if older:
            print(f"⚠️  {older:,} files were summarised with an older prompt version; changed files use "
                  f"version {PROMPT_VERSION}. Run with --force to regenerate every summary.\n")

    def _refresh_unchanged_lenses(self, semantic_generator, snapshot, paths: set[str]) -> None:
        """Regenerate the lens record (only) of unchanged files with the installed pack."""
        if not paths:
            return
        print(f"REFRESHING LENS FOR {len(paths):,} UNCHANGED FILES (pack {self.pack.version})")
        print("-" * 80)
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(semantic_generator.generate_file_lens, path, snapshot.get_file(path).blob_oid): path
                for path in sorted(paths)
            }
            for future in as_completed(futures):
                path = futures[future]
                completed += 1
                try:
                    record, usage = future.result()
                    self._file_lenses[path] = record
                    self._lens_only_paths.add(path)
                    self._accumulate_usage(usage)
                    self._print_progress(f"[{completed}/{len(paths)}] {path} rel={record.rel} | ${usage.total_cost_usd:.4f}")
                except Exception as e:
                    self.log.error("lens_refresh_failed", path=path, error=str(e))
                    with self.error_lock:
                        self.failed_items.append((path, f"Lens refresh: {e}"))
                    self._print_progress(f"[{completed}/{len(paths)}] ❌ FAILED: {path}")
        print()

    def _lens_of_file(self, path: str) -> FileLensRecord | None:
        return self._file_lenses.get(path) or self._stored_file_lenses.get(path)

    def _folder_lens_inputs(
        self, folder_path: str, direct_files: list[str], child_folders: set[str]
    ) -> tuple[FolderLensRollup, str, str]:
        """The folder's roll-up and the compact lens lines its lens narrative is written from."""
        file_lenses = {p: lens for p in direct_files if (lens := self._lens_of_file(p)) is not None}
        child_rollups = {
            c: r for c in sorted(child_folders)
            if (r := self._folder_rollups.get(c) or self._stored_rollups.get(c)) is not None
        }
        rollup = compute_rollup(file_lenses, list(child_rollups.values()), self.pack.version)
        file_lines = "\n".join(f"{Path(p).name}: {compact_file_lens(l)}" for p, l in sorted(file_lenses.items()))
        folder_lines = "\n".join(f"{Path(c).name}/: {compact_rollup(r)}" for c, r in child_rollups.items())
        return rollup, file_lines, folder_lines

    def _generate_repo_lens(self, agent_client) -> None:
        """Tier 0 scan plus the repository lens pass; stores the result for persistence."""
        print("\nGENERATING REPOSITORY LENS")
        snapshot = self.git.get_snapshot()
        contents = {path: self.git.get_file_content(fs.blob_oid) for path, fs in snapshot.files.items()}
        tier0 = tier0_scan(self.pack, contents)

        # Records with rel >= 1 (rel 3 first within the budget; rel 1 gives gates their
        # evidence): fresh ones, plus stored ones for files not regenerated.
        records: dict[str, tuple[int, str]] = {}
        root_rollup = self._folder_rollups.get("")
        if agent_client is not None:
            stored = agent_client.query_documents(
                repo_id=self.config.repo_id or "unknown",
                collection="file_summaries",
                filter_query={"lens.rel": {"$gte": 1}},
                projection={"path": 1, "lens.rel": 1, "lens_prose": 1},
            )
            for doc in stored:
                if doc.get("path") in snapshot.files and doc.get("lens_prose"):
                    records[doc["path"]] = (doc["lens"]["rel"], doc["lens_prose"])
            if root_rollup is None:
                root_docs = agent_client.query_documents(
                    repo_id=self.config.repo_id or "unknown", collection="folder_summaries",
                    filter_query={"path": "."}, projection={"lens": 1}, limit=1,
                )
                if root_docs and root_docs[0].get("lens"):
                    root_rollup = FolderLensRollup.model_validate(root_docs[0]["lens"])
        for path, lens in self._file_lenses.items():
            if lens.rel >= 1:
                records[path] = (lens.rel, compact_file_lens(lens))
            else:
                records.pop(path, None)

        # Incremental runs keep the process list stable: the pass sees the previous
        # processes and the changed files, and changes only what the changes call for.
        previous = None
        if agent_client is not None and getattr(self, "_incremental", False):
            docs = agent_client.query_documents(
                repo_id=self.config.repo_id or "unknown", collection="repo_summaries",
                filter_query={"document_type": "repo_summary"}, projection={"lens.processes": 1}, limit=1,
            )
            if docs and (docs[0].get("lens") or {}).get("processes"):
                previous = docs[0]["lens"]["processes"]

        result = run_repo_pass(
            self.llm, self.pack, tier0,
            compact_rollup(root_rollup) if root_rollup else "(no roll-up)",
            records, set(snapshot.files), self.docs_search,
            previous=previous, changed=sorted(self._file_lenses) if previous else None,
        )
        self._accumulate_usage(result.usage)
        record = {
            **result.lens.model_dump(),
            "pack_version": self.pack.version,
            "docs_searches": result.searches,
            "docs_urls_returned": result.urls_returned,
            "corrections": result.corrections,
            "tier0": {
                "languages": tier0.languages,
                "temporal_counts": tier0.temporal_counts,
                "temporal_manifests": tier0.temporal_manifests,
            },
        }
        self._repo_lens = (record, compact_repo_lens(result.lens))
        print(f"  status={result.lens.status} archetype={result.lens.archetype} "
              f"processes={len(result.lens.processes)} searches={result.searches} | "
              f"${result.usage.total_cost_usd:.4f}")

    def _get_affected_folders(self, changed_files: set[str]) -> set[str]:
        """Determine which folders are affected by file changes."""
        affected: set[str] = set()
        for file_path in changed_files:
            path = Path(file_path)
            for parent in path.parents:
                parent_str = str(parent)
                if parent_str == ".":
                    parent_str = ""
                affected.add(parent_str)
        return affected

    # Cap on the prose sent for the repository summary (root + top-level folders).
    REPO_SUMMARY_MAX_INPUT_CHARS = 400_000

    def _repo_summary_exists(self, agent_client, need_lens: bool = False) -> bool:
        """True if a repository summary (with a repo lens, when need_lens) is already stored."""
        repo_id = self.config.repo_id or "unknown"
        doc_id = StoredDocument.generate_id(repo_id, DocumentType.REPO_SUMMARY, "")
        try:
            docs = agent_client.query_documents(
                repo_id=repo_id,
                collection="repo_summaries",
                filter_query={"_id": doc_id},
                projection={"_id": 1, "lens": 1},
                limit=1,
            )
            return bool(docs) and (not need_lens or bool(docs[0].get("lens")))
        except Exception as e:
            self.log.warning("repo_summary_existence_check_failed", error=str(e))
            return True  # do not spend tokens on a guess

    def _collect_repo_summary_input(
        self,
        folder_summaries: dict[str, FolderSemanticSummary],
        agent_client=None,
    ) -> str:
        """
        Build the prompt text for the repository summary: the root folder summary
        followed by the top-level folder summaries, as prose.

        Freshly generated summaries win; in incremental runs the rest come from the
        index, since unchanged folders are not regenerated.
        """
        repo_id = self.config.repo_id or "unknown"

        fresh = {
            ("." if path == "" else path): serialize_folder_summary(summary)
            for path, summary in folder_summaries.items()
        }

        stored: dict[str, str] = {}
        if agent_client is not None:
            docs = agent_client.query_documents(
                repo_id=repo_id,
                collection="folder_summaries",
                filter_query={"path": {"$regex": r"^(\.|[^/]+)$"}},
                projection={"path": 1, "prose_summary": 1},
            )
            stored = {
                d["path"]: d["prose_summary"]
                for d in docs
                if d.get("path") and d.get("prose_summary")
            }

        merged = {**stored, **fresh}
        selected = {p: t for p, t in merged.items() if p == "." or "/" not in p}
        ordered = sorted(selected, key=lambda p: (p != ".", p))

        parts: list[str] = []
        used = 0
        omitted = 0
        for path in ordered:
            label = "." if path == "." else path
            block = f"### {label}" + (" (repository root)" if path == "." else "") + f"\n{selected[path]}\n"
            if parts and used + len(block) > self.REPO_SUMMARY_MAX_INPUT_CHARS:
                omitted += 1
                continue
            parts.append(block)
            used += len(block)

        if omitted:
            parts.append(f"(... {omitted} more top-level folders omitted for length ...)")
        return "\n".join(parts)

    def _generate_repo_summary(
        self,
        semantic_generator: ArtifactGenerator,
        folder_summaries: dict[str, FolderSemanticSummary],
        agent_client=None,
    ) -> RepositorySemanticSummary | None:
        """
        Generate the repository-level summary. Returns None (and records a failed
        item) if it cannot be generated, so the rest of the sync still completes.
        """
        print("\n" + "=" * 80)
        print("GENERATING REPOSITORY SUMMARY")
        print("=" * 80)
        try:
            text = self._collect_repo_summary_input(folder_summaries, agent_client)
            if not text.strip():
                print("No folder summaries available; skipping.")
                return None

            repo_summary, usage = semantic_generator.generate_repo_summary(
                self.config.repo_id or "unknown",
                text,
            )
            self._accumulate_usage(usage)
            print(
                f"Tokens: {usage.input_tokens:,} in / {usage.output_tokens:,} out | "
                f"Cost: ${usage.total_cost_usd:.4f}"
            )
            self.log.info("repo_summary_generated")
            if self.pack is not None:
                try:
                    self._generate_repo_lens(agent_client)
                except Exception as e:  # the repo summary stands on its own
                    self.log.error("repo_lens_generation_failed", error=str(e))
                    with self.error_lock:
                        self.failed_items.append(("REPOSITORY LENS", str(e)))
                    print(f"❌ Repository lens FAILED: {e}")
            return repo_summary
        except Exception as e:
            error_msg = str(e)
            self.log.error("repo_summary_generation_failed", error=error_msg)
            with self.error_lock:
                self.failed_items.append(("REPOSITORY SUMMARY", error_msg))
            print(f"❌ FAILED: {error_msg}")
            return None

    def _run_source_mirror(
        self,
        snapshot: RepositorySnapshot,
        delta: SnapshotDelta | None,
        dry_run: bool,
    ) -> SourceMirrorResult:
        """
        Run source mirroring after successful index persistence.
        
        Args:
            snapshot: Current repository snapshot
            delta: Snapshot delta (None for full sync)
            dry_run: If True, only show what would be changed
        
        Returns:
            SourceMirrorResult with operation summary
        """
        self.log.info("starting_source_mirror", dry_run=dry_run)
        
        mirror_orchestrator = SourceMirrorOrchestrator(self.config)
        
        if delta is None:
            # Full sync mode
            return mirror_orchestrator.mirror_full(snapshot, dry_run=dry_run)
        else:
            # Incremental sync mode
            return mirror_orchestrator.mirror_incremental(delta, snapshot, dry_run=dry_run)