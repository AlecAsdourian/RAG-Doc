"""End-to-end ingestion pipeline orchestrator."""

import logging
import time
from typing import Any, Dict, List, Tuple
from uuid import UUID

from workers.chunker import SemanticChunker, Chunk
from workers.embeddings import EmbeddingGenerator
from workers.storage import PostgresWriter, QdrantWriter

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates the full code ingestion pipeline."""

    def __init__(
        self,
        postgres_conn: str,
        qdrant_url: str = "http://localhost:6333",
        openai_api_key: str = None,
    ):
        """
        Initialize ingestion pipeline.

        Args:
            postgres_conn: Postgres connection string
            qdrant_url: Qdrant server URL
            openai_api_key: OpenAI API key (or uses OPENAI_API_KEY env var)
        """
        self.chunker = SemanticChunker()
        self.embedding_gen = EmbeddingGenerator(api_key=openai_api_key)
        self.postgres = PostgresWriter(postgres_conn)
        self.qdrant = QdrantWriter(qdrant_url)

        logger.info("Ingestion pipeline initialized")

    def process_files(
        self,
        files: List[Tuple[str, str, str]],  # (file_path, content, language)
        repository_id: UUID,
        commit_sha: str = "local",
        branch: str = "main",
    ) -> Dict[str, Any]:
        """
        Process files through the complete pipeline.

        Args:
            files: List of (file_path, content, language) tuples
            repository_id: UUID of repository
            commit_sha: Git commit SHA
            branch: Git branch name

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
                repository_id, commit_sha, branch
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
                    ingestion_run_id, 0, "No chunks created"
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

            # Step 4: Store chunks in Postgres
            logger.info(f"Storing {len(all_chunks)} chunks in Postgres...")
            content_hash_to_chunk_id = self.postgres.insert_chunks(
                all_chunks, ingestion_run_id, repository_id
            )
            logger.info(f"✓ Stored {len(all_chunks)} chunks in Postgres")

            # Step 5: Store embeddings in Qdrant
            logger.info("Storing embeddings in Qdrant...")

            # Map chunk_id → embedding
            chunk_id_to_embedding = {}
            chunk_id_to_metadata = {}

            import hashlib

            for chunk in all_chunks:
                # Compute content hash same way as PostgresWriter (raw content)
                content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()

                if content_hash in content_hash_to_chunk_id:
                    chunk_id = content_hash_to_chunk_id[content_hash]

                    if content_hash in content_hash_to_embedding:
                        chunk_id_to_embedding[chunk_id] = content_hash_to_embedding[
                            content_hash
                        ]
                        chunk_id_to_metadata[chunk_id] = {
                            "chunk_id": chunk_id,
                            "repository_id": repository_id,
                            "file_path": chunk.file_path,
                            "language": chunk.language,
                            "chunk_type": chunk.chunk_type,
                            "breadcrumb": chunk.metadata.get("breadcrumb", ""),
                        }

            print(f"[DEBUG] Prepared {len(chunk_id_to_embedding)} embeddings for Qdrant upsert")
            print(f"[DEBUG] content_hash_to_embedding has {len(content_hash_to_embedding)} items")
            print(f"[DEBUG] content_hash_to_chunk_id has {len(content_hash_to_chunk_id)} items")

            # Show sample hashes for debugging
            if content_hash_to_embedding:
                sample_hash_emb = list(content_hash_to_embedding.keys())[0]
                print(f"[DEBUG] Sample embedding hash: {sample_hash_emb[:32]}...")
            if content_hash_to_chunk_id:
                sample_hash_chunk = list(content_hash_to_chunk_id.keys())[0]
                print(f"[DEBUG] Sample chunk hash: {sample_hash_chunk[:32]}...")

            vectors_stored = self.qdrant.upsert_embeddings(
                chunk_id_to_embedding, chunk_id_to_metadata
            )
            print(f"[DEBUG] Upserted {vectors_stored} vectors to Qdrant")
            logger.info(f"✓ Stored {vectors_stored} vectors in Qdrant")

            # Step 6: Complete ingestion run
            self.postgres.complete_ingestion_run(ingestion_run_id, len(all_chunks))
            logger.info(f"✓ Completed ingestion run")

        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            stats["status"] = "failed"
            stats["error"] = str(e)

            # Mark ingestion run as failed
            if ingestion_run_id:
                try:
                    self.postgres.complete_ingestion_run(
                        ingestion_run_id, stats["chunks_created"], error_message=str(e)
                    )
                except Exception as complete_err:
                    logger.error(f"Failed to mark run as failed: {complete_err}")

        finally:
            # Calculate duration
            stats["duration_seconds"] = time.time() - start_time

            # Close connections
            self.postgres.close()

        return stats
