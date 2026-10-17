# semantic_ingress/core/artifact_generator.py
"""
Semantic artifact generation orchestrator.
Coordinates LLM calls to generate file, folder, and repository summaries.
"""
import json
from pathlib import Path

import structlog
from pydantic import BaseModel

from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.models import (
    FileSemanticSummary,
    FolderSemanticSummary,
    RepositorySemanticSummary,
    FileReference,
    SubfolderReference, 
)
from semantic_ingress.lens.models import (
    FileLensOnly,
    FileLensRecord,
    FileSummaryWithLens,
    FolderSummaryWithLens,
)
from semantic_ingress.lens.normalize import NARRATIVE_CAPS, normalize
from semantic_ingress.lens.pack import Pack
from semantic_ingress.lens.prompts import FOLDER_LENS_SECTION, file_system_prompt, file_user_prompt
from semantic_ingress.prompts import (
    FILE_SUMMARY_PROMPT,
    FOLDER_SUMMARY_PROMPT,
    REPO_SUMMARY_PROMPT,
)

logger = structlog.get_logger()


class ArtifactGenerator:
    """Generates semantic artifacts (and, when a pack is given, Temporal lens records) using LLM calls."""
    
    def __init__(
        self,
        git_adapter: GitAdapter,
        llm_client: LLMClient,
        commit_sha: str,
        pack: Pack | None = None,
    ) -> None:
        """
        Initialize artifact generator.
        
        Args:
            git_adapter: Git repository adapter
            llm_client: LLM client for generation
            commit_sha: Current commit SHA for metadata
            pack: Knowledge pack; when given, file calls also produce a lens record
        """
        self.git = git_adapter
        self.llm = llm_client
        self.commit_sha = commit_sha
        self.pack = pack
        self.log = logger.bind(component="artifact_generator")
        # (mode, lang, lens_only) -> (system prompt, signal codes, entry ids); built once per run
        self._views: dict[tuple[str, str | None, bool], tuple[str, set[str], set[str]]] = {}

    MAX_FILE_CHARS = 525_000  # about 150k tokens

    def _read(self, file_path: str, blob_oid: str) -> str:
        file_content = self.git.get_file_content(blob_oid)
        if len(file_content) > self.MAX_FILE_CHARS:
            self.log.warning(
                "file_content_too_large_truncating",
                path=file_path,
                original_size=len(file_content),
                truncated_size=self.MAX_FILE_CHARS,
            )
            file_content = file_content[:self.MAX_FILE_CHARS] + "\n\n[... content truncated due to size ...]"
        return file_content

    def _view(self, mode: str, lang: str | None, lens_only: bool) -> tuple[str, set[str], set[str]]:
        key = (mode, lang if mode == "eval" else None, lens_only)
        if key not in self._views:
            text, codes, ids = self.pack.view(mode, key[1])
            self._views[key] = (file_system_prompt(text, lens_only=lens_only), codes, ids)
        return self._views[key]

    def generate_file_summary(
        self, file_path: str, blob_oid: str
    ) -> tuple[FileSemanticSummary, FileLensRecord | None, LLMUsage]:
        """
        Generate the semantic summary for a single file, plus its lens record when
        the lens is on (one combined call).

        Returns:
            Tuple of (summary, lens record or None, usage stats)
        """
        self.log.info("generating_file_summary", path=file_path, blob_oid=blob_oid)
        file_content = self._read(file_path, blob_oid)

        if self.pack is None:
            prompt = FILE_SUMMARY_PROMPT.format(file_path=file_path, file_content=file_content)
            summary, usage = self.llm.generate(prompt, FileSemanticSummary)
            self.log.info("file_summary_generated", path=file_path)
            return summary, None, usage

        result, record, usage = self._lens_call(file_path, file_content, FileSummaryWithLens, lens_only=False)
        self.log.info("file_summary_generated", path=file_path, rel=record.rel)
        return result.summary, record, usage

    def generate_file_lens(self, file_path: str, blob_oid: str) -> tuple[FileLensRecord, LLMUsage]:
        """Lens record only (summary unchanged), for --refresh-lens on unchanged files."""
        file_content = self._read(file_path, blob_oid)
        _, record, usage = self._lens_call(file_path, file_content, FileLensOnly, lens_only=True)
        return record, usage

    def _lens_call(self, file_path, file_content, response_model, lens_only: bool):
        mode = self.pack.file_mode(file_path, file_content)
        lang = self.pack.language_of(file_path)
        system, codes, ids = self._view(mode, lang, lens_only)

        result, usage = self.llm.generate(
            file_user_prompt(file_path, file_content), response_model, system=system, enforce_schema=True
        )
        norm = normalize(result.lens, codes, ids)
        if norm.retry:
            # One retry when the narrative is far over its cap; keep whichever is shorter.
            cap = NARRATIVE_CAPS[norm.lens.rel]
            reminder = f"Keep the lens narrative `n` within {cap} words."
            retried, retry_usage = self.llm.generate(
                file_user_prompt(file_path, file_content, reminder), response_model,
                system=system, enforce_schema=True,
            )
            usage = usage + retry_usage
            second = normalize(retried.lens, codes, ids)
            if len(second.lens.n.split()) < len(norm.lens.n.split()):
                result, norm = retried, second
        if norm.problems:
            self.log.info("lens_normalized", path=file_path, corrections=norm.problems)

        record = FileLensRecord(
            **norm.lens.model_dump(), mode=mode, lang=lang, pack_version=self.pack.version
        )
        return result, record, usage

    def generate_folder_summary(
        self,
        folder_path: str,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
    ) -> tuple[FolderSemanticSummary, LLMUsage]:
        """Folder summary only (no lens). See _generate_folder."""
        summary, usage, _ = self._generate_folder(folder_path, file_summaries, subfolder_summaries, None)
        return summary, usage

    def generate_folder_summary_with_lens(
        self,
        folder_path: str,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
        file_lens_lines: str,
        folder_lens_lines: str,
    ) -> tuple[FolderSemanticSummary, LLMUsage, str]:
        """Folder summary plus the folder lens narrative, in one call. Returns (summary, usage, lens_n)."""
        return self._generate_folder(
            folder_path, file_summaries, subfolder_summaries, (file_lens_lines, folder_lens_lines)
        )

    def _generate_folder(
        self,
        folder_path: str,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
        lens_context: tuple[str, str] | None,
    ) -> tuple[FolderSemanticSummary, LLMUsage, str]:
        """
        Generate semantic summary for a folder (shallow aggregation).
        
        Args:
            folder_path: Repository-relative folder path
            file_summaries: Dict of filename -> FileSemanticSummary for direct children
            subfolder_summaries: Dict of subfolder name -> FolderSemanticSummary for direct children
        
        Returns:
            Tuple of (FolderSemanticSummary instance, usage stats)
        """
        self.log.info(
            "generating_folder_summary",
            path=folder_path,
            file_count=len(file_summaries),
            subfolder_count=len(subfolder_summaries),
        )
        
        # Check if folder is too large for single LLM call
        estimated_tokens = self._estimate_folder_summary_tokens(file_summaries, subfolder_summaries)
        
        # Strategy 1: Extremely large folders (>500 files) - stub summary without LLM
        if len(file_summaries) > 500:
            self.log.warning(
                "huge_folder_creating_stub_summary",
                path=folder_path,
                file_count=len(file_summaries),
            )
            
            return (*self._create_stub_folder_summary(folder_path, file_summaries, subfolder_summaries), "")
        
        # Strategy 2: Very large folders (>120k tokens) - chunked summarization
        if estimated_tokens > 120000:
            self.log.warning(
                "large_folder_using_chunking",
                path=folder_path,
                file_count=len(file_summaries),
                estimated_tokens=int(estimated_tokens),
            )
            
            return (*self._generate_large_folder_summary(folder_path, file_summaries, subfolder_summaries), "")
        
        # Strategy 3: Large folders (>150k tokens when serialized) - condensed summaries
        file_summaries_full = {name: summary.model_dump() for name, summary in file_summaries.items()}
        folder_summaries_full = {name: summary.model_dump() for name, summary in subfolder_summaries.items()}
        
        file_summaries_text = json.dumps(file_summaries_full, indent=2)
        folder_summaries_text = json.dumps(folder_summaries_full, indent=2)
        
        estimated_tokens_full = (len(file_summaries_text) + len(folder_summaries_text)) // 3.5
        
        if estimated_tokens_full > 150000:
            self.log.warning(
                "folder_summary_using_condensed",
                path=folder_path,
                estimated_tokens=int(estimated_tokens_full),
            )
            
            file_summaries_condensed = {
                name: {"role": summary.role[:400]}
                for name, summary in file_summaries.items()
            }
            folder_summaries_condensed = {
                name: {"summary": summary.summary[:400]}
                for name, summary in subfolder_summaries.items()
            }
            
            file_summaries_text = json.dumps(file_summaries_condensed, indent=2)
            folder_summaries_text = json.dumps(folder_summaries_condensed, indent=2)
        
        # Strategy 4: Normal folder - full LLM generation
        prompt = FOLDER_SUMMARY_PROMPT.format(
            folder_path=folder_path,
            file_summaries=file_summaries_text,
            folder_summaries=folder_summaries_text,
        )

        if lens_context is None:
            summary, usage = self.llm.generate(prompt, FolderSemanticSummary)
            self.log.info("folder_summary_generated", path=folder_path)
            return summary, usage, ""

        prompt = (
            "Return one JSON object with two fields: `summary` (the folder summary specified "
            "below) and `lens_n` (specified at the end).\n\n"
            + prompt
            + FOLDER_LENS_SECTION.format(
                file_lenses=lens_context[0] or "(none)",
                folder_lenses=lens_context[1] or "(none)",
            )
        )
        result, usage = self.llm.generate(prompt, FolderSummaryWithLens, enforce_schema=True)
        self.log.info("folder_summary_generated", path=folder_path, with_lens=True)
        return result.summary, usage, result.lens_n.strip()
    
    def _estimate_folder_summary_tokens(
        self,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
    ) -> int:
        """Estimate token count for folder summary generation."""
        # Serialize to estimate size
        file_json = json.dumps({name: s.model_dump() for name, s in file_summaries.items()})
        folder_json = json.dumps({name: s.model_dump() for name, s in subfolder_summaries.items()})
        
        # ~3.5 chars per token
        return int((len(file_json) + len(folder_json)) / 3.5)
    
    def _create_stub_folder_summary(
        self,
        folder_path: str,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
    ) -> tuple[FolderSemanticSummary, LLMUsage]:
        """Create a stub summary for extremely large folders without LLM call."""
        file_names = list(file_summaries.keys())
        subfolder_names = list(subfolder_summaries.keys())
        
        summary = FolderSemanticSummary(
            path=folder_path,
            summary=f"Large folder containing {len(file_summaries)} files. Individual file details should be accessed by traversing into this folder programmatically.",
            files=[
                FileReference(
                    name=name,
                    description="See individual file summary"
                )
                for name in file_names
            ],
            subfolders=[
                SubfolderReference(
                    name=name,
                    description="See subfolder summary"
                )
                for name in subfolder_names
            ]
        )
        
        # Create dummy usage stats
        usage = LLMUsage(input_tokens=0, output_tokens=0)
        
        self.log.info("stub_folder_summary_created", path=folder_path)
        return summary, usage
    
    def _generate_large_folder_summary(
        self,
        folder_path: str,
        file_summaries: dict[str, FileSemanticSummary],
        subfolder_summaries: dict[str, FolderSemanticSummary],
    ) -> tuple[FolderSemanticSummary, LLMUsage]:
        """
        Generate summary for very large folders using chunking strategy.
        
        Strategy:
        1. Split files into chunks of 50
        2. Generate mini-summaries for each chunk
        3. Combine mini-summaries into final summary
        """
        chunk_size = 50
        file_items = list(file_summaries.items())
        chunks = [dict(file_items[i:i + chunk_size]) for i in range(0, len(file_items), chunk_size)]
        
        self.log.info(
            "large_folder_chunking_strategy",
            path=folder_path,
            total_files=len(file_summaries),
            chunk_count=len(chunks),
            chunk_size=chunk_size,
        )
        print(f"  Splitting into {len(chunks)} chunks (~{chunk_size} files each)...")
        
        # Generate condensed summaries for each chunk
        chunk_descriptions = []
        total_usage = LLMUsage(input_tokens=0, output_tokens=0)
        
        for idx, chunk in enumerate(chunks, 1):
            # Create condensed representation (just roles, no full summaries)
            condensed = {
                name: {"role": summary.role[:250]}  # Truncate roles to 250 chars
                for name, summary in chunk.items()
            }
            
            chunk_json = json.dumps(condensed, indent=2)
            
            prompt = f"""Summarize this group of {len(chunk)} files in ONE SHORT SENTENCE (max 20 words):
Files: {chunk_json}

Respond with JSON: {{"description": "your one-sentence summary here"}}"""
            
            # Create dynamic Pydantic model for chunk summary
            ChunkSummary = type('ChunkSummary', (BaseModel,), {
                '__annotations__': {'description': str},
                'model_config': {'extra': 'forbid'}
            })
            
            try:
                response, usage = self.llm.generate(prompt, ChunkSummary)
                chunk_descriptions.append(response.description)
                total_usage = total_usage + usage
            except Exception as e:
                self.log.warning("chunk_summary_failed", chunk=idx, error=str(e))
                chunk_descriptions.append(f"Group of {len(chunk)} related files")
        
        # Build final folder summary from chunks
        summary = FolderSemanticSummary(
            path=folder_path,
            summary=f"Large folder ({len(file_summaries)} files) containing: " + "; ".join(chunk_descriptions[:3]),
            files=[
                FileReference(
                    name=name,
                    description=summary.role[:200]  # Truncated role
                )
                for name, summary in list(file_summaries.items())[:200]  # Only include first 200
            ],
            subfolders=[
                SubfolderReference(
                    name=name,
                    description=summary.summary[:200]
                )
                for name, summary in subfolder_summaries.items()
            ]
        )
        
        self.log.info("large_folder_summary_complete", path=folder_path, chunks_processed=len(chunks))
        
        return summary, total_usage
    
    def generate_repo_summary(
        self,
        repo_id: str,
        folder_summaries_text: str,
    ) -> tuple[RepositorySemanticSummary, LLMUsage]:
        """
        Generate the high-level repository summary.

        Args:
            repo_id: Repository identifier
            folder_summaries_text: Prose summaries of the root folder and the
                top-level folders, already formatted for the prompt

        Returns:
            Tuple of (RepositorySemanticSummary instance, usage stats)
        """
        self.log.info(
            "generating_repo_summary",
            repo_id=repo_id,
            input_chars=len(folder_summaries_text),
        )

        prompt = REPO_SUMMARY_PROMPT.format(
            repo_id=repo_id,
            folder_summaries=folder_summaries_text,
        )

        summary, usage = self.llm.generate(prompt, RepositorySemanticSummary)

        self.log.info("repo_summary_generated", repo_id=repo_id)
        return summary, usage
