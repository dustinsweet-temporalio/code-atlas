"""
The real workflow and activities end to end (Temporal test server, a real git
repository, in-memory Mongo and LLM fakes): full index, incremental runs,
deletions, forced re-index, dry run and Continue-As-New.
"""
from tests.temporal.conftest import commit_files


FILES = {
    "README.md": "# demo\n",
    "src/app/main.py": "print('main')\n",
    "src/app/util.py": "def util(): pass\n",
    "src/app/models/a.py": "class A: pass\n",
    "src/other.py": "x = 1\n",
}


def _ids(store, repo_id, collection):
    return sorted(store.dbs[repo_id][collection])


async def test_full_then_incremental_then_deletion(env, harness):
    commit_files(harness.repo_path, FILES)
    out = await harness.run(env)

    repo = harness.repo_id
    assert out.plan.mode == "full" and not out.plan.reset_index
    assert out.failed == [] and out.persist_error is None and out.finalize_error is None
    assert [p.split("::")[-1] for p in _ids(harness.store, repo, "file_summaries")] == sorted(FILES)
    assert [p.split("::")[-1] for p in _ids(harness.store, repo, "folder_summaries")] == [
        ".", "src", "src/app", "src/app/models"]
    assert {d["document_type"] for d in harness.store.dbs[repo]["repo_summaries"].values()} == {
        "repo_summary", "dir_list"}
    root = harness.store.dbs[repo]["folder_summaries"][f"{repo}::folder_summary::."]
    assert root["content"]["subtree"]["total_files"] == 5
    assert harness.store.get_index_meta(repo)["commit_sha"] == out.plan.commit_sha
    assert out.mirror.uploaded == 5 and (harness.source_dir / repo / "src/other.py").read_text() == "x = 1\n"
    assert harness.staging.docs == {} and harness.staging.plans == {}  # staging cleaned up
    calls_after_full = len(harness.llm.calls)
    assert calls_after_full == 5 + 4 + 1  # files, folders, repo

    # Unchanged: nothing regenerated, the repo summary already exists.
    out = await harness.run(env)
    assert out.plan.mode == "incremental" and out.plan.n_files == 0
    assert len(harness.llm.calls) == calls_after_full

    # One modified file: its file, its ancestors and the repo summary are regenerated.
    commit_files(harness.repo_path, {"src/app/main.py": "print('changed')\n"})
    out = await harness.run(env)
    assert out.plan.n_modified == 1 and out.files_done == 1 and out.folders_generated == 3
    assert harness.llm.calls[calls_after_full:].count("FolderSemanticSummary") == 3
    assert out.repo_summary_generated

    # Deleting the only file of a folder removes its summary and the folder's.
    commit_files(harness.repo_path, {"src/app/models/a.py": None})
    out = await harness.run(env)
    assert out.plan.n_deleted == 1 and out.deleted.files_deleted == 1 and out.deleted.folders_deleted == 1
    assert f"{repo}::folder_summary::src/app/models" not in harness.store.dbs[repo]["folder_summaries"]
    root = harness.store.dbs[repo]["folder_summaries"][f"{repo}::folder_summary::."]
    assert root["content"]["subtree"]["total_files"] == 4
    assert not (harness.source_dir / repo / "src/app/models/a.py").exists()
    assert harness.store.calls.index("delete") < len(harness.store.calls) - 1 - harness.store.calls[::-1].index("upsert")


async def test_force_resets_once_before_persisting(env, harness):
    commit_files(harness.repo_path, FILES)
    await harness.run(env)
    harness.store.calls.clear()
    out = await harness.run(env, force=True)
    assert out.plan.mode == "full" and out.plan.reset_index
    assert harness.store.calls.count("reset") == 1
    assert harness.store.calls.index("reset") < harness.store.calls.index("upsert")
    assert len(_ids(harness.store, harness.repo_id, "file_summaries")) == 5


async def test_dry_run_writes_nothing_and_cleans_staging(env, harness):
    commit_files(harness.repo_path, FILES)
    out = await harness.run(env, dry_run=True)
    assert out.dry_run and out.would_upsert == 5 + 4 + 2  # files, folders, repo summary, dir_list
    assert harness.store.calls == []
    assert out.persist is None and out.mirror is None and out.report is None
    assert not (harness.source_dir / harness.repo_id).exists()
    assert harness.staging.docs == {}


async def test_continue_as_new_processes_every_file_once(env, harness):
    files = {f"pkg{i % 3}/f{i}.py": f"v = {i}\n" for i in range(12)}
    commit_files(harness.repo_path, files)
    out = await harness.run(env, window_size=2, test_can_after_items=3)
    assert out.continued_as_new_count >= 4
    assert out.files_done == 12 and out.failed == []
    assert harness.llm.calls.count("FileSemanticSummary") == 12
    assert harness.llm.calls.count("FolderSemanticSummary") == 4  # pkg0..2 and the root
    assert len(_ids(harness.store, harness.repo_id, "file_summaries")) == 12


async def test_failed_file_is_recorded_and_the_run_continues(env, harness):
    from semantic_ingress.core.llm_client import LLMPermanentError

    commit_files(harness.repo_path, FILES)
    harness.llm.fail_paths = {"src/other.py"}
    harness.llm.error = LLMPermanentError("Anthropic API error (400): bad request")
    out = await harness.run(env)
    assert [(f.path, f.error) for f in out.failed] == [("src/other.py", "Anthropic API error (400): bad request")]
    assert harness.llm.calls.count("FileSemanticSummary") == 5  # non-retryable: one attempt
    assert len(_ids(harness.store, harness.repo_id, "file_summaries")) == 4
    assert out.persist_error is None and out.mirror.uploaded == 5
