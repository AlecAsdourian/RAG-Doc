"""Full-text search retrieval using PostgreSQL FTS.

All Postgres access here goes through `workers.db.require_tenant`. Without
tenant scope, RLS on `chunks` and `ingestion_runs` returns zero rows and
callers would see empty results with no indication anything is wrong.
Every public method takes `organization_id` so a missing tenant is a
programming error, not a silent empty-list bug.
"""

import logging
from typing import Dict, List, Optional
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant

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

    def _get_latest_run_id(
        self, organization_id: UUID, repository_id: UUID
    ) -> Optional[UUID]:
        """Return the latest completed ingestion_runs.id under this tenant.

        Args:
            organization_id: Tenant scope (required).
            repository_id: Repository UUID.

        Returns:
            UUID of the latest completed run, or None if none exist under
            this tenant.
        """
        self.connect()

        query = """
            SELECT id FROM ingestion_runs
            WHERE repository_id = %s AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
        """

        with require_tenant(self.conn, organization_id) as cur:
            cur.execute(query, (str(repository_id),))
            result = cur.fetchone()

            if result:
                return UUID(result[0]) if isinstance(result[0], str) else result[0]

            logger.warning(
                f"No completed ingestion runs found for repository {repository_id} under org {organization_id}"
            )
            return None

    def search(
        self,
        query: str,
        organization_id: UUID,
        repository_id: UUID,
        limit: int = 50,
        run_id: Optional[UUID] = None,
    ) -> List[Dict]:
        """Full-text search on `chunks`, scoped to the caller's tenant.

        Args:
            query: Search query string.
            organization_id: Tenant scope (required).
            repository_id: UUID of the repository to search.
            limit: Maximum number of results (default: 50).
            run_id: Optional specific ingestion_runs.id to search. If
                unset, the latest completed run for the repository is
                used.

        Returns:
            List of dicts with chunk metadata and `fts_score`.
        """
        self.connect()

        if run_id is None:
            run_id = self._get_latest_run_id(organization_id, repository_id)
            if run_id is None:
                logger.warning(
                    f"No completed runs for repository {repository_id} under org {organization_id}; returning empty"
                )
                return []

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
            with require_tenant(
                self.conn, organization_id, cursor_factory=RealDictCursor
            ) as cur:
                cur.execute(
                    query_sql,
                    (query, query, str(run_id), query, query, limit),
                )
                results = [dict(row) for row in cur.fetchall()]

            logger.info(
                f"FTS search for '{query}' returned {len(results)} results under org {organization_id}"
            )
            return results

        except psycopg2.Error as e:
            logger.error(f"FTS search failed: {e}")
            raise
