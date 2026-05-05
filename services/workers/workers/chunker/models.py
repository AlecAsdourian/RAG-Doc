"""Data models for code chunking."""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Chunk:
    """Represents a semantic chunk of code.

    A chunk is a meaningful unit of code (function, class, or fixed-size block)
    with metadata for RAG retrieval.
    """

    content: str
    """The actual code content of the chunk."""

    file_path: str
    """Path to the source file."""

    start_line: int
    """Starting line number (1-indexed)."""

    end_line: int
    """Ending line number (1-indexed, inclusive)."""

    language: str
    """Programming language (python, go, typescript, javascript, etc.)."""

    chunk_type: str
    """Type of chunk: function, class, module, file, fixed_size."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Additional metadata for the chunk.

    Expected keys:
    - ancestor_chain: List[str] - Full path from root to this element
    - breadcrumb: str - Human-readable path like "UserService.authenticate"
    - parent_scope: str - Immediate parent context (class signature, etc.)
    - docstring: Optional[str] - Associated documentation
    - function_name: Optional[str] - Name of function if chunk_type is function
    - class_name: Optional[str] - Name of class if chunk_type is class
    """

    def __repr__(self) -> str:
        """Readable representation of the chunk."""
        breadcrumb = self.metadata.get("breadcrumb", "unknown")
        lines = f"{self.start_line}-{self.end_line}"
        return f"Chunk({self.chunk_type}:{breadcrumb}@{lines})"
