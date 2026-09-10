"""Semantic cache for query similarity-based LLM response caching."""

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import numpy as np
import redis

from workers.embeddings import EmbeddingGenerator

logger = logging.getLogger(__name__)


class SemanticCache:
    """
    Semantic cache using Redis and vector similarity for query deduplication.

    Reduces LLM API costs by ~40% through similarity-based cache hits.
    Uses cosine similarity with 0.95 threshold to balance hit rate vs accuracy.
    """

    def __init__(
        self,
        redis_url: str,
        embedding_generator: EmbeddingGenerator,
        similarity_threshold: float = 0.95,
        ttl: int = 3600,  # 1 hour
    ):
        """
        Initialize semantic cache.

        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379)
            embedding_generator: EmbeddingGenerator for query embeddings
            similarity_threshold: Minimum cosine similarity for cache hit (default: 0.95)
            ttl: Time-to-live for cached entries in seconds (default: 3600)
        """
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        self.embedding_generator = embedding_generator
        self.similarity_threshold = similarity_threshold
        self.ttl = ttl

        logger.info(
            f"SemanticCache initialized: threshold={similarity_threshold}, ttl={ttl}s"
        )

    def get_cached_response(
        self,
        query: str,
        query_embedding: List[float],
        organization_id: UUID,
        repository_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached response if a similar query exists FOR THIS TENANT.

        Args:
            query: Query text
            query_embedding: Embedding vector for query
            organization_id: Owning organization. Part of the cache key --
                see the note on key format in `cache_response`.
            repository_id: Repository UUID for cache scoping

        Returns:
            Cached response dict if found, None otherwise
        """
        try:
            # Scan only this tenant's entries for this repository. The
            # organization segment is what keeps a caller who holds someone
            # else's repository_id from reading their cached answers -- this
            # layer returns before Postgres is touched, so RLS is not a
            # backstop here. See ISS-020.
            pattern = f"cache:query:*:{str(organization_id)}:{str(repository_id)}"
            cached_keys = list(self.redis_client.scan_iter(match=pattern))

            if not cached_keys:
                logger.debug("No cached queries found for repository")
                return None

            logger.debug(f"Scanning {len(cached_keys)} cached queries")

            # Find most similar cached query
            best_similarity = 0.0
            best_key = None

            for key in cached_keys:
                # Get cached entry
                cached_data = self.redis_client.hgetall(key)

                if not cached_data or "embedding" not in cached_data:
                    continue

                # Belt and braces. The key pattern above should already have
                # excluded other tenants; this re-checks the stored value so a
                # future change to the key format cannot silently reopen
                # ISS-020. Cheap, and we have gotten this wrong once.
                if cached_data.get("organization_id") != str(organization_id):
                    logger.warning(
                        "Skipping cache entry whose organization does not match "
                        "the requesting tenant; key format may have drifted"
                    )
                    continue

                # Parse cached embedding
                try:
                    cached_embedding = json.loads(cached_data["embedding"])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid embedding JSON in key {key}")
                    continue

                # Calculate cosine similarity
                similarity = self._cosine_similarity(
                    query_embedding, cached_embedding
                )

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_key = key

            # Check if best similarity meets threshold
            if best_similarity >= self.similarity_threshold:
                logger.info(
                    f"Cache HIT! Similarity={best_similarity:.3f} "
                    f"(threshold={self.similarity_threshold})"
                )

                # Retrieve and parse cached response
                cached_data = self.redis_client.hgetall(best_key)
                response = json.loads(cached_data["response"])

                return response
            else:
                logger.debug(
                    f"Cache MISS: Best similarity={best_similarity:.3f} "
                    f"< threshold={self.similarity_threshold}"
                )
                return None

        except Exception as e:
            logger.error(f"Error checking cache: {e}")
            # On cache error, return None to fall through to LLM
            return None

    def cache_response(
        self,
        query: str,
        query_embedding: List[float],
        organization_id: UUID,
        repository_id: UUID,
        response: Dict[str, Any],
    ):
        """
        Cache a response for future similar queries, scoped to one tenant.

        Args:
            query: Query text
            query_embedding: Embedding vector for query
            organization_id: Owning organization
            repository_id: Repository UUID for cache scoping
            response: Response dict to cache
        """
        try:
            # KEY FORMAT: cache:query:{hash}:{organization_id}:{repository_id}
            #
            # The organization sits between the hash and the repository so the
            # read path can keep scanning with the hash wildcarded while still
            # pinning both tenant and repository as an exact suffix.
            #
            # Every scan pattern in this file must include the organization
            # segment. Miss one and it silently stops matching, which breaks
            # invalidation rather than raising -- see ISS-020.
            query_hash = self._hash_query(query)
            cache_key = (
                f"cache:query:{query_hash}:"
                f"{str(organization_id)}:{str(repository_id)}"
            )

            # Prepare cache entry
            cache_entry = {
                "query": query,
                "embedding": json.dumps(query_embedding),
                "response": json.dumps(response),
                "organization_id": str(organization_id),
                "repository_id": str(repository_id),
                "timestamp": str(int(time.time())),
            }

            # Store in Redis with TTL
            self.redis_client.hset(cache_key, mapping=cache_entry)
            self.redis_client.expire(cache_key, self.ttl)

            logger.info(f"Cached response for query (ttl={self.ttl}s)")

        except Exception as e:
            logger.error(f"Error caching response: {e}")
            # Don't fail on cache errors, just log

    def clear_cache(
        self,
        organization_id: Optional[UUID] = None,
        repository_id: Optional[UUID] = None,
        all_tenants: bool = False,
    ):
        """
        Clear cached entries.

        Args:
            organization_id: Organization to clear. Required whenever
                `repository_id` is given -- a repository id alone no longer
                identifies a key, and matching on it without the tenant would
                reach across organizations.
            repository_id: Optional repository UUID to clear within the org.
            all_tenants: Flush EVERY tenant's entries. Must be passed
                explicitly; there is no way to reach a global flush by omitting
                arguments.

        Raises:
            ValueError: if no `organization_id` is given without
                `all_tenants=True`, or if `all_tenants` is combined with a scope.
        """
        # A GLOBAL FLUSH IS OPT-IN, NOT A FALLTHROUGH.
        #
        # The first version of this guard checked `organization_id is None`
        # while the branches below tested truthiness. An empty string passed
        # the guard, failed every branch, and fell through to `cache:query:*`
        # -- deleting every tenant's entries. Found in review, reproduced.
        #
        # Now: anything falsy is refused, and the global pattern is only
        # reachable by asking for it by name.
        if all_tenants:
            if organization_id or repository_id:
                raise ValueError(
                    "all_tenants=True cannot be combined with organization_id "
                    "or repository_id"
                )
        elif not organization_id:
            raise ValueError(
                "clear_cache requires organization_id, or all_tenants=True for a "
                "deliberate global flush (ISS-020)"
            )
        try:
            if all_tenants:
                pattern = "cache:query:*"
            elif repository_id:
                pattern = (
                    f"cache:query:*:{str(organization_id)}:{str(repository_id)}"
                )
            else:
                pattern = f"cache:query:*:{str(organization_id)}:*"

            keys = list(self.redis_client.scan_iter(match=pattern))

            if keys:
                self.redis_client.delete(*keys)
                logger.info(f"Cleared {len(keys)} cache entries")
            else:
                logger.info("No cache entries to clear")

        except Exception as e:
            logger.error(f"Error clearing cache: {e}")

    def _cosine_similarity(
        self, vec1: List[float], vec2: List[float]
    ) -> float:
        """
        Calculate cosine similarity between two vectors.

        Args:
            vec1: First vector
            vec2: Second vector

        Returns:
            Cosine similarity in range [-1, 1]
        """
        # Convert to numpy arrays
        v1 = np.array(vec1)
        v2 = np.array(vec2)

        # Calculate cosine similarity
        dot_product = np.dot(v1, v2)
        norm_product = np.linalg.norm(v1) * np.linalg.norm(v2)

        if norm_product == 0:
            return 0.0

        return float(dot_product / norm_product)

    def _hash_query(self, query: str) -> str:
        """
        Generate hash for query text.

        Args:
            query: Query text

        Returns:
            SHA256 hash as hex string
        """
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

    def get_cache_stats(
        self,
        organization_id: Optional[UUID] = None,
        repository_id: Optional[UUID] = None,
        all_tenants: bool = False,
    ) -> Dict[str, int]:
        """
        Get cache statistics.

        Args:
            organization_id: Organization to filter by. Required whenever
                `repository_id` is given.
            repository_id: Optional repository UUID to filter

        Returns:
            Dict with cache stats

        Raises:
            ValueError: if `repository_id` is given without `organization_id`.
        """
        # Same guard as clear_cache, for the same reason -- a falsy
        # organization_id must not fall through to the all-tenants pattern.
        if all_tenants:
            if organization_id or repository_id:
                raise ValueError(
                    "all_tenants=True cannot be combined with organization_id "
                    "or repository_id"
                )
        elif not organization_id:
            raise ValueError(
                "get_cache_stats requires organization_id, or all_tenants=True "
                "(ISS-020)"
            )
        try:
            if all_tenants:
                pattern = "cache:query:*"
            elif repository_id:
                pattern = (
                    f"cache:query:*:{str(organization_id)}:{str(repository_id)}"
                )
            else:
                pattern = f"cache:query:*:{str(organization_id)}:*"

            keys = list(self.redis_client.scan_iter(match=pattern))

            return {
                "total_entries": len(keys),
                "repository_id": str(repository_id) if repository_id else "all",
            }

        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {"total_entries": 0, "error": str(e)}
