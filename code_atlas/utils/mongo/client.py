"""
MongoDB connection management.
"""
import logging
from typing import Optional
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from config import MONGO_URL

logger = logging.getLogger(__name__)

# Connection pool settings
MAX_POOL_SIZE = 10
SERVER_SELECTION_TIMEOUT_MS = 10000
CONNECT_TIMEOUT_MS = 10000
SOCKET_TIMEOUT_MS = 30000

# Global client variable (singleton pattern)
_mongo_client: Optional[MongoClient] = None


def get_mongo_client() -> MongoClient:
    """
    Get or create the MongoDB client with connection pooling.

    Returns:
        MongoClient: Configured MongoDB client instance

    Raises:
        ConnectionFailure: If unable to connect to MongoDB
    """
    global _mongo_client

    if _mongo_client is None:
        logger.info("Initializing MongoDB client...")

        try:
            client = MongoClient(
                MONGO_URL,
                serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
                connectTimeoutMS=CONNECT_TIMEOUT_MS,
                socketTimeoutMS=SOCKET_TIMEOUT_MS,
                maxPoolSize=MAX_POOL_SIZE,
            )

            # Test connection
            client.admin.command('ping')
            _mongo_client = client
            logger.info("MongoDB client initialized successfully")

        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"Failed to connect to MongoDB: {str(e)}")
            raise ConnectionFailure(f"Unable to connect to MongoDB: {str(e)}")

    return _mongo_client


def close_mongo_client() -> None:
    """
    Close the MongoDB client connection.
    Should be called during application shutdown.
    """
    global _mongo_client

    if _mongo_client is not None:
        logger.info("Closing MongoDB client...")
        _mongo_client.close()
        _mongo_client = None
        logger.info("MongoDB client closed")


def test_connection() -> dict:
    """
    Test the MongoDB connection and return server information.

    Returns:
        dict: Server information including version and databases

    Raises:
        ConnectionFailure: If connection test fails
    """
    try:
        client = get_mongo_client()

        server_info = client.server_info()
        databases = client.list_database_names()

        return {
            'status': 'connected',
            'version': server_info.get('version', 'Unknown'),
            'databases': databases,
            'database_count': len(databases)
        }

    except Exception as e:
        logger.error(f"Connection test failed: {str(e)}")
        raise ConnectionFailure(f"Connection test failed: {str(e)}")
