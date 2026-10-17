"""
In-memory stand-ins for MongoStore, StagingStore and the LLM client, for tests that
run the real activities (and workflow) without MongoDB or the Anthropic API.
"""
import copy
import re
import threading
from collections import defaultdict
from typing import Any

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.models import (
    FileSemanticSummary,
    FolderSemanticSummary,
    RepositorySemanticSummary,
    StoredDocument,
)
from semantic_ingress.storage.mongo_store import COLLECTION_MAP


def _get(doc: dict, dotted: str) -> Any:
    value: Any = doc
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(doc: dict, query: dict) -> bool:
    for key, cond in (query or {}).items():
        value = _get(doc, key)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$in":
                    if not any((p.match(value) if isinstance(p, re.Pattern) else p == value)
                               for p in arg if value is not None):
                        return False
                elif op == "$regex":
                    if value is None or not re.search(arg, value):
                        return False
                elif op == "$gte":
                    if value is None or value < arg:
                        return False
                elif op == "$lt":
                    if value is None or value >= arg:
                        return False
                else:
                    raise NotImplementedError(op)
        elif value != cond:
            return False
    return True


class FakeStore:
    """The MongoStore methods the sync uses, over dicts: {repo_id: {collection: {_id: doc}}}."""

    def __init__(self) -> None:
        self.dbs: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
        self.calls: list[str] = []
        self.fail_queries = 0          # raise on the next N query_documents calls
        self.fail_upserts = 0          # report errors on the next N bulk upserts
        self.lock = threading.Lock()

    # naming
    def _get_database_name(self, repo_id: str) -> str:
        return f"semv2_{repo_id}"

    # reads
    def query_documents(self, repo_id, collection, filter_query=None, projection=None, limit=None, batch_size=100):
        from semantic_ingress.storage.mongo_store import MongoStoreError

        with self.lock:
            if self.fail_queries:
                self.fail_queries -= 1
                raise MongoStoreError("connection refused")
            docs = [copy.deepcopy(d) for d in self.dbs[repo_id][collection].values() if _matches(d, filter_query)]
        return docs[:limit] if limit else docs

    def get_tree_snapshot(self, repo_id):
        return {d["path"]: d["blob_oid"] for d in self.query_documents(repo_id, "file_summaries")
                if d.get("path") and d.get("blob_oid")}

    def get_index_meta(self, repo_id):
        return copy.deepcopy(self.dbs[repo_id]["index_meta"].get(f"{repo_id}::index_meta"))

    def _versions(self, repo_id, field):
        counts: dict = defaultdict(int)
        for d in self.dbs[repo_id]["file_summaries"].values():
            counts[_get(d, field)] += 1
        return [{"version": k, "files": n} for k, n in sorted(counts.items(), key=lambda kv: str(kv[0]))]

    def prompt_versions(self, repo_id):
        return self._versions(repo_id, "prompt_version")

    def lens_pack_versions(self, repo_id):
        return self._versions(repo_id, "lens.pack_version")

    def get_report(self, repo_id):
        return copy.deepcopy(self.dbs[repo_id]["reports"].get(f"{repo_id}::temporal_report"))

    def put_report(self, repo_id, doc):
        self.dbs[repo_id]["reports"][f"{repo_id}::temporal_report"] = copy.deepcopy(doc)

    # writes
    def upsert_documents_bulk(self, documents: list[StoredDocument], batch_size=500, preserve_if_none=frozenset()):
        with self.lock:
            self.calls.append("upsert")
            if self.fail_upserts:
                self.fail_upserts -= 1
                return {"total_processed": 0, "summary": {"inserted": 0, "updated": 0, "no_change": 0,
                                                          "errors": len(documents)}}
            inserted = updated = 0
            for doc in documents:
                data = doc.model_dump(by_alias=True, mode="json")
                coll = self.dbs[doc.repo_id][COLLECTION_MAP[doc.document_type.value]]
                if doc.id in coll:
                    updated += 1
                else:
                    inserted += 1
                coll[doc.id] = data
        return {"total_processed": len(documents),
                "summary": {"inserted": inserted, "updated": updated, "no_change": 0, "errors": 0}}

    def delete_documents_bulk(self, doc_ids, repo_id):
        with self.lock:
            self.calls.append("delete")
            deleted = 0
            for doc_id in doc_ids:
                coll = COLLECTION_MAP.get(doc_id.split("::")[1])
                if self.dbs[repo_id][coll].pop(doc_id, None) is not None:
                    deleted += 1
        return {"total_requested": len(doc_ids), "total_deleted": deleted}

    def update_file_lenses(self, repo_id, updates):
        n = 0
        for path, lens, prose in updates:
            doc = self.dbs[repo_id]["file_summaries"].get(f"{repo_id}::file_summary::{path}")
            if doc is not None:
                doc["lens"], doc["lens_prose"] = lens, prose
                n += 1
        return n

    def put_index_meta(self, meta):
        self.calls.append("index_meta")
        self.dbs[meta.repo_id]["index_meta"][meta.id] = meta.model_dump(by_alias=True)

    def reset_repository(self, repo_id):
        self.calls.append("reset")
        for name in ["file_summaries", "folder_summaries", "repo_summaries", "index_meta"]:
            self.dbs[repo_id].pop(name, None)

    def ensure_text_index(self, database, collection):
        return "exists"

    def text_search(self, database, collection, search_text, limit=5):
        return [{"_id": "x"}]


