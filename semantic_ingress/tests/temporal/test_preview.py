"""
preview_sync (the UI's "is a sync needed?" check) against real syncs: it must agree
with the plan the next sync makes, and read without staging anything.
"""
from semantic_ingress.config import FileFilterConfig
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.core.sync_steps import preview_sync
from tests.temporal.conftest import commit_files

FILES = {"README.md": "# demo\n", "src/a.py": "a = 1\n", "src/b.py": "b = 2\n"}


def _preview(harness, force=False):
    return preview_sync(harness.store, GitAdapter(harness.repo_path, FileFilterConfig()), harness.repo_id, force)


async def test_preview_matches_the_next_sync(env, harness):
    head = commit_files(harness.repo_path, FILES)
    p = _preview(harness)
    assert p.mode == "full" and p.needed and p.n_files == 3 and p.indexed_commit is None
    assert harness.staging.plans == {} and harness.staging.docs == {}  # reads only

    await harness.run(env)
    assert harness.store.get_index_meta(harness.repo_id)["repo_path"] == str(harness.repo_path)
    p = _preview(harness)
    assert p.mode == "incremental" and not p.needed and p.indexed_commit == head
    assert (p.n_added, p.n_modified, p.n_deleted, p.n_unchanged) == (0, 0, 0, 3)

    commit_files(harness.repo_path, {"src/a.py": "a = 2\n", "src/c.py": "c = 3\n", "src/b.py": None})
    p = _preview(harness)
    assert p.needed and (p.n_added, p.n_modified, p.n_deleted, p.n_unchanged) == (1, 1, 1, 1)
    out = await harness.run(env)
    assert (out.plan.n_added, out.plan.n_modified, out.plan.n_deleted) == (1, 1, 1)


async def test_head_moving_without_indexed_changes_needs_no_sync(env, harness):
    commit_files(harness.repo_path, FILES)
    await harness.run(env)
    head = commit_files(harness.repo_path, {"image.png": "not indexed\n"})  # excluded by INDEX_EXCLUDE
    p = _preview(harness)
    assert not p.needed and p.commit_sha == head and p.indexed_commit != head


async def test_force_previews_a_full_reindex(env, harness):
    commit_files(harness.repo_path, FILES)
    await harness.run(env)
    p = _preview(harness, force=True)
    assert p.mode == "full" and p.needed and p.reason.startswith("--force")
