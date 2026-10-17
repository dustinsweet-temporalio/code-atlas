# semantic_ingress/core/generation_orchestrator.py
"""
In-process helpers for generating folder summaries and the directory listing.

`sync` itself runs as a Temporal workflow (semantic_ingress/temporal/); its steps
live in core/sync_steps.py. This class keeps the in-memory, bottom-up folder pass
(built on the same per-folder step) and the dir_list document for `dir-list-only`.
"""
from collections import defaultdict
from threading import Lock

import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core import sync_steps
from semantic_ingress.core.artifact_generator import ArtifactGenerator
from semantic_ingress.core.folder_tree import ancestors_inclusive, direct_child_pattern, parent_of
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.lens.models import FileLensRecord, FolderLensRollup
from semantic_ingress.lens.pack import Pack, load_pack
from semantic_ingress.lens.repo_pass import DocsSearch
from semantic_ingress.models import (
    FileSemanticSummary,
    FolderSemanticSummary,
    RepositorySnapshot,
    StoredDocument,
)

logger = structlog.get_logger()


class GenerationOrchestrator:
    """Folder summaries from in-memory children, and the directory listing."""

    def __init__(self, config: IngestionConfig) -> None:
        self.config = config
        self.log = logger.bind(component="generation_orchestrator")

        self.git = GitAdapter(config.repo_path, config.file_filter)
        self.llm = LLMClient(config.llm)

        self.usage_lock = Lock()
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)
        self.progress_lock = Lock()
        self.error_lock = Lock()
        self.failed_items: list[tuple[str, str]] = []

        # Temporal lens: the pack (None when the lens is off).
        self.pack: Pack | None = load_pack(config.lens.pack_path) if config.lens.enabled else None
        self.docs_search: DocsSearch | None = None
        self._reset_lens_state()

    def _reset_lens_state(self) -> None:
        """Lens state: fresh records, and stored ones loaded for unchanged children."""
        self._file_lenses: dict[str, FileLensRecord] = {}
        self._folder_rollups: dict[str, FolderLensRollup] = {}
        self._stored_file_lenses: dict[str, FileLensRecord] = {}
        self._stored_rollups: dict[str, FolderLensRollup] = {}

    def _generate_dir_list_document(self, current_snapshot: RepositorySnapshot) -> StoredDocument:
        """Generate the directory listing document."""
        self.log.info("generating_dir_list_document")
        doc = sync_steps.dir_list_document(self.config.repo_id or "unknown", current_snapshot)
        self.log.info("dir_list_document_generated", line_count=len(doc.prose_summary.splitlines()))
        return doc

    def _accumulate_usage(self, usage: LLMUsage) -> None:
        with self.usage_lock:
            self.total_usage = self.total_usage + usage

    def _get_total_usage(self) -> LLMUsage:
        with self.usage_lock:
            return self.total_usage

    def _lens_of_file(self, path: str) -> FileLensRecord | None:
        return self._file_lenses.get(path) or self._stored_file_lenses.get(path)

    def _generate_folder_artifacts(
        self,
        semantic_generator: ArtifactGenerator,
        file_summaries: dict[str, FileSemanticSummary],
        specific_folders: set[str] | None = None,
        known_folder_summaries: dict[str, FolderSemanticSummary] | None = None,
    ) -> dict[str, FolderSemanticSummary]:
        """
        Generate folder summaries bottom-up.

        Each folder is summarized from all of its direct children: the file summaries
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

        folder_paths = sorted(all_folders, key=sync_steps.folder_depth, reverse=True)
        if specific_folders is not None:
            folder_paths = [p for p in folder_paths if p in specific_folders]

        folder_summaries: dict[str, FolderSemanticSummary] = {}
        folder_count = len(folder_paths)

        for idx, folder_path in enumerate(folder_paths, 1):
            display_path = folder_path if folder_path else "."
            direct_files = folders_with_files.get(folder_path, [])
            child_folders = sorted(folder_children.get(folder_path, set()))
            children = {
                c: summary for c in child_folders
                if (summary := folder_summaries.get(c) or known.get(c)) is not None
            }
            if not direct_files and not children:
                continue

            print(f"[{idx}/{folder_count}] {display_path}")
            try:
                out = sync_steps.generate_folder_from_children(
                    semantic_generator,
                    self.pack,
                    folder_path,
                    {p: file_summaries[p] for p in direct_files},
                    children,
                    {p: lens for p in direct_files if (lens := self._lens_of_file(p)) is not None},
                    {
                        c: r for c in child_folders
                        if (r := self._folder_rollups.get(c) or self._stored_rollups.get(c)) is not None
                    },
                )
                summary, rollup, summary_usage = out
                if rollup is not None:
                    self._folder_rollups[folder_path] = rollup
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
