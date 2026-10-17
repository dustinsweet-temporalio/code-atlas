"""
Storage models for persisted semantic artifacts.
These wrap semantic content with metadata required for index persistence.
"""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from semantic_ingress.models.artifacts import (
    FileSemanticSummary,
    FolderSemanticSummary,
    RepositorySemanticSummary,
)

# Version of the stored document layout. Bump when stored fields change in a way
# that older documents cannot serve; an index on an older version is fully re-indexed.
#   1: prose only (implicit; documents carry no schema_version)
#   2: structured `content` persisted alongside prose; lens fields; index_meta document
SCHEMA_VERSION = 2

# Version of the summary and lens prompts. Bump when a prompt change alters what
# summaries say; documents keep the version that wrote them, so an index that mixes
# versions is visible (sync warns, the UI marks it) and --force makes it uniform.
#   1: implicit (documents carry no prompt_version)
#   2: longer role and interface descriptions; lens narrative caps 50/110 words
PROMPT_VERSION = 2


class DocumentType(Enum):
    """Types of semantic documents stored in the index."""

    FILE_SUMMARY = "file_summary"
    FOLDER_SUMMARY = "folder_summary"
    REPO_SUMMARY = "repo_summary"
    DIR_LIST = "dir_list"

    @classmethod
    def from_string(cls, value: str) -> "DocumentType":
        """
        Parse a document type from string value.
        
        Args:
            value: String representation (e.g., "file_summary")
        
        Returns:
            DocumentType enum instance
        
        Raises:
            ValueError: If value is not a valid document type
        """
        try:
            return cls(value)
        except ValueError:
            valid_values = [dt.value for dt in cls]
            raise ValueError(
                f"Invalid document type: '{value}'. "
                f"Valid types: {', '.join(valid_values)}"
            )

class StoredDocument(BaseModel):
    """
    Base wrapper for all stored semantic documents.
    Adds metadata required for persistence, versioning, and retrieval.
    """

    id: str = Field(
        alias="_id",
        description="Deterministic document ID: {repo_id}::{document_type}::{path}",
    )
    repo_id: str = Field(min_length=1, description="Repository identifier")
    document_type: DocumentType = Field(description="Type of semantic document")
    path: str = Field(description="Repository-relative path (empty string for repo-level docs)")
    commit_sha: str = Field(min_length=1, description="Git commit SHA at generation time")
    blob_oid: str | None = Field(
        default=None,
        description="Git blob object ID (null for aggregated documents)",
    )
    generated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp of semantic generation",
    )
    prose_summary: str = Field(
        min_length=1,
        description="Natural language prose representation of the semantic artifact",
    )
    content: dict[str, Any] | None = Field(
        default=None,
        description="Structured summary (model_dump of the semantic artifact); null for dir_list",
    )
    lens: dict[str, Any] | None = Field(
        default=None,
        description="Temporal lens record (null when the lens is disabled or not yet generated)",
    )
    lens_prose: str | None = Field(
        default=None,
        description="Compact serving form of the lens record, text-indexed alongside prose_summary",
    )
    schema_version: int = Field(default=SCHEMA_VERSION, description="Stored document layout version")
    prompt_version: int | None = Field(default=PROMPT_VERSION, description="Summary/lens prompt version that wrote it")

    model_config = {"extra": "forbid", "populate_by_name": True}

    @staticmethod
    def generate_id(repo_id: str, document_type: DocumentType, path: str) -> str:
        """
        Generate deterministic document ID.
        Format: {repo_id}::{document_type}::{path}
        This ensures idempotent upserts.
        """
        return f"{repo_id}::{document_type.value}::{path}"

    @classmethod
    def for_file_summary(
        cls,
        repo_id: str,
        path: str,
        commit_sha: str,
        blob_oid: str,
        prose_summary: str,
        content: dict[str, Any],
    ) -> "StoredDocument":
        """Create a stored document for a file summary."""
        doc_id = cls.generate_id(repo_id, DocumentType.FILE_SUMMARY, path)
        return cls(
            _id=doc_id,
            repo_id=repo_id,
            document_type=DocumentType.FILE_SUMMARY,
            path=path,
            commit_sha=commit_sha,
            blob_oid=blob_oid,
            prose_summary=prose_summary,
            content=content,
        )

    @classmethod
    def for_folder_summary(
        cls,
        repo_id: str,
        path: str,
        commit_sha: str,
        prose_summary: str,
        content: dict[str, Any],
    ) -> "StoredDocument":
        """Create a stored document for a folder summary."""
        doc_id = cls.generate_id(repo_id, DocumentType.FOLDER_SUMMARY, path)
        return cls(
            _id=doc_id,
            repo_id=repo_id,
            document_type=DocumentType.FOLDER_SUMMARY,
            path=path,
            commit_sha=commit_sha,
            blob_oid=None,
            prose_summary=prose_summary,
            content=content,
        )

    @classmethod
    def for_repo_summary(
        cls,
        repo_id: str,
        commit_sha: str,
        prose_summary: str,
        content: dict[str, Any],
    ) -> "StoredDocument":
        """Create a stored document for a repository summary."""
        doc_id = cls.generate_id(repo_id, DocumentType.REPO_SUMMARY, "")
        return cls(
            _id=doc_id,
            repo_id=repo_id,
            document_type=DocumentType.REPO_SUMMARY,
            path="",
            commit_sha=commit_sha,
            blob_oid=None,
            prose_summary=prose_summary,
            content=content,
        )
    
    @classmethod
    def for_dir_list(
        cls,
        repo_id: str,
        commit_sha: str,
        dir_list_content: str,
    ) -> "StoredDocument":
        """
        Factory method to create a stored document for directory listing.
        
        Args:
            repo_id: Repository identifier
            commit_sha: Git commit SHA
            dir_list_content: Content of the directory listing file (one path per line)
            
        Returns:
            StoredDocument for dir_list persistence
        """
        doc_id = cls.generate_id(repo_id, DocumentType.DIR_LIST, "")
        return cls(
            _id=doc_id,
            repo_id=repo_id,
            document_type=DocumentType.DIR_LIST,
            path="",
            commit_sha=commit_sha,
            blob_oid=None,
            prose_summary=dir_list_content,  # Use prose_summary, not content
        )


class PackRef(BaseModel):
    """Identity of the knowledge pack an index was built with."""

    id: str
    version: str
    hash: str


class IndexMeta(BaseModel):
    """
    One per repository database, written at the end of every successful sync.

    Records how the index was built so later runs can detect an outdated layout
    and the UI can show which knowledge pack the lens rests on.
    """

    id: str = Field(alias="_id", description="{repo_id}::index_meta")
    repo_id: str
    schema_version: int = SCHEMA_VERSION
    index_model: str
    commit_sha: str
    # Where the repository was read from (the host path when indexed from the UI or index.sh),
    # so the UI can offer it again for the next sync. None for indexes built before this.
    repo_path: str | None = None
    indexed_at: datetime = Field(default_factory=datetime.utcnow)
    lens_enabled: bool = False
    pack: PackRef | None = None
    # File lens records per pack version, e.g. [{"version": "0.1.0", "files": 1980}].
    # (A list, because pack versions contain dots, which are awkward as Mongo keys.)
    # More than one entry means the index is mixed.
    lens_pack_versions: list[dict[str, Any]] = Field(default_factory=list)
    # File summaries per prompt version, e.g. [{"version": 2, "files": 110}]; None = before versions.
    prompt_versions: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "forbid", "populate_by_name": True}

    @staticmethod
    def make_id(repo_id: str) -> str:
        return f"{repo_id}::index_meta"