class FakeStaging:
    """StagingStore over dicts (same method contract; see tests/temporal/test_staging.py)."""

    def __init__(self) -> None:
        self.plans: dict[str, dict] = {}
        self.items: dict[tuple[str, str], list[dict]] = {}
        self.docs: dict[str, dict] = {}
        self.lens: dict[str, dict] = {}
        self.writes = 0
        self.lock = threading.Lock()

    def ensure_indexes(self):
        pass

    def put_plan(self, sync_id, plan):
        self.plans[sync_id] = copy.deepcopy({**plan, "_id": sync_id, "sync_id": sync_id})

    def get_plan(self, sync_id):
        return copy.deepcopy(self.plans.get(sync_id))

    def put_work_items(self, sync_id, kind, items):
        self.items[(sync_id, kind)] = [
            {**copy.deepcopy(i), "sync_id": sync_id, "kind": kind, "seq": n} for n, i in enumerate(items)
        ]

    def load_work_items(self, sync_id, kind, offset, limit):
        return copy.deepcopy(self.items.get((sync_id, kind), [])[offset:offset + limit])

    def work_item_paths(self, sync_id, kind, paths):
        paths = set(paths)
        return {i["path"] for i in self.items.get((sync_id, kind), []) if i["path"] in paths}

    def stage_document(self, sync_id, doc):
        with self.lock:
            self.writes += 1
            self.docs[f"{sync_id}:{doc.id}"] = {
                "sync_id": sync_id,
                "collection": COLLECTION_MAP[doc.document_type.value],
                "doc": doc.model_dump(by_alias=True, mode="json"),
            }

    def get_staged(self, sync_id, doc_ids):
        with self.lock:
            return {d["doc"]["_id"]: copy.deepcopy(d["doc"]) for k in doc_ids
                    if (d := self.docs.get(f"{sync_id}:{k}")) is not None}

    def staged_documents(self, sync_id, collection=None, fields=None):
        with self.lock:
            return [copy.deepcopy(v["doc"]) for k, v in sorted(self.docs.items())
                    if v["sync_id"] == sync_id and (collection is None or v["collection"] == collection)]

    def staged_page(self, sync_id, offset, limit):
        return [StoredDocument.model_validate(d) for d in self.staged_documents(sync_id)[offset:offset + limit]]

    def count_staged(self, sync_id):
        return len(self.staged_documents(sync_id))

    def stage_lens_update(self, sync_id, path, lens, lens_prose):
        with self.lock:
            self.lens[f"{sync_id}:{path}"] = {"sync_id": sync_id, "path": path, "lens": lens, "lens_prose": lens_prose}

    def lens_updates(self, sync_id, paths=None):
        with self.lock:
            rows = sorted((v for v in self.lens.values() if v["sync_id"] == sync_id), key=lambda v: v["path"])
        wanted = set(paths) if paths is not None else None
        return {r["path"]: (r["lens"], r["lens_prose"]) for r in rows if wanted is None or r["path"] in wanted}

    def cleanup(self, sync_id):
        counts = {"documents": self.count_staged(sync_id),
                  "lens_updates": len(self.lens_updates(sync_id))}
        self.docs = {k: v for k, v in self.docs.items() if v["sync_id"] != sync_id}
        self.lens = {k: v for k, v in self.lens.items() if v["sync_id"] != sync_id}
        self.items = {k: v for k, v in self.items.items() if k[0] != sync_id}
        self.plans.pop(sync_id, None)
        return counts


class FakeLLM:
    """Answers every generate() with a minimal valid summary; records the prompts it saw."""

    def __init__(self, fail_paths: set[str] | None = None, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail_paths = fail_paths or set()
        self.error = error
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)
        self.lock = threading.Lock()

    def generate(self, prompt, response_model, max_retries=3, system=None, enforce_schema=False):
        with self.lock:
            self.calls.append(response_model.__name__)
        for path in self.fail_paths:
            if path in prompt:
                raise self.error or RuntimeError(f"boom: {path}")
        usage = LLMUsage(input_tokens=100, output_tokens=10)
        if response_model is FileSemanticSummary:
            return FileSemanticSummary(role="A file."), usage
        if response_model is FolderSemanticSummary:
            path = re.search(r"FOLDER PATH: (\S+)", prompt)
            return FolderSemanticSummary(path=path.group(1) if path else ".", summary="A folder."), usage
        if response_model is RepositorySemanticSummary:
            return RepositorySemanticSummary(purpose="A repository."), usage
        raise NotImplementedError(response_model.__name__)
