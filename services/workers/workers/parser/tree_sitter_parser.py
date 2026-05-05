"""Tree-sitter based parser for extracting semantic code structures."""

from typing import Dict, List, Optional
from tree_sitter import Language, Parser, Tree, Node, Query, QueryCursor
import tree_sitter_python
import tree_sitter_go
import tree_sitter_javascript


class TreeSitterParser:
    """Language-agnostic AST parser using tree-sitter."""

    def __init__(self):
        """Initialize parser with language grammars."""
        # Load language grammars
        self.languages = {
            "python": Language(tree_sitter_python.language()),
            "go": Language(tree_sitter_go.language()),
            "typescript": Language(tree_sitter_javascript.language()),
            "javascript": Language(tree_sitter_javascript.language()),
        }

        # Create parsers for each language
        self.parsers = {
            lang: Parser(language) for lang, language in self.languages.items()
        }

        # Define tree-sitter queries for each language
        self._init_queries()

    def _init_queries(self):
        """Initialize tree-sitter queries for extracting code structures."""
        # Python queries
        self.queries = {
            "python": {
                "functions": Query(
                    self.languages["python"],
                    """
                    (function_definition
                        name: (identifier) @name
                        body: (block)? @body) @function
                    """
                ),
                "classes": Query(
                    self.languages["python"],
                    """
                    (class_definition
                        name: (identifier) @name
                        body: (block)? @body) @class
                    """
                ),
            },
            "go": {
                "functions": Query(
                    self.languages["go"],
                    """
                    (function_declaration
                        name: (identifier) @name
                        body: (block)? @body) @function
                    """
                ),
                "classes": Query(
                    self.languages["go"],
                    """
                    (type_declaration
                        (type_spec
                            name: (type_identifier) @name
                            type: (struct_type))) @class
                    """
                ),
            },
            "typescript": {
                "functions": Query(
                    self.languages["typescript"],
                    """
                    [
                        (function_declaration
                            name: (identifier) @name
                            body: (statement_block)? @body) @function
                        (method_definition
                            name: (property_identifier) @name
                            body: (statement_block)? @body) @function
                    ]
                    """
                ),
                "classes": Query(
                    self.languages["typescript"],
                    """
                    (class_declaration
                        name: (identifier) @name
                        body: (class_body)? @body) @class
                    """
                ),
            },
        }

        # JavaScript uses the same queries as TypeScript
        self.queries["javascript"] = self.queries["typescript"]

    def parse_file(self, content: str, language: str) -> Tree:
        """
        Parse file content and return AST tree.

        Args:
            content: Source code content as string
            language: Language name (python, go, typescript, javascript)

        Returns:
            Tree-sitter Tree object

        Raises:
            ValueError: If language is not supported
        """
        if language not in self.parsers:
            raise ValueError(
                f"Unsupported language: {language}. "
                f"Supported: {list(self.parsers.keys())}"
            )

        # Use language-specific parser
        parser = self.parsers[language]
        tree = parser.parse(bytes(content, "utf8"))
        return tree

    def extract_functions(self, tree: Tree, content: str, language: str) -> List[Dict]:
        """
        Extract function definitions from AST.

        Args:
            tree: Tree-sitter Tree object
            content: Original source code content
            language: Language name

        Returns:
            List of dictionaries with function metadata:
            - name: Function name
            - start_byte: Start byte position
            - end_byte: End byte position
            - start_line: Start line number (1-indexed)
            - end_line: End line number (1-indexed)
            - docstring: Docstring if present (Python only)
        """
        if language not in self.queries:
            raise ValueError(f"No queries defined for language: {language}")

        content_bytes = bytes(content, "utf8")
        functions = []

        query = self.queries[language]["functions"]
        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)

        # Process each match
        for _, captures_dict in matches:
            # Get function node and name node from captures
            function_nodes = captures_dict.get("function", [])
            name_nodes = captures_dict.get("name", [])

            if function_nodes and name_nodes:
                func_node = function_nodes[0]  # Take first match
                name_node = name_nodes[0]
                name = self.get_node_text(name_node, content_bytes)

                func_info = {
                    "name": name,
                    "start_byte": func_node.start_byte,
                    "end_byte": func_node.end_byte,
                    "start_line": func_node.start_point[0] + 1,  # Convert to 1-indexed
                    "end_line": func_node.end_point[0] + 1,
                }

                # Extract docstring for Python
                if language == "python":
                    docstring = self._extract_python_docstring(func_node, content_bytes)
                    if docstring:
                        func_info["docstring"] = docstring

                functions.append(func_info)

        return functions

    def extract_classes(self, tree: Tree, content: str, language: str) -> List[Dict]:
        """
        Extract class definitions from AST.

        Args:
            tree: Tree-sitter Tree object
            content: Original source code content
            language: Language name

        Returns:
            List of dictionaries with class metadata:
            - name: Class name
            - start_byte: Start byte position
            - end_byte: End byte position
            - start_line: Start line number (1-indexed)
            - end_line: End line number (1-indexed)
            - docstring: Docstring if present (Python only)
        """
        if language not in self.queries:
            raise ValueError(f"No queries defined for language: {language}")

        content_bytes = bytes(content, "utf8")
        classes = []

        query = self.queries[language]["classes"]
        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)

        # Process each match
        for _, captures_dict in matches:
            # Get class node and name node from captures
            class_nodes = captures_dict.get("class", [])
            name_nodes = captures_dict.get("name", [])

            if class_nodes and name_nodes:
                class_node = class_nodes[0]  # Take first match
                name_node = name_nodes[0]
                name = self.get_node_text(name_node, content_bytes)

                class_info = {
                    "name": name,
                    "start_byte": class_node.start_byte,
                    "end_byte": class_node.end_byte,
                    "start_line": class_node.start_point[0] + 1,  # Convert to 1-indexed
                    "end_line": class_node.end_point[0] + 1,
                }

                # Extract docstring for Python
                if language == "python":
                    docstring = self._extract_python_docstring(class_node, content_bytes)
                    if docstring:
                        class_info["docstring"] = docstring

                classes.append(class_info)

        return classes

    def get_node_text(self, node: Node, content: bytes) -> str:
        """
        Extract text content from a node.

        Args:
            node: Tree-sitter Node
            content: Source code as bytes

        Returns:
            Node text as string
        """
        return content[node.start_byte:node.end_byte].decode("utf8")

    def _extract_python_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """
        Extract docstring from Python function or class.

        Args:
            node: Function or class definition node
            content: Source code as bytes

        Returns:
            Docstring text or None
        """
        # Look for string literal as first statement in body
        for child in node.children:
            if child.type == "block":
                for statement in child.children:
                    if statement.type == "expression_statement":
                        for expr_child in statement.children:
                            if expr_child.type == "string":
                                docstring = self.get_node_text(expr_child, content)
                                # Remove quotes
                                docstring = docstring.strip('"""').strip("'''")
                                docstring = docstring.strip('"').strip("'")
                                return docstring.strip()
                        break
                break
        return None
