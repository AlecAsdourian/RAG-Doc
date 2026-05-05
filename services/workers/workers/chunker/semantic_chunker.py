"""Semantic code chunker that respects code structure."""

import logging
from typing import List, Optional, Tuple

from tree_sitter import Node, Tree
from workers.parser import TreeSitterParser
from .models import Chunk
from .metadata_builder import MetadataBuilder
from .fixed_size_chunker import FixedSizeChunker
from .summary_generator import FileSummaryGenerator, ClassSummaryGenerator

logger = logging.getLogger(__name__)


class SemanticChunker:
    """Chunks code at semantic boundaries (functions, classes)."""

    def __init__(self):
        """Initialize semantic chunker with tree-sitter parser."""
        self.parser = TreeSitterParser()
        self.fixed_size_chunker = FixedSizeChunker(chunk_size=50, overlap=5)
        self.file_summary_generator = FileSummaryGenerator()
        self.class_summary_generator = ClassSummaryGenerator(
            method_threshold=5, line_threshold=100
        )

    def chunk_file(self, file_path: str, content: str, language: str) -> List[Chunk]:
        """
        Chunk a file into semantic units with fallback to fixed-size.

        Args:
            file_path: Path to the source file
            content: File content as string
            language: Programming language (python, go, typescript, javascript)

        Returns:
            List of Chunk objects representing semantic units
        """
        chunks = []

        try:
            # Parse the file
            tree = self.parser.parse_file(content, language)
            content_bytes = bytes(content, "utf8")

            # Create metadata builder
            metadata_builder = MetadataBuilder(language)

            # Extract functions with their nodes
            functions = self.parser.extract_functions(tree, content, language)
            function_nodes = self._find_function_nodes(tree, functions, language)

            for func_info, node in zip(functions, function_nodes):
                if node:  # Only create chunk if we found the node
                    chunk = self._create_function_chunk(
                        func_info, node, content, content_bytes, file_path, language, metadata_builder
                    )
                    chunks.append(chunk)

            # Extract classes with their nodes
            classes = self.parser.extract_classes(tree, content, language)
            class_nodes = self._find_class_nodes(tree, classes, language)

            for cls_info, node in zip(classes, class_nodes):
                if node:  # Only create chunk if we found the node
                    chunk = self._create_class_chunk(
                        cls_info, node, content, content_bytes, file_path, language, metadata_builder
                    )
                    chunks.append(chunk)

            # Sort chunks by start line to maintain file order
            chunks.sort(key=lambda c: c.start_line)

            # If semantic parsing produced no chunks, fall back to fixed-size
            if len(chunks) == 0:
                logger.warning(
                    f"Semantic parsing produced no chunks for {file_path} ({language}), "
                    "falling back to fixed-size chunking"
                )
                return self.fixed_size_chunker.chunk_by_lines(content, file_path, language)

            # Generate summaries
            code_chunks = chunks.copy()  # Keep code chunks separate for summary generation

            # Generate file-level summary
            file_summary = self.file_summary_generator.generate_file_summary(
                file_path, content, language, code_chunks
            )
            chunks.append(file_summary)

            # Generate class-level summaries for large classes
            class_chunks = [c for c in code_chunks if c.chunk_type == "class"]
            for class_chunk in class_chunks:
                # Find all method chunks belonging to this class
                class_name = class_chunk.metadata.get("class_name", "")
                method_chunks = [
                    c
                    for c in code_chunks
                    if c.chunk_type == "function"
                    and class_name in c.metadata.get("ancestor_chain", [])
                ]

                # Generate summary if needed
                class_summary = self.class_summary_generator.generate_class_summary(
                    class_chunk, method_chunks
                )
                if class_summary:
                    chunks.append(class_summary)

        except ValueError as e:
            # Unsupported language
            logger.warning(
                f"Unsupported language '{language}' for {file_path}: {e}. "
                "Falling back to fixed-size chunking"
            )
            return self.fixed_size_chunker.chunk_by_lines(content, file_path, language)

        except Exception as e:
            # Any other error (syntax errors, parser failures, etc.)
            logger.warning(
                f"Semantic parsing failed for {file_path} ({language}): {e}. "
                "Falling back to fixed-size chunking"
            )
            return self.fixed_size_chunker.chunk_by_lines(content, file_path, language)

        return chunks

    def _create_function_chunk(
        self,
        func_info: dict,
        node: Node,
        content: str,
        content_bytes: bytes,
        file_path: str,
        language: str,
        metadata_builder: MetadataBuilder,
    ) -> Chunk:
        """
        Create a chunk for a function.

        Args:
            func_info: Function metadata from TreeSitterParser
            node: AST node for the function
            content: Full file content as string
            content_bytes: Full file content as bytes
            file_path: Source file path
            language: Programming language
            metadata_builder: MetadataBuilder instance

        Returns:
            Chunk object for the function
        """
        # Extract the function content by line numbers
        lines = content.splitlines()
        start_idx = func_info["start_line"] - 1  # Convert to 0-indexed
        end_idx = func_info["end_line"]  # end_line is inclusive, so this works
        chunk_content = "\n".join(lines[start_idx:end_idx])

        # Build enhanced metadata using MetadataBuilder
        function_name = func_info["name"]
        ancestor_chain = metadata_builder.build_ancestor_chain(node, content_bytes)
        breadcrumb = metadata_builder.generate_breadcrumb(ancestor_chain, function_name)
        parent_scope = metadata_builder.extract_parent_scope(node, content_bytes)

        metadata = {
            "function_name": function_name,
            "ancestor_chain": ancestor_chain,
            "breadcrumb": breadcrumb,
        }

        # Add parent scope if present
        if parent_scope:
            metadata["parent_scope"] = parent_scope

        # Include docstring if present
        if "docstring" in func_info:
            metadata["docstring"] = func_info["docstring"]

        return Chunk(
            content=chunk_content,
            file_path=file_path,
            start_line=func_info["start_line"],
            end_line=func_info["end_line"],
            language=language,
            chunk_type="function",
            metadata=metadata,
        )

    def _create_class_chunk(
        self,
        class_info: dict,
        node: Node,
        content: str,
        content_bytes: bytes,
        file_path: str,
        language: str,
        metadata_builder: MetadataBuilder,
    ) -> Chunk:
        """
        Create a chunk for a class.

        Args:
            class_info: Class metadata from TreeSitterParser
            node: AST node for the class
            content: Full file content as string
            content_bytes: Full file content as bytes
            file_path: Source file path
            language: Programming language
            metadata_builder: MetadataBuilder instance

        Returns:
            Chunk object for the class
        """
        # Extract the class content by line numbers
        lines = content.splitlines()
        start_idx = class_info["start_line"] - 1  # Convert to 0-indexed
        end_idx = class_info["end_line"]  # end_line is inclusive
        chunk_content = "\n".join(lines[start_idx:end_idx])

        # Build enhanced metadata using MetadataBuilder
        class_name = class_info["name"]
        ancestor_chain = metadata_builder.build_ancestor_chain(node, content_bytes)
        breadcrumb = metadata_builder.generate_breadcrumb(ancestor_chain, class_name)
        parent_scope = metadata_builder.extract_parent_scope(node, content_bytes)

        metadata = {
            "class_name": class_name,
            "ancestor_chain": ancestor_chain,
            "breadcrumb": breadcrumb,
        }

        # Add parent scope if present
        if parent_scope:
            metadata["parent_scope"] = parent_scope

        # Include docstring if present
        if "docstring" in class_info:
            metadata["docstring"] = class_info["docstring"]

        return Chunk(
            content=chunk_content,
            file_path=file_path,
            start_line=class_info["start_line"],
            end_line=class_info["end_line"],
            language=language,
            chunk_type="class",
            metadata=metadata,
        )

    def _find_function_nodes(
        self, tree: Tree, functions: List[dict], language: str
    ) -> List[Optional[Node]]:
        """
        Find AST nodes corresponding to extracted functions.

        Args:
            tree: Parsed AST tree
            functions: List of function info dicts from TreeSitterParser
            language: Programming language

        Returns:
            List of nodes (or None if not found) matching the functions
        """
        nodes = []
        for func_info in functions:
            node = self._find_node_by_position(
                tree.root_node, func_info["start_line"], func_info["start_byte"]
            )
            nodes.append(node)
        return nodes

    def _find_class_nodes(
        self, tree: Tree, classes: List[dict], language: str
    ) -> List[Optional[Node]]:
        """
        Find AST nodes corresponding to extracted classes.

        Args:
            tree: Parsed AST tree
            classes: List of class info dicts from TreeSitterParser
            language: Programming language

        Returns:
            List of nodes (or None if not found) matching the classes
        """
        nodes = []
        for cls_info in classes:
            node = self._find_node_by_position(
                tree.root_node, cls_info["start_line"], cls_info["start_byte"]
            )
            nodes.append(node)
        return nodes

    def _find_node_by_position(
        self, root: Node, target_line: int, target_byte: int
    ) -> Optional[Node]:
        """
        Find a node at a specific position in the tree.

        Args:
            root: Root node to search from
            target_line: Target line number (1-indexed)
            target_byte: Target byte position

        Returns:
            Node at the position or None if not found
        """
        # Convert target_line to 0-indexed for comparison
        target_line_idx = target_line - 1

        def traverse(node: Node) -> Optional[Node]:
            # Check if this node starts at the target position
            if (
                node.start_point[0] == target_line_idx
                and node.start_byte == target_byte
            ):
                return node

            # Recursively search children
            for child in node.children:
                result = traverse(child)
                if result:
                    return result

            return None

        return traverse(root)
