"""Tests for embedding generation."""

import logging
from unittest.mock import Mock, patch
import pytest

from workers.chunker.models import Chunk
from workers.embeddings.openai_client import OpenAIEmbeddingClient
from workers.embeddings.embedding_generator import EmbeddingGenerator, embedding_text


class TestOpenAIEmbeddingClient:
    """Test OpenAI client functionality."""

    def test_client_initialization(self):
        """Test client can be instantiated."""
        client = OpenAIEmbeddingClient(api_key="test-key-12345")
        assert client.model == "text-embedding-ada-002"
        assert client.max_retries == 3

    def test_token_counting(self):
        """Test token counting."""
        client = OpenAIEmbeddingClient(api_key="test-key-12345")
        text = "def hello(): return 'world'"
        token_count = client.count_tokens(text)
        assert token_count > 0
        assert isinstance(token_count, int)

    def test_truncate_to_token_limit(self):
        """Test text truncation."""
        client = OpenAIEmbeddingClient(api_key="test-key-12345")
        # Create long text
        long_text = "word " * 10000
        truncated = client.truncate_to_token_limit(long_text, max_tokens=100)
        assert len(truncated) < len(long_text)
        assert client.count_tokens(truncated) <= 100

    @patch("workers.embeddings.openai_client.OpenAI")
    def test_generate_embedding_success(self, mock_openai):
        """Test successful embedding generation."""
        # Mock the API response
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]
        mock_response.usage = Mock(total_tokens=10)

        mock_client = Mock()
        mock_client.embeddings.create.return_value = mock_response
        mock_openai.return_value = mock_client

        client = OpenAIEmbeddingClient(api_key="test-key")
        embedding = client.generate_embedding("test text")

        assert len(embedding) == 1536
        assert all(isinstance(x, float) for x in embedding)

    @patch("workers.embeddings.openai_client.OpenAI")
    def test_generate_embeddings_batch_success(self, mock_openai):
        """Test batch embedding generation."""
        # Mock the API response
        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 1536),
            Mock(embedding=[0.3] * 1536),
        ]
        mock_response.usage = Mock(total_tokens=30)

        mock_client = Mock()
        mock_client.embeddings.create.return_value = mock_response
        mock_openai.return_value = mock_client

        client = OpenAIEmbeddingClient(api_key="test-key")
        embeddings = client.generate_embeddings_batch(["text1", "text2", "text3"])

        assert len(embeddings) == 3
        assert all(len(emb) == 1536 for emb in embeddings)

    def test_empty_text_handling(self):
        """Test handling of empty text."""
        client = OpenAIEmbeddingClient(api_key="test-key")
        # Should not raise, should use placeholder
        token_count = client.count_tokens("")
        assert token_count >= 0


