"""
Pydantic models for semantic artifacts.

These models represent the semantic content only (no metadata).
Metadata (repo_id, commit_sha, blob_oid, timestamps) is added at persistence time.
"""

from pydantic import BaseModel, Field


class PublicSymbol(BaseModel):
    """A publicly exported symbol from a source file."""

    name: str = Field(min_length=1, description="Symbol name")
    signature: str = Field(min_length=1, description="Function, method, or class signature")
    description: str = Field(
        min_length=1, description="Single-sentence description of the symbol's purpose"
    )


class SemanticRegion(BaseModel):
    """An approximate semantic region within a file."""

    name: str = Field(min_length=1, description="Human-meaningful semantic region name")
    start_line: int = Field(ge=1, description="Approximate starting line number")
    end_line: int = Field(ge=1, description="Approximate ending line number")


class FileSemanticSummary(BaseModel):
    """Semantic summary of a single source file."""

    role: str = Field(
        min_length=1,
        description="1-2 sentence description of the file's responsibility and intent",
    )
    imports: list[str] = Field(
        default_factory=list,
        description="Best-effort list of external libraries or internal modules",
    )
    public_interface: list[PublicSymbol] = Field(
        default_factory=list,
        description="List of externally visible symbols exposed by this file",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Optional non-obvious contextual notes",
    )
    regions: list[SemanticRegion] = Field(
        default_factory=list,
        description="Approximate semantic regions within the file",
    )

    model_config = {"extra": "forbid"}


class FileReference(BaseModel):
    """Reference to a file within a folder summary."""

    name: str = Field(min_length=1, description="Filename of the direct child file")
    description: str = Field(
        min_length=1, description="Brief description derived from the file's semantic role"
    )


class SubfolderReference(BaseModel):
    """Reference to a subfolder within a folder summary."""

    name: str = Field(min_length=1, description="Name of the direct child subfolder")
    description: str = Field(
        min_length=1,
        description="Brief description derived from the subfolder's own semantic summary",
    )


class SubtreeStats(BaseModel):
    """Deterministic counts for everything beneath a folder (computed, not LLM-written)."""

    direct_files: int = Field(ge=0, description="Files directly in this folder")
    total_files: int = Field(ge=0, description="Files in this folder and all subfolders")
    total_folders: int = Field(ge=0, description="Subfolders at any depth beneath this folder")
    max_depth: int = Field(ge=0, description="Deepest subfolder level below this folder")
    by_subfolder: dict[str, int] = Field(
        default_factory=dict,
        description="Direct child folder name -> total files in its subtree",
    )

    model_config = {"extra": "forbid"}


class FolderSemanticSummary(BaseModel):
    """Semantic summary of a folder, covering the folder and everything beneath it."""

    path: str = Field(min_length=1, description="Repository-relative path to the folder")
    summary: str = Field(
        min_length=1,
        description="One or two sentences on what this folder and everything beneath it is responsible for",
    )
    files: list[FileReference] = Field(
        default_factory=list,
        description="Semantic summaries of files that are direct children",
    )
    subfolders: list[SubfolderReference] = Field(
        default_factory=list,
        description="Semantic summaries of subfolders that are direct children",
    )
    # Filled in by the indexer after generation; never written by the LLM.
    subtree: SubtreeStats | None = None

    model_config = {"extra": "forbid"}


class MajorArea(BaseModel):
    """A major functional area within the repository."""

    name: str = Field(min_length=1, description="Top-level folder or logical area name")
    description: str = Field(
        min_length=1, description="One-sentence description of the area's role"
    )


class RepositorySemanticSummary(BaseModel):
    """High-level semantic overview of the entire repository."""

    purpose: str = Field(
        min_length=1,
        description="2-3 sentence conceptual description of what the system is for",
    )
    major_areas: list[MajorArea] = Field(
        default_factory=list,
        description="Top-level functional areas or folders in the repository",
    )
    high_level_notes: list[str] = Field(
        default_factory=list,
        description="Optional non-obvious, system-wide characteristics",
    )

    model_config = {"extra": "forbid"}

