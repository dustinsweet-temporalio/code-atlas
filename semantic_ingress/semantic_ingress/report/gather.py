"""
Load what a report may use from the index: index metadata, the repository lens,
every file lens record, the root roll-up, and source lines from the source mirror
(used to verify leads and count lines). No LLM calls.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semantic_ingress.lens.models import TEMPORAL_KINDS, FileLensRecord, FolderLensRollup


class ReportError(Exception):
    """The index cannot support a report; the message says what to do."""


@dataclass
class IndexData:
    repo_id: str
    commit_sha: str
    indexed_at: str
    pack_versions: list[dict[str, Any]]
    repo_lens: dict[str, Any]
    files: dict[str, FileLensRecord]  # files with a lens record
    unlensed: int  # indexed files without one
    rollup: FolderLensRollup | None
    source_root: Path | None
    blobs: dict[str, str] = field(default_factory=dict)  # path -> git blob id, every indexed file
    _lines: dict[str, list[str]] = field(default_factory=dict, repr=False)

    def source_lines(self, path: str) -> list[str]:
        """Lines of a mirrored file (empty if the mirror does not have it)."""
        if path not in self._lines:
            lines: list[str] = []
            if self.source_root is not None:
                target = (self.source_root / path).resolve()
                if self.source_root.resolve() in target.parents and target.is_file():
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            self._lines[path] = lines
        return self._lines[path]

    def line_count(self, path: str) -> int:
        return len(self.source_lines(path))

    @property
    def temporal_files(self) -> set[str]:
        return {p for p, r in self.files.items() if set(r.kind) & TEMPORAL_KINDS}

    @property
    def lens_state(self) -> str:
        versions = [v for v in self.pack_versions if v.get("version")]
        if self.unlensed:
            return "partial"
        return "mixed" if len(versions) > 1 else "on"


def load_index(store, repo_id: str, source_root: Path | None) -> IndexData:
    meta = store.get_index_meta(repo_id)
    if meta is None:
        raise ReportError(
            f"'{repo_id}' has no index metadata: it is not indexed, or was indexed by an older version. "
            "Run ./scripts/index.sh first."
        )
    repo_docs = store.query_documents(
        repo_id, "repo_summaries", {"document_type": "repo_summary"}, {"lens": 1}, limit=1
    )
    if not repo_docs or not repo_docs[0].get("lens"):
        raise ReportError(
            f"'{repo_id}' has no repository lens. Index it with LENS_ENABLED=true "
            "(./scripts/index.sh <repo> <name>; add --force if the index predates the lens)."
        )
    docs = store.query_documents(repo_id, "file_summaries", projection={"path": 1, "lens": 1, "blob_oid": 1})
    files = {d["path"]: FileLensRecord.model_validate(d["lens"]) for d in docs if d.get("lens")}
    if not files:
        raise ReportError(f"'{repo_id}' has no file lens records. Re-index with LENS_ENABLED=true and --force.")
    root = store.query_documents(repo_id, "folder_summaries", {"path": "."}, {"lens": 1}, limit=1)
    rollup = FolderLensRollup.model_validate(root[0]["lens"]) if root and root[0].get("lens") else None

    return IndexData(
        repo_id=repo_id,
        commit_sha=meta.get("commit_sha", ""),
        indexed_at=str(meta.get("indexed_at", ""))[:19],
        pack_versions=meta.get("lens_pack_versions") or [],
        repo_lens=repo_docs[0]["lens"],
        files=files,
        unlensed=len(docs) - len(files),
        rollup=rollup,
        source_root=source_root,
        blobs={d["path"]: d.get("blob_oid") or "" for d in docs},
    )
