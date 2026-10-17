"""
Lens record models.

`FileLens` is what the index model writes for a file (it is also the stored shape,
plus provenance in `FileLensRecord`). Folder roll-ups and the repository lens are
defined here too; Phase 3 fills them.
"""
from typing import Literal

from pydantic import BaseModel, Field

from semantic_ingress.models import FileSemanticSummary, FolderSemanticSummary

Kind = Literal["ORCH", "ACT", "PURE", "ENTRY", "STATE", "NONE", "TWF", "TACT", "TWORKER", "TCLIENT"]
TEMPORAL_KINDS = {"TWF", "TACT", "TWORKER", "TCLIENT"}
LensMode = Literal["conv", "eval"]


class LensSymbol(BaseModel):
    """A public symbol and its role."""

    n: str = Field(description="Symbol name")
    k: Literal["ORCH", "ACT", "PURE"] = Field(description="Sequences steps, performs side effects, or is deterministic logic")

    model_config = {"extra": "forbid"}


class FileLens(BaseModel):
    """Temporal lens record for one file (field meanings are in the lens prompt)."""

    rel: Literal[0, 1, 2, 3] = Field(description="Durable-execution relevance")
    cov: Literal["full", "partial"] = Field(description="Whether the whole file was seen")
    kind: list[Kind] = Field(description="One or two roles")
    sig: list[str] = Field(description="Signal codes, optional ':detail' and '@Lstart-end' anchor")
    sym: list[LensSymbol] = Field(description="Public symbols (rel >= 2 only)")
    c: list[str] = Field(description="Pack concept ids the file's code evidences")
    f: list[str] = Field(description="Review-check leads with line anchors (Temporal code only)")
    n: str = Field(description="Narrative")

    model_config = {"extra": "forbid"}


class FileLensRecord(FileLens):
    """A stored file lens record: the lens plus where it came from."""

    mode: LensMode
    lang: str | None
    pack_version: str


class FileSummaryWithLens(BaseModel):
    """Combined response for one file: the summary and its lens record in one call."""

    summary: FileSemanticSummary
    lens: FileLens

    model_config = {"extra": "forbid"}


class FileLensOnly(BaseModel):
    """Lens-only response, used by --refresh-lens for unchanged files."""

    lens: FileLens

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Folder and repository (filled in Phase 3)
# ---------------------------------------------------------------------------


class FolderSummaryWithLens(BaseModel):
    """Combined folder response: the summary plus a short lens narrative (may be empty)."""

    summary: FolderSemanticSummary
    lens_n: str = Field(description="At most 40 words on the subtree's durable-execution picture, or empty")

    model_config = {"extra": "forbid"}


class CountEntry(BaseModel):
    """A key (kind, signal code or pack id) and the number of files that have it."""

    key: str
    files: int


class FolderLensRollup(BaseModel):
    """
    Deterministic roll-up of the file lens records in a folder's whole subtree.

    Counts are kept in full (not just the top entries) so a parent's roll-up is
    the exact sum of its direct files and its child folders' roll-ups.
    """

    files: int = Field(description="Files with a lens record in the subtree")
    rel_counts: list[int] = Field(description="Files at rel 0, 1, 2, 3")
    kind_counts: list[CountEntry] = Field(default_factory=list)
    signal_counts: list[CountEntry] = Field(default_factory=list)
    concept_counts: list[CountEntry] = Field(default_factory=list)
    core_files: list[str] = Field(default_factory=list, description="rel-3 paths (up to 25)")
    temporal_files: int = Field(default=0, description="Files that are Temporal workflow/activity/worker/client code")
    n: str = Field(default="", description="At most 40 words written with the folder summary; empty if nothing rel >= 2")
    pack_version: str


class DetectedTech(BaseModel):
    kind: Literal["temporal_sdk", "adjacent"]
    name: str
    evidence: list[str] = Field(description="Repository-relative paths")

    model_config = {"extra": "forbid"}


class ProcessCandidate(BaseModel):
    name: str
    entry: str = Field(description="Path of the entry point")
    files: list[str]
    signals: list[str]
    concepts: list[str]
    narrative: str

    model_config = {"extra": "forbid"}


class AntiSignal(BaseModel):
    id: str = Field(description="Gate id from the pack")
    evidence: list[str]

    model_config = {"extra": "forbid"}


class Citation(BaseModel):
    ref: str = Field(description="Pack id, or a URL returned by the docs search tool")
    supports: str = Field(description="What the citation supports, at most 15 words")

    model_config = {"extra": "forbid"}


class RepoLens(BaseModel):
    """Repository-level lens written by the repo pass."""

    status: Literal["none", "adjacent", "partial", "full"]
    archetype: Literal["service", "library", "frontend", "pipeline", "cli", "infra", "mixed"]
    detected: list[DetectedTech]
    processes: list[ProcessCandidate]
    anti: list[AntiSignal]
    n: str
    cites: list[Citation]

    model_config = {"extra": "forbid"}
