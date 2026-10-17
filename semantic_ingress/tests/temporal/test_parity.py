"""
Parity: the activity-side steps produce what the in-process sync produced.

Files: the stored document matches the old per-file conversion. Folders: rebuilding
affected folders level by level from staging plus the live index generates each
folder from the same children (and the same roll-ups) as the old in-memory pass.
"""
from pathlib import Path

import pytest

from semantic_ingress.core import sync_steps
from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.core.prose_serializer import serialize_file_summary
from semantic_ingress.lens.models import FileLens, FileLensRecord
from semantic_ingress.lens.pack import load_pack
from semantic_ingress.lens.serialize import compact_file_lens
from semantic_ingress.models import (
    DocumentType,
    FileSemanticSummary,
    FileSnapshot,
    RepositorySnapshot,
    StoredDocument,
)
from tests.test_incremental_folders import FakeGenerator, _file, _folder, _orchestrator
from tests.test_lens_generation import LensFakeGenerator
from tests.temporal.fakes import FakeStaging, FakeStore

PACK_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "temporal-pack.yaml"
REPO = "r"


def _record(rel: int, kind: str, pack_version: str) -> FileLensRecord:
    lens = FileLens(rel=rel, cov="full", kind=[kind], sig=[], sym=[], c=[], f=[], n="x")
    return FileLensRecord(**lens.model_dump(), mode="conv", lang=None, pack_version=pack_version)


def _without_time(doc: StoredDocument) -> dict:
    data = doc.model_dump(by_alias=True)
    data.pop("generated_at")
    return data


def test_file_document_matches_the_old_conversion():
    summary = FileSemanticSummary(role="Handles orders.", imports=["os"])
    record = _record(3, "ORCH", "0.1.0")

    class Generator:
        def generate_file_summary(self, path, blob_oid):
            return summary, record, LLMUsage(input_tokens=5, output_tokens=1)

    new, usage, rel = sync_steps.generate_file(REPO, Generator(), "c1", "src/orders.py", "b1")

    old = StoredDocument.for_file_summary(  # GenerationOrchestrator._convert_to_stored_documents
        repo_id=REPO, path="src/orders.py", commit_sha="c1", blob_oid="b1",
        prose_summary=serialize_file_summary(summary), content=summary.model_dump(),
    )
    old.lens, old.lens_prose = record.model_dump(), compact_file_lens(record)
    assert _without_time(new) == _without_time(old)
    assert rel == 3 and usage.input_tokens == 5


def _snapshot(paths):
    return RepositorySnapshot(commit_sha="c1", files={p: FileSnapshot(path=p, blob_oid="b") for p in paths})


def _rebuild(generator, pack, store, staging, sync_id, snapshot, folders, incremental):
    """The activity path: plan levels, then each folder from staging and (incremental) the live index."""
    ordered = sorted(sorted(folders), key=sync_steps.folder_depth, reverse=True)
    staging.put_work_items(sync_id, "folder", [{"path": p} for p in ordered])
    tree = sync_steps.FolderTree.of(snapshot)
    results = {}
    for folder in ordered:
        children = sync_steps.load_children(store, staging, sync_id, REPO, folder, tree, incremental)
        out = sync_steps.generate_folder_from_children(
            generator, pack, folder, children.files, children.folders, children.file_lenses, children.rollups)
        if out is None:
            continue
        summary, rollup, _ = out
        staging.stage_document(sync_id, sync_steps.folder_document(REPO, "c1", folder, summary, rollup))
        results[folder] = (summary, rollup)
    return results


