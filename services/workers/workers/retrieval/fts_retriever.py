"""Full-text search retrieval using PostgreSQL FTS."""

import logging
from typing import Dict, List, Optional
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class FTSRetriever:
    """Full-text search retriever using PostgreSQL to_tsvector and ts_rank."""

    def __init__(self, connection_string: str):
        """
        Initialize FTS retriever.

        Args:
            connection_string: Postgres connection string (postgresql://...)
        """
        self.connection_string = connection_string
        self.conn = None

    def connect(self):
        """Establish database connection."""
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(self.connection_string)
            logger.info("FTSRetriever connected to Postgres")

    def close(self):
        """Close database connection."""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("FTSRetriever closed Postgres connection")

    def _get_latest_run_id(self, repository_id: UUID) -> Optional[UUID]:
        """
        Get the latest successful ingestion run ID for a repository.

        Args:
            repository_id: UUID of the repository

        Returns:
            UUID of latest completed run, or None if no completed runs exist
        """
        self.connect()

        query = """
            SELECT id FROM ingestion_runs
            WHERE repository_id = %s AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
        """

        with self.conn.cursor() as cur:
            cur.execute(query, (str(repository_id),))
            result = cur.fetchone()

            if result:
                return UUID(result[0]) if isinstance(result[0], str) else result[0]

            logger.warning(
                f"No completed ingestion runs found for repository {repository_id}"
            )
            return None

    def search(
        self,
        query: str,
        repository_id: UUID,
        limit: int = 50,
        run_id: Optional[UUID] = None,
    ) -> List[Dict]:
        """
        Search chunks using full-text search.

        Args:
            query: Search query string
            repository_id: UUID of the repository to search
            limit: Maximum number of results to return (default: 50)
            run_id: Optional specific ingestion run ID to search.
                   If None, searches latest completed run.

        Returns:
            List of dicts with chunk metadata and fts_score.
            Each dict contains:
            - chunk_id (UUID): Chunk identifier
            - file_path (str): File path relative to repo root
            - start_line (int): Starting line number
            - end_line (int): Ending line number
            - breadcrumb (str): Qualified name breadcrumb
            - chunk_type (str): Type of chunk (function, class, etc.)
            - content_preview (str): First 200 chars of content
            - fts_score (float): Full-text search relevance score

        Example:
            >>> retriever = FTSRetriever("postgresql://...")
            >>> results = retriever.search(
            ...     "authentication error",
            ...     repository_id=UUID("..."),
            ...     limit=10
            ... )
            >>> for result in results:
            ...     print(f"{result['breadcrumb']}: {result['fts_score']}")
        """
        self.connect()

        # If run_id not provided, get latest successful run
        if run_id is None:
            run_id = self._get_latest_run_id(repository_id)
            if run_id is None:
                logger.warning(
                    f"No completed runs for repository {repository_id}, returning empty results"
                )
                return []

        # Full-text search query with scoring
        # Searches both content and breadcrumb fields
        # Uses plainto_tsquery for simple query conversion (handles spaces/punctuation)
        # Uses ts_rank_cd for scoring (considers proximity)
        query_sql = """
            SELECT
                id::text as chunk_id,
                file_path,
                start_line,
                end_line,
                breadcrumb,
                chunk_type,
                LEFT(content, 200) as content_preview,
                GREATEST(
                    ts_rank_cd(to_tsvector('english', content), plainto_tsquery('english', %s)),
                    ts_rank_cd(to_tsvector('english', COALESCE(breadcrumb, '')), plainto_tsquery('english', %s))
                ) as fts_score
            FROM chunks
            WHERE
                ingestion_run_id = %s
                AND (
                    to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                    OR to_tsvector('english', COALESCE(breadcrumb, '')) @@ plainto_tsquery('english', %s)
                )
            ORDER BY fts_score DESC
            LIMIT %s
        """

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    query_sql,
                    (
                        query,  # For content scoring
                        query,  # For breadcrumb scoring
                        str(run_id),  # Filter by run_id
                        query,  # For content matching
                        query,  # For breadcrumb matching
                        limit,  # Result limit
                    ),
                )
                results = cur.fetchall()

                # Convert RealDictRow to plain dict
                results_list = [dict(row) for row in results]

                logger.info(
                    f"FTS search for '{query}' returned {len(results_list)} results"
                )

                return results_list

        except psycopg2.Error as e:
            logger.error(f"FTS search failed: {e}")
            raise
