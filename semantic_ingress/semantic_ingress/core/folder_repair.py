"""
Folder summary repair orchestrator.

Forcefully regenerates folder summaries for a specified folder and all its ancestors.
Useful for fixing corrupted or outdated folder summaries without regenerating file summaries.
"""
from pathlib import Path

import structlog

from semantic_ingress.storage import MongoStore
from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.artifact_generator import ArtifactGenerator
from semantic_ingress.core.folder_tree import compute_subtree, direct_child_pattern, subtree_of_child
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.core.prose_serializer import serialize_folder_summary
from semantic_ingress.models import (
    DocumentType,
    FileSemanticSummary,
    FolderSemanticSummary,
    StoredDocument,
)

logger = structlog.get_logger()


class FolderRepairOrchestrator:
    """Orchestrates folder summary repair operations."""

    def __init__(self, config: IngestionConfig) -> None:
        """
        Initialize folder repair orchestrator.

        Args:
            config: Ingestion configuration
        """
        self.config = config
        self.log = logger.bind(component="folder_repair")
        
        # Initialize components
        self.git = GitAdapter(config.repo_path, config.file_filter)
        self.llm = LLMClient(config.llm)
        
        # Get current snapshot
        snapshot = self.git.get_snapshot()
        self.commit_sha = snapshot.commit_sha
        self.semantic_generator = ArtifactGenerator(
            self.git,
            self.llm,
            self.commit_sha
        )
        
        # Track usage
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)

    def repair(
        self,
        target_folder: str,
        agent_client: MongoStore,
        dry_run: bool = False
    ) -> dict:
        """
        Repair folder summaries for target folder and all ancestors.

        Args:
            target_folder: Folder path (absolute or repo-relative)
            agent_client: Mongo store for index operations
            dry_run: If True, don't persist changes

        Returns:
            Summary dict with results
        """
        self.log.info("starting_folder_repair", target_folder=target_folder)
        
        # Normalize folder path
        normalized_path = self._normalize_folder_path(target_folder)
        self.log.info("normalized_path", path=normalized_path)
        
        # Identify all folders to repair (target + ancestors)
        folders_to_repair = self._get_folder_chain(normalized_path)
        self.log.info("folders_to_repair", count=len(folders_to_repair), folders=folders_to_repair)
        
        print("\n" + "=" * 80)
        print(f"FOLDER REPAIR MODE")
        print("=" * 80)
        print(f"Target folder: {normalized_path if normalized_path else '(root)'}")
        print(f"Folders to repair: {len(folders_to_repair)}")
        for folder in folders_to_repair:
            print(f"  • {folder if folder else '(root)'}")
        print("=" * 80 + "\n")
        
        # Load file summaries for all affected folders
        print("Loading file summaries from the index...")
        all_file_summaries = self._load_file_summaries_for_folders(
            folders_to_repair,
            agent_client
        )
        stored_child_folders = self._load_child_folder_summaries(folders_to_repair, agent_client)
        print(f"Loaded {len(all_file_summaries)} file summaries and "
              f"{len(stored_child_folders)} unchanged child folder summaries\n")
        
        # Regenerate folder summaries bottom-up
        print("=" * 80)
        print("REGENERATING FOLDER SUMMARIES")
        print("=" * 80)
        
        folder_summaries: dict[str, FolderSemanticSummary] = {}
        
        for idx, folder_path in enumerate(folders_to_repair, 1):
            display_path = folder_path if folder_path else "(root)"
            print(f"[{idx}/{len(folders_to_repair)}] {display_path}")
            
            try:
                # Get child files
                child_files = {
                    Path(p).name: summary
                    for p, summary in all_file_summaries.items()
                    if self._get_parent_folder(p) == folder_path
                }
                
                # Get child subfolders: repaired in this run, or stored and unchanged
                child_subfolders = {
                    Path(child_folder).name: folder_summaries[child_folder]
                    for child_folder in folders_to_repair
                    if self._get_parent_folder(child_folder) == folder_path
                    and child_folder in folder_summaries
                }
                for child_folder, child_summary in stored_child_folders.items():
                    if self._get_parent_folder(child_folder) == folder_path:
                        child_subfolders.setdefault(Path(child_folder).name, child_summary)

                # Generate summary
                summary, usage = self.semantic_generator.generate_folder_summary(
                    display_path,
                    child_files,
                    child_subfolders
                )
                summary.subtree = compute_subtree(
                    len(child_files),
                    {
                        name: subtree_of_child(child.subtree, len(child.files))
                        for name, child in child_subfolders.items()
                    },
                )

                folder_summaries[folder_path] = summary
                self.total_usage = self.total_usage + usage
                
                print(f"  ✅ Generated | "
                      f"{usage.input_tokens:,} in / {usage.output_tokens:,} out | "
                      f"${usage.total_cost_usd:.4f}")
                
            except Exception as e:
                self.log.error("folder_generation_failed", path=display_path, error=str(e))
                print(f"  ❌ FAILED: {e}")
                continue
        
        print("\n" + "=" * 80)
        print("REPAIR SUMMARY")
        print("=" * 80)
        print(f"Folders processed: {len(folders_to_repair)}")
        print(f"Folders regenerated: {len(folder_summaries)}")
        print(f"Total input tokens: {self.total_usage.input_tokens:,}")
        print(f"Total output tokens: {self.total_usage.output_tokens:,}")
        print(f"Total cost: ${self.total_usage.total_cost_usd:.4f}")
        print("=" * 80 + "\n")
        
        if dry_run:
            print("DRY-RUN: No changes persisted to the index\n")
            return {
                "folders_processed": len(folders_to_repair),
                "folders_regenerated": len(folder_summaries),
                "usage": self.total_usage,
                "dry_run": True
            }
        
        # Persist to the index
        if folder_summaries:
            print("=" * 80)
            print("PERSISTING TO INDEX")
            print("=" * 80)
            
            documents = self._convert_to_stored_documents(folder_summaries)
            
            try:
                # The folder lens depends only on file lens records, which repair does not
                # change, so keep each folder's stored lens.
                result = agent_client.upsert_documents_bulk(
                    documents, batch_size=10, preserve_if_none=frozenset({"lens", "lens_prose"})
                )
                
                print(f"Total processed: {result['total_processed']}")
                print(f"Inserted:        {result['summary']['inserted']}")
                print(f"Updated:         {result['summary']['updated']}")
                print(f"No change:       {result['summary']['no_change']}")
                print(f"Errors:          {result['summary']['errors']}")
                print("=" * 80 + "\n")
                
                if result['summary']['errors'] > 0:
                    print("⚠️  Some documents failed to persist")
                
            except Exception as e:
                self.log.error("index_persistence_failed", error=str(e))
                print(f"\n❌ index persistence failed: {e}\n")
                raise
        
        return {
            "folders_processed": len(folders_to_repair),
            "folders_regenerated": len(folder_summaries),
            "usage": self.total_usage,
            "dry_run": False
        }

    def _normalize_folder_path(self, folder_path: str) -> str:
        """
        Normalize folder path to repo-relative format.

        Args:
            folder_path: Absolute or relative folder path

        Returns:
            Repo-relative folder path (empty string for root)
        """
        path_obj = Path(folder_path)
        
        # If absolute, make relative to repo root
        if path_obj.is_absolute():
            try:
                path_obj = path_obj.relative_to(self.config.repo_path)
            except ValueError:
                raise ValueError(
                    f"Folder path {folder_path} is not within repository {self.config.repo_path}"
                )
        
        # Normalize to string
        normalized = str(path_obj)
        
        # Handle current directory
        if normalized == ".":
            return ""
        
        # Remove trailing slash
        return normalized.rstrip("/")

    def _get_folder_chain(self, target_folder: str) -> list[str]:
        """
        Get list of all folders from target up to root.

        Args:
            target_folder: Normalized target folder path

        Returns:
            List of folder paths, sorted deepest-first (bottom-up)
        """
        folders = []
        
        current = target_folder
        while True:
            folders.append(current)
            
            if not current:
                # Reached root
                break
            
            # Move to parent
            current = self._get_parent_folder(current)
        
        return folders

    def _get_parent_folder(self, path: str) -> str:
        """
        Get parent folder of a path.

        Args:
            path: File or folder path

        Returns:
            Parent folder path (empty string for root)
        """
        if not path:
            return ""
        
        parent = str(Path(path).parent)
        if parent == ".":
            return ""
        return parent

    def _load_file_summaries_for_folders(
        self,
        folders: list[str],
        agent_client: MongoStore
    ) -> dict[str, FileSemanticSummary]:
        """
        Load all file summaries for files within specified folders.

        Args:
            folders: List of folder paths
            agent_client: Mongo store

        Returns:
            Dict mapping file paths to their summaries
        """
        summaries: dict[str, FileSemanticSummary] = {}
        
        try:
            # Query all file summaries
            docs = agent_client.query_documents(
                repo_id=self.config.repo_id or "unknown",
                collection="file_summaries",
                filter_query={},
                batch_size=100
            )
            
            # Filter to files in target folders
            for doc in docs:
                path = doc.get("path")
                content = doc.get("content")
                
                if not path or not content:
                    continue
                
                # Check if file is in one of our target folders
                file_folder = self._get_parent_folder(path)
                if file_folder in folders:
                    try:
                        summaries[path] = FileSemanticSummary.model_validate(content)
                    except Exception as e:
                        self.log.warning("failed_to_parse_file_summary", path=path, error=str(e))
            
        except Exception as e:
            self.log.error("failed_to_load_file_summaries", error=str(e))
            raise
        
        return summaries

    def _load_child_folder_summaries(
        self,
        folders: list[str],
        agent_client: MongoStore,
    ) -> dict[str, FolderSemanticSummary]:
        """Stored summaries of direct child folders of `folders` that are not being repaired."""
        docs = agent_client.query_documents(
            repo_id=self.config.repo_id or "unknown",
            collection="folder_summaries",
            filter_query={"path": {"$in": [direct_child_pattern(f) for f in folders]}},
            projection={"path": 1, "content": 1},
        )
        result: dict[str, FolderSemanticSummary] = {}
        for doc in docs:
            path = doc.get("path")
            if not path or path == "." or path in folders or not doc.get("content"):
                continue
            result[path] = FolderSemanticSummary.model_validate(doc["content"])
        return result

    def _convert_to_stored_documents(
        self,
        folder_summaries: dict[str, FolderSemanticSummary]
    ) -> list[StoredDocument]:
        """Convert folder summaries to StoredDocuments."""
        documents = []
        
        for path, summary in folder_summaries.items():
            doc = StoredDocument.for_folder_summary(
                repo_id=self.config.repo_id or "unknown",
                path="." if path == "" else path,  # root is stored as "."
                commit_sha=self.commit_sha,
                prose_summary=serialize_folder_summary(summary),
                content=summary.model_dump(),
            )
            documents.append(doc)
        
        return documents
