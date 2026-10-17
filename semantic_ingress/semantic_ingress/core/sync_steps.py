# semantic_ingress/core/sync_steps.py
"""
The steps of `sync` as stateless functions, called by the Temporal activities
(semantic_ingress/temporal/activities.py).

The bodies are taken from the in-process sync this replaces (GenerationOrchestrator
and cli.py): prompts, normalization, folder-size strategies, lens roll-ups and the
text-index handling are unchanged. What changed is where results go: generated
documents are staged (storage/staging_store.py) under the run's `sync_id`, and
reads that used to come from in-memory dicts read staging first, then the live
index (incremental runs only).
"""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.artifact_generator import ArtifactGenerator
from semantic_ingress.core.dir_list_generator import DirListGenerator
from semantic_ingress.core.folder_tree import (
    compute_subtree,
    folders_to_regenerate,
    parent_of,
    subtree_of_child,
)
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.incremental_loader import IncrementalLoader
from semantic_ingress.core.llm_client import LLMClient, LLMUsage
from semantic_ingress.core.prose_serializer import (
    serialize_file_summary,
    serialize_folder_summary,
    serialize_repo_summary,
)
from semantic_ingress.core.snapshot_comparer import SnapshotComparer
from semantic_ingress.lens.models import FileLensRecord, FolderLensRollup
from semantic_ingress.lens.pack import Pack
from semantic_ingress.lens.repo_pass import DocsSearch, compact_repo_lens, run_repo_pass
from semantic_ingress.lens.rollup import compact_rollup, compute_rollup, max_rel
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.lens.tier0 import scan as tier0_scan
from semantic_ingress.models import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    DocumentType,
    FileChange,
    FileChangeType,
    FileSemanticSummary,
    FolderSemanticSummary,
    IndexMeta,
    PackRef,
    RepositorySnapshot,
    SnapshotDelta,
    StoredDocument,
)
from semantic_ingress.storage.mongo_store import MongoStore
from semantic_ingress.storage.staging_store import StagingStore
from semantic_ingress.temporal.models import LevelInfo, SyncParams, SyncPlan

logger = structlog.get_logger()

FOLDER_LENS_WORDS = 40
# Cap on the prose sent for the repository summary (root + top-level folders).
REPO_SUMMARY_MAX_INPUT_CHARS = 400_000


def cap_words(text: str, limit: int) -> str:
    """Keep whole sentences up to `limit` words (20% slack); otherwise cut at the limit."""
    words = text.split()
    if len(words) <= limit * 1.2:
        return text.strip()
    kept, count = [], 0
    for sentence in text.replace("\n", " ").split(". "):
        n = len(sentence.split())
        if count + n > limit:
            break
        kept.append(sentence.rstrip("."))
        count += n
    return (". ".join(kept) + ".") if kept else " ".join(words[:limit])


# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------

def full_reindex_reason(store, repo_id: str, old_snapshot: RepositorySnapshot | None, force: bool) -> str | None:
    """
    Why an existing index must be rebuilt from scratch, or None to proceed normally.

    A missing or older index_meta means documents lack the structured `content`
    that incremental folder regeneration needs, so they cannot be reused.
    """
    if old_snapshot is None:
        return None  # nothing to replace; a normal full index
    if force:
        return "--force: replacing the existing index"
    meta = store.get_index_meta(repo_id)
    stored_version = (meta or {}).get("schema_version", 1)
    if stored_version < SCHEMA_VERSION:
        return (
            f"index layout v{stored_version} is older than v{SCHEMA_VERSION}; "
            "rebuilding once so folder summaries can be regenerated incrementally"
        )
    return None


def pack_version_warnings(store, repo_id: str, pack: Pack | None, refresh_lens: bool) -> list[str]:
    """Warn when the index's lens was built with another pack version (never regenerates on its own)."""
    if pack is None:
        if refresh_lens:
            raise ValueError("--refresh-lens needs LENS_ENABLED=true")
        return []
    meta = store.get_index_meta(repo_id) or {}
    built_with = (meta.get("pack") or {}).get("version")
    if not meta.get("lens_enabled"):
        if not refresh_lens:
            return ["ℹ️  This index has no Temporal lens yet. Changed files get one now; "
                    "run with --refresh-lens to add it to every file."]
    elif built_with and built_with != pack.version and not refresh_lens:
        return [f"⚠️  Index lens built with pack {built_with}; installed pack is {pack.version}. "
                f"Changed files will use {pack.version}. Run with --refresh-lens to update all files."]
    return []


