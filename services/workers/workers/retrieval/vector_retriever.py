"""Vector retrieval using Qdrant and OpenAI embeddings."""

import logging
from typing import Dict, List, Optional
from uuid import UUID

from workers.embeddings.embedding_generator import EmbeddingGenerator
from workers.storage.qdrant_writer import QdrantWriter

logger = logging.getLogger(__name__)


class VectorRetriever:
    """Retrieves code chunks using semantic vector search."""

    def __init__(self, qdrant_url: str, openai_api_key: str):
        """
        Initialize vector retriever.

        Args:
            qdrant_url: Qdrant server URL
            openai_api_key: OpenAI API key for embedding generation
        """
        self.qdrant_writer = QdrantWriter(url=qdrant_url)
        self.embedding_generator = EmbeddingGenerator(api_key=openai_api_key)
        logger.info(f"VectorRetriever initialized with Qdrant at {qdrant_url}")

    def search(
        self,
        query: str,
        repository_id: UUID,
        limit: int = 50,
        run_id: Optional[UUID] = None,
    ) -> List[Dict]:
        """
        Search for similar chunks using semantic vector search.

        Args:
            query: Search query string
            repository_id: Repository UUID to filter results
            limit: Maximum number of results to return
            run_id: Optional run UUID to further filter results

        Returns:
            List of dicts with: chunk_id, file_path, breadcrumb, chunk_type, vector_score
        """
        logger.info(
            f"Vector search: query='{query[:50]}...', repository_id={repository_id}, "
            f"limit={limit}, run_id={run_id}"
        )

        # Generate query embedding
        query_embedding = self.embedding_generator.client.generate_embeddings_batch(
            [query]
        )[0]

        logger.info(f"Generated query embedding: {len(query_embedding)} dimensions")

        # Search Qdrant with repository_id filtering
        results = self.qdrant_writer.search_similar(
            query_embedding=query_embedding,
            limit=limit,
            score_threshold=0.0,  # No threshold, get top N
            repository_id=str(repository_id),
        )

        logger.info(f"Qdrant returned {len(results)} results for repository {repository_id}")

        # Format results
        formatted_results = [
            {
                "chunk_id": result.get("chunk_id"),
                "file_path": result.get("file_path"),
                "breadcrumb": result.get("breadcrumb", ""),
                "chunk_type": result.get("chunk_type", ""),
                "vector_score": result.get("score", 0.0),
            }
            for result in results
        ]

        # Filter by run_id if provided
        if run_id:
            # Note: run_id filtering would require run_id to be stored in Qdrant payload
            # For now, this is a placeholder - run_id is not currently stored in QdrantWriter
            logger.warning(
                "run_id filtering requested but not implemented in Qdrant payload"
            )

        logger.info(f"Returning {len(formatted_results)} vector search results")

        return formatted_results
