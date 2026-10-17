"""
Custom exception classes for MongoDB operations.
"""


class DocumentNotFoundError(Exception):
    """
    Raised when a requested document ID doesn't exist in the collection.
    
    This exception provides context to help the LLM understand what went wrong
    and decide on alternative actions.
    """
    
    def __init__(self, document_id: str, collection: str, database: str):
        self.document_id = document_id
        self.collection = collection
        self.database = database
        super().__init__(
            f"Document '{document_id}' not found in "
            f"collection '{collection}' of database '{database}'"
        )


class DatabaseNotFoundError(Exception):
    """
    Raised when a requested database doesn't exist.
    
    This typically indicates the repository hasn't been indexed yet.
    """
    
    def __init__(self, database_name: str):
        self.database_name = database_name
        super().__init__(
            f"Database '{database_name}' not found. "
            f"The repository may not have been indexed yet."
        )


class CollectionNotFoundError(Exception):
    """
    Raised when a required collection doesn't exist in the database.
    
    This indicates incomplete or corrupted index data.
    """
    
    def __init__(self, collection_name: str, database_name: str):
        self.collection_name = collection_name
        self.database_name = database_name
        super().__init__(
            f"Collection '{collection_name}' not found in database '{database_name}'. "
            f"The semantic index may be incomplete."
        )


class SearchIndexMissingError(Exception):
    """
    Raised when text search indexes are not available.
    
    This should trigger index creation at startup.
    """
    
    def __init__(self, collection_name: str, database_name: str):
        self.collection_name = collection_name
        self.database_name = database_name
        super().__init__(
            f"Text search indexes missing for collection '{collection_name}' "
            f"in database '{database_name}'. Run ensure_search_indexes()."
        )


class InvalidRepositoryNameError(Exception):
    """
    Raised when repository name extraction or validation fails.
    
    This indicates the user provided an invalid or ambiguous repository name.
    """
    
    def __init__(self, message: str):
        super().__init__(message)


class DocumentValidationError(Exception):
    """
    Raised when a retrieved document doesn't match expected schema.
    
    This indicates data corruption or version mismatch in the index.
    """
    
    def __init__(self, document_id: str, reason: str):
        self.document_id = document_id
        self.reason = reason
        super().__init__(
            f"Document '{document_id}' failed validation: {reason}"
        )