def prompt_version_warnings(store, repo_id: str) -> list[str]:
    """Warn when stored summaries were written by an older prompt version (never regenerates on its own)."""
    versions = store.prompt_versions(repo_id)
    older = sum(v["files"] for v in versions if v["version"] != PROMPT_VERSION)
    if older:
        return [f"⚠️  {older:,} files were summarized with an older prompt version; changed files use "
                f"version {PROMPT_VERSION}. Run with --force to regenerate every summary."]
    return []


def all_folders(file_paths) -> set[str]:
    """Every folder containing one of the files ("" for the root)."""
    folders: set[str] = set()
    for file_path in file_paths:
        for parent in Path(file_path).parents:
            parent_str = str(parent)
            folders.add("" if parent_str == "." else parent_str)
    return folders


def dir_list_document(repo_id: str, snapshot: RepositorySnapshot) -> StoredDocument:
    """The directory listing document (one path per line)."""
    return StoredDocument.for_dir_list(
        repo_id=repo_id,
        commit_sha=snapshot.commit_sha,
        dir_list_content=DirListGenerator().generate(snapshot),
    )


def _delta_dict(delta: SnapshotDelta) -> dict:
    """The parts of a delta the source mirror needs, as stored in the staged plan."""
    return {
        "old_commit": delta.old_commit,
        "new_commit": delta.new_commit,
        "added": [[c.path, c.new_blob_oid] for c in delta.added],
        "modified": [[c.path, c.old_blob_oid, c.new_blob_oid] for c in delta.modified],
        "deleted": [[c.path, c.old_blob_oid] for c in delta.deleted],
    }


def delta_from_plan(plan: dict) -> SnapshotDelta | None:
    """Rebuild the snapshot delta stored by plan_sync (None for a full sync)."""
    d = plan.get("delta")
    if d is None:
        return None
    changes = (
        [FileChange(path=p, change_type=FileChangeType.ADDED, old_blob_oid=None, new_blob_oid=n)
         for p, n in d["added"]]
        + [FileChange(path=p, change_type=FileChangeType.MODIFIED, old_blob_oid=o, new_blob_oid=n)
           for p, o, n in d["modified"]]
        + [FileChange(path=p, change_type=FileChangeType.DELETED, old_blob_oid=o, new_blob_oid=None)
           for p, o in d["deleted"]]
    )
    return SnapshotDelta(old_commit=d["old_commit"], new_commit=d["new_commit"], changes=changes)


@dataclass
class _Comparison:
    """HEAD against the index: what plan_sync and preview_sync both start from."""

    commit_sha: str
    snapshot: RepositorySnapshot
    old_snapshot: RepositorySnapshot | None   # None: a full index (nothing indexed, or a reset)
    indexed: bool                             # the index has file summaries
    reset_reason: str | None
    delta: SnapshotDelta | None


def _compare_with_index(store, git: GitAdapter, repo_id: str, force: bool) -> _Comparison:
    """Pin HEAD and compare it with the index (strictly: a database error raises)."""
    commit_sha = git.get_head_sha()
    snapshot = git.get_snapshot(commit_sha)
    old_snapshot = IncrementalLoader(store).load_previous_snapshot(repo_id, strict=True)
    indexed = old_snapshot is not None
    reset_reason = full_reindex_reason(store, repo_id, old_snapshot, force)
    if reset_reason:
        old_snapshot = None
    delta = SnapshotComparer().compare(old_snapshot, snapshot) if old_snapshot is not None else None
    return _Comparison(commit_sha, snapshot, old_snapshot, indexed, reset_reason, delta)


@dataclass
class SyncPreview:
    """What a sync would do now, without staging or generating anything (the UI's check)."""

    commit_sha: str
    indexed_commit: str | None   # the commit the index was last built from
    mode: str                    # "full" | "incremental"
    reason: str
    needed: bool                 # False: the index already matches HEAD
    n_files: int                 # files at HEAD that would be indexed
    n_added: int = 0
    n_modified: int = 0
    n_deleted: int = 0
    n_unchanged: int = 0


