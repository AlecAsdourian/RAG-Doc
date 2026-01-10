"""Metadata-based ranking adjustments for search results."""

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class MetadataBooster:
    """Applies configurable boosts/penalties based on chunk metadata."""

    # Noise patterns that indicate generated/vendor code
    NOISE_PATTERNS = [
        r'(^|.*/)node_modules/.*',
        r'(^|.*/)vendor/.*',
        r'(^|.*/)dist/.*',
        r'(^|.*/)build/.*',
        r'.*\.lock$',
        r'.*-lock\.json$',
        r'(^|.*/)migrations/.*',
        r'(^|.*/)__pycache__/.*',
        r'(^|.*/)\.git/.*',
    ]

    # Default boost weights
    DEFAULT_CONFIG = {
        "chunk_type_boosts": {
            "docs": 1.5,
            "file_summary": 1.4,
            "class_summary": 1.3,
            "function": 1.0,
            "class": 1.0,
            "test": 0.8,
        },
        "path_boost": 1.2,
        "breadcrumb_match_boost": 1.3,
        "quoted_match_boost": 1.4,
        "identifier_match_boost": 1.3,
        "noise_penalty": 0.3,
    }

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize MetadataBooster with configurable weights.

        Args:
            config: Optional config dict to override default boost weights.
                   Supports partial overrides (only specified keys are replaced).
        """
        # Start with default config
        self.config = self.DEFAULT_CONFIG.copy()

        # Override with custom config if provided
        if config:
            # Handle chunk_type_boosts specially to allow partial override
            if "chunk_type_boosts" in config:
                self.config["chunk_type_boosts"] = {
                    **self.DEFAULT_CONFIG["chunk_type_boosts"],
                    **config["chunk_type_boosts"],
                }

            # Override other keys
            for key, value in config.items():
                if key != "chunk_type_boosts":
                    self.config[key] = value

        # Compile noise patterns
        self.noise_regexes = [re.compile(pattern) for pattern in self.NOISE_PATTERNS]

        logger.info(f"MetadataBooster initialized with config: {self.config}")

    def boost(
        self, chunks: List[Dict[str, Any]], parsed_query: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Apply metadata-based boosts to chunks.

        Args:
            chunks: List of chunks with rrf_score and metadata fields
            parsed_query: Parsed query dict with quoted_terms and identifiers

        Returns:
            List of chunks with boosted_score and boost_multiplier added
        """
        logger.debug(
            f"Boosting {len(chunks)} chunks with query: {parsed_query.get('raw_query')}"
        )

        boosted_chunks = []

        for chunk in chunks:
            multiplier = self._calculate_multiplier(chunk, parsed_query)
            base_score = chunk.get("rrf_score", 0.0)
            boosted_score = base_score * multiplier

            # Create a copy of the chunk with boost info
            boosted_chunk = chunk.copy()
            boosted_chunk["boosted_score"] = boosted_score
            boosted_chunk["boost_multiplier"] = multiplier

            boosted_chunks.append(boosted_chunk)

            logger.debug(
                f"Chunk {chunk.get('chunk_id')}: "
                f"base={base_score:.4f} -> boosted={boosted_score:.4f} "
                f"(multiplier={multiplier:.4f})"
            )

        return boosted_chunks

    def _calculate_multiplier(
        self, chunk: Dict[str, Any], parsed_query: Dict[str, Any]
    ) -> float:
        """
        Calculate the boost multiplier for a single chunk.

        Args:
            chunk: Chunk dict with metadata fields
            parsed_query: Parsed query dict

        Returns:
            Float multiplier to apply to base score
        """
        multiplier = 1.0

        # Apply chunk type boost/penalty
        chunk_type = chunk.get("chunk_type")
        if chunk_type:
            chunk_type_boosts = self.config["chunk_type_boosts"]
            if chunk_type in chunk_type_boosts:
                type_multiplier = chunk_type_boosts[chunk_type]
                multiplier *= type_multiplier
                logger.debug(f"Chunk type '{chunk_type}' boost: {type_multiplier}x")

        # Apply noise penalty if path matches noise patterns
        file_path = chunk.get("file_path", "")
        if file_path and self._is_noise_path(file_path):
            noise_penalty = self.config["noise_penalty"]
            multiplier *= noise_penalty
            logger.debug(f"Noise penalty applied: {noise_penalty}x")

        # Apply breadcrumb match boost for identifiers
        breadcrumb = chunk.get("breadcrumb", "")
        identifiers = parsed_query.get("identifiers", [])
        if breadcrumb and identifiers and self._matches_identifiers(breadcrumb, identifiers):
            breadcrumb_boost = self.config["breadcrumb_match_boost"]
            multiplier *= breadcrumb_boost
            logger.debug(f"Breadcrumb match boost: {breadcrumb_boost}x")

        # Apply quoted term match boost
        content = chunk.get("content", "")
        if content and self._matches_quoted_terms(
            content, parsed_query.get("quoted_terms", [])
        ):
            quoted_boost = self.config["quoted_match_boost"]
            multiplier *= quoted_boost
            logger.debug(f"Quoted term match boost: {quoted_boost}x")

        # Apply identifier match boost in content (independent of breadcrumb)
        # Both breadcrumb and content identifier boosts can apply
        if content and identifiers and self._matches_identifiers(content, identifiers):
            identifier_boost = self.config["identifier_match_boost"]
            multiplier *= identifier_boost
            logger.debug(f"Identifier match in content boost: {identifier_boost}x")

        return multiplier

    def _is_noise_path(self, file_path: str) -> bool:
        """
        Check if file path matches noise patterns.

        Args:
            file_path: File path to check

        Returns:
            True if path matches any noise pattern
        """
        # Normalize path separators for cross-platform compatibility
        normalized_path = file_path.replace("\\", "/")

        for regex in self.noise_regexes:
            if regex.match(normalized_path):
                return True

        return False

    def _matches_identifiers(self, text: str, identifiers: List[str]) -> bool:
        """
        Check if any identifier appears in text (case-insensitive).

        Args:
            text: Text to search in
            identifiers: List of identifiers to search for

        Returns:
            True if any identifier is found in text
        """
        if not identifiers or not text:
            return False

        text_lower = text.lower()

        for identifier in identifiers:
            if identifier.lower() in text_lower:
                return True

        return False

    def _matches_quoted_terms(self, text: str, quoted_terms: List[str]) -> bool:
        """
        Check if any quoted term appears in text (case-insensitive).

        Args:
            text: Text to search in
            quoted_terms: List of quoted terms to search for

        Returns:
            True if any quoted term is found in text
        """
        if not quoted_terms or not text:
            return False

        text_lower = text.lower()

        for term in quoted_terms:
            if term.lower() in text_lower:
                return True

        return False
