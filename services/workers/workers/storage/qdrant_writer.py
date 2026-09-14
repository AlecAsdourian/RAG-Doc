"""Qdrant writer for storing vector embeddings."""

import logging
from typing import Dict, List
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams

logger = logging.getLogger(__name__)

# Points per upsert request. A 1536-dimension point serialises to roughly 34 KB of
# JSON, and Qdrant drops the connection on a request body over its
# `max_request_size_mb` (32 MB by default) without logging anything. Until
# 2026-09-13 every point went in one request, so an ingestion of more than
# somewhere between 1,000 and 1,500 chunks failed outright: a 335-file Go
# repository (2,122 vectors) could not be indexed at all. 256 points is ~9 MB.
UPSERT_BATCH_SIZE = 256


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
        batch_size: int = UPSERT_BATCH_SIZE,
    ) -> int:
        """
        Upsert embeddings to Qdrant, at most `batch_size` points per request.

        Args:
            chunk_embeddings: Map of chunk_id → embedding vector
            chunk_metadata: Map of chunk_id → metadata dict
                Expected keys: chunk_id, repository_id, file_path, language
            batch_size: Points per request (see UPSERT_BATCH_SIZE)

        Returns:
            Number of vectors inserted

        Raises:
            ValueError: If batch_size is less than 1.
            Exception: Whatever the client raised for a failed batch, re-raised
                after the points this call had already written are deleted.
        """
        if not chunk_embeddings:
            logger.info("No embeddings to upsert")
            return 0
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

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

        # Upsert in bounded requests. If one fails, delete what this call already
        # wrote before re-raising: vector search ignores run status (ISS-027), so a
        # failed run's partial vectors would otherwise stay searchable.
        written: List[str] = []
        try:
            for start in range(0, len(points), batch_size):
                batch = points[start : start + batch_size]
                self.client.upsert(collection_name=self.collection_name, points=batch)
                written.extend(point.id for point in batch)
        except Exception:
            if written:
                try:
                    self.client.delete(
                        collection_name=self.collection_name,
                        points_selector=PointIdsList(points=written),
                    )
                except Exception as cleanup_error:
                    logger.error(
                        f"Could not delete {len(written)} vectors after a failed upsert: {cleanup_error}"
                    )
            raise

        requests = (len(points) + batch_size - 1) // batch_size
        logger.info(f"Upserted {len(points)} embeddings to Qdrant in {requests} requests")
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