def preview_sync(store, git: GitAdapter, repo_id: str, force: bool = False) -> SyncPreview:
    """
    Compare HEAD's eligible files (by blob hash) with what the index holds, as plan_sync
    does, and say whether a sync would change anything. Reads only; no model calls.
    """
    c = _compare_with_index(store, git, repo_id, force)
    meta = store.get_index_meta(repo_id) or {}
    n_files = len(c.snapshot.files)
    if c.delta is None:
        reason = c.reset_reason or "nothing is indexed under this name yet"
        return SyncPreview(
            commit_sha=c.commit_sha, indexed_commit=meta.get("commit_sha"), mode="full",
            reason=reason, needed=True, n_files=n_files,
        )
    d = c.delta
    changed = len(d.added) + len(d.modified) + len(d.deleted)
    return SyncPreview(
        commit_sha=c.commit_sha, indexed_commit=meta.get("commit_sha") or c.old_snapshot.commit_sha,
        mode="incremental", reason="comparing index vs Git state", needed=changed > 0, n_files=n_files,
        n_added=len(d.added), n_modified=len(d.modified), n_deleted=len(d.deleted), n_unchanged=len(d.unchanged),
    )


def plan_sync(
    config: IngestionConfig,
    pack: Pack | None,
    store: MongoStore,
    staging: StagingStore,
    git: GitAdapter,
    params: SyncParams,
    sync_id: str,
) -> SyncPlan:
    """
    Pin HEAD, decide full or incremental, and stage the work: file and lens work
    items, ids to delete, the delta (for the source mirror) and the dir_list document.

    The previous snapshot is loaded strictly: a database error raises (and is
    retried) instead of silently choosing a full re-index.
    """
    repo_id = config.repo_id or "unknown"
    c = _compare_with_index(store, git, repo_id, params.force)
    commit_sha, snapshot, old_snapshot, reset_reason = c.commit_sha, c.snapshot, c.old_snapshot, c.reset_reason

    warnings: list[str] = []
    delta: SnapshotDelta | None = None
    deleted_paths: list[str] = []
    lens_only: list[str] = []
    if old_snapshot is None:
        mode = "full"
        reason = reset_reason or "index empty or collection doesn't exist"
        to_generate = sorted(snapshot.files)
    else:
        mode = "incremental"
        reason = "comparing index vs Git state"
        warnings += pack_version_warnings(store, repo_id, pack, params.refresh_lens)
        warnings += prompt_version_warnings(store, repo_id)
        delta = c.delta
        to_generate = sorted({c.path for c in delta.added} | {c.path for c in delta.modified})
        deleted_paths = sorted(c.path for c in delta.deleted)
        if params.refresh_lens and pack is not None:
            lens_only = sorted(set(snapshot.files) - set(to_generate))

    # Removed files, and folders left empty by them (deleted just before persisting).
    deleted_ids = [StoredDocument.generate_id(repo_id, DocumentType.FILE_SUMMARY, p) for p in deleted_paths]
    orphaned = sorted(all_folders(deleted_paths) - all_folders(snapshot.files)) if deleted_paths else []
    orphaned_ids = [StoredDocument.generate_id(repo_id, DocumentType.FOLDER_SUMMARY, f) for f in orphaned]

    staging.put_work_items(sync_id, "file", [
        {"path": p, "blob_oid": snapshot.files[p].blob_oid} for p in to_generate
    ])
    staging.put_work_items(sync_id, "lens", [
        {"path": p, "blob_oid": snapshot.files[p].blob_oid} for p in lens_only
    ])
    staging.put_plan(sync_id, {
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "mode": mode,
        "reset_index": reset_reason is not None,
        "delta": _delta_dict(delta) if delta is not None else None,
        "deleted_paths": deleted_paths,
        "deleted_ids": deleted_ids,
        "orphaned_folder_ids": orphaned_ids,
    })
    staging.stage_document(sync_id, dir_list_document(repo_id, snapshot))

    return SyncPlan(
        commit_sha=commit_sha,
        mode=mode,
        reset_index=reset_reason is not None,
        reason=reason,
        n_files=len(to_generate),
        n_lens_only=len(lens_only),
        n_deleted=len(deleted_paths),
        warnings=warnings,
        n_added=len(delta.added) if delta else 0,
        n_modified=len(delta.modified) if delta else 0,
        n_unchanged=len(delta.unchanged) if delta else 0,
        n_snapshot_files=len(snapshot.files),
    )


def folder_depth(path: str) -> int:
    return path.count("/") if path else -1


