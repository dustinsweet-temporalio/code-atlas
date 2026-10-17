"""
Cache for the report's model calls, keyed on everything that decides their result.

A key hashes the step's kind and its inputs: the evidence it reads (file blobs,
card content, findings), the pack, rubric and model versions, and the prompt text.
An unchanged repository therefore gives an identical report with no model calls,
and a changed input re-runs only the steps that read it. `fresh` skips reads
(every step runs again) but still writes, so the next run reuses the new results.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

CACHE_VERSION = 1  # bump to invalidate every cached step


def prompt_hash(*texts: str) -> str:
    return hashlib.sha256("\n".join(texts).encode()).hexdigest()[:16]


class ReportCache:
    def __init__(self, store, repo_id: str, fresh: bool = False):
        self.store = store
        self.repo_id = repo_id
        self.fresh = fresh
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(kind: str, *parts: Any) -> str:
        blob = json.dumps([CACHE_VERSION, kind, *parts], sort_keys=True, default=str)
        return f"{kind}::{hashlib.sha256(blob.encode()).hexdigest()[:32]}"

    def get(self, kind: str, *parts: Any) -> Any | None:
        doc = None if self.fresh else self.store.get_cached(self.repo_id, self.key(kind, *parts))
        if doc is None:
            self.misses += 1
            return None
        self.hits += 1
        return doc["value"]

    def put(self, kind: str, *parts: Any, value: Any) -> None:
        self.store.put_cached(self.repo_id, self.key(kind, *parts), {
            "kind": kind, "value": value, "created": datetime.now(timezone.utc).isoformat(),
        })
