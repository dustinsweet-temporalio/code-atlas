"""
MongoDB persistence for the semantic index.
"""

from semantic_ingress.storage.mongo_store import MongoStore, MongoStoreError

__all__ = [
    "MongoStore",
    "MongoStoreError",
]
