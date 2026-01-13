"""Answer generator with RAG prompting and retry handling."""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

import tiktoken
from openai import OpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception_type,
)

from workers.retrieval.query_engine import QueryEngine

logger = logging.getLogger(__name__)


class AnswerGenerator:
    """
    Orchestrates QueryEngine → LLM pipeline for RAG-based codebase Q&A.

    Features:
    - RAG prompting with explicit grounding and citation requirements
    - Token counting with pre-flight context management
    - Retry handling with exponential backoff for rate limits
    - Cost calculation for observability
    - Optional semantic caching integration
    """

    # Cost per million tokens (GPT-4o Mini)
    COST_PER_MILLION_INPUT = 0.15
    COST_PER_MILLION_OUTPUT = 0.60

    def __init__(
        self,
        query_engine: QueryEngine,
        openai_api_key: str,
        model: str = "gpt-4o-mini",
        semantic_cache: Optional[Any] = None,
        max_context_tokens: int = 100000,  # Safe margin for 128K window
    ):
        """
        Initialize Answer Generator.

        Args:
            query_engine: QueryEngine for retrieving relevant chunks
            openai_api_key: OpenAI API key
            model: Model to use (default: gpt-4o-mini for cost efficiency)
            semantic_cache: Optional SemanticCache instance
            max_context_tokens: Maximum tokens for context (default: 100K)
        """
        self.query_engine = query_engine
        self.model = model
        self.semantic_cache = semantic_cache
        self.max_context_tokens = max_context_tokens

        # Initialize OpenAI client
        self.client = OpenAI(api_key=openai_api_key)

        # Initialize tokenizer for token counting
        try:
            self.tokenizer = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback to cl100k_base for newer models
            logger.warning(f"No tokenizer found for {model}, using cl100k_base")
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        logger.info(f"AnswerGenerator initialized with model={model}")

    def generate(
        self,
        query: str,
        repository_id: UUID,
        top_k: int = 5,
        run_id: Optional[UUID] = None,
    ) -> Dict[str, Any]:
        """
        Generate an answer to a query using RAG pipeline.

        Args:
            query: User's question
            repository_id: Repository UUID to search
            top_k: Number of chunks to retrieve (default: 5)
            run_id: Optional specific ingestion run to search

        Returns:
            Dict with:
                - answer: Generated answer
                - model: Model used
                - prompt_tokens: Number of input tokens
                - completion_tokens: Number of output tokens
                - total_cost: Total cost in USD
                - cache_hit: Whether answer came from cache (if cache enabled)
                - chunks_retrieved: Number of chunks retrieved
                - sources: List of source chunks with citations
        """
        logger.info(f"Generating answer for query: {query}")

        # Check semantic cache if enabled
        if self.semantic_cache:
            # Generate embedding for query
            from workers.embeddings import EmbeddingGenerator

            embedding_gen = EmbeddingGenerator(
                api_key=self.client.api_key
            )
            query_embedding = embedding_gen.generate_embeddings_for_chunks(
                [type('Chunk', (), {'content': query, 'metadata': {}})()]
            )

            # Extract the embedding vector
            if query_embedding:
                embedding_vector = list(query_embedding.values())[0]

                # Check cache
                cached_response = self.semantic_cache.get_cached_response(
                    query=query,
                    query_embedding=embedding_vector,
                    repository_id=repository_id,
                )

                if cached_response:
                    logger.info("Cache hit! Returning cached response")
                    cached_response["cache_hit"] = True
                    cached_response["total_cost"] = 0.0
                    return cached_response

        # Cache miss - retrieve chunks
        retrieval_result = self.query_engine.query(
            query_text=query,
            repository_id=repository_id,
            top_k=top_k,
            run_id=run_id,
        )

        chunks = retrieval_result.get("results", [])
        logger.info(f"Retrieved {len(chunks)} chunks")

        if not chunks:
            # No chunks found - return "don't know" response
            return {
                "answer": "I don't have enough information to answer that question. The query didn't match any code in the repository.",
                "model": self.model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_cost": 0.0,
                "cache_hit": False,
                "chunks_retrieved": 0,
                "sources": [],
            }

        # Build context from chunks
        context, sources = self._build_context(chunks)

        # Build prompt
        system_message, user_message = self._build_prompt(context, query)

        # Count tokens
        prompt_tokens = self._count_tokens(system_message + user_message)
        logger.info(f"Prompt tokens: {prompt_tokens}")

        # Check if we need to truncate context
        if prompt_tokens > self.max_context_tokens:
            logger.warning(
                f"Prompt exceeds max context ({prompt_tokens} > {self.max_context_tokens}), "
                f"truncating chunks"
            )
            context, sources = self._truncate_context(chunks, query)
            system_message, user_message = self._build_prompt(context, query)
            prompt_tokens = self._count_tokens(system_message + user_message)
            logger.info(f"Truncated prompt tokens: {prompt_tokens}")

        # Call LLM with retry handling
        response = self._call_llm_with_retry(system_message, user_message)

        # Extract response data
        answer = response.choices[0].message.content
        completion_tokens = response.usage.completion_tokens
        total_tokens = response.usage.total_tokens

        # Calculate cost
        total_cost = self._calculate_cost(prompt_tokens, completion_tokens)

        result = {
            "answer": answer,
            "model": self.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_cost": total_cost,
            "cache_hit": False,
            "chunks_retrieved": len(chunks),
            "sources": sources,
        }

        # Cache the response if cache enabled
        if self.semantic_cache and 'embedding_vector' in locals():
            self.semantic_cache.cache_response(
                query=query,
                query_embedding=embedding_vector,
                repository_id=repository_id,
                response=result,
            )
            logger.info("Response cached for future queries")

        logger.info(
            f"Answer generated: {completion_tokens} tokens, "
            f"${total_cost:.4f} cost"
        )

        return result

    def _build_context(self, chunks: List[Dict]) -> tuple[str, List[Dict]]:
        """
        Build context string from retrieved chunks with citations.

        Args:
            chunks: List of retrieved chunks with metadata

        Returns:
            Tuple of (context_string, sources_list)
        """
        context_parts = []
        sources = []

        for idx, chunk in enumerate(chunks, start=1):
            file_path = chunk.get("file_path", "unknown")
            start_line = chunk.get("start_line", 0)
            content = chunk.get("content", "")
            breadcrumb = chunk.get("breadcrumb", "")

            # Build citation header
            citation = f"[{idx}] {file_path}:{start_line}"
            if breadcrumb:
                citation += f" ({breadcrumb})"

            # Add to context
            context_parts.append(f"{citation}\n```\n{content}\n```\n")

            # Track source metadata
            sources.append({
                "number": idx,
                "file_path": file_path,
                "start_line": start_line,
                "end_line": chunk.get("end_line", 0),
                "breadcrumb": breadcrumb,
                "chunk_type": chunk.get("chunk_type", "code"),
            })

        context = "\n".join(context_parts)
        return context, sources

    def _build_prompt(self, context: str, query: str) -> tuple[str, str]:
        """
        Build system and user messages for RAG prompting.

        Args:
            context: Context string with code chunks
            query: User's question

        Returns:
            Tuple of (system_message, user_message)
        """
        # System message (cached by OpenAI automatically)
        system_message = (
            "You are a code documentation assistant. "
            "Answer questions using ONLY the provided code context. "
            "Cite sources with [number] notation. "
            "If the answer isn't in the context, say 'I don't have enough information to answer that'. "
            "Do not add information from your training data."
        )

        # User message with context and query
        user_message = f"""Code Context:

{context}

Question: {query}

Provide a clear, accurate answer based only on the code context above. Cite your sources using [number] notation."""

        return system_message, user_message

    def _count_tokens(self, text: str) -> int:
        """
        Count tokens in text using tiktoken.

        Args:
            text: Text to count

        Returns:
            Number of tokens
        """
        return len(self.tokenizer.encode(text))

    def _truncate_context(
        self,
        chunks: List[Dict],
        query: str,
        target_tokens: Optional[int] = None,
    ) -> tuple[str, List[Dict]]:
        """
        Truncate context to fit within token budget.

        Strategy: Keep removing the lowest-scoring chunk until we fit.

        Args:
            chunks: List of chunks sorted by score (highest first)
            query: User's query
            target_tokens: Target token count (default: max_context_tokens)

        Returns:
            Tuple of (truncated_context, sources_list)
        """
        if target_tokens is None:
            target_tokens = self.max_context_tokens

        # Start with all chunks and remove lowest-scoring until we fit
        working_chunks = chunks.copy()

        while len(working_chunks) > 0:
            context, sources = self._build_context(working_chunks)
            system_message, user_message = self._build_prompt(context, query)
            tokens = self._count_tokens(system_message + user_message)

            if tokens <= target_tokens:
                logger.info(
                    f"Truncated to {len(working_chunks)} chunks "
                    f"({tokens} tokens)"
                )
                return context, sources

            # Remove last chunk (lowest score)
            working_chunks.pop()

        # Edge case: even 0 chunks is too big (shouldn't happen)
        logger.error("Cannot fit context even with 0 chunks")
        return "", []

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        retry=retry_if_exception_type(Exception),
    )
    def _call_llm_with_retry(self, system_message: str, user_message: str):
        """
        Call OpenAI API with exponential backoff retry.

        Args:
            system_message: System message
            user_message: User message

        Returns:
            OpenAI ChatCompletion response

        Raises:
            Exception: After 6 failed attempts
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,  # Deterministic for factual answers
            )
            return response
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    def _calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """
        Calculate cost in USD for the API call.

        Args:
            prompt_tokens: Number of input tokens
            completion_tokens: Number of output tokens

        Returns:
            Total cost in USD
        """
        input_cost = (prompt_tokens / 1_000_000) * self.COST_PER_MILLION_INPUT
        output_cost = (completion_tokens / 1_000_000) * self.COST_PER_MILLION_OUTPUT
        return input_cost + output_cost
