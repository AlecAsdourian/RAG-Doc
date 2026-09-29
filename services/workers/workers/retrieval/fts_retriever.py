"""Full-text search retrieval using PostgreSQL FTS.

All Postgres access here goes through `workers.db.require_tenant`. Without
tenant scope, RLS on `chunks` returns zero rows and callers would see empty
results with no indication anything is wrong. Every public method takes
`organization_id` so a missing tenant is a programming error, not a silent
empty-list bug.

The statement filters by repository, not by ingestion run. Until 22-03 it
filtered to the repository's latest completed run, which is the wrong notion
of currency for incremental indexing: unchanged files legitimately keep
chunks from earlier runs, so a latest-run filter would hide most of the
repository (ISS-027). Currency becomes per file in 22.1-02; until then a
repository has one run's chunks and the two legs agree on what they search.
"""

import logging
from typing import Dict, List
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant

logger = logging.getLogger(__name__)

# The breadcrumb expression, verbatim the expression migration 000017 indexes
# (`chunks_breadcrumb_fts_idx`, GIN). An index on an expression serves only a
# query that uses the same expression, so the SQL below is composed from this
# constant and tests/isolation/test_query_engine_isolation.py proves the
# planner uses the index for it (22-03).
BREADCRUMB_TSVECTOR = "to_tsvector('english', COALESCE(breadcrumb, ''))"
CONTENT_TSVECTOR = "to_tsvector('english', content)"

FTS_SEARCH_SQL = f"""
    SELECT
        id::text as chunk_id,
        file_path,
        start_line,
        end_line,
        breadcrumb,
        chunk_type,
        LEFT(content, 200) as content_preview,
        GREATEST(
            ts_rank_cd({CONTENT_TSVECTOR}, plainto_tsquery('english', %(q)s)),
            ts_rank_cd({BREADCRUMB_TSVECTOR}, plainto_tsquery('english', %(q)s))
        ) as fts_score
    FROM chunks
    WHERE
        repository_id = %(repo)s
        AND (
            {CONTENT_TSVECTOR} @@ plainto_tsquery('english', %(q)s)
            OR {BREADCRUMB_TSVECTOR} @@ plainto_tsquery('english', %(q)s)
        )
    ORDER BY fts_score DESC
    LIMIT %(limit)s
"""


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

    def search(
        self,
        query: str,
        organization_id: UUID,
        repository_id: UUID,
        limit: int = 50,
    ) -> List[Dict]:
        """Full-text search on `chunks`, scoped to the caller's tenant.

        Args:
            query: Search query string.
            organization_id: Tenant scope (required).
            repository_id: UUID of the repository to search.
            limit: Maximum number of results (default: 50).

        Returns:
            List of dicts with chunk metadata and `fts_score`.
        """
        self.connect()

        try:
            with require_tenant(
                self.conn, organization_id, cursor_factory=RealDictCursor
            ) as cur:
                cur.execute(
                    FTS_SEARCH_SQL,
                    {"q": query, "repo": str(repository_id), "limit": limit},
                )
                results = [dict(row) for row in cur.fetchall()]

            logger.info(
                f"FTS search for '{query}' returned {len(results)} results under org {organization_id}"
            )
            return results

        except psycopg2.Error as e:
            logger.error(f"FTS search failed: {e}")
            raise
