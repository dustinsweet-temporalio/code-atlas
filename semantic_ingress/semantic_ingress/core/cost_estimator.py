"""
Cost estimation for semantic generation.
Estimates token usage and Anthropic API costs without making any network calls.
"""
from dataclasses import dataclass
from pathlib import Path

import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.prompts import (
    FILE_SUMMARY_PROMPT,
    FOLDER_SUMMARY_PROMPT,
    REPO_SUMMARY_PROMPT,
)

logger = structlog.get_logger()


@dataclass
class ArtifactCostEstimate:
    """Cost estimate for a single artifact type."""

    artifact_type: str
    count: int
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        """Total cost for this artifact type."""
        return self.input_cost_usd + self.output_cost_usd


@dataclass
class TotalCostEstimate:
    """Complete cost estimate for repository generation."""

    file_summaries: ArtifactCostEstimate
    folder_summaries: ArtifactCostEstimate
    repo_summary: ArtifactCostEstimate
    temporal_lens: ArtifactCostEstimate | None = None
    temporal_report: ArtifactCostEstimate | None = None

    @property
    def _parts(self) -> list[ArtifactCostEstimate]:
        parts = [self.file_summaries, self.folder_summaries, self.repo_summary]
        return parts + [p for p in (self.temporal_lens, self.temporal_report) if p]

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens across all artifacts."""
        return sum(p.input_tokens for p in self._parts)

    @property
    def total_output_tokens(self) -> int:
        """Total output tokens across all artifacts."""
        return sum(p.output_tokens for p in self._parts)

    @property
    def total_cost_usd(self) -> float:
        """Total estimated cost in USD."""
        return sum(p.total_cost_usd for p in self._parts)


class CostEstimator:
    """Estimates token usage and costs for semantic generation."""

    # Prices per 1M tokens come from the environment (INDEX_PRICE_INPUT_PER_1M /
    # INDEX_PRICE_OUTPUT_PER_1M) and are set per instance in __init__.

    # Conservative token estimation multipliers
    CHARS_PER_TOKEN = 4  # ~4 characters per token for English text

    # Expected output sizes (conservative estimates)
    FILE_SUMMARY_OUTPUT_TOKENS = 450
    FOLDER_SUMMARY_OUTPUT_TOKENS = 200
    REPO_SUMMARY_OUTPUT_TOKENS = 400

    # Temporal lens (measured on a 16-file run, 2026-10-16): the cached system prompt
    # (instructions plus pack view) is about 11k tokens. The first wave of parallel
    # calls each write it to cache (1.25x) before any can read it; later calls read it
    # (0.1x). Each file adds about 450 output tokens (lens plus thinking). The repo
    # pass reads up to ~30k tokens of compact records.
    LENS_SYSTEM_TOKENS = 11000
    LENS_FIRST_WAVE_WRITES = 16  # about 2 views x 8 workers
    LENS_OUTPUT_TOKENS_PER_FILE = 480
    REPO_LENS_INPUT_TOKENS = 30000
    REPO_LENS_OUTPUT_TOKENS = 2000

    def __init__(self, config: IngestionConfig) -> None:
        """
        Initialize cost estimator.

        Args:
            config: Ingestion configuration
        """
        self.config = config
        self.INPUT_PRICE_PER_1M = config.llm.input_price_per_1m
        self.OUTPUT_PRICE_PER_1M = config.llm.output_price_per_1m
        self.CACHE_READ_MULTIPLIER = config.llm.cache_read_multiplier
        self.git = GitAdapter(config.repo_path, config.file_filter)
        self.log = logger.bind(component="cost_estimator")

    def estimate(self) -> TotalCostEstimate:
        """
        Estimate costs for full semantic generation.

        Returns:
            TotalCostEstimate with breakdown by artifact type
        """
        self.log.info("starting_cost_estimation", repo_id=self.config.repo_id)

        # Get snapshot to enumerate files
        snapshot = self.git.get_snapshot()
        file_count = len(snapshot.files)

        self.log.info("snapshot_acquired", file_count=file_count)

        # Estimate file summaries
        file_summary_estimate = self._estimate_file_summaries(snapshot.files)

        # Estimate folder summaries (approximate based on file count)
        folder_count = self._estimate_folder_count(snapshot.files)
        folder_summary_estimate = self._estimate_folder_summaries(folder_count)

        # Estimate repository summary
        repo_summary_estimate = self._estimate_repo_summary(file_count, folder_count)

        total = TotalCostEstimate(
            file_summaries=file_summary_estimate,
            folder_summaries=folder_summary_estimate,
            repo_summary=repo_summary_estimate,
            temporal_lens=self._estimate_lens(file_count) if self.config.lens.enabled else None,
            temporal_report=self._estimate_report(file_count) if self.config.lens.enabled else None,
        )

        self.log.info(
            "cost_estimation_complete",
            total_cost_usd=f"${total.total_cost_usd:.4f}",
            total_input_tokens=total.total_input_tokens,
            total_output_tokens=total.total_output_tokens,
        )

        return total

    def _estimate_file_summaries(self, files: dict[str, any]) -> ArtifactCostEstimate:
        """
        Estimate costs for all file summaries.

        Args:
            files: Dictionary of file snapshots

        Returns:
            Cost estimate for file summaries
        """
        total_input_tokens = 0
        file_count = len(files)

        # Estimate input tokens for each file
        for path, file_snapshot in files.items():
            try:
                # Get file size
                file_content = self.git.get_file_content(file_snapshot.blob_oid)
                file_tokens = len(file_content) // self.CHARS_PER_TOKEN

                # Add prompt template overhead
                prompt_template_tokens = len(FILE_SUMMARY_PROMPT) // self.CHARS_PER_TOKEN
                total_input_tokens += file_tokens + prompt_template_tokens
            except Exception as e:
                self.log.debug("failed_to_estimate_file", path=path, error=str(e))
                # Use conservative fallback
                total_input_tokens += 1000

        total_output_tokens = file_count * self.FILE_SUMMARY_OUTPUT_TOKENS

        input_cost = (total_input_tokens / 1_000_000) * self.INPUT_PRICE_PER_1M
        output_cost = (total_output_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_1M

        return ArtifactCostEstimate(
            artifact_type="file_summaries",
            count=file_count,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
        )

    def _estimate_folder_summaries(self, folder_count: int) -> ArtifactCostEstimate:
        """
        Estimate costs for all folder summaries.

        Args:
            folder_count: Estimated number of folders

        Returns:
            Cost estimate for folder summaries
        """
        # Average folder has ~5 direct children (files + subfolders)
        avg_children_per_folder = 5
        avg_child_summary_size = 200  # tokens per child summary
        prompt_template_tokens = len(FOLDER_SUMMARY_PROMPT) // self.CHARS_PER_TOKEN

        input_tokens_per_folder = (
            prompt_template_tokens + (avg_children_per_folder * avg_child_summary_size)
        )

        total_input_tokens = folder_count * input_tokens_per_folder
        total_output_tokens = folder_count * self.FOLDER_SUMMARY_OUTPUT_TOKENS

        input_cost = (total_input_tokens / 1_000_000) * self.INPUT_PRICE_PER_1M
        output_cost = (total_output_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_1M

        return ArtifactCostEstimate(
            artifact_type="folder_summaries",
            count=folder_count,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
        )

    def _estimate_repo_summary(self, file_count: int, folder_count: int) -> ArtifactCostEstimate:
        """
        Estimate costs for repository summary.

        Args:
            file_count: Number of files
            folder_count: Number of folders

        Returns:
            Cost estimate for repository summary
        """
        prompt_template_tokens = len(REPO_SUMMARY_PROMPT) // self.CHARS_PER_TOKEN

        # Assume we include all folder summaries and selective file summaries
        folder_summary_tokens = folder_count * 150  # Compressed folder summary
        file_summary_tokens = min(file_count, 20) * 100  # Top-level files only

        total_input_tokens = prompt_template_tokens + folder_summary_tokens + file_summary_tokens
        total_output_tokens = self.REPO_SUMMARY_OUTPUT_TOKENS

        input_cost = (total_input_tokens / 1_000_000) * self.INPUT_PRICE_PER_1M
        output_cost = (total_output_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_1M

        return ArtifactCostEstimate(
            artifact_type="repo_summary",
            count=1,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
        )

    def _estimate_folder_count(self, files: dict[str, any]) -> int:
        """
        Estimate number of unique folders from file paths.

        Args:
            files: Dictionary of file snapshots

        Returns:
            Estimated number of folders
        """
        folders: set[str] = set()
        for path in files.keys():
            path_obj = Path(path)
            for parent in path_obj.parents:
                parent_str = str(parent)
                if parent_str and parent_str != ".":
                    folders.add(parent_str)
        return len(folders)

    def _estimate_lens(self, file_count: int) -> ArtifactCostEstimate:
        """Extra cost of the Temporal lens on top of the summaries (cache-adjusted input)."""
        writes = min(file_count, self.LENS_FIRST_WAVE_WRITES)
        input_tokens = int(
            writes * self.LENS_SYSTEM_TOKENS * 1.25
            + (file_count - writes) * self.LENS_SYSTEM_TOKENS * self.CACHE_READ_MULTIPLIER
            + self.REPO_LENS_INPUT_TOKENS
        )
        output_tokens = file_count * self.LENS_OUTPUT_TOKENS_PER_FILE + self.REPO_LENS_OUTPUT_TOKENS
        return ArtifactCostEstimate(
            artifact_type="temporal_lens",
            count=file_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_cost_usd=(input_tokens / 1_000_000) * self.INPUT_PRICE_PER_1M,
            output_cost_usd=(output_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_1M,
        )

    # Temporal report on a first index (measured 2026-10-17: about $0.25 per investigated
    # process, cache-adjusted). Re-runs reuse cached results and cost about nothing.
    REPORT_INPUT_TOKENS_PER_PROCESS = 60_000
    REPORT_OUTPUT_TOKENS_PER_PROCESS = 13_000
    REPORT_MAX_PROCESSES = 8

    def _estimate_report(self, file_count: int) -> ArtifactCostEstimate:
        """First-index cost of the Temporal report: assumes one process per 20 files, 2 to 8."""
        processes = min(self.REPORT_MAX_PROCESSES, max(2, file_count // 20))
        input_tokens = processes * self.REPORT_INPUT_TOKENS_PER_PROCESS
        output_tokens = processes * self.REPORT_OUTPUT_TOKENS_PER_PROCESS
        return ArtifactCostEstimate(
            artifact_type="temporal_report",
            count=processes,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_cost_usd=(input_tokens / 1_000_000) * self.INPUT_PRICE_PER_1M,
            output_cost_usd=(output_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_1M,
        )

    def format_report(self, estimate: TotalCostEstimate) -> str:
        """
        Format cost estimate as human-readable report.

        Args:
            estimate: Total cost estimate

        Returns:
            Formatted report string
        """
        lines = [
            "=" * 70,
            "SEMANTIC GENERATION COST ESTIMATE",
            "=" * 70,
            "",
            f"Repository: {self.config.repo_id}",
            "",
            "BREAKDOWN BY ARTIFACT TYPE:",
            "-" * 70,
            "",
        ]

        # File summaries
        lines.extend(
            self._format_artifact_section(
                "File Summaries",
                estimate.file_summaries,
            )
        )

        # Folder summaries
        lines.extend(
            self._format_artifact_section(
                "Folder Summaries",
                estimate.folder_summaries,
            )
        )

        # Repository summary
        lines.extend(
            self._format_artifact_section(
                "Repository Summary",
                estimate.repo_summary,
            )
        )

        if estimate.temporal_lens:
            lines.extend(
                self._format_artifact_section(
                    "Temporal Lens (LENS_ENABLED=true)",
                    estimate.temporal_lens,
                )
            )
        if estimate.temporal_report:
            lines.extend(
                self._format_artifact_section(
                    f"Temporal Report, first index (about {estimate.temporal_report.count} processes; "
                    "unchanged re-runs cost about nothing; --no-report skips it)",
                    estimate.temporal_report,
                )
            )

        # Total
        total_input_cost = sum(p.input_cost_usd for p in estimate._parts)
        total_output_cost = sum(p.output_cost_usd for p in estimate._parts)

        lines.extend(
            [
                "-" * 70,
                "",
                "TOTAL ESTIMATE:",
                f"  Input tokens:    {estimate.total_input_tokens:,}",
                f"  Output tokens:   {estimate.total_output_tokens:,}",
                f"  Input cost:      ${total_input_cost:.4f}",
                f"  Output cost:     ${total_output_cost:.4f}",
                f"  TOTAL COST:      ${estimate.total_cost_usd:.4f}",
                "",
                "=" * 70,
                "",
                "NOTE: This is a conservative estimate. Actual costs may vary based on:",
                "  - Exact token counts (estimated at ~4 chars/token)",
                "  - LLM response lengths",
                "  - Anthropic pricing changes",
                "",
            ]
        )

        return "\n".join(lines)

    def _format_artifact_section(
        self,
        title: str,
        estimate: ArtifactCostEstimate,
    ) -> list[str]:
        """Format a single artifact type section."""
        return [
            f"{title}:",
            f"  Count:           {estimate.count:,}",
            f"  Input tokens:    {estimate.input_tokens:,}",
            f"  Output tokens:   {estimate.output_tokens:,}",
            f"  Cost:            ${estimate.total_cost_usd:.4f}",
            "",
        ]
