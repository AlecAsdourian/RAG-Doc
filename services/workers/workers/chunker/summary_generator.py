"""Summary generation for files and complex classes."""

import os
from typing import List, Optional

from .models import Chunk


class FileSummaryGenerator:
    """Generates file-level summary chunks."""

    def generate_file_summary(
        self, file_path: str, content: str, language: str, chunks: List[Chunk]
    ) -> Chunk:
        """
        Generate a file-level summary from parsed chunks.

        Args:
            file_path: Path to the source file
            content: File content as string
            language: Programming language
            chunks: List of parsed chunks (functions, classes)

        Returns:
            Chunk containing file summary
        """
        # Count component types
        function_chunks = [c for c in chunks if c.chunk_type == "function"]
        class_chunks = [c for c in chunks if c.chunk_type == "class"]

        # Get main exports (top-level functions and classes)
        main_exports = []
        for chunk in chunks:
            if chunk.chunk_type == "function":
                # Top-level function if no ancestor chain
                if len(chunk.metadata.get("ancestor_chain", [])) == 0:
                    main_exports.append(chunk.metadata.get("function_name", ""))
            elif chunk.chunk_type == "class":
                # Top-level class if no ancestor chain
                if len(chunk.metadata.get("ancestor_chain", [])) == 0:
                    main_exports.append(chunk.metadata.get("class_name", ""))

        # Filter out empty names
        main_exports = [e for e in main_exports if e]

        # Infer purpose from file name and structure
        file_name = os.path.basename(file_path)
        purpose = self._infer_file_purpose(file_name, language, chunks)

        # Extract module-level docstring
        module_docstring = self._extract_module_docstring(content, language)

        # Generate structured summary
        summary_lines = [f"File: {file_path}", f"Purpose: {purpose}", ""]

        # Components section
        summary_lines.append("Components:")
        summary_lines.append(f"- {len(function_chunks)} functions")
        summary_lines.append(f"- {len(class_chunks)} classes")
        summary_lines.append("")

        # Main exports
        if main_exports:
            summary_lines.append(f"Main exports: {', '.join(main_exports[:10])}")
            if len(main_exports) > 10:
                summary_lines.append(f"  ({len(main_exports) - 10} more)")
            summary_lines.append("")

        # Module docstring
        if module_docstring:
            summary_lines.append(module_docstring)

        summary_content = "\n".join(summary_lines)

        # Truncate if too long (keep under ~200 words / 1000 chars)
        if len(summary_content) > 1000:
            summary_content = summary_content[:997] + "..."

        # Build metadata
        metadata = {
            "chunk_type": "file_summary",
            "summarizes": "file",
            "file_path": file_path,
            "breadcrumb": file_name,
            "component_counts": {
                "functions": len(function_chunks),
                "classes": len(class_chunks),
            },
            "main_exports": main_exports,
        }

        return Chunk(
            content=summary_content,
            file_path=file_path,
            start_line=1,
            end_line=len(content.splitlines()) if content else 1,
            language=language,
            chunk_type="file_summary",
            metadata=metadata,
        )

    def _infer_file_purpose(
        self, file_name: str, language: str, chunks: List[Chunk]
    ) -> str:
        """Infer file purpose from name and structure."""
        # Test files
        if "_test" in file_name or ".test." in file_name or file_name.startswith("test_"):
            return "Test file"

        # Package/module initialization
        if file_name == "__init__.py":
            return "Package initialization"

        # Entry points
        if file_name == "main.go" or file_name == "main.py":
            return "Application entry point"

        # API/Routes
        if "route" in file_name.lower() or "api" in file_name.lower():
            return "API endpoint definitions"

        # Models/Schemas
        if "model" in file_name.lower() or "schema" in file_name.lower():
            return "Data model definitions"

        # Services
        if "service" in file_name.lower():
            return "Service layer implementation"

        # Controllers/Handlers
        if "controller" in file_name.lower() or "handler" in file_name.lower():
            return "Request handler implementation"

        # Utils/Helpers
        if "util" in file_name.lower() or "helper" in file_name.lower():
            return "Utility functions"

        # Config
        if "config" in file_name.lower() or "settings" in file_name.lower():
            return "Configuration"

        # Generic based on structure
        function_count = len([c for c in chunks if c.chunk_type == "function"])
        class_count = len([c for c in chunks if c.chunk_type == "class"])

        if class_count > function_count:
            return "Class definitions"
        elif function_count > 0:
            return "Function implementations"
        else:
            return "Source file"

    def _extract_module_docstring(self, content: str, language: str) -> Optional[str]:
        """Extract module-level docstring from file content."""
        if language == "python":
            # Python module docstrings are at the top of the file
            lines = content.strip().split("\n")
            if not lines:
                return None

            # Skip shebang and encoding declarations
            start = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("# -*-"):
                    start = i + 1
                else:
                    break

            if start >= len(lines):
                return None

            first_line = lines[start].strip()
            # Check for triple-quoted string
            if first_line.startswith('"""') or first_line.startswith("'''"):
                quote = '"""' if first_line.startswith('"""') else "'''"
                # Single-line docstring
                if first_line.endswith(quote) and len(first_line) > 6:
                    return first_line.strip(quote).strip()
                # Multi-line docstring
                docstring_lines = [first_line.strip(quote)]
                for line in lines[start + 1 :]:
                    if quote in line:
                        docstring_lines.append(line.split(quote)[0])
                        break
                    docstring_lines.append(line)
                return " ".join(docstring_lines).strip()

        # Other languages - could extract header comments
        return None


