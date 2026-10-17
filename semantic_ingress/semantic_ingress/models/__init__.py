"""
Data models for semantic artifacts and internal structures.
"""
from semantic_ingress.models.artifacts import (
    FileReference,
    FileSemanticSummary,
    FolderSemanticSummary,
    MajorArea,
    PublicSymbol,
    RepositorySemanticSummary,
    SemanticRegion,
    SubfolderReference,
    SubtreeStats,
)
from semantic_ingress.models.snapshots import (
    FileChange,
    FileChangeType,
    FileSnapshot,
    RepositorySnapshot,
    SnapshotDelta,
)
from semantic_ingress.models.storage import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    DocumentType,
    IndexMeta,
    PackRef,
    StoredDocument,
)

__all__ = [
    # Semantic Artifacts
    "FileReference",
    "FileSemanticSummary",
    "FolderSemanticSummary",
    "MajorArea",
    "PublicSymbol",
    "RepositorySemanticSummary",
    "SemanticRegion",
    "SubfolderReference",
    "SubtreeStats",
    # Snapshots
    "FileChange",
    "FileChangeType",
    "FileSnapshot",
    "RepositorySnapshot",
    "SnapshotDelta",
    # Storage
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "DocumentType",
    "IndexMeta",
    "PackRef",
    "StoredDocument",
]
