"""QueryEngine orchestrator for complete RAG query pipeline."""

import concurrent.futures
import logging
import os
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

from .fts_retriever import FTSRetriever
from .metadata_booster import MetadataBooster
from .query_parser import QueryParser
from .rrf_fusion import RRFFusion
from .vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)


class QueryEngine:
    """
    Complete RAG query engine orchestrating hybrid search pipeline.

    Pipeline:
    1. Parse query → extract quoted terms, identifiers
    2. Run FTS and vector searches in parallel
    3. Fuse results with RRF (reciprocal rank fusion)
    4. Apply metadata boosts (chunk-type, path, exact-match)
    5. Sort by final score, return top-k chunks with provenance
    """

    def __init__(
        self,
        postgres_conn: str,
        qdrant_url: str,
        openai_api_key: str,
        boost_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize QueryEngine with all retrieval components.

        Args:
            postgres_conn: PostgreSQL connection string
            qdrant_url: Qdrant server URL
            openai_api_key: OpenAI API key for embeddings
            boost_config: Optional boost configuration for MetadataBooster
        """
        self.postgres_conn = postgres_conn
        self.qdrant_url = qdrant_url
        self.openai_api_key = openai_api_key

        # Initialize all components
        self.fts_retriever = FTSRetriever(connection_string=postgres_conn)
        self.vector_retriever = VectorRetriever(
            qdrant_url=qdrant_url, openai_api_key=openai_api_key
        )
        self.query_parser = QueryParser()
        self.rrf_fusion = RRFFusion()

        # Initialize metadata booster with custom config or defaults
        if boost_config is None:
            # Load from environment variables if present
            boost_config = self._load_boost_config_from_env()

        self.metadata_booster = MetadataBooster(config=boost_config)

        logger.info("QueryEngine initialized with all components")

    def _load_boost_config_from_env(self) -> Dict[str, Any]:
        """
        Load boost configuration from environment variables.

        Returns:
            Dict with boost configuration, or empty dict for defaults
        """
        config = {}

        # Load chunk type boosts if present
        if os.getenv("BOOST_DOCS"):
            config.setdefault("chunk_type_boosts", {})["docs"] = float(
                os.getenv("BOOST_DOCS")
            )
        if os.getenv("BOOST_ENTRYPOINT"):
            config.setdefault("chunk_type_boosts", {})["file_summary"] = float(
                os.getenv("BOOST_ENTRYPOINT")
            )

        # Load penalty values
        if os.getenv("PENALTY_VENDOR"):
            config["noise_penalty"] = float(os.getenv("PENALTY_VENDOR"))

        # Load boost multipliers
        if os.getenv("BOOST_QUOTED_MATCH"):
            config["quoted_match_boost"] = float(os.getenv("BOOST_QUOTED_MATCH"))
        if os.getenv("BOOST_IDENTIFIER_MATCH"):
            config["identifier_match_boost"] = float(os.getenv("BOOST_IDENTIFIER_MATCH"))

        return config

    def query(
        self,
        query_text: str,
        repository_id: UUID,
        top_k: int = 5,
        run_id: Optional[UUID] = None,
    ) -> Dict[str, Any]:
        """
        Execute complete query pipeline and return ranked results.

        Args:
            query_text: Search query string
            repository_id: Repository UUID to search
            top_k: Number of top results to return (default: 5)
            run_id: Optional specific ingestion run to search

        Returns:
            Dict with:
                - query: Original query string
                - repository_id: Repository UUID
                - run_id: Run UUID (or None)
                - results: List of top-k chunks with metadata and scores
                - metadata: Pipeline stats (counts, duration)

        Example:
            >>> engine = QueryEngine(postgres_conn, qdrant_url, openai_key)
            >>> result = engine.query(
            ...     "authentication error",
            ...     repository_id=UUID("..."),
            ...     top_k=5
            ... )
            >>> for chunk in result["results"]:
            ...     print(f"{chunk['file_path']}:{chunk['start_line']} - {chunk['score']}")
        """
        start_time = time.time()

        logger.info(
            f"Query pipeline starting: query='{query_text}', "
            f"repository_id={repository_id}, top_k={top_k}"
        )

        # Step 1: Parse query
        parsed_query = self.query_parser.parse(query_text)
        logger.info(
            f"Query parsed: {len(parsed_query['quoted_terms'])} quoted terms, "
            f"{len(parsed_query['identifiers'])} identifiers"
        )

        # Step 2: Run FTS and vector searches in parallel
        fts_results = []
        vector_results = []
        fts_error = None
        vector_error = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both searches
            fts_future = executor.submit(
                self._run_fts_search, query_text, repository_id, run_id
            )
            vector_future = executor.submit(
                self._run_vector_search, query_text, repository_id, run_id
            )

            # Get FTS results with error handling
            try:
                fts_results = fts_future.result()
            except Exception as e:
                fts_error = e
                logger.error(f"FTS search failed: {e}")

            # Get vector results with error handling
            try:
                vector_results = vector_future.result()
            except Exception as e:
                vector_error = e
                logger.error(f"Vector search failed: {e}")

        logger.info(
            f"Parallel search complete: FTS={len(fts_results)} results, "
            f"Vector={len(vector_results)} results"
        )

        # Step 3: Fuse results with RRF
        fused_results = self.rrf_fusion.fuse(
            {"fts": fts_results, "vector": vector_results}
        )
        logger.info(f"RRF fusion produced {len(fused_results)} unique results")

        # Step 4: Apply metadata boosts
        boosted_results = self.metadata_booster.boost(fused_results, parsed_query)
        logger.info(f"Metadata boosts applied to {len(boosted_results)} results")

        # Step 5: Sort by boosted_score and take top_k
        boosted_results.sort(key=lambda x: x.get("boosted_score", 0.0), reverse=True)
        top_results = boosted_results[:top_k]

        # Step 6: Fetch full metadata from Postgres for top results
        enriched_results = self._enrich_results_with_metadata(top_results, repository_id)

        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)

        # Build response
        response = {
            "query": query_text,
            "repository_id": str(repository_id),
            "run_id": str(run_id) if run_id else None,
            "results": enriched_results,
            "metadata": {
                "fts_results": len(fts_results),
                "vector_results": len(vector_results),
                "fused_results": len(fused_results),
                "duration_ms": duration_ms,
                "fts_error": str(fts_error) if fts_error else None,
                "vector_error": str(vector_error) if vector_error else None,
            },
        }

        logger.info(
            f"Query pipeline complete: {len(enriched_results)} results, "
            f"duration={duration_ms}ms"
        )

        return response

    def _run_fts_search(
        self, query_text: str, repository_id: UUID, run_id: Optional[UUID]
    ) -> List[Dict]:
        """
        Run FTS search with error handling.

        Args:
            query_text: Search query
            repository_id: Repository UUID
            run_id: Optional run UUID

        Returns:
            List of FTS results
        """
        try:
            return self.fts_retriever.search(
                query=query_text, repository_id=repository_id, limit=50, run_id=run_id
            )
        except Exception as e:
            logger.error(f"FTS search error: {e}")
            raise

    def _run_vector_search(
        self, query_text: str, repository_id: UUID, run_id: Optional[UUID]
    ) -> List[Dict]:
        """
        Run vector search with error handling.

        Args:
            query_text: Search query
            repository_id: Repository UUID
            run_id: Optional run UUID

        Returns:
            List of vector results
        """
        try:
            return self.vector_retriever.search(
                query=query_text, repository_id=repository_id, limit=50, run_id=run_id
            )
        except Exception as e:
            logger.error(f"Vector search error: {e}")
            raise

    def _enrich_results_with_metadata(
        self, results: List[Dict], repository_id: UUID
    ) -> List[Dict]:
        """
        Fetch full metadata from Postgres for result chunks.

        Args:
            results: List of results with chunk_id and scores
            repository_id: Repository UUID

        Returns:
            List of enriched results with full metadata and provenance
        """
        if not results:
            return []

        enriched = []

        # Connect to Postgres
        conn = psycopg2.connect(self.postgres_conn)

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for result in results:
                    chunk_id = result.get("chunk_id")
                    if not chunk_id:
                        logger.warning("Result missing chunk_id, skipping")
                        continue

                    # Fetch chunk metadata and provenance
                    query = """
                        SELECT
                            c.id::text as chunk_id,
                            c.file_path,
                            c.start_line,
                            c.end_line,
                            c.breadcrumb,
                            c.chunk_type,
                            LEFT(c.content, 200) as content_preview,
                            c.content,
                            ir.id::text as run_id,
                            ir.commit_sha,
                            ir.completed_at as ingestion_date
                        FROM chunks c
                        LEFT JOIN ingestion_runs ir ON c.ingestion_run_id = ir.id
                        WHERE c.id = %s AND c.repository_id = %s
                        LIMIT 1
                    """

                    cur.execute(query, (chunk_id, str(repository_id)))
                    row = cur.fetchone()

                    if not row:
                        logger.warning(
                            f"Chunk {chunk_id} not found in database, skipping"
                        )
                        continue

                    # Build enriched result
                    enriched_result = {
                        "chunk_id": row["chunk_id"],
                        "file_path": row["file_path"],
                        "start_line": row["start_line"],
                        "end_line": row["end_line"],
                        "breadcrumb": row["breadcrumb"] or "",
                        "chunk_type": row["chunk_type"],
                        "content_preview": row["content_preview"],
                        "content": row["content"],  # Full content for LLM context
                        "score": result.get("boosted_score", 0.0),
                        "rrf_score": result.get("rrf_score", 0.0),
                        "boost_multiplier": result.get("boost_multiplier", 1.0),
                        "sources": result.get("sources", []),
                        "provenance": {
                            "run_id": row["run_id"],
                            "commit_sha": row["commit_sha"],
                            "ingestion_date": (
                                row["ingestion_date"].isoformat()
                                if row["ingestion_date"]
                                else None
                            ),
                        },
                    }

                    enriched.append(enriched_result)

        finally:
            conn.close()

        logger.info(f"Enriched {len(enriched)} results with full metadata")

        return enriched