def plan_folders(staging: StagingStore, git: GitAdapter, sync_id: str, lens_refreshed: bool) -> list[LevelInfo]:
    """
    Stage the folders to (re)generate, deepest first, and return their depth levels.

    Full: every folder. Incremental: every surviving ancestor of a changed or deleted
    file, or every folder when the lens of unchanged files was refreshed (so roll-ups
    use the new records). Runs after the file phases because of that last rule.
    """
    plan = staging.get_plan(sync_id)
    snapshot = git.get_snapshot(plan["commit_sha"])
    existing = all_folders(snapshot.files)
    if plan["mode"] == "full" or lens_refreshed:
        folders = existing
    else:
        delta = delta_from_plan(plan)
        changed = {c.path for c in delta.added} | {c.path for c in delta.modified}
        folders = folders_to_regenerate(changed, set(plan["deleted_paths"]), existing)

    ordered = sorted(sorted(folders), key=folder_depth, reverse=True)
    staging.put_work_items(sync_id, "folder", [{"path": p, "depth": folder_depth(p)} for p in ordered])

    levels: list[LevelInfo] = []
    for seq, path in enumerate(ordered):
        depth = folder_depth(path)
        if levels and levels[-1].depth == depth:
            levels[-1].count += 1
        else:
            levels.append(LevelInfo(depth=depth, offset=seq, count=1))
    return levels


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------

def generate_file(
    repo_id: str,
    generator: ArtifactGenerator,
    commit_sha: str,
    path: str,
    blob_oid: str,
) -> tuple[StoredDocument, LLMUsage, int | None]:
    """Summarize one file (plus its lens record when the lens is on). Returns (document, usage, rel)."""
    summary, lens, usage = generator.generate_file_summary(path, blob_oid)
    doc = StoredDocument.for_file_summary(
        repo_id=repo_id,
        path=path,
        commit_sha=commit_sha,
        blob_oid=blob_oid,
        prose_summary=serialize_file_summary(summary),
        content=summary.model_dump(),
    )
    if lens is not None:
        doc.lens, doc.lens_prose = lens.model_dump(), compact_file_lens(lens)
    return doc, usage, lens.rel if lens is not None else None


def refresh_lens(generator: ArtifactGenerator, path: str, blob_oid: str) -> tuple[FileLensRecord, LLMUsage]:
    """Regenerate the lens record (only) of an unchanged file with the installed pack."""
    return generator.generate_file_lens(path, blob_oid)


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------

def folder_lens_inputs(
    file_lenses: dict[str, FileLensRecord],
    child_rollups: dict[str, FolderLensRollup],
    pack_version: str,
) -> tuple[FolderLensRollup, str, str]:
    """The folder's roll-up and the compact lens lines its lens narrative is written from."""
    rollup = compute_rollup(file_lenses, list(child_rollups.values()), pack_version)
    file_lines = "\n".join(f"{Path(p).name}: {compact_file_lens(l)}" for p, l in sorted(file_lenses.items()))
    folder_lines = "\n".join(f"{Path(c).name}/: {compact_rollup(r)}" for c, r in child_rollups.items())
    return rollup, file_lines, folder_lines


def generate_folder_from_children(
    generator: ArtifactGenerator,
    pack: Pack | None,
    folder_path: str,
    child_files: dict[str, FileSemanticSummary],
    child_folders: dict[str, FolderSemanticSummary],
    file_lenses: dict[str, FileLensRecord],
    child_rollups: dict[str, FolderLensRollup],
) -> tuple[FolderSemanticSummary, FolderLensRollup | None, LLMUsage] | None:
    """
    Summarize one folder from all of its direct children.

    Args:
        folder_path: Repository-relative folder ("" for the root)
        child_files: Summaries of direct child files (repo-relative path -> summary)
        child_folders: Summaries of direct child folders (path -> summary); they already
            cover their own subtrees, so the folder summary describes everything beneath it
        file_lenses: Lens records of direct child files
        child_rollups: Lens roll-ups of direct child folders, in sorted path order

    Returns:
        (summary with subtree counts, roll-up or None, usage), or None when the folder
        has no child summaries (it is skipped).
    """
    display_path = folder_path if folder_path else "."
    child_files_summaries = {Path(p).name: child_files[p] for p in sorted(child_files)}
    child_subfolders_summaries = {Path(c).name: child_folders[c] for c in sorted(child_folders)}
    if not child_files_summaries and not child_subfolders_summaries:
        return None

    rollup = None
    if pack is not None:
        rollup, file_lines, folder_lines = folder_lens_inputs(file_lenses, child_rollups, pack.version)
    if rollup is not None and max_rel(rollup) >= 2:
        summary, usage, lens_n = generator.generate_folder_summary_with_lens(
            display_path,
            child_files_summaries,
            child_subfolders_summaries,
            file_lines,
            folder_lines,
        )
        rollup.n = cap_words(lens_n, FOLDER_LENS_WORDS)
    else:
        summary, usage = generator.generate_folder_summary(
            display_path,
            child_files_summaries,
            child_subfolders_summaries,
        )
    summary.subtree = compute_subtree(
        len(child_files_summaries),
        {
            name: subtree_of_child(child.subtree, len(child.files))
            for name, child in child_subfolders_summaries.items()
        },
    )
    return summary, rollup, usage


