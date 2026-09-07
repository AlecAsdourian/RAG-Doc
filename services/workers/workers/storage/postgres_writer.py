"""Postgres writer for storing chunks and ingestion metadata.

All writes to tenant-scoped tables (`ingestion_runs`, `chunks`) go
through `workers.db.require_tenant` — the assert_tenant_scoped trigger
from migration 000009 refuses raw writes and RLS filters SELECTs, so a
missing tenant here means the ingestion silently loses data (or, without
the trigger, silently leaks). Every public method takes `organization_id`
so the caller cannot forget.
"""

import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional
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

    def insert_chunks(
        self,
        organization_id: UUID,
        chunks: List[Chunk],
        ingestion_run_id: UUID,
        repository_id: UUID,
    ) -> Dict[str, UUID]:
        """Batch-insert chunks under the caller's tenant scope.

        Args:
            organization_id: Tenant scope for the write (required).
            chunks: List of chunks to insert.
            ingestion_run_id: UUID of the parent ingestion run.
            repository_id: UUID of the repository (denormalized on chunks).

        Returns:
            Mapping content_hash → chunk_id for freshly-inserted chunks.
        """
        if not chunks:
            logger.info("No chunks to insert")
            return {}

        self.connect()

        query = """
            INSERT INTO chunks (
                id, ingestion_run_id, repository_id, file_path,
                start_line, end_line, content, content_hash,
                language, chunk_type, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        batch_data = []
        content_hash_to_id: Dict[str, UUID] = {}

        for chunk in chunks:
            chunk_id = uuid4()
            content_hash = self._compute_content_hash(chunk.content)

            content_hash_to_id[content_hash] = chunk_id

            batch_data.append(
                (
                    str(chunk_id),
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
                )
            )

        with require_tenant(self.conn, organization_id) as cur:
            execute_batch(cur, query, batch_data, page_size=100)

        logger.info(
            f"Inserted {len(chunks)} chunks under org {organization_id}"
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
