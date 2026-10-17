"""
Incremental folder regeneration must rebuild a folder from ALL its children:
fresh summaries for changed files plus stored summaries for unchanged siblings
and unchanged child folders (regression test for a bug where unchanged siblings
were silently dropped from regenerated folder summaries).
"""
from threading import Lock

import structlog

from semantic_ingress.core.generation_orchestrator import GenerationOrchestrator
from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.models import FileSemanticSummary, FolderSemanticSummary


def _file(role: str) -> FileSemanticSummary:
    return FileSemanticSummary(role=role)


def _folder(path: str, files: list[str]) -> FolderSemanticSummary:
    return FolderSemanticSummary(
        path=path,
        summary=f"summary of {path}",
        files=[{"name": f, "description": "d"} for f in files],
    )


class FakeGenerator:
    """Records what each folder was generated from."""

    def __init__(self):
        self.calls: dict[str, tuple[list[str], list[str]]] = {}

    def generate_folder_summary(self, folder_path, file_summaries, subfolder_summaries):
        self.calls[folder_path] = (sorted(file_summaries), sorted(subfolder_summaries))
        summary = FolderSemanticSummary(
            path=folder_path,
            summary=f"summary of {folder_path}",
            files=[{"name": n, "description": "d"} for n in file_summaries],
            subfolders=[{"name": n, "description": "d"} for n in subfolder_summaries],
        )
        return summary, LLMUsage(input_tokens=0, output_tokens=0)


class FakeStore:
    """query_documents over in-memory collections, supporting {"path": {"$in": [regex...]}}."""

    def __init__(self, collections: dict[str, list[dict]]):
        self.collections = collections

    def query_documents(self, repo_id, collection, filter_query=None, projection=None, limit=None, batch_size=100):
        patterns = (filter_query or {}).get("path", {}).get("$in", [])
        return [d for d in self.collections.get(collection, []) if any(p.match(d["path"]) for p in patterns)]


def _orchestrator() -> GenerationOrchestrator:
    orch = object.__new__(GenerationOrchestrator)  # no git repo or API key needed
    orch.config = type("C", (), {"repo_id": "r"})()
    orch.log = structlog.get_logger()
    orch.usage_lock, orch.error_lock, orch.progress_lock = Lock(), Lock(), Lock()
    orch.total_usage = LLMUsage(input_tokens=0, output_tokens=0)
    orch.failed_items = []
    orch.pack = None  # lens off; lens folder behaviour is covered in test_lens_generation.py
    orch.docs_search = None
    orch._reset_lens_state()
    return orch


def test_regenerated_folder_includes_unchanged_siblings_and_child_folders():
    # Repository:
    #   src/app/main.py      <- modified
    #   src/app/util.py      (unchanged)
    #   src/app/models/a.py  (unchanged child folder src/app/models)
    #   src/other.py         (unchanged)
    #   README.md            (unchanged)
    store = FakeStore({
        "file_summaries": [
            {"path": p, "content": _file(p).model_dump()}
            for p in ["src/app/main.py", "src/app/util.py", "src/app/models/a.py", "src/other.py", "README.md"]
        ],
        "folder_summaries": [
            {"path": p, "content": _folder(p, fs).model_dump()}
            for p, fs in [("src/app/models", ["a.py"]), ("src/app", ["main.py", "util.py"]),
                          ("src", ["other.py"]), (".", ["README.md"])]
        ],
    })
    orch = _orchestrator()
    affected = {"src/app", "src", ""}
    fresh = {"src/app/main.py": _file("new main")}

    stored_files, stored_folders = orch._load_children_for_folders(store, affected, skip_files=set(fresh))
    assert set(stored_files) == {"src/app/util.py", "src/other.py", "README.md"}  # not main.py, not models/a.py
    assert set(stored_folders) == {"src/app/models"}  # src/app and src are regenerated, root never a child

    gen = FakeGenerator()
    result = orch._generate_folder_artifacts(gen, {**stored_files, **fresh}, affected, known_folder_summaries=stored_folders)

    assert gen.calls["src/app"] == (["main.py", "util.py"], ["models"])
    assert gen.calls["src"] == (["other.py"], ["app"])
    assert gen.calls["."] == (["README.md"], ["src"])
    assert "src/app/models" not in gen.calls  # unchanged folder is reused, not regenerated

    # Subtree counts cover everything beneath each folder.
    assert result["src/app"].subtree.total_files == 3  # main, util, models/a
    assert result[""].subtree.total_files == 5
    assert result[""].subtree.by_subfolder == {"src": 4}


def test_full_generation_unchanged_without_stored_children():
    gen = FakeGenerator()
    files = {"a.py": _file("a"), "pkg/b.py": _file("b")}
    result = _orchestrator()._generate_folder_artifacts(gen, files)
    assert gen.calls == {"pkg": (["b.py"], []), ".": (["a.py"], ["pkg"])}
    assert result[""].subtree.total_files == 2
