"""Tests for ingestion pipeline."""

import hashlib
from unittest.mock import Mock, patch
from uuid import uuid4
import pytest

from workers.pipeline.ingestion_pipeline import IngestionPipeline


class TestIngestionPipeline:
    """Test pipeline orchestration."""

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_pipeline_initialization(self, mock_chunker, mock_embedgen, mock_postgres):
        """Test pipeline initializes all components, and only these."""
        pipeline = IngestionPipeline(
            postgres_conn="postgresql://test",
            openai_api_key="test-key",
        )

        assert pipeline.chunker is not None
        assert pipeline.embedding_gen is not None
        assert pipeline.postgres is not None
        # 22-03: vectors live in Postgres; there is no second store.
        assert not hasattr(pipeline, "qdrant")

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_process_files_success(self, mock_chunker_class, mock_embedgen_class, mock_postgres_class):
        """Test successful file processing."""
        # The pipeline hashes chunk content itself, so the mocked stores must be
        # keyed by the real hashes of the mock chunks' real content.
        content1 = "def func(): pass"
        content2 = "def func2(): pass"
        hash1 = hashlib.sha256(content1.encode("utf-8")).hexdigest()
        hash2 = hashlib.sha256(content2.encode("utf-8")).hexdigest()

        # Setup mocks
        mock_postgres = Mock()
        mock_postgres.create_ingestion_run.return_value = uuid4()
        mock_postgres.insert_chunks.return_value = {
            hash1: uuid4(),
            hash2: uuid4(),
        }
        mock_postgres_class.return_value = mock_postgres

        embeddings = {
            hash1: [0.1] * 1536,
            hash2: [0.2] * 1536,
        }
        mock_embedgen = Mock()
        mock_embedgen.generate_embeddings_for_chunks.return_value = embeddings
        mock_embedgen._prepare_text_for_embedding.return_value = "test"
        # A name no real generator has: the pipeline must pass the
        # GENERATOR'S model through, not restate a default (22-CONTEXT P4).
        mock_embedgen.model = "mock-embedding-model-7"
        mock_embedgen_class.return_value = mock_embedgen

        # Mock chunks
        mock_chunk1 = Mock()
        mock_chunk1.file_path = "test.py"
        mock_chunk1.language = "python"
        mock_chunk1.chunk_type = "function"
        mock_chunk1.metadata = {"breadcrumb": "test.func"}
        mock_chunk1.content = content1

        mock_chunk2 = Mock()
        mock_chunk2.file_path = "test.py"
        mock_chunk2.language = "python"
        mock_chunk2.chunk_type = "function"
        mock_chunk2.metadata = {"breadcrumb": "test.func2"}
        mock_chunk2.content = content2

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
        organization_id = uuid4()
        repository_id = uuid4()
        stats = pipeline.process_files(files, organization_id, repository_id)

        # Verify results
        assert stats["status"] == "success"
        assert stats["files_processed"] == 1
        assert stats["chunks_created"] == 2
        assert stats["embeddings_generated"] == 2

        # Migration 000017: the chunks reach Postgres WITH their vectors and
        # the model that produced them, taken from the generator. The
        # arguments, not just the call: a pipeline that dropped the map or
        # hard-coded the model would still return "success".
        mock_postgres.insert_chunks.assert_called_once()
        args, kwargs = mock_postgres.insert_chunks.call_args
        assert args == (
            organization_id,
            [mock_chunk1, mock_chunk2],
            mock_postgres.create_ingestion_run.return_value,
            repository_id,
        )
        assert kwargs["embeddings"] is embeddings
        assert kwargs["embedding_model"] == "mock-embedding-model-7"

        # The run is completed with the chunk count, and Postgres is the only
        # store that was written.
        mock_postgres.complete_ingestion_run.assert_called_once_with(
            organization_id, mock_postgres.create_ingestion_run.return_value, 2
        )

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter")
    @patch("workers.pipeline.ingestion_pipeline.EmbeddingGenerator")
    @patch("workers.pipeline.ingestion_pipeline.SemanticChunker")
    def test_process_files_empty(self, mock_chunker_class, mock_embedgen_class, mock_postgres_class):
        """Test handling of files with no chunks."""
        # Setup mocks
        mock_postgres = Mock()
        mock_postgres.create_ingestion_run.return_value = uuid4()
        mock_postgres_class.return_value = mock_postgres

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
        organization_id = uuid4()
        repository_id = uuid4()
        stats = pipeline.process_files(files, organization_id, repository_id)

        # Should fail gracefully
        assert stats["status"] == "failed"
        assert "No chunks created" in stats.get("error", "")
        assert stats["chunks_created"] == 0


class _FakeWriter:
    """Stands in for PostgresWriter: captures what insert_chunks is given, writes nothing."""

    def __init__(self, *args, **kwargs):
        self.inserted = []

    def create_ingestion_run(self, *args):
        return uuid4()

    def insert_chunks(self, organization_id, chunks, ingestion_run_id, repository_id, embeddings, embedding_model):
        self.inserted.append({"chunks": chunks, "embeddings": embeddings, "embedding_model": embedding_model})
        return {}

    def complete_ingestion_run(self, *args, **kwargs):
        pass

    def close(self):
        pass


class TestTheEmbeddingModel:
    """22.2-07: the model is an argument of IngestionPipeline, ada-002 by
    default, and it is what every stored row records. No Postgres, no OpenAI:
    the writer is a fake and the OpenAI client is patched; the chunker is real."""

    def _pipeline(self, client_class, **kwargs):
        client_class.return_value.count_tokens.return_value = 10
        client_class.return_value.generate_embeddings_batch.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
        return IngestionPipeline(postgres_conn="postgresql://test", openai_api_key="test-key", **kwargs)

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter", _FakeWriter)
    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_the_default_is_ada_002(self, client_class):
        pipeline = self._pipeline(client_class)
        assert pipeline.embedding_gen.model == "text-embedding-ada-002"
        assert client_class.call_args.kwargs["model"] == "text-embedding-ada-002"

    @patch("workers.pipeline.ingestion_pipeline.PostgresWriter", _FakeWriter)
    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_a_given_model_reaches_the_generator_and_the_stored_rows(self, client_class):
        pipeline = self._pipeline(client_class, embedding_model="text-embedding-3-small")
        assert pipeline.embedding_gen.model == "text-embedding-3-small"
        assert client_class.call_args.kwargs["model"] == "text-embedding-3-small"
        stats = pipeline.process_files(
            [("pkg/tool.py", "def run():\n    return 1\n\n\ndef stop():\n    return 0\n", "python")],
            uuid4(), uuid4())
        assert stats["status"] == "success", stats
        (inserted,) = pipeline.postgres.inserted
        assert inserted["chunks"], "the real chunker made chunks"
        assert inserted["embedding_model"] == "text-embedding-3-small"
