"""Qdrant writer for storing vector embeddings."""

import logging
from typing import Dict, List
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger(__name__)


class QdrantWriter:
    """Writes embeddings to Qdrant vector database."""

    def __init__(self, url: str = "http://localhost:6333", collection_name: str = "code_embeddings"):
        """
        Initialize Qdrant writer.

        Args:
            url: Qdrant server URL
            collection_name: Name of collection to use
        """
        self.client = QdrantClient(url=url)
        self.collection_name = collection_name
        logger.info(f"Connected to Qdrant at {url}")

    def ensure_collection_exists(self):
        """
        Ensure the collection exists, create if not.

        Creates collection with:
        - 1536 dimensions (OpenAI ada-002)
        - Cosine distance metric
        """
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.collection_name not in collection_names:
            logger.info(f"Creating collection: {self.collection_name}")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
            )
            logger.info(f"Collection {self.collection_name} created")
        else:
            logger.info(f"Collection {self.collection_name} already exists")

    def upsert_embeddings(
        self,
        chunk_embeddings: Dict[UUID, List[float]],
        chunk_metadata: Dict[UUID, Dict],
    ) -> int:
        """
        Upsert embeddings to Qdrant.

        Args:
            chunk_embeddings: Map of chunk_id → embedding vector
            chunk_metadata: Map of chunk_id → metadata dict
                Expected keys: chunk_id, repository_id, file_path, language

        Returns:
            Number of vectors inserted
        """
        if not chunk_embeddings:
            logger.info("No embeddings to upsert")
            return 0

        # Ensure collection exists
        self.ensure_collection_exists()

        # Prepare points for batch upsert
        points = []
        for chunk_id, embedding in chunk_embeddings.items():
            metadata = chunk_metadata.get(chunk_id, {})

            # Qdrant point
            point = PointStruct(
                id=str(chunk_id),  # Use chunk_id as point ID
                vector=embedding,
                payload={
                    "chunk_id": str(chunk_id),
                    "repository_id": str(metadata.get("repository_id", "")),
                    "file_path": metadata.get("file_path", ""),
                    "language": metadata.get("language", ""),
                    "chunk_type": metadata.get("chunk_type", ""),
                    "breadcrumb": metadata.get("breadcrumb", ""),
                },
            )
            points.append(point)

        # Batch upsert
        self.client.upsert(collection_name=self.collection_name, points=points)

        logger.info(f"Upserted {len(points)} embeddings to Qdrant")
        return len(points)

    def search_similar(
        self,
        query_embedding: List[float],
        limit: int = 5,
        score_threshold: float = 0.0,
        repository_id: str = None,
    ) -> List[Dict]:
        """
        Search for similar chunks.

        Args:
            query_embedding: Query vector (1536-dim)
            limit: Maximum number of results
            score_threshold: Minimum similarity score
            repository_id: Optional repository ID to filter results

        Returns:
            List of search results with payload and score
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        # Build query filter if repository_id provided
        query_filter = None
        if repository_id:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="repository_id",
                        match=MatchValue(value=repository_id),
                    )
                ]
            )

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_embedding,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=query_filter,
        ).points

        # Convert to dict format
        return [
            {
                "chunk_id": result.payload.get("chunk_id"),
                "file_path": result.payload.get("file_path"),
                "language": result.payload.get("language"),
                "chunk_type": result.payload.get("chunk_type"),
                "breadcrumb": result.payload.get("breadcrumb"),
                "score": result.score,
            }
            for result in results
        ]
