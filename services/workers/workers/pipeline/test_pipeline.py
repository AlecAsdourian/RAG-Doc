"""Tests for ingestion pipeline."""

from unittest.mock import Mock, patch
from uuid import uuid4
import pytest

from workers.pipeline.ingestion_pipeline import IngestionPipeline


class TestIngestionPipeline:
    """Test pipeline orchestration."""

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.QdrantWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_pipeline_initialization(
        self, mock_chunker, mock_embedgen, mock_qdrant, mock_postgres
    ):
        """Test pipeline initializes all components."""
        pipeline = IngestionPipeline(
            postgres_conn="postgresql://test",
            qdrant_url="http://test:6333",
            openai_api_key="test-key",
        )

        assert pipeline.chunker is not None
        assert pipeline.embedding_gen is not None
        assert pipeline.postgres is not None
        assert pipeline.qdrant is not None

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.QdrantWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_process_files_success(
        self, mock_chunker_class, mock_embedgen_class, mock_qdrant_class, mock_postgres_class
    ):
        """Test successful file processing."""
        # Setup mocks
        mock_postgres = Mock()
        mock_postgres.create_ingestion_run.return_value = uuid4()
        mock_postgres.insert_chunks.return_value = {
            "hash1": uuid4(),
            "hash2": uuid4(),
        }
        mock_postgres_class.return_value = mock_postgres

        mock_qdrant = Mock()
        mock_qdrant.upsert_embeddings.return_value = 2
        mock_qdrant_class.return_value = mock_qdrant

        mock_embedgen = Mock()
        mock_embedgen.generate_embeddings_for_chunks.return_value = {
            "hash1": [0.1] * 1536,
            "hash2": [0.2] * 1536,
        }
        mock_embedgen._prepare_text_for_embedding.return_value = "test"
        mock_embedgen._compute_content_hash.side_effect = ["hash1", "hash2"]
        mock_embedgen_class.return_value = mock_embedgen

        # Mock chunks
        mock_chunk1 = Mock()
        mock_chunk1.file_path = "test.py"
        mock_chunk1.language = "python"
        mock_chunk1.chunk_type = "function"
        mock_chunk1.metadata = {"breadcrumb": "test.func"}

        mock_chunk2 = Mock()
        mock_chunk2.file_path = "test.py"
        mock_chunk2.language = "python"
        mock_chunk2.chunk_type = "function"
        mock_chunk2.metadata = {"breadcrumb": "test.func2"}

        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = [mock_chunk1, mock_chunk2]
        mock_chunker_class.return_value = mock_chunker

        # Create pipeline
        pipeline = IngestionPipeline(
            postgres_conn="postgresql://test",
            openai_api_key="test-key",
        )

        # Process files
        files = [("test.py", "def foo(): pass", "python")]
        repository_id = uuid4()
        stats = pipeline.process_files(files, repository_id)

        # Verify results
        assert stats["status"] == "success"
        assert stats["files_processed"] == 1
        assert stats["chunks_created"] == 2
        assert stats["embeddings_generated"] == 2

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.QdrantWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_process_files_empty(
        self, mock_chunker_class, mock_embedgen_class, mock_qdrant_class, mock_postgres_class
    ):
        """Test handling of files with no chunks."""
        # Setup mocks
        mock_postgres = Mock()
        mock_postgres.create_ingestion_run.return_value = uuid4()
        mock_postgres_class.return_value = mock_postgres

        mock_qdrant_class.return_value = Mock()
        mock_embedgen_class.return_value = Mock()

        mock_chunker = Mock()
        mock_chunker.chunk_file.return_value = []  # No chunks
        mock_chunker_class.return_value = mock_chunker

        # Create pipeline
        pipeline = IngestionPipeline(
            postgres_conn="postgresql://test",
            openai_api_key="test-key",
        )

        # Process files
        files = [("empty.py", "", "python")]
        repository_id = uuid4()
        stats = pipeline.process_files(files, repository_id)

        # Should fail gracefully
        assert stats["status"] == "failed"
        assert "No chunks created" in stats.get("error", "")
        assert stats["chunks_created"] == 0