def folder_document(
    repo_id: str,
    commit_sha: str,
    folder_path: str,
    summary: FolderSemanticSummary,
    rollup: FolderLensRollup | None,
) -> StoredDocument:
    """The stored folder summary (the root is stored as ".")."""
    doc = StoredDocument.for_folder_summary(
        repo_id=repo_id,
        path="." if folder_path == "" else folder_path,
        commit_sha=commit_sha,
        prose_summary=serialize_folder_summary(summary),
        content=summary.model_dump(),
    )
    if rollup is not None:
        doc.lens, doc.lens_prose = rollup.model_dump(), compact_rollup(rollup)
    return doc


@dataclass
class FolderTree:
    """Direct child files and folders of every folder in a snapshot."""

    files: dict[str, list[str]]
    folders: dict[str, list[str]]

    @classmethod
    def of(cls, snapshot: RepositorySnapshot) -> "FolderTree":
        files: dict[str, list[str]] = defaultdict(list)
        for path in snapshot.files:
            files[parent_of(path)].append(path)
        folders: dict[str, set[str]] = defaultdict(set)
        for folder in all_folders(snapshot.files):
            if folder != "":
                folders[parent_of(folder)].add(folder)
        return cls(
            files={k: sorted(v) for k, v in files.items()},
            folders={k: sorted(v) for k, v in folders.items()},
        )


@dataclass
class FolderChildren:
    files: dict[str, FileSemanticSummary]
    folders: dict[str, FolderSemanticSummary]
    file_lenses: dict[str, FileLensRecord]
    rollups: dict[str, FolderLensRollup]


def load_children(
    store: MongoStore,
    staging: StagingStore,
    sync_id: str,
    repo_id: str,
    folder_path: str,
    tree: FolderTree,
    incremental: bool,
) -> FolderChildren:
    """
    A folder's direct children with summaries: staged ones (this run) win; in
    incremental runs the rest come from the live index, except child folders that
    are regenerated in this run (a failed one is left out, as before).
    """
    log = logger.bind(component="sync_steps", folder=folder_path or ".")
    child_file_paths = tree.files.get(folder_path, [])
    child_folder_paths = tree.folders.get(folder_path, [])
    file_id = lambda p: StoredDocument.generate_id(repo_id, DocumentType.FILE_SUMMARY, p)  # noqa: E731
    folder_id = lambda p: StoredDocument.generate_id(repo_id, DocumentType.FOLDER_SUMMARY, p)  # noqa: E731

    staged = staging.get_staged(sync_id, [file_id(p) for p in child_file_paths]
                                + [folder_id(p) for p in child_folder_paths])
    lens_updates = staging.lens_updates(sync_id, child_file_paths) if child_file_paths else {}

    live_files: dict[str, dict] = {}
    live_folders: dict[str, dict] = {}
    if incremental:
        missing_files = [p for p in child_file_paths if file_id(p) not in staged]
        if missing_files:
            live_files = {d["path"]: d for d in store.query_documents(
                repo_id=repo_id, collection="file_summaries",
                filter_query={"path": {"$in": missing_files}},
                projection={"path": 1, "content": 1, "lens": 1},
            ) if d.get("path")}
        not_staged = [p for p in child_folder_paths if folder_id(p) not in staged]
        regenerated = staging.work_item_paths(sync_id, "folder", not_staged)
        missing_folders = [p for p in not_staged if p not in regenerated]
        if missing_folders:
            live_folders = {d["path"]: d for d in store.query_documents(
                repo_id=repo_id, collection="folder_summaries",
                filter_query={"path": {"$in": missing_folders}},
                projection={"path": 1, "content": 1, "lens": 1},
            ) if d.get("path")}

    children = FolderChildren(files={}, folders={}, file_lenses={}, rollups={})
    for path in child_file_paths:
        doc = staged.get(file_id(path)) or live_files.get(path)
        if doc is None:
            continue
        if not doc.get("content"):
            log.warning("stored_file_summary_without_content", path=path)
            continue
        children.files[path] = FileSemanticSummary.model_validate(doc["content"])
        lens = lens_updates[path][0] if path in lens_updates else doc.get("lens")
        if lens:
            children.file_lenses[path] = FileLensRecord.model_validate(lens)
    for path in child_folder_paths:
        doc = staged.get(folder_id(path)) or live_folders.get(path)
        if doc is None:
            continue
        if not doc.get("content"):
            log.warning("stored_folder_summary_without_content", path=path)
            continue
        children.folders[path] = FolderSemanticSummary.model_validate(doc["content"])
        if doc.get("lens"):
            children.rollups[path] = FolderLensRollup.model_validate(doc["lens"])
    return children


