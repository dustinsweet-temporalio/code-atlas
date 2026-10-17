"""
Search index creation and verification (startup utility).

This is NOT a tool - it runs automatically at agent startup to ensure
text search indexes exist on the semantic summary collections.
"""
import logging
from pymongo.errors import OperationFailure
from utils.mongo import get_mongo_client
from config import FILE_SUMMARIES_COLLECTION, FOLDER_SUMMARIES_COLLECTION

logger = logging.getLogger(__name__)


def ensure_search_indexes(database_name: str) -> dict:
    """
    Ensure text search indexes exist on summary collections.
    
    Creates text indexes on searchable fields if they don't exist.
    This operation is idempotent - safe to run multiple times.
    
    Args:
        database_name: Database name (e.g., "semv2_infra")
        
    Returns:
        dict: Status information about index creation
            - status: "created", "exists", or "error"
            - collections: List of collections processed
            - details: Additional information
    """
    try:
        client = get_mongo_client()
        db = client[database_name]
        
        results = {
            "status": "exists",
            "collections": [],
            "details": []
        }
        
        # Process file_summaries collection
        file_collection = db[FILE_SUMMARIES_COLLECTION]
        file_status = _ensure_text_index(file_collection)
        results["collections"].append(FILE_SUMMARIES_COLLECTION)
        results["details"].append(f"{FILE_SUMMARIES_COLLECTION}: {file_status}")
        
        if file_status == "created":
            results["status"] = "created"
        
        # Process folder_summaries collection
        folder_collection = db[FOLDER_SUMMARIES_COLLECTION]
        folder_status = _ensure_text_index(folder_collection)
        results["collections"].append(FOLDER_SUMMARIES_COLLECTION)
        results["details"].append(f"{FOLDER_SUMMARIES_COLLECTION}: {folder_status}")
        
        if folder_status == "created":
            results["status"] = "created"
        
        logger.info(f"Search indexes verified for database '{database_name}': {results['status']}")
        return results
        
    except Exception as e:
        logger.error(f"Error ensuring search indexes: {str(e)}")
        return {
            "status": "error",
            "collections": [],
            "details": [str(e)]
        }


# Must match the indexer (semantic_ingress/storage/mongo_store.py): the indexer
# replaces any text index whose fields differ from these.
TEXT_INDEX_NAME = "text_idx"
TEXT_INDEX_WEIGHTS = {"prose_summary": 2, "lens_prose": 1}


def _ensure_text_index(collection) -> str:
    """
    Ensure a text index exists on a collection.

    Any existing text index is left alone (the indexer upgrades it if its fields
    differ). A new one uses the same fields and weights as the indexer.

    Args:
        collection: PyMongo collection object

    Returns:
        str: "created" if index was created, "exists" if already present
    """
    try:
        # Check if text index already exists
        existing_indexes = list(collection.list_indexes())
        
        for index in existing_indexes:
            # Text indexes have a special structure
            key = index.get("key", {})
            if "_fts" in key or any(v == "text" for v in key.values()):
                logger.debug(f"Text index already exists on collection '{collection.name}'")
                return "exists"
        
        # Create text index - simplified for MongoDB compatibility
        logger.info(f"Creating text index on collection '{collection.name}'...")
        
        collection.create_index(
            [(field, "text") for field in TEXT_INDEX_WEIGHTS],
            weights=TEXT_INDEX_WEIGHTS,
            name=TEXT_INDEX_NAME,
        )
        
        logger.info(f"Text index created successfully on '{collection.name}'")
        return "created"
        
    except OperationFailure as e:
        error_msg = str(e).lower()
        # Index might already exist with a different name or structure
        if "already exists" in error_msg or "index" in error_msg:
            logger.debug(f"Text index already exists on '{collection.name}' (detected via error)")
            return "exists"
        else:
            logger.error(f"Failed to create text index on '{collection.name}': {str(e)}")
            raise
    except Exception as e:
        logger.error(f"Unexpected error creating index on '{collection.name}': {str(e)}")
        raise