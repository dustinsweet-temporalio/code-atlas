# semantic_ingress/storage/staging_store.py
"""
Staging area for a sync run (the Temporal `SyncWorkflow`).

Activities write what they generate here instead of returning it to the workflow,
so workflow history stays small, and the live index is untouched until the
persist phase (a full re-index keeps the old index until generation succeeded).
Everything is keyed by the run's `sync_id`, which survives Continue-As-New.

The database is named ``code_atlas_ingest``. The web app discovers indexes by the
``{DB_PREFIX}_`` prefix, and prefixes must start with "sem", so this database can
never be mistaken for an index.

Collections:
    plans                one document per sync: the plan, the snapshot delta, ids to delete
    work_items           {sync_id, kind: file|lens|folder, seq, path, blob_oid?, depth?}
    staged_docs          {_id: "{sync_id}:{doc_id}", sync_id, collection, doc: <StoredDocument>}
    staged_lens_updates  {sync_id, path, lens, lens_prose} for --refresh-lens

Every document carries `expire_at` (7 days out, refreshed on write); a TTL index
removes abandoned runs. A finished run deletes its own documents.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import structlog
from pymongo import ASCENDING, MongoClient, ReplaceOne
from pymongo.errors import PyMongoError

from semantic_ingress.config import MongoConfig
from semantic_ingress.models import StoredDocument
from semantic_ingress.storage.mongo_store import COLLECTION_MAP, MongoStoreError

logger = structlog.get_logger()

STAGING_DATABASE = "code_atlas_ingest"
STAGING_TTL = timedelta(days=7)

PLANS = "plans"
WORK_ITEMS = "work_items"
STAGED_DOCS = "staged_docs"
LENS_UPDATES = "staged_lens_updates"


def _expire_at() -> datetime:
    return datetime.now(timezone.utc) + STAGING_TTL


class StagingStore:
    """Read and write one sync run's staged plan, work items and documents."""

    def __init__(self, client: MongoClient | None = None, config: MongoConfig | None = None,
                 database: str = STAGING_DATABASE) -> None:
        if client is None:
            config = config or MongoConfig()
            client = MongoClient(config.url, serverSelectionTimeoutMS=config.server_selection_timeout_ms)
        self.db = client[database]
        self.log = logger.bind(component="staging_store")
        self._indexes_ready = False

    def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        try:
            for name in (PLANS, WORK_ITEMS, STAGED_DOCS, LENS_UPDATES):
                self.db[name].create_index("expire_at", expireAfterSeconds=0)
            self.db[WORK_ITEMS].create_index(
                [("sync_id", ASCENDING), ("kind", ASCENDING), ("seq", ASCENDING)], unique=True
            )
            self.db[WORK_ITEMS].create_index([("sync_id", ASCENDING), ("kind", ASCENDING), ("path", ASCENDING)])
            self.db[STAGED_DOCS].create_index(
                [("sync_id", ASCENDING), ("collection", ASCENDING), ("_id", ASCENDING)]
            )
            self.db[LENS_UPDATES].create_index([("sync_id", ASCENDING), ("path", ASCENDING)])
        except PyMongoError as e:
            raise MongoStoreError(f"Staging index setup failed: {e}") from e
        self._indexes_ready = True

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def put_plan(self, sync_id: str, plan: dict[str, Any]) -> None:
        self._replace(PLANS, {**plan, "_id": sync_id, "sync_id": sync_id})

    def get_plan(self, sync_id: str) -> dict[str, Any] | None:
        return self._find_one(PLANS, {"_id": sync_id})

    # ------------------------------------------------------------------
    # Work items
    # ------------------------------------------------------------------

    def put_work_items(self, sync_id: str, kind: str, items: list[dict[str, Any]]) -> None:
        """Write the items of one kind in order (seq 0..n-1). Idempotent: a rewrite replaces them."""
        self.ensure_indexes()
        expire = _expire_at()
        ops = [
            ReplaceOne(
                {"_id": f"{sync_id}:{kind}:{seq}"},
                {**item, "_id": f"{sync_id}:{kind}:{seq}", "sync_id": sync_id, "kind": kind, "seq": seq,
                 "expire_at": expire},
                upsert=True,
            )
            for seq, item in enumerate(items)
        ]
        try:
            for start in range(0, len(ops), 1000):
                self.db[WORK_ITEMS].bulk_write(ops[start:start + 1000], ordered=False)
            self.db[WORK_ITEMS].delete_many({"sync_id": sync_id, "kind": kind, "seq": {"$gte": len(items)}})
        except PyMongoError as e:
            raise MongoStoreError(f"Writing {kind} work items failed: {e}") from e

    def load_work_items(self, sync_id: str, kind: str, offset: int, limit: int) -> list[dict[str, Any]]:
        try:
            cursor = (
                self.db[WORK_ITEMS]
                .find({"sync_id": sync_id, "kind": kind, "seq": {"$gte": offset, "$lt": offset + limit}},
                      {"_id": 0, "expire_at": 0})
                .sort("seq", ASCENDING)
            )
            return list(cursor)
        except PyMongoError as e:
            raise MongoStoreError(f"Reading {kind} work items failed: {e}") from e

    def work_item_paths(self, sync_id: str, kind: str, paths: Iterable[str]) -> set[str]:
        """Which of `paths` are work items of this kind (e.g. folders regenerated in this run)."""
        paths = list(paths)
        if not paths:
            return set()
        try:
            docs = self.db[WORK_ITEMS].find(
                {"sync_id": sync_id, "kind": kind, "path": {"$in": paths}}, {"path": 1}
            )
            return {d["path"] for d in docs}
        except PyMongoError as e:
            raise MongoStoreError(f"Reading {kind} work items failed: {e}") from e

    # ------------------------------------------------------------------
    # Staged documents
    # ------------------------------------------------------------------

    def stage_document(self, sync_id: str, doc: StoredDocument) -> None:
        """Stage a ready-to-upsert document (idempotent: a retry overwrites the same record)."""
        self.ensure_indexes()
        self._replace(STAGED_DOCS, {
            "_id": f"{sync_id}:{doc.id}",
            "sync_id": sync_id,
            "collection": COLLECTION_MAP[doc.document_type.value],
            "doc": doc.model_dump(by_alias=True, mode="json"),
        })

    def get_staged(self, sync_id: str, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Staged documents (as stored dicts) by document id; ids not staged are absent."""
        keys = [f"{sync_id}:{doc_id}" for doc_id in doc_ids]
        if not keys:
            return {}
        try:
            return {d["doc"]["_id"]: d["doc"] for d in self.db[STAGED_DOCS].find({"_id": {"$in": keys}})}
        except PyMongoError as e:
            raise MongoStoreError(f"Reading staged documents failed: {e}") from e

    def staged_documents(self, sync_id: str, collection: str | None = None,
                         fields: list[str] | None = None) -> list[dict[str, Any]]:
        """Staged documents (dicts) of one collection or all, optionally only some fields."""
        query: dict[str, Any] = {"sync_id": sync_id}
        if collection:
            query["collection"] = collection
        projection = {f"doc.{f}": 1 for f in fields} if fields else {"doc": 1}
        try:
            return [d["doc"] for d in self.db[STAGED_DOCS].find(query, projection).sort("_id", ASCENDING)]
        except PyMongoError as e:
            raise MongoStoreError(f"Reading staged documents failed: {e}") from e

    def staged_page(self, sync_id: str, offset: int, limit: int) -> list[StoredDocument]:
        """A page of staged documents in a stable order (by _id), for persisting in batches."""
        try:
            docs = (
                self.db[STAGED_DOCS].find({"sync_id": sync_id}, {"doc": 1})
                .sort("_id", ASCENDING).skip(offset).limit(limit)
            )
            return [StoredDocument.model_validate(d["doc"]) for d in docs]
        except PyMongoError as e:
            raise MongoStoreError(f"Reading staged documents failed: {e}") from e

    def count_staged(self, sync_id: str) -> int:
        try:
            return self.db[STAGED_DOCS].count_documents({"sync_id": sync_id})
        except PyMongoError as e:
            raise MongoStoreError(f"Counting staged documents failed: {e}") from e

    # ------------------------------------------------------------------
    # Lens-only updates (--refresh-lens)
    # ------------------------------------------------------------------

    def stage_lens_update(self, sync_id: str, path: str, lens: dict[str, Any], lens_prose: str) -> None:
        self.ensure_indexes()
        self._replace(LENS_UPDATES, {
            "_id": f"{sync_id}:{path}", "sync_id": sync_id, "path": path, "lens": lens, "lens_prose": lens_prose,
        })

    def lens_updates(self, sync_id: str, paths: Iterable[str] | None = None) -> dict[str, tuple[dict, str]]:
        """path -> (lens, lens_prose) staged for this run (only `paths`, when given)."""
        query: dict[str, Any] = {"sync_id": sync_id}
        if paths is not None:
            query["path"] = {"$in": list(paths)}
        try:
            return {
                d["path"]: (d["lens"], d["lens_prose"])
                for d in self.db[LENS_UPDATES].find(query).sort("path", ASCENDING)
            }
        except PyMongoError as e:
            raise MongoStoreError(f"Reading staged lens updates failed: {e}") from e

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, sync_id: str) -> dict[str, int]:
        """Delete everything staged for this run. Returns the staged document and lens update counts."""
        try:
            counts = {
                "documents": self.db[STAGED_DOCS].count_documents({"sync_id": sync_id}),
                "lens_updates": self.db[LENS_UPDATES].count_documents({"sync_id": sync_id}),
            }
            for name in (STAGED_DOCS, LENS_UPDATES, WORK_ITEMS):
                self.db[name].delete_many({"sync_id": sync_id})
            self.db[PLANS].delete_one({"_id": sync_id})
        except PyMongoError as e:
            raise MongoStoreError(f"Cleaning up staging for {sync_id} failed: {e}") from e
        self.log.info("staging_cleaned_up", sync_id=sync_id, **counts)
        return counts

    # ------------------------------------------------------------------

    def _replace(self, collection: str, doc: dict[str, Any]) -> None:
        try:
            self.db[collection].replace_one({"_id": doc["_id"]}, {**doc, "expire_at": _expire_at()}, upsert=True)
        except PyMongoError as e:
            raise MongoStoreError(f"Writing {collection} failed: {e}") from e

    def _find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self.db[collection].find_one(query)
        except PyMongoError as e:
            raise MongoStoreError(f"Reading {collection} failed: {e}") from e