# ----------------------------------------------------------------------
# Repository summary and lens
# ----------------------------------------------------------------------

def repo_summary_exists(store, repo_id: str, need_lens: bool = False) -> bool:
    """True if a repository summary (with a repo lens, when need_lens) is already stored."""
    doc_id = StoredDocument.generate_id(repo_id, DocumentType.REPO_SUMMARY, "")
    try:
        docs = store.query_documents(
            repo_id=repo_id,
            collection="repo_summaries",
            filter_query={"_id": doc_id},
            projection={"_id": 1, "lens": 1},
            limit=1,
        )
        return bool(docs) and (not need_lens or bool(docs[0].get("lens")))
    except Exception as e:
        logger.warning("repo_summary_existence_check_failed", error=str(e))
        return True  # do not spend tokens on a guess


def _is_top_level(path: str) -> bool:
    return path == "." or "/" not in path


def collect_repo_summary_input(fresh: dict[str, str], stored: dict[str, str]) -> str:
    """
    The prompt text for the repository summary: the root folder summary followed by
    the top-level folder summaries, as prose (paths as stored: the root is ".").
    Fresh summaries win over stored ones.
    """
    merged = {**stored, **fresh}
    selected = {p: t for p, t in merged.items() if _is_top_level(p)}
    ordered = sorted(selected, key=lambda p: (p != ".", p))

    parts: list[str] = []
    used = 0
    omitted = 0
    for path in ordered:
        block = f"### {path}" + (" (repository root)" if path == "." else "") + f"\n{selected[path]}\n"
        if parts and used + len(block) > REPO_SUMMARY_MAX_INPUT_CHARS:
            omitted += 1
            continue
        parts.append(block)
        used += len(block)

    if omitted:
        parts.append(f"(... {omitted} more top-level folders omitted for length ...)")
    return "\n".join(parts)


@dataclass
class RepoSummaryOutcome:
    generated: bool
    skipped: bool
    usage: LLMUsage


def generate_repo(
    store: MongoStore,
    staging: StagingStore,
    generator: ArtifactGenerator,
    sync_id: str,
    repo_id: str,
    commit_sha: str,
    incremental: bool,
    folders_generated: int,
    need_lens: bool,
) -> RepoSummaryOutcome:
    """
    Generate and stage the repository summary. Incremental runs regenerate it only
    when a folder changed or none is stored yet (this also adds one to indexes
    created before repo summaries were enabled).
    """
    zero = LLMUsage(input_tokens=0, output_tokens=0)
    if incremental and folders_generated == 0 and repo_summary_exists(store, repo_id, need_lens=need_lens):
        logger.info("skipping_repo_summary_no_changes")
        return RepoSummaryOutcome(generated=False, skipped=True, usage=zero)

    fresh = {
        d["path"]: d["prose_summary"]
        for d in staging.staged_documents(sync_id, "folder_summaries", fields=["path", "prose_summary"])
        if d.get("path") and d.get("prose_summary")
    }
    stored: dict[str, str] = {}
    if incremental:
        docs = store.query_documents(
            repo_id=repo_id,
            collection="folder_summaries",
            filter_query={"path": {"$regex": r"^(\.|[^/]+)$"}},
            projection={"path": 1, "prose_summary": 1},
        )
        stored = {d["path"]: d["prose_summary"] for d in docs if d.get("path") and d.get("prose_summary")}

    text = collect_repo_summary_input(fresh, stored)
    if not text.strip():
        print("No folder summaries available; skipping the repository summary.")
        return RepoSummaryOutcome(generated=False, skipped=False, usage=zero)

    summary, usage = generator.generate_repo_summary(repo_id, text)
    staging.stage_document(sync_id, StoredDocument.for_repo_summary(
        repo_id=repo_id,
        commit_sha=commit_sha,
        prose_summary=serialize_repo_summary(summary),
        content=summary.model_dump(),
    ))
    print(f"Repository summary: {usage.input_tokens:,} in / {usage.output_tokens:,} out | "
          f"${usage.total_cost_usd:.4f}")
    return RepoSummaryOutcome(generated=True, skipped=False, usage=usage)


@dataclass
class RepoLensOutcome:
    usage: LLMUsage
    status: str
    archetype: str
    n_processes: int
    searches: int


