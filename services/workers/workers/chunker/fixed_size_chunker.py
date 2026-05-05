"""Fixed-size chunker for fallback when semantic parsing fails."""

import logging
from typing import List

from .models import Chunk

logger = logging.getLogger(__name__)


class FixedSizeChunker:
    """Chunks code by fixed line count when semantic parsing unavailable."""

    def __init__(self, chunk_size: int = 50, overlap: int = 5):
        """
        Initialize fixed-size chunker.

        Args:
            chunk_size: Number of lines per chunk (default: 50)
            overlap: Number of overlapping lines between chunks (default: 5)
        """
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_by_lines(
        self, content: str, file_path: str, language: str
    ) -> List[Chunk]:
        """
        Chunk content into fixed-size blocks.

        Args:
            content: File content as string
            file_path: Path to source file
            language: Programming language (for metadata only)

        Returns:
            List of fixed-size chunks
        """
        lines = content.splitlines()
        total_lines = len(lines)

        if total_lines == 0:
            # Empty file - return single empty chunk
            return [
                Chunk(
                    content="",
                    file_path=file_path,
                    start_line=1,
                    end_line=1,
                    language=language,
                    chunk_type="fixed_size",
                    metadata={
                        "ancestor_chain": [file_path],
                        "breadcrumb": f"{file_path}:1-1",
                    },
                )
            ]

        chunks = []
        start_line = 0

        while start_line < total_lines:
            # Determine end line for this chunk
            end_line = min(start_line + self.chunk_size, total_lines)

            # Try to break at a blank line if we're not at the end
            if end_line < total_lines:
                end_line = self._find_break_point(
                    lines, start_line, end_line, total_lines
                )

            # Extract chunk content
            chunk_lines = lines[start_line:end_line]
            chunk_content = "\n".join(chunk_lines)

            # Create chunk with 1-indexed line numbers
            start_line_1indexed = start_line + 1
            end_line_1indexed = end_line

            chunk = Chunk(
                content=chunk_content,
                file_path=file_path,
                start_line=start_line_1indexed,
                end_line=end_line_1indexed,
                language=language,
                chunk_type="fixed_size",
                metadata={
                    "ancestor_chain": [file_path],
                    "breadcrumb": f"{file_path}:{start_line_1indexed}-{end_line_1indexed}",
                },
            )

            chunks.append(chunk)

            # Move to next chunk with overlap
            # Don't overlap if we've reached the end
            if end_line >= total_lines:
                break

            start_line = end_line - self.overlap

        logger.info(
            f"Fixed-size chunking: {file_path} -> {len(chunks)} chunks "
            f"(~{self.chunk_size} lines each, {self.overlap} line overlap)"
        )

        return chunks

    def _find_break_point(
        self, lines: List[str], start: int, preferred_end: int, total: int
    ) -> int:
        """
        Find a good break point near the preferred end.

        Looks for blank lines within ±5 lines of the preferred end.

        Args:
            lines: All lines in the file
            start: Start line index
            preferred_end: Preferred end line index
            total: Total number of lines

        Returns:
            Adjusted end line index
        """
        search_range = 5

        # Look for blank lines within range
        search_start = max(start + 1, preferred_end - search_range)
        search_end = min(total, preferred_end + search_range)

        # Find blank lines in range
        blank_lines = []
        for i in range(search_start, search_end):
            if i < len(lines) and lines[i].strip() == "":
                blank_lines.append(i)

        # Use the blank line closest to preferred_end
        if blank_lines:
            closest = min(blank_lines, key=lambda x: abs(x - preferred_end))
            return closest + 1  # Include the blank line in the chunk

        # No blank line found, use preferred end
        return preferred_end
