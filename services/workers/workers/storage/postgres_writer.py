"""Postgres writer for storing chunks and ingestion metadata.

All writes to tenant-scoped tables (`ingestion_runs`, `chunks`) go
through `workers.db.require_tenant` — the assert_tenant_scoped trigger
from migration 000009 refuses raw writes and RLS filters SELECTs, so a
missing tenant here means the ingestion silently loses data (or, without
the trigger, silently leaks). Every public method takes `organization_id`
so the caller cannot forget.

Since migration 000017 (22-02) a chunk row also carries, and this writer
supplies on EVERY row:

- `organization_id`: `chunks` is partitioned by it and its row-level
  security policy reads it. Nothing in the database fills it in (a BEFORE
  trigger cannot route a row to another partition), and the composite key
  chunks_repo_tenant_fk refuses a value that is not the repository's.
- `embedding`: the chunk's vector, `vector(1536) NOT NULL`. Vectors live in
  Postgres under the same policy as the text (DECISIONS.md D2).
- `embedding_model`: the model that produced it (22-CONTEXT P4). It is
  read from the generator that made the vectors, never restated.
"""

import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, execute_batch

from workers.chunker.models import Chunk
from workers.db import require_tenant

logger = logging.getLogger(__name__)


class PostgresWriter:
    """Writes chunks and ingestion metadata to Postgres, tenant-scoped."""

    def __init__(self, connection_string: str):
        """
        Initialize Postgres writer.

        Args:
            connection_string: Postgres connection string (postgresql://...)
        """
        self.connection_string = connection_string
        self.conn = None

    def connect(self):
        """Establish database connection."""
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(self.connection_string)
            logger.info("Connected to Postgres")

    def close(self):
        """Close database connection."""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("Closed Postgres connection")

    def _compute_content_hash(self, content: str) -> str:
        """Return the SHA256 hex digest of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def create_ingestion_run(
        self,
        organization_id: UUID,
        repository_id: UUID,
        commit_sha: str = "local",
        branch: str = "main",
    ) -> UUID:
        """Create an ingestion_runs row under the caller's tenant scope.

        Args:
            organization_id: Tenant scope for the write (required).
            repository_id: UUID of the repository. The repo must already
                belong to `organization_id`; a mismatch is silently
                filtered by RLS and no row is written.
            commit_sha: Git commit SHA (default: "local").
            branch: Git branch name (default: "main").

        Returns:
            UUID of created ingestion run.
        """
        self.connect()

        ingestion_run_id = uuid4()

        query = """
            INSERT INTO ingestion_runs (
                id, repository_id, commit_sha, branch, status, started_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """

        with require_tenant(self.conn, organization_id) as cur:
            cur.execute(
                query,
                (
                    str(ingestion_run_id),
                    str(repository_id),
                    commit_sha,
                    branch,
                    "processing",
                    datetime.utcnow(),
                ),
            )

        logger.info(
            f"Created ingestion run {ingestion_run_id} for org {organization_id}"
        )
        return ingestion_run_id

    @staticmethod
    def _vector_literal(vector: Sequence[float]) -> str:
        """Render a vector as pgvector's text input form, `[x,y,z]`.

        Passed as `%s::vector`. Each element goes through `repr(float(x))`,
        the shortest decimal that reads back as the same double, so the
        transport is lossless: the only rounding is the server's, to the
        single-precision elements pgvector stores. Measured by
        `test_writer_stores_the_vector_it_was_given` in
        tests/isolation/test_postgres_writer_isolation.py, which is why no
        client-side pgvector package is needed.
        """
        return "[" + ",".join(repr(float(x)) for x in vector) + "]"

    def insert_chunks(
        self,
        organization_id: UUID,
        chunks: List[Chunk],
        ingestion_run_id: UUID,
        repository_id: UUID,
        embeddings: Mapping[str, Sequence[float]],
        embedding_model: str,
    ) -> Dict[str, UUID]:
        """Batch-insert chunks under the caller's tenant scope, each with its
        vector, the model that produced it, and the tenant.

        Args:
            organization_id: Tenant scope for the write (required), and the
                value written to every row's `organization_id`. It must be
                the repository's organization: chunks_repo_tenant_fk refuses
                any other pair with 23503.
            chunks: List of chunks to insert.
            ingestion_run_id: UUID of the parent ingestion run.
            repository_id: UUID of the repository (denormalized on chunks).
            embeddings: content_hash -> vector, as
                `EmbeddingGenerator.generate_embeddings_for_chunks` returns
                it. EVERY chunk must have one. Chunks with identical content
                share a hash and therefore a vector, and each of them gets
                its own row with that vector: before 000017 the vectors
                lived in Qdrant keyed by ONE chunk id per hash, so every
                duplicate-content chunk after the first had no vector at all
                (12 in miniflux, 80 in mealie; 22-RESEARCH.md Q3).
            embedding_model: The model that produced `embeddings`. Pass the
                generator's `.model`; never a literal, or a model change
                would store vectors under the wrong name and the retriever
                would compare across models (22-CONTEXT P4).

        Returns:
            Mapping content_hash -> chunk_id. For duplicate-content chunks it
            holds the LAST row's id; every row was written.

        Raises:
            ValueError: a chunk has no embedding, or the model is empty.
                Raised before anything is written, naming the chunk's file
                and lines, so a partial batch is never left behind.
        """
        if not chunks:
            logger.info("No chunks to insert")
            return {}

        if not embedding_model:
            raise ValueError(
                "embedding_model is required: every chunk records the model that "
                "produced its vector (migration 000017, 22-CONTEXT P4)"
            )

        # `breadcrumb` has its own column (migration 000006): keyword search
        # matches against it, and query results are rebuilt from it after
        # ranking. Until 2026-09-13 this insert never wrote it, so the column
        # was NULL for every chunk -- the breadcrumb branch of keyword search
        # matched nothing, and every query result came back with an empty
        # breadcrumb even though the chunk's metadata carried one.
        query = """
            INSERT INTO chunks (
                id, organization_id, ingestion_run_id, repository_id, file_path,
                start_line, end_line, content, content_hash,
                language, chunk_type, metadata, breadcrumb,
                embedding, embedding_model
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
        """

        batch_data = []
        content_hash_to_id: Dict[str, UUID] = {}

        for chunk in chunks:
            chunk_id = uuid4()
            content_hash = self._compute_content_hash(chunk.content)

            # Resolved BEFORE the batch is sent: a missing vector aborts the
            # whole insert with nothing written, rather than failing part
            # way through on NOT NULL.
            vector = embeddings.get(content_hash)
            if vector is None:
                raise ValueError(
                    f"no embedding for chunk {chunk.file_path}:"
                    f"{chunk.start_line}-{chunk.end_line} "
                    f"(content_hash {content_hash[:12]}...); every chunk needs "
                    f"its vector, and nothing was written"
                )

            content_hash_to_id[content_hash] = chunk_id

            batch_data.append(
                (
                    str(chunk_id),
                    str(organization_id),
                    str(ingestion_run_id),
                    str(repository_id),
                    chunk.file_path,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                    content_hash,
                    chunk.language,
                    chunk.chunk_type,
                    Json(chunk.metadata),
                    (chunk.metadata or {}).get("breadcrumb") or None,
                    self._vector_literal(vector),
                    embedding_model,
                )
            )

        self.connect()

        with require_tenant(self.conn, organization_id) as cur:
            execute_batch(cur, query, batch_data, page_size=100)

        logger.info(
            f"Inserted {len(chunks)} chunks with {embedding_model} vectors "
            f"under org {organization_id}"
        )
        return content_hash_to_id

    def complete_ingestion_run(
        self,
        organization_id: UUID,
        ingestion_run_id: UUID,
        chunks_count: int,
        error_message: Optional[str] = None,
    ):
        """Mark an ingestion_runs row as completed or failed.

        Args:
            organization_id: Tenant scope for the write (required).
            ingestion_run_id: UUID of the run to update.
            chunks_count: Number of chunks the run produced.
            error_message: If non-empty, marks the run as `failed`.
        """
        self.connect()

        status = "failed" if error_message else "completed"

        query = """
            UPDATE ingestion_runs
            SET status = %s,
                chunks_processed = %s,
                completed_at = %s,
                error_message = %s
            WHERE id = %s
        """

        with require_tenant(self.conn, organization_id) as cur:
            cur.execute(
                query,
                (
                    status,
                    chunks_count,
                    datetime.utcnow(),
                    error_message,
                    str(ingestion_run_id),
                ),
            )

        logger.info(
            f"Ingestion run {ingestion_run_id} {status} ({chunks_count} chunks) under org {organization_id}"
        )

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
