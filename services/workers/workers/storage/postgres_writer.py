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

TWO WAYS IN, ONE STATEMENT EACH (22-05). The methods that take
`organization_id` open their own tenant transaction on this writer's
connection, as the harness and the benchmark pipeline use them. The `*_on`
methods take a CURSOR and run inside whatever transaction the caller holds:
the ingest handler's `write_results` runs them in `complete()`'s, so the
chunks, the run and the job's completion commit together or not at all. The
SQL is one module constant per statement, shared by both, so the two paths
cannot drift apart.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, execute_batch

from workers.chunker.models import Chunk
from workers.db import require_tenant

logger = logging.getLogger(__name__)


def content_hash(content: str) -> str:
    """SHA-256 hex of a chunk's content: the key its vector is looked up by.

    `EmbeddingGenerator` keys the vectors it returns the same way; a
    disagreement between the two is loud, not silent -- `insert_chunks_on`
    refuses a chunk with no vector under its hash before writing anything.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# `breadcrumb` has its own column (migration 000006): keyword search matches
# against it, and query results are rebuilt from it after ranking. Until
# 2026-09-13 this insert never wrote it, so the column was NULL for every
# chunk -- the breadcrumb branch of keyword search matched nothing, and every
# query result came back with an empty breadcrumb even though the chunk's
# metadata carried one.
INSERT_CHUNK_SQL = """
    INSERT INTO chunks (
        id, organization_id, ingestion_run_id, repository_id, file_path,
        start_line, end_line, content, content_hash,
        language, chunk_type, metadata, breadcrumb,
        embedding, embedding_model
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
"""

# Marks a run completed (or failed) with its chunk count. %s status,
# %s chunks_processed, %s completed_at, %s error_message, %s id.
COMPLETE_RUN_SQL = """
    UPDATE ingestion_runs
    SET status = %s,
        chunks_processed = %s,
        completed_at = %s,
        error_message = %s
    WHERE id = %s
"""

# Every chunk of one repository, whichever run wrote it (22-05, P1): a full
# ingest RE-CREATES the repository's chunks, and this is the "re-" half. It
# runs in the same transaction as the insert of the replacement set, so a
# reader sees the old set or the new one, never neither and never both
# (ISS-027). Tenant-scoped: row-level security bounds it to the caller's
# tenant, and `chunks_repo_tenant_fk` makes every chunk of the repository
# that tenant's. %s repository_id.
#
# ⚠ `retrievals` ROWS CITING A DELETED CHUNK ARE LEFT IN PLACE, DANGLING, BY
# DESIGN (22-CONTEXT P17, the user's answer U9: "a logged result keeps a chunk
# id that may later point at nothing"). Since 000017 nothing cascades from
# `chunks` to `retrievals`, so nothing here touches them: deleting them would
# destroy user-authored `feedback` on every re-index, and repointing them is
# the link-shape decision U9 deferred to when feedback ships. They stay bound
# to their query and project, which delete them in turn; what they lose is
# the chunk, and `DELETE /api/repositories/{id}` already documents that it
# reaches only retrievals whose chunk still exists. Pinned by
# `test_a_reingest_leaves_a_retrieval_of_a_replaced_chunk_dangling`.
DELETE_REPOSITORY_CHUNKS_SQL = """
    DELETE FROM chunks WHERE repository_id = %s
"""


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
        """Return the SHA256 hex digest of content. See `content_hash`."""
        return content_hash(content)

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

        self.connect()

        with require_tenant(self.conn, organization_id) as cur:
            content_hash_to_id = self.insert_chunks_on(
                cur,
                organization_id,
                chunks,
                ingestion_run_id,
                repository_id,
                embeddings=embeddings,
                embedding_model=embedding_model,
            )

        logger.info(
            f"Inserted {len(chunks)} chunks with {embedding_model} vectors "
            f"under org {organization_id}"
        )
        return content_hash_to_id

    @classmethod
    def insert_chunks_on(
        cls,
        cur: Any,
        organization_id: UUID,
        chunks: List[Chunk],
        ingestion_run_id: UUID,
        repository_id: UUID,
        embeddings: Mapping[str, Sequence[float]],
        embedding_model: str,
    ) -> Dict[str, UUID]:
        """`insert_chunks` on the CALLER's cursor and transaction (22-05).

        Everything `insert_chunks` says holds here -- every chunk gets its
        hash's vector, duplicates included; a chunk with no vector or an
        empty model raises `ValueError` BEFORE anything is sent -- because
        `insert_chunks` is this, inside a tenant transaction it opened.

        ⚠ THE CURSOR MUST ALREADY BE TENANT-SCOPED to `organization_id`
        (`require_tenant`, or `complete()`'s transaction, which is one). It
        does not commit, roll back or open anything: the ingest handler's
        `write_results` calls it inside `complete()`, so the chunks and the
        job's completion commit together or not at all.
        """
        if not chunks:
            return {}

        if not embedding_model:
            raise ValueError(
                "embedding_model is required: every chunk records the model that "
                "produced its vector (migration 000017, 22-CONTEXT P4)"
            )

        batch_data = []
        content_hash_to_id: Dict[str, UUID] = {}

        for chunk in chunks:
            chunk_id = uuid4()
            chunk_hash = content_hash(chunk.content)

            # Resolved BEFORE the batch is sent: a missing vector aborts the
            # whole insert with nothing written, rather than failing part
            # way through on NOT NULL.
            vector = embeddings.get(chunk_hash)
            if vector is None:
                raise ValueError(
                    f"no embedding for chunk {chunk.file_path}:"
                    f"{chunk.start_line}-{chunk.end_line} "
                    f"(content_hash {chunk_hash[:12]}...); every chunk needs "
                    f"its vector, and nothing was written"
                )

            content_hash_to_id[chunk_hash] = chunk_id

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
                    chunk_hash,
                    chunk.language,
                    chunk.chunk_type,
                    Json(chunk.metadata),
                    (chunk.metadata or {}).get("breadcrumb") or None,
                    cls._vector_literal(vector),
                    embedding_model,
                )
            )

        execute_batch(cur, INSERT_CHUNK_SQL, batch_data, page_size=100)
        return content_hash_to_id

    @staticmethod
    def delete_repository_chunks_on(cur: Any, repository_id: UUID) -> int:
        """Delete every chunk of one repository, on the caller's cursor.

        The first half of a full ingest's replacement (P1); see
        `DELETE_REPOSITORY_CHUNKS_SQL`, including why `retrievals` citing the
        deleted chunks are left dangling. Returns how many rows went.
        """
        cur.execute(DELETE_REPOSITORY_CHUNKS_SQL, (str(repository_id),))
        return cur.rowcount

    @staticmethod
    def complete_ingestion_run_on(cur: Any, ingestion_run_id: UUID, chunks_count: int) -> None:
        """Mark a run `completed` with its chunk count, on the caller's cursor.

        The same statement as `complete_ingestion_run`; the ingest handler
        runs it in `complete()`'s transaction, after the chunks it counts.
        """
        cur.execute(
            COMPLETE_RUN_SQL,
            ("completed", chunks_count, datetime.now(timezone.utc), None, str(ingestion_run_id)),
        )

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

        with require_tenant(self.conn, organization_id) as cur:
            cur.execute(
                COMPLETE_RUN_SQL,
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
