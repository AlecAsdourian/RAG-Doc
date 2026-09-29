"""End-to-end ingestion pipeline orchestrator."""

import logging
import time
from typing import Any, Dict, List, Tuple
from uuid import UUID

from workers.chunker import SemanticChunker, Chunk
from workers.embeddings import EmbeddingGenerator
from workers.storage import PostgresWriter

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates the full code ingestion pipeline."""

    def __init__(
        self,
        postgres_conn: str,
        openai_api_key: str = None,
    ):
        """
        Initialize ingestion pipeline.

        Args:
            postgres_conn: Postgres connection string. Chunks and their
                vectors are stored there, together, under the tenant's
                row-level security (DECISIONS.md D2; Qdrant was retired in
                22-03 after the storage-move equivalence gate passed).
            openai_api_key: OpenAI API key (or uses OPENAI_API_KEY env var)
        """
        self.chunker = SemanticChunker()
        self.embedding_gen = EmbeddingGenerator(api_key=openai_api_key)
        self.postgres = PostgresWriter(postgres_conn)

        logger.info("Ingestion pipeline initialized")

    def process_files(
        self,
        files: List[Tuple[str, str, str]],  # (file_path, content, language)
        organization_id: UUID,
        repository_id: UUID,
        commit_sha: str = "local",
        branch: str = "main",
    ) -> Dict[str, Any]:
        """
        Process files through the complete pipeline.

        Args:
            files: List of (file_path, content, language) tuples.
            organization_id: Tenant scope for every Postgres write done
                by this run (create_ingestion_run, insert_chunks,
                complete_ingestion_run). Required — the assert_tenant_scoped
                trigger from migration 000009 refuses raw writes.
            repository_id: UUID of repository. Must belong to
                `organization_id`; otherwise RLS silently filters the
                write and the run drops rows.
            commit_sha: Git commit SHA.
            branch: Git branch name.

        Returns:
            Statistics dict with:
            - files_processed: int
            - chunks_created: int
            - embeddings_generated: int
            - duration_seconds: float
            - status: "success" or "failed"
            - error: str (if failed)
        """
        start_time = time.time()
        stats = {
            "files_processed": 0,
            "chunks_created": 0,
            "embeddings_generated": 0,
            "duration_seconds": 0,
            "status": "success",
        }

        ingestion_run_id = None

        try:
            # Step 1: Create ingestion run
            logger.info(f"Creating ingestion run for repository {repository_id}")
            ingestion_run_id = self.postgres.create_ingestion_run(
                organization_id, repository_id, commit_sha, branch
            )
            logger.info(f"✓ Created ingestion run: {ingestion_run_id}")

            # Step 2: Parse and chunk all files
            logger.info(f"Parsing and chunking {len(files)} files...")
            all_chunks = []

            for file_path, content, language in files:
                try:
                    chunks = self.chunker.chunk_file(file_path, content, language)
                    all_chunks.extend(chunks)
                    stats["files_processed"] += 1
                    logger.debug(
                        f"  {file_path}: {len(chunks)} chunks ({language})"
                    )
                except Exception as e:
                    logger.error(f"Failed to chunk {file_path}: {e}")
                    # Continue with other files

            stats["chunks_created"] = len(all_chunks)
            logger.info(
                f"✓ Parsed and chunked {stats['files_processed']} files → "
                f"{len(all_chunks)} chunks"
            )

            if not all_chunks:
                logger.warning("No chunks created, aborting pipeline")
                self.postgres.complete_ingestion_run(
                    organization_id, ingestion_run_id, 0, "No chunks created"
                )
                stats["status"] = "failed"
                stats["error"] = "No chunks created"
                return stats

            # Step 3: Generate embeddings
            logger.info(f"Generating embeddings for {len(all_chunks)} chunks...")
            content_hash_to_embedding = (
                self.embedding_gen.generate_embeddings_for_chunks(all_chunks)
            )
            stats["embeddings_generated"] = len(content_hash_to_embedding)
            logger.info(
                f"✓ Generated {len(content_hash_to_embedding)} embeddings"
            )

            # Step 4: Store chunks in Postgres, each with its vector and the
            # model that produced it (migration 000017). The model name comes
            # from the generator, so a model change here is one line. Every
            # duplicate-content chunk gets its hash's vector too; there is no
            # second store to keep one point per hash for.
            logger.info(f"Storing {len(all_chunks)} chunks in Postgres...")
            self.postgres.insert_chunks(
                organization_id,
                all_chunks,
                ingestion_run_id,
                repository_id,
                embeddings=content_hash_to_embedding,
                embedding_model=self.embedding_gen.model,
            )
            logger.info(f"✓ Stored {len(all_chunks)} chunks with their vectors in Postgres")

            # Step 5: Complete ingestion run
            self.postgres.complete_ingestion_run(
                organization_id, ingestion_run_id, len(all_chunks)
            )
            logger.info(f"✓ Completed ingestion run")

        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            stats["status"] = "failed"
            stats["error"] = str(e)

            # Mark ingestion run as failed
            if ingestion_run_id:
                try:
                    self.postgres.complete_ingestion_run(
                        organization_id,
                        ingestion_run_id,
                        stats["chunks_created"],
                        error_message=str(e),
                    )
                except Exception as complete_err:
                    logger.error(f"Failed to mark run as failed: {complete_err}")

        finally:
            # Calculate duration
            stats["duration_seconds"] = time.time() - start_time

            # Close connections
            self.postgres.close()

        return stats
