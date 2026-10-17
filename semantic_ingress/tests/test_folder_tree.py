from semantic_ingress.core.folder_tree import (
    ancestors,
    compute_subtree,
    direct_child_pattern,
    folders_to_regenerate,
    parent_of,
)
from semantic_ingress.models import SubtreeStats


def test_parent_and_ancestors():
    assert parent_of("a.py") == ""
    assert parent_of("src/app/main.py") == "src/app"
    assert ancestors("src/app/main.py") == ["src/app", "src", ""]
    assert ancestors("a.py") == [""]


def test_folders_to_regenerate_includes_ancestors_of_changes_and_deletions():
    existing = {"", "src", "src/app", "docs"}
    result = folders_to_regenerate(
        changed_files={"src/app/main.py"},
        deleted_files={"docs/old.md"},
        existing_folders=existing,
    )
    assert result == {"", "src", "src/app", "docs"}


def test_folders_to_regenerate_skips_folders_that_no_longer_exist():
    # The only file in "gone/" was deleted, so "gone" is not rebuilt; its parent is.
    result = folders_to_regenerate(set(), {"gone/x.py"}, existing_folders={""})
    assert result == {""}


def test_direct_child_pattern():
    root = direct_child_pattern("")
    assert root.match("a.py") and root.match("src")
    assert not root.match("src/a.py")
    nested = direct_child_pattern("src/app")
    assert nested.match("src/app/main.py")
    assert not nested.match("src/app/sub/x.py")
    assert not nested.match("src/apple/x.py")


def _leaf(files: int) -> SubtreeStats:
    return SubtreeStats(direct_files=files, total_files=files, total_folders=0, max_depth=0, by_subfolder={})


def test_compute_subtree_rolls_up_children():
    api = compute_subtree(2, {"v1": _leaf(3)})  # api: 2 direct + v1(3)
    root = compute_subtree(1, {"api": api, "web": _leaf(4)})
    assert api.total_files == 5 and api.total_folders == 1 and api.max_depth == 1
    assert root.direct_files == 1
    assert root.total_files == 1 + 5 + 4
    assert root.total_folders == 3  # api, api/v1, web
    assert root.max_depth == 2
    assert root.by_subfolder == {"api": 5, "web": 4}
