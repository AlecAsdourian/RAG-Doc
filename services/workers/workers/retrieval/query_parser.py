"""Query parser for detecting exact-match terms in search queries."""

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class QueryParser:
    """Parses queries to extract quoted terms and identifiers for boosting."""

    def __init__(self):
        """Initialize query parser with regex patterns."""
        # Pattern for quoted strings (both double and single quotes)
        self.quoted_pattern = re.compile(r'"([^"]+)"|\'([^\']+)\'')

        # Pattern for PascalCase identifiers (e.g., AuthService, JWTValidator)
        self.pascal_case_pattern = re.compile(r'\b[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]*)+\b')

        # Pattern for camelCase identifiers (e.g., getUserById, isValid)
        self.camel_case_pattern = re.compile(r'\b[a-z]+[A-Z][a-zA-Z0-9]*\b')

        # Pattern for snake_case identifiers (e.g., get_user_by_id, is_valid)
        # Minimum 3 characters to avoid noise
        self.snake_case_pattern = re.compile(r'\b[a-z_][a-z0-9_]{2,}\b')

    def parse(self, query: str) -> Dict[str, Any]:
        """
        Parse query to extract exact-match terms.

        Args:
            query: Search query string

        Returns:
            Dict with:
                - raw_query: Original query
                - quoted_terms: List of terms in quotes
                - identifiers: List of identifier patterns (PascalCase, camelCase, snake_case)
                - clean_query: Query with quoted terms removed
        """
        logger.debug(f"Parsing query: {query}")

        # Extract quoted terms
        quoted_terms = []
        for match in self.quoted_pattern.finditer(query):
            # Get the captured group (either group 1 or 2 depending on quote type)
            term = match.group(1) or match.group(2)
            if term:
                quoted_terms.append(term)

        logger.debug(f"Found {len(quoted_terms)} quoted terms: {quoted_terms}")

        # Remove quoted strings to get clean query
        clean_query = self.quoted_pattern.sub('', query)

        # Extract identifiers from the original query (not clean_query)
        # This ensures we catch identifiers that might be inside quotes too
        identifiers = []

        # Find PascalCase identifiers
        pascal_matches = self.pascal_case_pattern.findall(query)
        identifiers.extend(pascal_matches)

        # Find camelCase identifiers
        camel_matches = self.camel_case_pattern.findall(query)
        identifiers.extend(camel_matches)

        # Find snake_case identifiers
        snake_matches = self.snake_case_pattern.findall(query)
        identifiers.extend(snake_matches)

        # Remove duplicates while preserving order
        seen = set()
        unique_identifiers = []
        for identifier in identifiers:
            if identifier not in seen:
                seen.add(identifier)
                unique_identifiers.append(identifier)

        logger.debug(f"Found {len(unique_identifiers)} identifiers: {unique_identifiers}")

        result = {
            "raw_query": query,
            "quoted_terms": quoted_terms,
            "identifiers": unique_identifiers,
            "clean_query": clean_query.strip(),
        }

        logger.info(
            f"Query parsed: {len(quoted_terms)} quoted terms, "
            f"{len(unique_identifiers)} identifiers"
        )

        return result
