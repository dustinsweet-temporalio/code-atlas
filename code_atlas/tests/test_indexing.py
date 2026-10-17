"""Indexing from the UI (code_atlas/indexing.py): the folder picker and the sync check's wording."""
import sys
from pathlib import Path

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import indexing  # noqa: E402
from semantic_ingress.core.sync_steps import SyncPreview  # noqa: E402


def commit(repo: Path) -> str:
    repo.mkdir(parents=True)
    pygit2.init_repository(str(repo))
    (repo / "a.py").write_text("a = 1\n")
    r = pygit2.Repository(str(repo))
    r.index.add_all(); r.index.write()
    sig = pygit2.Signature("Test", "test@example.com")
    return str(r.create_commit("HEAD", sig, sig, "first commit\n\nbody", r.index.write_tree(), []))


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(indexing, "REPOS_ROOT", str(root))
    monkeypatch.setattr(indexing, "HOST_HOME", str(root))
    return root


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(indexing.router)
    return TestClient(app)


def test_browse_lists_folders_and_marks_repositories(root, api):
    sha = commit(root / "service")
    (root / "notes").mkdir()
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x")
    body = api.get("/api/fs").json()
    assert body["path"] == str(root.resolve()) and body["parent"] is None and body["git"] is None
    assert [(e["name"], e["git"]) for e in body["entries"]] == [("notes", False), ("service", True)]

    body = api.get("/api/fs", params={"path": "~/service"}).json()
    assert body["git"]["commit"] == sha and body["git"]["subject"] == "first commit"
    assert body["parent"] == str(root.resolve()) and body["suggested_name"] == "service"


def test_paths_outside_the_root_or_missing_are_refused(root, api, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    res = api.get("/api/fs", params={"path": str(tmp_path / "elsewhere")})
    assert res.status_code == 400 and "Only folders under" in res.json()["error"]
    res = api.get("/api/fs", params={"path": str(root / "missing")})
    assert res.status_code == 404
    res = api.get("/api/fs", params={"path": "relative/path"})
    assert res.status_code == 400 and "full path" in res.json()["error"]


def test_a_folder_without_git_cannot_be_synced(root):
    (root / "plain").mkdir()
    with pytest.raises(indexing.PathError, match="no .git folder"):
        indexing._repository(str(root / "plain"))


def test_indexing_off_without_repos_root(monkeypatch, api):
    monkeypatch.setattr(indexing, "REPOS_ROOT", "")
    res = api.get("/api/fs")
    assert res.status_code == 503 and "REPOS_ROOT" in res.json()["error"]


def test_suggested_names_use_the_allowed_characters():
    assert indexing.suggest_name("my.service v2") == "my_service_v2"


def preview(**over):
    base = dict(commit_sha="b" * 40, indexed_commit="a" * 40, mode="incremental", reason="", needed=True,
                n_files=10, n_added=0, n_modified=0, n_deleted=0, n_unchanged=10)
    base.update(over)
    return SyncPreview(**base)


def test_check_messages():
    m = indexing.check_message
    assert m(preview(needed=False), False).startswith("Up to date") and "HEAD moved from aaaaaaa" in m(preview(needed=False), False)
    assert "HEAD moved" not in m(preview(needed=False, indexed_commit="b" * 40), False)
    assert m(preview(n_added=2, n_deleted=1), False) == (
        "Sync needed: 2 added, 1 deleted since aaaaaaa. An incremental sync summarises 2 files.")
    new = preview(mode="full", indexed_commit=None, reason="nothing is indexed under this name yet")
    assert m(new, False) == "Not indexed yet. Indexing summarises 10 files at bbbbbbb."
    assert m(preview(mode="full", reason="--force: replacing the existing index"), True).startswith("A full re-index summarises all 10 files")
