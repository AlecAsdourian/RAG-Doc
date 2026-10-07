"""Vector retrieval in Postgres with pgvector (22-03).

The vector leg reads `chunks.embedding` under the same row-level security as
the text: every query runs inside `workers.db.require_tenant`, so a tenant's
partition is the only one the statement can touch, and pruning to it comes
from the policy alone (22-02's "Subplans Removed: 63"). Qdrant, which held
the vectors with no tenant on them, is gone (22-CONTEXT P15, DECISIONS D2).

Three things about the statement are load-bearing, and each has a test in
tests/isolation/test_query_engine_isolation.py:

- **The query vector is a bound parameter**, `%(q)s::vector`, never a
  subquery, so the HNSW index is eligible for the ORDER BY (P5).
- **`hnsw.iterative_scan = relaxed_order`, per transaction.** Every product
  query filters by repository inside the tenant's partition, and without
  iterative scan that filter returned short results in 16 of 20 and 20 of 20
  measured queries (22-RESEARCH Q5, P5). Relaxed order may return rows
  slightly out of distance order, so the rows are re-sorted here by the exact
  distance the SELECT list computes.
- **`hnsw.ef_search = 2 x the search limit`, per transaction** (22.1-05, the
  user's decision of 2026-10-06). pgvector's default, 40, is below the leg's
  LIMIT of 50, so an HNSW-served query could come back short; see
  `HNSW_EF_SEARCH`.
- **`embedding_model` is filtered to the generator's model.** Two models at
  the same dimension produce vectors in different spaces, and mixing them
  fails silently (P4). The predicate reads `EmbeddingGenerator.model`, never a
  copy of it.
"""

import logging
from typing import Dict, List, Sequence
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.embeddings.embedding_generator import EmbeddingGenerator

logger = logging.getLogger(__name__)

# No organization_id here, deliberately: the partition is pruned from the
# row-level-security policy, which is scalar equality on the partition key.
VECTOR_SEARCH_SQL = """
    SELECT id::text AS chunk_id, file_path, breadcrumb, chunk_type,
           embedding <=> %(q)s::vector AS distance
    FROM chunks
    WHERE repository_id = %(repo)s AND embedding_model = %(model)s
    ORDER BY embedding <=> %(q)s::vector LIMIT %(limit)s
"""

ITERATIVE_SCAN_SQL = "SET LOCAL hnsw.iterative_scan = relaxed_order"

#: How many rows the vector leg asks for. `QueryEngine` passes this, and it
#: is the one authority for the number.
VECTOR_LEG_LIMIT = 50

#: `hnsw.ef_search` for the vector leg: TWO TIMES THE SEARCH LIMIT. A
#: correctness setting, not tuning (the user's decision of 2026-10-06, 22.1-05,
#: `22.1-05-recall.md`). pgvector's default is 40, BELOW the leg's LIMIT of
#: 50, so an HNSW-served query could not return the results it asked for:
#: D2's recall test measured a short result and mean recall@10 of 0.93 at the
#: default, against the locked rule's 0.95. Set per transaction beside
#: `ITERATIVE_SCAN_SQL`. A caller asking for more than `VECTOR_LEG_LIMIT` rows
#: must raise this with it.
HNSW_EF_SEARCH = 2 * VECTOR_LEG_LIMIT
EF_SEARCH_SQL = f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text input form, `[x,y,z]`, as PostgresWriter sends vectors.

    `repr(float(x))` is the shortest decimal that reads back as the same
    double, so nothing is lost before the server rounds to the float32 it
    stores (22-02, measured on 1,536 values).
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class VectorRetriever:
    """Retrieves code chunks by cosine similarity, from Postgres, under the tenant."""

    def __init__(self, postgres_conn: str, embedding_generator: EmbeddingGenerator):
        """
        Args:
            postgres_conn: Postgres connection string (postgresql://...). The
                connection is opened lazily and kept, like FTSRetriever's.
            embedding_generator: The generator that embeds queries. Shared with
                the QueryEngine that owns this retriever; its `.model` is the
                model filter.
        """
        self.connection_string = postgres_conn
        self.embedding_generator = embedding_generator
        self.conn = None

    def connect(self):
        """Establish database connection."""
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(self.connection_string)
            logger.info("VectorRetriever connected to Postgres")

    def close(self):
        """Close database connection."""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("VectorRetriever closed Postgres connection")

    def search(
        self,
        query: str,
        organization_id: UUID,
        repository_id: UUID,
        limit: int = 50,
    ) -> List[Dict]:
        """Nearest chunks to `query` in the repository, under the caller's tenant.

        Args:
            query: Search query string.
            organization_id: Tenant scope (required).
            repository_id: Repository UUID to search.
            limit: Maximum number of results (default: 50).

        Returns:
            List of dicts with chunk_id, file_path, breadcrumb, chunk_type and
            `vector_score` (cosine similarity, 1 - distance), in exact
            distance order.
        """
        logger.info(
            f"Vector search: query='{query[:50]}...', organization_id={organization_id}, "
            f"repository_id={repository_id}, limit={limit}"
        )

        # This exact call path is what the quality harness's --query-vectors
        # pins to a cached vector; keep it (22-03 Task 1).
        query_embedding = self.embedding_generator.client.generate_embeddings_batch(
            [query]
        )[0]
        logger.info(f"Generated query embedding: {len(query_embedding)} dimensions")

        self.connect()
        params = {
            "q": vector_literal(query_embedding),
            "repo": str(repository_id),
            "model": self.embedding_generator.model,
            "limit": limit,
        }
        try:
            with require_tenant(
                self.conn, organization_id, cursor_factory=RealDictCursor
            ) as cur:
                cur.execute(ITERATIVE_SCAN_SQL)
                cur.execute(EF_SEARCH_SQL)
                cur.execute(VECTOR_SEARCH_SQL, params)
                rows = [dict(row) for row in cur.fetchall()]
        except psycopg2.Error as e:
            logger.error(f"Vector search failed: {e}")
            raise

        # relaxed_order: the final order is the exact distance (P5).
        rows.sort(key=lambda row: row["distance"])

        results = [
            {
                "chunk_id": row["chunk_id"],
                "file_path": row["file_path"],
                "breadcrumb": row["breadcrumb"] or "",
                "chunk_type": row["chunk_type"] or "",
                "vector_score": 1.0 - float(row["distance"]),
            }
            for row in rows
        ]
        logger.info(
            f"Vector search returned {len(results)} results for repository {repository_id} "
            f"under org {organization_id}"
        )
        return results
