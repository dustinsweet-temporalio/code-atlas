# semantic_ingress/core/incremental_loader.py
"""
Incremental loader for retrieving previous repository state from the index.
"""
import structlog

from semantic_ingress.models import FileSnapshot, RepositorySnapshot

logger = structlog.get_logger()


class IncrementalLoader:
    """Loads previous repository state from the index for incremental sync."""

    def __init__(self, agent_client) -> None:
        """
        Initialize incremental loader.

        Args:
            agent_client: Mongo store for index operations
        """
        self.agent = agent_client
        self.log = logger.bind(component="incremental_loader")

    def load_previous_snapshot(self, repo_id: str) -> RepositorySnapshot | None:
        """
        Load previous repository snapshot from the index.

        Args:
            repo_id: Repository identifier

        Returns:
            RepositorySnapshot if found, None if collection doesn't exist or is empty
        """
        self.log.info("loading_previous_snapshot", repo_id=repo_id)

        try:
            # Use the Mongo store's get_tree_snapshot method
            # This method already handles pagination and returns a flat tree structure
            tree_data = self.agent.get_tree_snapshot(repo_id)

            if not tree_data:
                self.log.info("no_previous_snapshot_found", repo_id=repo_id)
                return None

            # Convert flat tree to RepositorySnapshot
            # FIX: Pass both 'path' and 'blob_oid' to FileSnapshot constructor
            files = {
                path: FileSnapshot(path=path, blob_oid=blob_oid)
                for path, blob_oid in tree_data.items()
            }

            # Get commit SHA from first document (all should have same commit_sha)
            # Query one document to get metadata
            sample_docs = self.agent.query_documents(
                repo_id=repo_id,
                collection="file_summaries",
                limit=1,
                projection={
                    "prose_summary": 0,  # Exclude prose_summary
                }
            )

            commit_sha = "unknown"
            if sample_docs:
                commit_sha = sample_docs[0].get("commit_sha", "unknown")

            snapshot = RepositorySnapshot(
                commit_sha=commit_sha,
                files=files,
            )

            self.log.info(
                "previous_snapshot_loaded",
                repo_id=repo_id,
                commit_sha=commit_sha,
                file_count=len(files),
            )

            return snapshot

        except Exception as e:
            self.log.warning(
                "failed_to_load_previous_snapshot",
                repo_id=repo_id,
                error=str(e),
            )
            return None