def generate_repo_lens(
    pack: Pack,
    store: MongoStore,
    staging: StagingStore,
    git: GitAdapter,
    llm: LLMClient,
    docs_search: DocsSearch | None,
    sync_id: str,
    repo_id: str,
    commit_sha: str,
    incremental: bool,
) -> RepoLensOutcome:
    """Tier 0 scan plus the repository lens pass; sets the lens on the staged repository summary."""
    snapshot = git.get_snapshot(commit_sha)
    contents = {path: git.get_file_content(fs.blob_oid) for path, fs in snapshot.files.items()}
    tier0 = tier0_scan(pack, contents)

    # Records with rel >= 1 (rel 3 first within the budget; rel 1 gives gates their
    # evidence): fresh ones, plus stored ones for files not regenerated.
    records: dict[str, tuple[int, str]] = {}
    root_rollup = None
    root_id = StoredDocument.generate_id(repo_id, DocumentType.FOLDER_SUMMARY, ".")
    staged_root = staging.get_staged(sync_id, [root_id]).get(root_id)
    if staged_root and staged_root.get("lens"):
        root_rollup = FolderLensRollup.model_validate(staged_root["lens"])
    if incremental:
        stored = store.query_documents(
            repo_id=repo_id,
            collection="file_summaries",
            filter_query={"lens.rel": {"$gte": 1}},
            projection={"path": 1, "lens.rel": 1, "lens_prose": 1},
        )
        for doc in stored:
            if doc.get("path") in snapshot.files and doc.get("lens_prose"):
                records[doc["path"]] = (doc["lens"]["rel"], doc["lens_prose"])
        if root_rollup is None:
            root_docs = store.query_documents(
                repo_id=repo_id, collection="folder_summaries",
                filter_query={"path": "."}, projection={"lens": 1}, limit=1,
            )
            if root_docs and root_docs[0].get("lens"):
                root_rollup = FolderLensRollup.model_validate(root_docs[0]["lens"])

    fresh: dict[str, tuple[int, str]] = {
        d["path"]: (d["lens"]["rel"], d["lens_prose"])
        for d in staging.staged_documents(sync_id, "file_summaries", fields=["path", "lens", "lens_prose"])
        if d.get("lens") and d.get("lens_prose")
    }
    fresh.update({path: (lens["rel"], prose) for path, (lens, prose) in staging.lens_updates(sync_id).items()})
    for path, (rel, prose) in fresh.items():
        if rel >= 1:
            records[path] = (rel, prose)
        else:
            records.pop(path, None)

    # Incremental runs keep the process list stable: the pass sees the previous
    # processes and the changed files, and changes only what the changes call for.
    previous = None
    if incremental:
        docs = store.query_documents(
            repo_id=repo_id, collection="repo_summaries",
            filter_query={"document_type": "repo_summary"}, projection={"lens.processes": 1}, limit=1,
        )
        if docs and (docs[0].get("lens") or {}).get("processes"):
            previous = docs[0]["lens"]["processes"]

    result = run_repo_pass(
        llm, pack, tier0,
        compact_rollup(root_rollup) if root_rollup else "(no roll-up)",
        records, set(snapshot.files), docs_search,
        previous=previous, changed=sorted(fresh) if previous else None,
    )
    record = {
        **result.lens.model_dump(),
        "pack_version": pack.version,
        "docs_searches": result.searches,
        "docs_urls_returned": result.urls_returned,
        "corrections": result.corrections,
        "tier0": {
            "languages": tier0.languages,
            "temporal_counts": tier0.temporal_counts,
            "temporal_manifests": tier0.temporal_manifests,
        },
    }
    repo_doc_id = StoredDocument.generate_id(repo_id, DocumentType.REPO_SUMMARY, "")
    staged_repo = staging.get_staged(sync_id, [repo_doc_id]).get(repo_doc_id)
    if staged_repo is None:
        raise ValueError("No staged repository summary to attach the repository lens to")
    doc = StoredDocument.model_validate(staged_repo)
    doc.lens, doc.lens_prose = record, compact_repo_lens(result.lens)
    staging.stage_document(sync_id, doc)
    print(f"Repository lens: status={result.lens.status} archetype={result.lens.archetype} "
          f"processes={len(result.lens.processes)} searches={result.searches} | "
          f"${result.usage.total_cost_usd:.4f}")
    return RepoLensOutcome(
        usage=result.usage, status=result.lens.status, archetype=result.lens.archetype,
        n_processes=len(result.lens.processes), searches=result.searches,
    )


