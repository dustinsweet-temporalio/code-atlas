"""
Folder-tree helpers for folder summary generation.

Pure functions (no I/O): which folders to regenerate after a change, how to find
a folder's direct children in the index, and the deterministic subtree counts
stored on every folder summary. In-memory folder keys use "" for the root; the
index stores the root folder as ".".
"""
import re
from pathlib import PurePosixPath

from semantic_ingress.models import SubtreeStats


def parent_of(path: str) -> str:
    """Parent folder of a repository-relative path ("" for the root)."""
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def ancestors(path: str) -> list[str]:
    """All folders containing a path, nearest first, ending with the root ("")."""
    result = []
    current = path
    while current:
        current = parent_of(current)
        result.append(current)
    return result


def ancestors_inclusive(path: str, is_folder: bool) -> list[str]:
    """Folders implied by a path: the folder itself when it is one, plus its ancestors."""
    return ([path] if is_folder else []) + ancestors(path)


def folders_to_regenerate(
    changed_files: set[str],
    deleted_files: set[str],
    existing_folders: set[str],
) -> set[str]:
    """
    Folders whose summaries must be rebuilt after a change: every ancestor of a
    changed (added or modified) or deleted file that still exists. Deleted files
    count too, so a deletion refreshes the surviving parents that listed them.
    """
    affected: set[str] = set()
    for path in changed_files | deleted_files:
        affected.update(ancestors(path))
    return affected & existing_folders


def direct_child_pattern(folder: str) -> re.Pattern:
    """
    Regex matching paths that are direct children of `folder` ("" = root).

    For the root it also matches ".", the stored root folder; callers that look
    up child folders must skip that entry.
    """
    if folder == "":
        return re.compile(r"^[^/]+$")
    return re.compile("^" + re.escape(folder) + r"/[^/]+$")


def compute_subtree(direct_files: int, child_subtrees: dict[str, SubtreeStats]) -> SubtreeStats:
    """
    Subtree counts for a folder from its direct file count and its direct child
    folders' own subtree counts (which already cover everything beneath them).
    """
    by_subfolder = {name: stats.total_files for name, stats in sorted(child_subtrees.items())}
    return SubtreeStats(
        direct_files=direct_files,
        total_files=direct_files + sum(by_subfolder.values()),
        total_folders=sum(1 + stats.total_folders for stats in child_subtrees.values()),
        max_depth=max((1 + stats.max_depth for stats in child_subtrees.values()), default=0),
        by_subfolder=by_subfolder,
    )


def subtree_of_child(child_content_subtree: SubtreeStats | None, child_direct_files: int) -> SubtreeStats:
    """
    A child folder's subtree counts, falling back to its direct file count when
    the stored summary predates subtree counts.
    """
    if child_content_subtree is not None:
        return child_content_subtree
    return SubtreeStats(
        direct_files=child_direct_files,
        total_files=child_direct_files,
        total_folders=0,
        max_depth=0,
        by_subfolder={},
    )
