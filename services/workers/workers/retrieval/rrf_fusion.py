"""Reciprocal Rank Fusion (RRF) algorithm for merging ranked result lists."""

import logging
from collections import defaultdict
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


class RRFFusion:
    """
    Reciprocal Rank Fusion algorithm for combining multiple ranked result lists.

    RRF merges results from different retrieval systems (e.g., FTS and vector search)
    into a single ranked list. Chunks appearing in multiple systems receive boosted
    scores (overlap boosting).

    Formula: score(chunk) = Σ(1 / (k + rank_i)) across all systems
    Where:
    - k = 60 (standard constant, balances rank contributions)
    - rank_i = position in result list (1-indexed: 1st place = rank 1)
    - Σ sums across all systems where the chunk appears

    Example:
        >>> fusion = RRFFusion()
        >>> results = {
        ...     "fts": [{"chunk_id": "A"}, {"chunk_id": "B"}],
        ...     "vector": [{"chunk_id": "B"}, {"chunk_id": "C"}]
        ... }
        >>> output = fusion.fuse(results)
        >>> # B appears in both systems, so it has the highest score
        >>> output[0]["chunk_id"]
        'B'
    """

    def fuse(self, results: Dict[str, List[Dict]], k: int = 60) -> List[Dict]:
        """
        Fuse multiple ranked result lists using RRF algorithm.

        Args:
            results: Dictionary mapping system names to ranked result lists.
                    Each result list contains dicts with at least a "chunk_id" field.
                    Example: {"fts": [{chunk_id: "A", ...}], "vector": [{...}]}
            k: RRF constant (default: 60). Higher values give more weight to lower ranks.

        Returns:
            List of result dicts sorted by rrf_score (descending).
            Each dict contains:
            - All original chunk metadata fields
            - rrf_score (float): Computed RRF score
            - sources (list): List of system names that found this chunk

        Behavior:
            - Chunks appearing in multiple systems accumulate scores (overlap boost)
            - Results are deduplicated by chunk_id
            - Chunks without chunk_id are skipped with a warning
            - Empty input returns empty list
        """
        if not results:
            return []

        # Accumulators
        scores: Dict[str, float] = defaultdict(float)  # chunk_id -> total RRF score
        metadata: Dict[str, Dict] = {}  # chunk_id -> chunk metadata (first occurrence)
        sources: Dict[str, Set[str]] = defaultdict(set)  # chunk_id -> set of system names

        # Process each system's ranked results
        for system_name, result_list in results.items():
            # Enumerate with 1-indexed positions (rank 1 = first place)
            for position, chunk in enumerate(result_list, start=1):
                # Validate chunk has chunk_id
                chunk_id = chunk.get("chunk_id")
                if chunk_id is None:
                    logger.warning(
                        f"Chunk in system '{system_name}' at position {position} "
                        f"missing 'chunk_id', skipping"
                    )
                    continue

                # Compute RRF score contribution: 1 / (k + rank)
                rrf_contribution = 1.0 / (k + position)
                scores[chunk_id] += rrf_contribution

                # Store metadata from first occurrence
                if chunk_id not in metadata:
                    metadata[chunk_id] = chunk.copy()

                # Track which systems found this chunk
                sources[chunk_id].add(system_name)

        # Build output list
        output = []
        for chunk_id, rrf_score in scores.items():
            # Get original metadata
            chunk_data = metadata[chunk_id].copy()

            # Add RRF score and sources
            chunk_data["rrf_score"] = rrf_score
            chunk_data["sources"] = sorted(list(sources[chunk_id]))  # Sort for consistency

            output.append(chunk_data)

        # Sort by RRF score descending (highest first)
        output.sort(key=lambda x: x["rrf_score"], reverse=True)

        logger.info(
            f"RRF fusion combined {len(results)} systems, "
            f"produced {len(output)} unique results"
        )

        return output