def test_incremental_folders_match_the_old_pass():
    paths = ["src/app/main.py", "src/app/util.py", "src/app/models/a.py", "src/other.py", "README.md"]
    store = FakeStore()
    store.upsert_documents_bulk(
        [StoredDocument.for_file_summary(REPO, p, "c0", "b", "prose", _file(p).model_dump()) for p in paths]
        + [StoredDocument.for_folder_summary(REPO, p, "c0", "prose", _folder(p, fs).model_dump())
           for p, fs in [("src/app/models", ["a.py"]), ("src/app", ["main.py", "util.py"]),
                         ("src", ["other.py"]), (".", ["README.md"])]]
    )
    affected = {"src/app", "src", ""}
    fresh = {"src/app/main.py": _file("new main")}

    # Old: load stored children, then the in-memory bottom-up pass.
    orch = _orchestrator()
    stored_files, stored_folders = orch._load_children_for_folders(store, affected, skip_files=set(fresh))
    old_gen = FakeGenerator()
    old = orch._generate_folder_artifacts(old_gen, {**stored_files, **fresh}, affected,
                                          known_folder_summaries=stored_folders)

    # New: the fresh file summary is staged; everything else comes from the live index.
    staging = FakeStaging()
    staging.stage_document("s", StoredDocument.for_file_summary(
        REPO, "src/app/main.py", "c1", "b", "prose", fresh["src/app/main.py"].model_dump()))
    new_gen = FakeGenerator()
    new = _rebuild(new_gen, None, store, staging, "s", _snapshot(paths), affected, incremental=True)

    assert new_gen.calls == old_gen.calls
    assert {p: s.model_dump() for p, (s, _) in new.items()} == {p: s.model_dump() for p, s in old.items()}


def test_a_failed_child_folder_is_left_out_as_before():
    # src/app is regenerated in this run but failed: its parent must not fall back to the stored summary.
    paths = ["src/app/main.py", "src/other.py"]
    store = FakeStore()
    store.upsert_documents_bulk([StoredDocument.for_folder_summary(
        REPO, "src/app", "c0", "prose", _folder("src/app", ["main.py"]).model_dump())])
    staging = FakeStaging()
    staging.put_work_items("s", "folder", [{"path": "src/app"}, {"path": "src"}])
    staging.stage_document("s", StoredDocument.for_file_summary(REPO, "src/other.py", "c1", "b", "p",
                                                                 _file("o").model_dump()))
    children = sync_steps.load_children(store, staging, "s", REPO, "src", sync_steps.FolderTree.of(_snapshot(paths)),
                                        incremental=True)
    assert list(children.files) == ["src/other.py"] and children.folders == {}


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_PATH)


def test_full_run_folder_lenses_match_the_old_pass(pack):
    records = {"core/wf.py": _record(3, "ORCH", pack.version), "docs/a.md": _record(0, "NONE", pack.version),
               "core/sub/x.py": _record(2, "ACT", pack.version)}
    summaries = {p: _file(p) for p in records}

    orch = _orchestrator()
    orch.pack = pack
    orch._file_lenses.update(records)
    old_gen = LensFakeGenerator()
    old = orch._generate_folder_artifacts(old_gen, summaries)

    staging = FakeStaging()
    for p, rec in records.items():
        doc = StoredDocument.for_file_summary(REPO, p, "c1", "b", "prose", summaries[p].model_dump())
        doc.lens, doc.lens_prose = rec.model_dump(), compact_file_lens(rec)
        staging.stage_document("s", doc)
    new_gen = LensFakeGenerator()
    snapshot = _snapshot(records)
    new = _rebuild(new_gen, pack, FakeStore(), staging, "s", snapshot, sync_steps.all_folders(records),
                   incremental=False)

    assert sorted(new_gen.with_lens) == sorted(old_gen.with_lens)
    for folder, summary in old.items():
        assert new[folder][0].model_dump() == summary.model_dump()
        assert new[folder][1].model_dump() == orch._folder_rollups[folder].model_dump()
    root = staging.get_staged("s", [StoredDocument.generate_id(REPO, DocumentType.FOLDER_SUMMARY, ".")])
    assert root and next(iter(root.values()))["lens"]["files"] == 3
