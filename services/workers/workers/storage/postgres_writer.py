"""Postgres writer for storing chunks and ingestion metadata."""

import hashlib
import json
import logging
from datetime import datetime
from typing import Dict, List
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import execute_batch, Json

from workers.chunker.models import Chunk

logger = logging.getLogger(__name__)


class PostgresWriter:
    """Writes chunks and ingestion metadata to Postgres."""

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
        """
        Compute SHA256 hash of content.

        Args:
            content: Content to hash

        Returns:
            Hex string of SHA256 hash
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def create_ingestion_run(
        self, repository_id: UUID, commit_sha: str = "local", branch: str = "main"
    ) -> UUID:
        """
        Create a new ingestion run record.

        Args:
            repository_id: UUID of the repository
            commit_sha: Git commit SHA (default: "local")
            branch: Git branch name (default: "main")

        Returns:
            UUID of created ingestion run
        """
        self.connect()

        ingestion_run_id = uuid4()

        query = """
            INSERT INTO ingestion_runs (
                id, repository_id, commit_sha, branch, status, started_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """

        with self.conn.cursor() as cur:
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
            self.conn.commit()

        logger.info(f"Created ingestion run: {ingestion_run_id}")
        return ingestion_run_id

    def insert_chunks(
        self, chunks: List[Chunk], ingestion_run_id: UUID, repository_id: UUID
    ) -> Dict[str, UUID]:
        """
        Batch insert chunks into database.

        Args:
            chunks: List of chunks to insert
            ingestion_run_id: UUID of ingestion run
            repository_id: UUID of repository

        Returns:
            Dictionary mapping content_hash → chunk_id
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

        # Prepare batch data
        batch_data = []
        content_hash_to_id = {}

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
                    Json(chunk.metadata),  # Convert dict to JSONB
                )
            )

        # Batch insert
        with self.conn.cursor() as cur:
            execute_batch(cur, query, batch_data, page_size=100)
            self.conn.commit()

        logger.info(f"Inserted {len(chunks)} chunks into Postgres")
        return content_hash_to_id

    def complete_ingestion_run(
        self, ingestion_run_id: UUID, chunks_count: int, error_message: str = None
    ):
        """
        Mark ingestion run as completed or failed.

        Args:
            ingestion_run_id: UUID of ingestion run
            chunks_count: Number of chunks processed
            error_message: Error message if failed (None if successful)
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

        with self.conn.cursor() as cur:
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
            self.conn.commit()

        logger.info(
            f"Ingestion run {ingestion_run_id} {status} ({chunks_count} chunks)"
        )

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
