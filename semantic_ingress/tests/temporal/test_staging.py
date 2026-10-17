"""
The staging store contract, run against the in-memory fake and, when MongoDB is
reachable at TEST_MONGO_URL, against the real StagingStore.
"""
import os
import uuid

import pytest

from semantic_ingress.models import StoredDocument
from tests.temporal.fakes import FakeStaging


def _real_staging():
    url = os.environ.get("TEST_MONGO_URL")
    if not url:
        pytest.skip("TEST_MONGO_URL not set")
    from pymongo import MongoClient

    from semantic_ingress.storage.staging_store import StagingStore

    client = MongoClient(url, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception as e:
        pytest.skip(f"MongoDB not reachable at TEST_MONGO_URL: {e}")
    return StagingStore(client, database=f"code_atlas_ingest_test_{uuid.uuid4().hex[:6]}")


@pytest.fixture(params=["fake", "mongo"])
def staging(request):
    if request.param == "fake":
        yield FakeStaging()
        return
    store = _real_staging()
    yield store
    store.db.client.drop_database(store.db.name)


def _doc(path: str, text: str = "prose") -> StoredDocument:
    return StoredDocument.for_file_summary("r", path, "c1", "b", text, {"role": path})


def test_plan_and_work_items(staging):
    staging.put_plan("s", {"commit_sha": "c1", "deleted_ids": ["x"]})
    assert staging.get_plan("s")["commit_sha"] == "c1" and staging.get_plan("other") is None

    staging.put_work_items("s", "file", [{"path": f"f{i}", "blob_oid": "b"} for i in range(5)])
    page = staging.load_work_items("s", "file", 2, 2)
    assert [(i["seq"], i["path"]) for i in page] == [(2, "f2"), (3, "f3")]
    assert staging.work_item_paths("s", "file", ["f1", "nope"]) == {"f1"}

    staging.put_work_items("s", "file", [{"path": "g0"}])  # a rewrite replaces the old items
    assert [i["path"] for i in staging.load_work_items("s", "file", 0, 10)] == ["g0"]


def test_staged_documents_are_idempotent_and_paged_in_a_stable_order(staging):
    for path in ["b.py", "a.py", "c.py"]:
        staging.stage_document("s", _doc(path))
    staging.stage_document("s", _doc("a.py", "newer"))  # a retry overwrites the same record
    staging.stage_document("t", _doc("a.py"))           # another sync is separate

    assert staging.count_staged("s") == 3
    ids = [StoredDocument.generate_id("r", _doc("a.py").document_type, p) for p in ["a.py", "zzz"]]
    staged = staging.get_staged("s", ids)
    assert list(staged) == [ids[0]] and staged[ids[0]]["prose_summary"] == "newer"
    pages = staging.staged_page("s", 0, 2) + staging.staged_page("s", 2, 2)
    assert [d.path for d in pages] == ["a.py", "b.py", "c.py"]
    assert [d["path"] for d in staging.staged_documents("s", "file_summaries")] == ["a.py", "b.py", "c.py"]


def test_lens_updates_and_cleanup(staging):
    staging.stage_document("s", _doc("a.py"))
    staging.stage_lens_update("s", "a.py", {"rel": 1}, "rel 1")
    staging.stage_lens_update("s", "b.py", {"rel": 2}, "rel 2")
    staging.put_work_items("s", "folder", [{"path": "."}])
    staging.put_plan("s", {"commit_sha": "c1"})
    assert staging.lens_updates("s", ["b.py"]) == {"b.py": ({"rel": 2}, "rel 2")}
    assert list(staging.lens_updates("s")) == ["a.py", "b.py"]

    assert staging.cleanup("s") == {"documents": 1, "lens_updates": 2}
    assert staging.count_staged("s") == 0 and staging.lens_updates("s") == {}
    assert staging.get_plan("s") is None and staging.load_work_items("s", "folder", 0, 10) == []
