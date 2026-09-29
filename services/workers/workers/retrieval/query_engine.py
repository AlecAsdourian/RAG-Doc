"""QueryEngine orchestrator for complete RAG query pipeline."""

import concurrent.futures
import logging
import os
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.embeddings.embedding_generator import EmbeddingGenerator

from .errors import RETRIEVER_LABELS, RetrievalError
from .fts_retriever import FTSRetriever
from .metadata_booster import MetadataBooster
from .query_parser import QueryParser
from .rrf_fusion import RRFFusion
from .vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)


def _leg_entry(result: Dict[str, Any], score_key: str) -> Dict[str, Any]:
    """One retriever result as the trace records it: the chunk and its score."""
    return {
        "chunk_id": result.get("chunk_id"),
        "file_path": result.get("file_path"),
        "breadcrumb": result.get("breadcrumb") or "",
        "chunk_type": result.get("chunk_type") or "",
        "score": result.get(score_key),
    }


class QueryEngine:
    """
    Complete RAG query engine orchestrating hybrid search pipeline.

    Pipeline:
    1. Parse query → extract quoted terms, identifiers
    2. Run FTS and vector searches in parallel, both in Postgres under the
       caller's tenant (22-03: the vector leg reads chunks.embedding with
       pgvector; Qdrant is gone)
    3. Fuse results with RRF (reciprocal rank fusion)
    4. Apply metadata boosts (chunk-type, path, exact-match)
    5. Sort by final score, return top-k chunks with provenance
    """

    def __init__(
        self,
        postgres_conn: str,
        openai_api_key: str,
        boost_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize QueryEngine with all retrieval components.

        Args:
            postgres_conn: PostgreSQL connection string. Both legs and result
                enrichment open their connections from it, so its role is the
                role every read runs as; in tests that is `rag_doc_app`.
            openai_api_key: OpenAI API key for embeddings
            boost_config: Optional boost configuration for MetadataBooster
        """
        self.postgres_conn = postgres_conn
        self.openai_api_key = openai_api_key

        # One generator, shared with the vector leg: its `.model` is the model
        # filter on chunks.embedding_model (22-CONTEXT P4), read from here and
        # never copied.
        self.embedding_generator = EmbeddingGenerator(api_key=openai_api_key)

        # Initialize all components
        self.fts_retriever = FTSRetriever(connection_string=postgres_conn)
        self.vector_retriever = VectorRetriever(
            postgres_conn=postgres_conn, embedding_generator=self.embedding_generator
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
        organization_id: UUID,
        repository_id: UUID,
        top_k: int = 5,
        run_id: Optional[UUID] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute complete query pipeline and return ranked results.

        Args:
            query_text: Search query string
            repository_id: Repository UUID to search
            top_k: Number of top results to return (default: 5)
            run_id: Must be None. Search is not scoped to an ingestion run:
                a run is the wrong unit of currency once files are re-indexed
                incrementally (ISS-027; per-file currency is 22.1-02's), and
                until 22-03 only the keyword leg honoured it while the vector
                leg ignored it. A value raises ValueError rather than being
                half-applied.
            trace: Optional dict. When given, every stage of this pipeline
                writes what it produced into it, in order and from inside the
                real code path: `fts` and `vector` (chunk ids with scores, as
                the retrievers returned them), `fused` (with `rrf_score`),
                `boosted` (in final order, with `boosted_score`) and `top`
                (the enriched results). The quality harness's `--record` uses
                it for the 22-03 storage-move equivalence gate. When `None`,
                the default, nothing is recorded and nothing else changes.

        Returns:
            Dict with:
                - query: Original query string
                - repository_id: Repository UUID
                - run_id: Run UUID (or None)
                - results: List of top-k chunks with metadata and scores
                - metadata: Pipeline stats (counts, duration)

        Raises:
            RetrievalError: If keyword (FTS) or vector search raises. It names
                the failed retriever(s) and chains the original exception. No
                partial result is returned (ISS-030).

        Example:
            >>> engine = QueryEngine(postgres_conn, openai_key)
            >>> result = engine.query(
            ...     "authentication error",
            ...     organization_id=UUID("..."),
            ...     repository_id=UUID("..."),
            ...     top_k=5
            ... )
            >>> for chunk in result["results"]:
            ...     print(f"{chunk['file_path']}:{chunk['start_line']} - {chunk['score']}")
        """
        if run_id is not None:
            raise ValueError(
                "run_id is not supported: search covers the repository's current chunks, "
                "not one ingestion run (ISS-027; per-file currency is 22.1-02's)"
            )

        start_time = time.time()

        logger.info(
            f"Query pipeline starting: query='{query_text}', "
            f"organization_id={organization_id}, repository_id={repository_id}, top_k={top_k}"
        )

        # Step 1: Parse query
        parsed_query = self.query_parser.parse(query_text)
        logger.info(
            f"Query parsed: {len(parsed_query['quoted_terms'])} quoted terms, "
            f"{len(parsed_query['identifiers'])} identifiers"
        )

        # Step 2: Run FTS and vector searches in parallel, and wait for both.
        #
        # If either retriever fails, the whole query fails with RetrievalError
        # (ISS-030). No partial result is returned, because a caller cannot tell
        # one from a real result:
        # - Without vector search the result is usually empty, since keyword
        #   search returns nothing for most natural-language questions (ISS-029).
        #   That reached users as an empty search, or as a chat answer of "I
        #   don't have enough information", with HTTP 200.
        # - Without keyword search, fusion and boosts run over the vector list
        #   alone, which silently changes the ranking. Keyword search also shares
        #   its Postgres with result enrichment (step 6), so the rest of the
        #   query depends on what just failed.
        # The failure used to survive only as response metadata, which nothing read.
        retrieved: Dict[str, List[Dict]] = {}
        failures: Dict[str, Exception] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                "fts": executor.submit(
                    self._run_fts_search, query_text, organization_id, repository_id
                ),
                "vector": executor.submit(
                    self._run_vector_search, query_text, organization_id, repository_id
                ),
            }
            for name, future in futures.items():
                try:
                    retrieved[name] = future.result()
                except Exception as e:  # re-raised below as RetrievalError
                    failures[name] = e

        if failures:
            for name, e in failures.items():
                # The only place a retriever failure is logged in full. The
                # traceback carries the exception text, so the message leaves it
                # out: an OpenAI auth error holds a key fragment, and it should
                # be written once per request. Callers log only the outcome.
                logger.error(
                    f"{RETRIEVER_LABELS[name]} failed: organization_id={organization_id}, "
                    f"repository_id={repository_id}",
                    exc_info=e,
                )
            raise RetrievalError(failures) from next(iter(failures.values()))

        fts_results = retrieved["fts"]
        vector_results = retrieved["vector"]

        logger.info(
            f"Parallel search complete: FTS={len(fts_results)} results, "
            f"Vector={len(vector_results)} results"
        )
        if trace is not None:
            trace["fts"] = [_leg_entry(r, "fts_score") for r in fts_results]
            trace["vector"] = [_leg_entry(r, "vector_score") for r in vector_results]

        # Step 3: Fuse results with RRF
        fused_results = self.rrf_fusion.fuse(
            {"fts": fts_results, "vector": vector_results}
        )
        logger.info(f"RRF fusion produced {len(fused_results)} unique results")
        if trace is not None:
            trace["fused"] = [
                {
                    "chunk_id": r.get("chunk_id"),
                    "rrf_score": r.get("rrf_score"),
                    "sources": list(r.get("sources", [])),
                }
                for r in fused_results
            ]

        # Step 4: Apply metadata boosts
        boosted_results = self.metadata_booster.boost(fused_results, parsed_query)
        logger.info(f"Metadata boosts applied to {len(boosted_results)} results")

        # Step 5: Sort by boosted_score and take top_k
        boosted_results.sort(key=lambda x: x.get("boosted_score", 0.0), reverse=True)
        top_results = boosted_results[:top_k]
        if trace is not None:
            # Recorded after the sort: this is the final order the cut is taken from.
            trace["boosted"] = [
                {
                    "chunk_id": r.get("chunk_id"),
                    "rrf_score": r.get("rrf_score"),
                    "boost_multiplier": r.get("boost_multiplier"),
                    "boosted_score": r.get("boosted_score"),
                }
                for r in boosted_results
            ]

        # Step 6: Fetch full metadata from Postgres for top results
        enriched_results = self._enrich_results_with_metadata(
            top_results, organization_id, repository_id
        )
        if trace is not None:
            trace["top"] = [
                {
                    "chunk_id": r.get("chunk_id"),
                    "file_path": r.get("file_path"),
                    "breadcrumb": r.get("breadcrumb") or "",
                    "score": r.get("score"),
                }
                for r in enriched_results
            ]

        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)

        # Build response
        response = {
            "query": query_text,
            "organization_id": str(organization_id),
            "repository_id": str(repository_id),
            "run_id": str(run_id) if run_id else None,
            "results": enriched_results,
            "metadata": {
                "fts_results": len(fts_results),
                "vector_results": len(vector_results),
                "fused_results": len(fused_results),
                "duration_ms": duration_ms,
            },
        }

        logger.info(
            f"Query pipeline complete: {len(enriched_results)} results, "
            f"duration={duration_ms}ms"
        )

        return response

    def _run_fts_search(
        self,
        query_text: str,
        organization_id: UUID,
        repository_id: UUID,
    ) -> List[Dict]:
        """
        Run FTS search. An exception propagates to `query`, which logs it and
        raises RetrievalError.

        Args:
            query_text: Search query.
            organization_id: Tenant scope for the FTS query.
            repository_id: Repository UUID.

        Returns:
            List of FTS results.
        """
        return self.fts_retriever.search(
            query=query_text,
            organization_id=organization_id,
            repository_id=repository_id,
            limit=50,
        )

    def _run_vector_search(
        self, query_text: str, organization_id: UUID, repository_id: UUID
    ) -> List[Dict]:
        """
        Run vector search, under the same tenant scope as the keyword leg. An
        exception propagates to `query`, which logs it and raises
        RetrievalError.

        Args:
            query_text: Search query
            organization_id: Tenant scope for the vector query
            repository_id: Repository UUID

        Returns:
            List of vector results
        """
        return self.vector_retriever.search(
            query=query_text,
            organization_id=organization_id,
            repository_id=repository_id,
            limit=50,
        )

    def _enrich_results_with_metadata(
        self,
        results: List[Dict],
        organization_id: UUID,
        repository_id: UUID,
    ) -> List[Dict]:
        """
        Fetch full metadata from Postgres for result chunks, scoped to org.

        Args:
            results: List of results with chunk_id and scores.
            organization_id: Tenant scope (required).
            repository_id: Repository UUID.

        Returns:
            List of enriched results with full metadata and provenance.
        """
        if not results:
            return []

        enriched = []

        # Fresh connection; scoped inside require_tenant so RLS filters
        # chunks and ingestion_runs to this tenant.
        conn = psycopg2.connect(self.postgres_conn)

        try:
            with require_tenant(
                conn, organization_id, cursor_factory=RealDictCursor
            ) as cur:
                for result in results:
                    chunk_id = result.get("chunk_id")
                    if not chunk_id:
                        logger.warning("Result missing chunk_id, skipping")
                        continue

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
                            f"Chunk {chunk_id} not found in database for tenant {organization_id}, skipping"
                        )
                        continue

                    enriched_result = {
                        "chunk_id": row["chunk_id"],
                        "file_path": row["file_path"],
                        "start_line": row["start_line"],
                        "end_line": row["end_line"],
                        "breadcrumb": row["breadcrumb"] or "",
                        "chunk_type": row["chunk_type"],
                        "content_preview": row["content_preview"],
                        "content": row["content"],
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