class ClassSummaryGenerator:
    """Generates class-level summary chunks for large classes."""

    def __init__(self, method_threshold: int = 5, line_threshold: int = 100):
        """
        Initialize class summary generator.

        Args:
            method_threshold: Generate summary if class has more than this many methods
            line_threshold: Generate summary if class spans more than this many lines
        """
        self.method_threshold = method_threshold
        self.line_threshold = line_threshold

    def should_generate_summary(self, class_chunk: Chunk, method_chunks: List[Chunk]) -> bool:
        """
        Determine if a class needs a summary.

        Args:
            class_chunk: The class chunk
            method_chunks: List of method chunks belonging to this class

        Returns:
            True if summary should be generated
        """
        # Check method count
        if len(method_chunks) > self.method_threshold:
            return True

        # Check line count
        line_count = class_chunk.end_line - class_chunk.start_line + 1
        if line_count > self.line_threshold:
            return True

        return False

    def generate_class_summary(
        self, class_chunk: Chunk, method_chunks: List[Chunk]
    ) -> Optional[Chunk]:
        """
        Generate a class-level summary.

        Args:
            class_chunk: The class chunk
            method_chunks: List of method chunks belonging to this class

        Returns:
            Chunk containing class summary or None if not needed
        """
        if not self.should_generate_summary(class_chunk, method_chunks):
            return None

        class_name = class_chunk.metadata.get("class_name", "Unknown")
        class_docstring = class_chunk.metadata.get("docstring", "")

        # Separate public and private methods
        public_methods = []
        private_methods = []

        for method in method_chunks:
            method_name = method.metadata.get("function_name", "")
            if method_name:
                if method_name.startswith("_") and not method_name.startswith("__"):
                    private_methods.append(method)
                else:
                    public_methods.append(method)

        # Build summary
        summary_lines = [f"Class: {class_name}"]

        if class_docstring:
            summary_lines.append(f"Purpose: {class_docstring}")
        else:
            # Infer purpose from class name
            purpose = self._infer_class_purpose(class_name)
            summary_lines.append(f"Purpose: {purpose}")

        summary_lines.append("")

        # Key methods section
        summary_lines.append("Key methods:")

        # List up to 5 public methods with descriptions
        for method in public_methods[:5]:
            method_name = method.metadata.get("function_name", "")
            method_doc = method.metadata.get("docstring", "")
            if method_doc:
                # Truncate long docstrings
                if len(method_doc) > 60:
                    method_doc = method_doc[:57] + "..."
                summary_lines.append(f"- {method_name}: {method_doc}")
            else:
                summary_lines.append(f"- {method_name}")

        # Indicate more methods if present
        remaining = len(public_methods) - 5
        if remaining > 0:
            summary_lines.append(f"- {remaining} more public methods")

        summary_lines.append("")

        # Method counts
        summary_lines.append(
            f"Total: {len(public_methods)} public, {len(private_methods)} private methods"
        )

        summary_content = "\n".join(summary_lines)

        # Truncate if too long
        if len(summary_content) > 1000:
            summary_content = summary_content[:997] + "..."

        # Build metadata
        metadata = {
            "chunk_type": "class_summary",
            "summarizes": "class",
            "class_name": class_name,
            "breadcrumb": class_chunk.metadata.get("breadcrumb", class_name),
            "method_count": len(method_chunks),
            "public_methods": [
                m.metadata.get("function_name", "") for m in public_methods[:10]
            ],
        }

        return Chunk(
            content=summary_content,
            file_path=class_chunk.file_path,
            start_line=class_chunk.start_line,
            end_line=class_chunk.end_line,
            language=class_chunk.language,
            chunk_type="class_summary",
            metadata=metadata,
        )

    def _infer_class_purpose(self, class_name: str) -> str:
        """Infer class purpose from name."""
        name_lower = class_name.lower()

        if "service" in name_lower:
            return "Service class providing business logic"
        elif "controller" in name_lower or "handler" in name_lower:
            return "Request handler"
        elif "model" in name_lower or "schema" in name_lower:
            return "Data model"
        elif "repository" in name_lower or "repo" in name_lower:
            return "Data access layer"
        elif "manager" in name_lower:
            return "Management class"
        elif "helper" in name_lower or "util" in name_lower:
            return "Utility class"
        elif "builder" in name_lower:
            return "Builder pattern implementation"
        elif "factory" in name_lower:
            return "Factory pattern implementation"
        elif "validator" in name_lower:
            return "Validation logic"
        else:
            return f"{class_name} implementation"
