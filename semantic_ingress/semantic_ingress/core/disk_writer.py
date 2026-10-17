"""
Disk writer for semantic artifacts.

Writes StoredDocuments to disk for debugging/backup purposes.
This is OPTIONAL and only occurs AFTER successful index persistence.
"""
import json
from pathlib import Path

import structlog

from semantic_ingress.models import DocumentType, StoredDocument

logger = structlog.get_logger()


class DiskWriter:
    """
    Writes StoredDocuments to disk in artifact format.
    
    Used for debugging and backup purposes after successful the index sync.
    Preserves the same folder structure as the old artifact_generator.
    """

    def __init__(self, output_dir: Path) -> None:
        """
        Initialize disk writer.

        Args:
            output_dir: Directory where artifacts will be written
        """
        self.output_dir = output_dir
        self.log = logger.bind(component="disk_writer", output_dir=str(output_dir))
        
        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log.info("disk_writer_initialized")

    def write_documents(self, documents: list[StoredDocument]) -> None:
        """
        Write all documents to disk.

        Args:
            documents: List of StoredDocuments to write
        """
        self.log.info("writing_documents_to_disk", count=len(documents))
        
        written_count = 0
        for doc in documents:
            try:
                self._write_document(doc)
                written_count += 1
            except Exception as e:
                self.log.warning(
                    "failed_to_write_document",
                    doc_id=doc.id,
                    path=doc.path,
                    error=str(e),
                )
        
        self.log.info("disk_write_complete", written=written_count, total=len(documents))

    def _write_document(self, doc: StoredDocument) -> None:
        """
        Write a single document to disk with metadata wrapper.

        Args:
            doc: StoredDocument to write
        """
        # Determine artifact path based on document type
        if doc.document_type == DocumentType.FILE_SUMMARY:
            artifact_path = self.output_dir / doc.path / "file_summary.json"
        elif doc.document_type == DocumentType.FOLDER_SUMMARY:
            artifact_path = self.output_dir / doc.path / "folder_summary.json"
        else:  # REPO_SUMMARY
            artifact_path = self.output_dir / "repo_summary.json"

        # Ensure parent directory exists
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        # Build metadata wrapper (matches old artifact_generator format)
        metadata = {
            "commit_sha": doc.commit_sha,
            "blob_oid": doc.blob_oid,
            "generated_at": doc.generated_at.isoformat() + "Z",
        }

        # Wrap content with metadata (same structure as before)
        wrapped = {
            "metadata": metadata,
            "content": doc.content,
        }

        # Write to disk
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, indent=2, ensure_ascii=False)

        self.log.debug("document_written", path=str(artifact_path))
