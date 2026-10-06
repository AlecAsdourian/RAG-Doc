"""Embedding generator with batch processing and cost optimization."""

import hashlib
import logging
import os
from typing import Dict, List, Optional

from workers.chunker.models import Chunk
from .openai_client import OpenAIEmbeddingClient

logger = logging.getLogger(__name__)


def embedding_text(chunk: Chunk) -> str:
    """The text a chunk is embedded as, before any truncation.

    The breadcrumb (for context), the docstring, then the content. A Go doc
    comment or a JSDoc reaches the embedding only here, as the docstring: it
    sits above the definition, outside the chunk's lines (QD6, QA5).

    The census (`scripts/rag_benchmarks/chunk_census.py`) counts tokens on
    this same function, so it measures what is embedded.
    """
    parts = []

    # Add breadcrumb for context
    breadcrumb = chunk.metadata.get("breadcrumb", "")
    if breadcrumb:
        parts.append(f"# {breadcrumb}")
        parts.append("")  # Blank line

    # Add docstring if present
    docstring = chunk.metadata.get("docstring", "")
    if docstring:
        parts.append(f'"""{docstring}"""')
        parts.append("")

    # Add main content
    parts.append(chunk.content)

    return "\n".join(parts)


class EmbeddingGenerator:
    """Generates embeddings for code chunks with cost optimization."""

    def __init__(
        self,
        api_key: str = None,
        model: str = "text-embedding-ada-002",
        batch_size: int = 100,
        max_tokens_per_chunk: int = 8000,
    ):
        """
        Initialize embedding generator.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Embedding model name. Public as `self.model`: every
                chunk row records the model that produced its vector
                (migration 000017, 22-CONTEXT P4), and writers read the
                name from the generator that made the vectors rather than
                restating it. It stays text-embedding-ada-002 through the
                storage move (U3).
            batch_size: Number of chunks to process per API call
            max_tokens_per_chunk: Maximum tokens per chunk (for cost control)
        """
        if api_key is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError(
                    "OpenAI API key required. Set OPENAI_API_KEY environment variable "
                    "or pass api_key parameter."
                )

        self.model = model
        self.client = OpenAIEmbeddingClient(api_key=api_key, model=model)
        self.batch_size = batch_size
        self.max_tokens_per_chunk = max_tokens_per_chunk

        # Cache for embeddings (content_hash → embedding)
        # In production, this would be a persistent cache (Redis, DB, etc.)
        self.cache: Dict[str, List[float]] = {}

    def _compute_content_hash(self, text: str) -> str:
        """
        Compute hash of content for caching.

        Args:
            text: Text to hash

        Returns:
            SHA256 hash as hex string
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def tokens_over_limit(self, chunk: Chunk) -> Optional[int]:
        """THE truncation rule: is this chunk's embedding text over the limit?

        Returns the token count of `embedding_text(chunk)` when it exceeds
        `max_tokens_per_chunk` -- the generator then embeds it truncated --
        and None when it fits. ⚠ THE ONLY COPY OF THE TEST (22.2-02, QA6):
        `_prepare_text_for_embedding` truncates by it, and the ingest handler
        counts `chunks_truncated` by it, so the count and the truncation
        cannot disagree.
        """
        return self._over_limit(self.client.count_tokens(embedding_text(chunk)))

    def _over_limit(self, tokens: int) -> Optional[int]:
        return tokens if tokens > self.max_tokens_per_chunk else None

    def _prepare_text_for_embedding(self, chunk: Chunk) -> str:
        """
        Prepare chunk text for embedding.

        Includes breadcrumb for context, content, and docstring
        (`embedding_text`), truncated to `max_tokens_per_chunk` when it is
        over (`tokens_over_limit`). A truncation is never silent: one WARNING
        per truncated chunk, naming its path and breadcrumb and the tokens
        before and after -- never its content, which is customer code.

        Args:
            chunk: Chunk to prepare

        Returns:
            Text ready for embedding
        """
        text = embedding_text(chunk)
        tokens = self._over_limit(self.client.count_tokens(text))
        if tokens is not None:
            text = self.client.truncate_to_token_limit(text, self.max_tokens_per_chunk)
            logger.warning(
                "chunk %s (%s) is embedded truncated: its embedding text is %d tokens, "
                "cut to %d (the limit is %d)",
                chunk.file_path,
                chunk.metadata.get("breadcrumb") or chunk.chunk_type,
                tokens,
                self.client.count_tokens(text),
                self.max_tokens_per_chunk,
            )
        return text

    def generate_embeddings_for_chunks(
        self, chunks: List[Chunk], use_cache: bool = True
    ) -> Dict[str, List[float]]:
        """
        Generate embeddings for a list of chunks.

        Args:
            chunks: List of chunks to embed
            use_cache: Whether to use cached embeddings

        Returns:
            Dictionary mapping content_hash → embedding vector
        """
        if not chunks:
            logger.info("No chunks to embed")
            return {}

        logger.info(f"Generating embeddings for {len(chunks)} chunks")

        # Prepare texts and track which need embedding
        chunks_to_embed = []
        chunk_hashes = []
        results = {}

        for chunk in chunks:
            text = self._prepare_text_for_embedding(chunk)
            # Hash the raw content, not the prepared text (for deduplication)
            content_hash = self._compute_content_hash(chunk.content)
            chunk_hashes.append((chunk, text, content_hash))

            # Check cache
            if use_cache and content_hash in self.cache:
                results[content_hash] = self.cache[content_hash]
                logger.debug(f"Using cached embedding for {content_hash[:8]}...")
            else:
                chunks_to_embed.append((chunk, text, content_hash))

        if not chunks_to_embed:
            logger.info("All embeddings retrieved from cache")
            return results

        logger.info(
            f"Need to generate {len(chunks_to_embed)} embeddings "
            f"({len(results)} from cache)"
        )

        # Process in batches
        total_batches = (len(chunks_to_embed) + self.batch_size - 1) // self.batch_size

        for batch_idx in range(0, len(chunks_to_embed), self.batch_size):
            batch = chunks_to_embed[batch_idx : batch_idx + self.batch_size]
            batch_num = batch_idx // self.batch_size + 1

            logger.info(
                f"Processing batch {batch_num}/{total_batches} "
                f"({len(batch)} chunks)..."
            )

            # Extract texts for this batch
            batch_texts = [text for _, text, _ in batch]
            batch_hashes = [content_hash for _, _, content_hash in batch]

            # Generate embeddings
            try:
                embeddings = self.client.generate_embeddings_batch(batch_texts)

                # Store results and update cache
                for content_hash, embedding in zip(batch_hashes, embeddings):
                    results[content_hash] = embedding
                    if use_cache:
                        self.cache[content_hash] = embedding

                logger.info(
                    f"Batch {batch_num}/{total_batches} completed "
                    f"({len(embeddings)} embeddings)"
                )

            except Exception as e:
                logger.error(
                    f"Failed to generate embeddings for batch {batch_num}: {e}"
                )
                raise

        logger.info(
            f"Embedding generation complete: {len(results)} total embeddings "
            f"({len(chunks_to_embed)} generated, {len(results) - len(chunks_to_embed)} cached)"
        )

        return results

    def clear_cache(self):
        """Clear the embedding cache."""
        self.cache.clear()
        logger.info("Embedding cache cleared")
