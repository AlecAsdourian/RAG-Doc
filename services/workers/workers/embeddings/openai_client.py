"""OpenAI API client for generating embeddings."""

import logging
import time
from typing import List

import tiktoken
from openai import OpenAI, RateLimitError, APIError, AuthenticationError

logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient:
    """Client for OpenAI embeddings API with retry logic and token counting."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-ada-002",
        max_retries: int = 3,
    ):
        """
        Initialize OpenAI embedding client.

        Args:
            api_key: OpenAI API key
            model: Embedding model name (default: text-embedding-ada-002)
            max_retries: Maximum number of retry attempts for rate limiting
        """
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.max_retries = max_retries

        # Initialize tokenizer for counting
        try:
            self.tokenizer = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback to cl100k_base if model not found
            logger.warning(f"Model {model} not found in tiktoken, using cl100k_base")
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        # Token limit for ada-002
        self.max_tokens = 8191

    def count_tokens(self, text: str) -> int:
        """
        Count tokens in text.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        return len(self.tokenizer.encode(text))

    def truncate_to_token_limit(self, text: str, max_tokens: int = None) -> str:
        """
        Truncate text to token limit.

        Args:
            text: Text to truncate
            max_tokens: Maximum tokens (defaults to model limit)

        Returns:
            Truncated text
        """
        if max_tokens is None:
            max_tokens = self.max_tokens

        tokens = self.tokenizer.encode(text)

        if len(tokens) <= max_tokens:
            return text

        # Truncate and decode
        truncated_tokens = tokens[:max_tokens]
        truncated_text = self.tokenizer.decode(truncated_tokens)

        logger.warning(
            f"Text truncated from {len(tokens)} to {max_tokens} tokens "
            f"({len(text)} to {len(truncated_text)} chars)"
        )

        return truncated_text

    def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (1536 dimensions for ada-002)

        Raises:
            AuthenticationError: Invalid API key
            APIError: API request failed after retries
        """
        if not text or not text.strip():
            logger.warning("Empty text provided for embedding, using placeholder")
            text = "[empty]"

        # Count and truncate tokens
        token_count = self.count_tokens(text)
        if token_count > self.max_tokens:
            text = self.truncate_to_token_limit(text)
            token_count = self.count_tokens(text)

        logger.debug(f"Generating embedding for text ({token_count} tokens)")

        # Generate embedding with retry logic
        for attempt in range(self.max_retries):
            try:
                response = self.client.embeddings.create(
                    model=self.model, input=text
                )

                embedding = response.data[0].embedding

                # Log token usage and cost
                tokens_used = response.usage.total_tokens
                cost = tokens_used / 1_000_000 * 0.10  # $0.10 per 1M tokens
                logger.info(
                    f"Embedding generated: {tokens_used} tokens, "
                    f"~${cost:.6f} cost"
                )

                return embedding

            except RateLimitError as e:
                if attempt < self.max_retries - 1:
                    delay = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Rate limit exceeded after {self.max_retries} retries")
                    raise

            except AuthenticationError as e:
                logger.error(f"Authentication failed: Invalid API key")
                raise

            except APIError as e:
                if attempt < self.max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        f"API error (attempt {attempt + 1}/{self.max_retries}): {e}, "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"API error after {self.max_retries} retries: {e}")
                    raise

        # Should not reach here, but for type safety
        raise APIError("Failed to generate embedding after all retries")

    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts in a batch.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors

        Raises:
            AuthenticationError: Invalid API key
            APIError: API request failed after retries
        """
        if not texts:
            return []

        # Filter empty texts and truncate long ones
        processed_texts = []
        for i, text in enumerate(texts):
            if not text or not text.strip():
                logger.warning(f"Empty text at index {i}, using placeholder")
                processed_texts.append("[empty]")
            else:
                token_count = self.count_tokens(text)
                if token_count > self.max_tokens:
                    text = self.truncate_to_token_limit(text)
                processed_texts.append(text)

        # Count total tokens
        total_tokens = sum(self.count_tokens(t) for t in processed_texts)
        logger.info(
            f"Generating batch embeddings for {len(processed_texts)} texts "
            f"({total_tokens} total tokens)"
        )

        # Generate embeddings with retry logic
        for attempt in range(self.max_retries):
            try:
                response = self.client.embeddings.create(
                    model=self.model, input=processed_texts
                )

                embeddings = [item.embedding for item in response.data]

                # Log token usage and cost
                tokens_used = response.usage.total_tokens
                cost = tokens_used / 1_000_000 * 0.10  # $0.10 per 1M tokens
                logger.info(
                    f"Batch embeddings generated: {len(embeddings)} embeddings, "
                    f"{tokens_used} tokens, ~${cost:.6f} cost"
                )

                return embeddings

            except RateLimitError as e:
                if attempt < self.max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Rate limit exceeded after {self.max_retries} retries")
                    raise

            except AuthenticationError as e:
                logger.error(f"Authentication failed: Invalid API key")
                raise

            except APIError as e:
                if attempt < self.max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        f"API error (attempt {attempt + 1}/{self.max_retries}): {e}, "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"API error after {self.max_retries} retries: {e}")
                    raise

        # Should not reach here, but for type safety
        raise APIError("Failed to generate embeddings after all retries")