class TestEmbeddingGenerator:
    """Test embedding generator functionality."""

    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_prepare_text_for_embedding(self, mock_client_class):
        """Test text preparation for embedding."""
        # Setup mock
        mock_client = Mock()
        mock_client.count_tokens.return_value = 50
        mock_client_class.return_value = mock_client

        generator = EmbeddingGenerator(api_key="test-key")

        chunk = Chunk(
            content="def hello(): pass",
            file_path="test.py",
            start_line=1,
            end_line=1,
            language="python",
            chunk_type="function",
            metadata={
                "breadcrumb": "test.hello",
                "docstring": "A simple hello function",
            },
        )

        text = generator._prepare_text_for_embedding(chunk)

        # Should include breadcrumb
        assert "test.hello" in text
        # Should include docstring
        assert "A simple hello function" in text
        # Should include content
        assert "def hello(): pass" in text

    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_generate_embeddings_for_chunks(self, mock_client_class):
        """Test embedding generation for chunks."""
        # Setup mock
        mock_client = Mock()
        mock_client.count_tokens.return_value = 50
        mock_client.generate_embeddings_batch.return_value = [
            [0.1] * 1536,
            [0.2] * 1536,
        ]
        mock_client_class.return_value = mock_client

        generator = EmbeddingGenerator(api_key="test-key")

        chunks = [
            Chunk(
                content="def foo(): pass",
                file_path="test.py",
                start_line=1,
                end_line=1,
                language="python",
                chunk_type="function",
                metadata={"breadcrumb": "test.foo"},
            ),
            Chunk(
                content="def bar(): pass",
                file_path="test.py",
                start_line=3,
                end_line=3,
                language="python",
                chunk_type="function",
                metadata={"breadcrumb": "test.bar"},
            ),
        ]

        embeddings = generator.generate_embeddings_for_chunks(chunks, use_cache=False)

        assert len(embeddings) == 2
        assert all(len(emb) == 1536 for emb in embeddings.values())

    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_caching(self, mock_client_class):
        """Test embedding caching."""
        # Setup mock
        mock_client = Mock()
        mock_client.count_tokens.return_value = 50
        mock_client.generate_embeddings_batch.return_value = [[0.1] * 1536]
        mock_client_class.return_value = mock_client

        generator = EmbeddingGenerator(api_key="test-key")

        chunk = Chunk(
            content="def foo(): pass",
            file_path="test.py",
            start_line=1,
            end_line=1,
            language="python",
            chunk_type="function",
            metadata={"breadcrumb": "test.foo"},
        )

        # First call - should hit API
        embeddings1 = generator.generate_embeddings_for_chunks([chunk], use_cache=True)
        assert mock_client.generate_embeddings_batch.call_count == 1

        # Second call - should use cache
        embeddings2 = generator.generate_embeddings_for_chunks([chunk], use_cache=True)
        assert mock_client.generate_embeddings_batch.call_count == 1  # Still 1

        # Results should be the same
        assert list(embeddings1.keys()) == list(embeddings2.keys())

    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_batch_processing(self, mock_client_class):
        """Test batch processing with multiple batches."""
        # Setup mock that returns embeddings matching batch size
        mock_client = Mock()
        mock_client.count_tokens.return_value = 50

        # Mock to return correct number of embeddings per batch
        def mock_batch_embeddings(texts):
            return [[0.1 + i * 0.01] * 1536 for i in range(len(texts))]

        mock_client.generate_embeddings_batch.side_effect = mock_batch_embeddings
        mock_client_class.return_value = mock_client

        # Use small batch size
        generator = EmbeddingGenerator(api_key="test-key", batch_size=2)

        # Create 5 chunks (should result in 3 batches: 2, 2, 1)
        chunks = [
            Chunk(
                content=f"def func{i}(): pass",
                file_path="test.py",
                start_line=i,
                end_line=i,
                language="python",
                chunk_type="function",
                metadata={"breadcrumb": f"test.func{i}"},
            )
            for i in range(5)
        ]

        embeddings = generator.generate_embeddings_for_chunks(chunks, use_cache=False)

        # Should make 3 API calls (3 batches)
        assert mock_client.generate_embeddings_batch.call_count == 3
        assert len(embeddings) == 5

    @patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient")
    def test_empty_chunks_list(self, mock_client_class):
        """Test handling of empty chunks list."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        generator = EmbeddingGenerator(api_key="test-key")
        embeddings = generator.generate_embeddings_for_chunks([])

        assert len(embeddings) == 0
        assert mock_client.generate_embeddings_batch.call_count == 0


class TestTruncationIsVisible:
    """QA6 (22.2-02): a chunk over the token limit is truncated by ONE rule
    (`tokens_over_limit`), and each truncation is logged -- by path and
    breadcrumb, never by content.

    The tokenizer here is the real one (tiktoken); only the API is mocked.
    """

    SENTINEL = "TruncS3ntinelCustomerCode"

    def _generator(self, mock_openai, limit):
        mock_openai.return_value.embeddings.create.side_effect = lambda model, input: Mock(
            data=[Mock(embedding=[0.1] * 1536) for _ in input], usage=Mock(total_tokens=0)
        )
        return EmbeddingGenerator(api_key="test-key", max_tokens_per_chunk=limit)

    def _chunk(self, words):
        return Chunk(
            content=" ".join(f"{self.SENTINEL}{i}" for i in range(words)),
            file_path="pkg/big.py",
            start_line=1,
            end_line=1,
            language="python",
            chunk_type="function",
            metadata={"breadcrumb": "Big.method"},
        )

    @patch("workers.embeddings.openai_client.OpenAI")
    def test_the_rule_names_an_over_limit_chunk_and_passes_one_that_fits(self, mock_openai):
        generator = self._generator(mock_openai, limit=50)
        big, small = self._chunk(200), self._chunk(2)
        tokens = generator.tokens_over_limit(big)
        assert tokens is not None and tokens > 50
        assert tokens == generator.client.count_tokens(embedding_text(big))
        assert generator.tokens_over_limit(small) is None

    @patch("workers.embeddings.openai_client.OpenAI")
    def test_an_over_limit_chunk_is_truncated_and_logged_without_its_content(self, mock_openai, caplog):
        caplog.set_level(logging.DEBUG)
        generator = self._generator(mock_openai, limit=50)
        big = self._chunk(200)

        generator.generate_embeddings_for_chunks([big, self._chunk(2)], use_cache=False)

        [sent] = [call.kwargs["input"] for call in mock_openai.return_value.embeddings.create.call_args_list]
        assert generator.client.count_tokens(sent[0]) <= 50, "the over-limit text was sent truncated"
        assert sent[1] == embedding_text(self._chunk(2)), "a text that fits is sent whole"

        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "workers.embeddings.embedding_generator" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, warnings
        assert "pkg/big.py" in warnings[0] and "Big.method" in warnings[0]
        assert f"{generator.tokens_over_limit(big)} tokens, cut to" in warnings[0]
        assert all(self.SENTINEL not in r.getMessage() for r in caplog.records), (
            "a chunk's content reached a log record"
        )