# ----------------------------------------------------------------------
# Finalize
# ----------------------------------------------------------------------

def build_index_meta(config: IngestionConfig, pack: Pack | None, commit_sha: str,
                     lens_pack_versions: list[dict] | None = None,
                     prompt_versions: list[dict] | None = None) -> IndexMeta:
    """Describe how this index was built (written after successful persistence)."""
    repo_id = config.repo_id or "unknown"
    return IndexMeta(
        _id=IndexMeta.make_id(repo_id),
        repo_id=repo_id,
        index_model=config.llm.model_id,
        commit_sha=commit_sha,
        repo_path=str(config.repo_path),
        lens_enabled=pack is not None,
        pack=PackRef(id=pack.id, version=pack.version, hash=pack.hash) if pack else None,
        lens_pack_versions=lens_pack_versions or [],
        prompt_versions=prompt_versions or [],
    )


TEXT_INDEX_COLLECTIONS = ["file_summaries", "folder_summaries"]  # not repo_summaries
VALIDATION_TERMS = ["file", "class", "function", "import"]       # generic terms most repos contain


def create_text_indexes(store, repo_id: str) -> list[str]:
    """Create text indexes without validation (full index). Returns the lines to print."""
    lines: list[str] = []
    database = store._get_database_name(repo_id)
    for collection in TEXT_INDEX_COLLECTIONS:
        lines.append(f"Creating index for {collection}...")
        try:
            if store.ensure_text_index(database, collection) == "created":
                lines.append(f"  ✓ Created text index on {collection} (prose_summary, lens_prose)")
            else:
                lines.append(f"  ℹ️  Text index already present on {collection}")
        except Exception as e:
            logger.error("index_creation_failed", collection=collection, error=str(e))
            lines.append(f"  ⚠️  FAILED: {e}")
            lines.append(f"  ⚠️  Text search may not work properly for {collection}")
    return lines


def validate_index_search(store, database: str, collection: str, search_terms: list[str]) -> bool:
    """True if at least one search term returns results."""
    for term in search_terms:
        try:
            if store.text_search(database=database, collection=collection, search_text=term, limit=1):
                return True
        except Exception as e:
            logger.debug("validation_search_failed", collection=collection, term=term, error=str(e))
    return False


def validate_text_indexes(store, repo_id: str) -> list[str]:
    """Make sure text indexes exist and return results (incremental). Returns the lines to print."""
    lines: list[str] = []
    database = store._get_database_name(repo_id)
    for collection in TEXT_INDEX_COLLECTIONS:
        lines.append(f"Validating {collection}...")
        try:
            # Make sure the index covers prose_summary and lens_prose (replaces older indexes)
            if store.ensure_text_index(database, collection) == "created":
                lines.append(f"  ✓ Created text index on {collection} (prose_summary, lens_prose)")
            if validate_index_search(store, database, collection, VALIDATION_TERMS):
                lines.append(f"  ✓ Text index on {collection} is working")
            else:
                raise Exception(f"Text index search returned no results for {collection}")
        except Exception as e:
            logger.error("index_validation_failed", collection=collection, error=str(e))
            lines.append(f"  ❌ FAILED: {e}")
            # Don't fail the entire sync, just warn
            lines.append(f"  ⚠️  Text search may not work properly for {collection}")
    return lines


def finalize_index(
    config: IngestionConfig,
    pack: Pack | None,
    store: MongoStore,
    staging: StagingStore,
    sync_id: str,
    commit_sha: str,
    incremental: bool,
) -> tuple[int | None, list[str]]:
    """
    After persistence: apply staged lens-only updates, write index_meta, then create
    (full) or validate (incremental) the text indexes.

    Returns (lens records refreshed or None when there were none, lines to print).
    """
    repo_id = config.repo_id or "unknown"
    updates = staging.lens_updates(sync_id)
    lens_refreshed = None
    if updates:
        lens_refreshed = store.update_file_lenses(
            repo_id, [(path, lens, prose) for path, (lens, prose) in updates.items()]
        )
    store.put_index_meta(build_index_meta(
        config, pack, commit_sha, store.lens_pack_versions(repo_id), store.prompt_versions(repo_id)
    ))
    lines = validate_text_indexes(store, repo_id) if incremental else create_text_indexes(store, repo_id)
    return lens_refreshed, lines


def stored_documents(staging: StagingStore, sync_id: str) -> list[StoredDocument]:
    """Every staged document of the run (for the optional disk export)."""
    return [StoredDocument.model_validate(d) for d in staging.staged_documents(sync_id)]

