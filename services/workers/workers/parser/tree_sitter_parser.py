"""Tree-sitter based parser for extracting semantic code structures."""

from typing import Dict, List, Optional
from tree_sitter import Language, Parser, Tree, Node, Query, QueryCursor
import tree_sitter_python
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_typescript


class GrammarMismatch(RuntimeError):
    """A tree was about to be queried with another grammar's compiled queries.

    A query compiled for one grammar matches NOTHING on another grammar's tree,
    and says nothing (measured 2026-10-06, `22.2-02-PLAN.md`: a TS-compiled
    `function_declaration` query finds 0 matches on the TSX tree of the same
    source). So querying with the wrong set is never silent here: it raises,
    and `SemanticChunker.chunk_file` reports it as a raised fallback, which the
    census counts (`fallback_reasons.raised`).
    """


# Functions, methods and module-level const-bound functions, the same shape in
# the three ECMAScript grammars. QD5 (22.2-CONTEXT.md, LOCKED): named functions
# and generators; methods ONLY inside a `class_body` (an object literal's
# methods are not chunks); a module-level `const`/`let` -- a direct child of
# `program`, or exported -- whose declarator's value is an arrow function or a
# function expression, one match per such declarator, named by the variable
# and spanning the whole declaration. Not matched, by design: `export default
# function () {}` (no name), a function passed to a call (`wrap(() => ...)`),
# class-field arrow functions (`public_field_definition`), interfaces, type
# aliases and enums (22.1-01's symbols, not chunks).
_ECMASCRIPT_FUNCTIONS = """
[
    (function_declaration
        name: (identifier) @name) @function
    (generator_function_declaration
        name: (identifier) @name) @function
    (class_body
        (method_definition
            name: (_) @name) @function)
    (program
        (lexical_declaration
            (variable_declarator
                name: (identifier) @name
                value: [(arrow_function) (function_expression)])) @function)
    (export_statement
        (lexical_declaration
            (variable_declarator
                name: (identifier) @name
                value: [(arrow_function) (function_expression)])) @function)
]
"""

# TypeScript names a class with a `type_identifier`, and has abstract classes.
# (Today's class query, `name: (identifier)`, fails to compile under both
# TypeScript grammars: `22.2-records/ts-grammar-checks.txt`.)
_TYPESCRIPT_CLASSES = """
[
    (class_declaration
        name: (type_identifier) @name) @class
    (abstract_class_declaration
        name: (type_identifier) @name) @class
]
"""

# JavaScript names a class with an `identifier`, and has no abstract classes.
_JAVASCRIPT_CLASSES = """
(class_declaration
    name: (identifier) @name) @class
"""


class TreeSitterParser:
    """Language-agnostic AST parser using tree-sitter.

    GRAMMARS, NOT LANGUAGES. A stored `language` names what a file is
    (`typescript` covers `.ts` and `.tsx`, `workers/fetch/filters.py`); a
    GRAMMAR is what parses it. `grammar_for(language, path)` is the ONE place
    a grammar is chosen: `.tsx` gets the TSX grammar, any other TypeScript
    file the TypeScript grammar, JavaScript its own. `parse_file`,
    `extract_functions` and `extract_classes` all choose through it, and so
    does the census (`chunk_census.py`, through `parse_file`).

    Each grammar has its OWN compiled queries (`self.queries[grammar]`): a
    query compiled for one grammar matches nothing, silently, on another's
    tree. The extractors check the tree's grammar against the set they chose
    and raise `GrammarMismatch` rather than query across grammars.
    """

    def __init__(self):
        """Initialize parser with language grammars."""
        # Load the grammars, keyed by grammar (see the class docstring).
        self.languages = {
            "python": Language(tree_sitter_python.language()),
            "go": Language(tree_sitter_go.language()),
            "typescript": Language(tree_sitter_typescript.language_typescript()),
            "tsx": Language(tree_sitter_typescript.language_tsx()),
            "javascript": Language(tree_sitter_javascript.language()),
        }

        # Create parsers for each grammar
        self.parsers = {
            grammar: Parser(language) for grammar, language in self.languages.items()
        }

        # Compile every query for every grammar. A query that fails to compile
        # raises `QueryError` here, from the constructor -- outside any
        # fallback -- so it stops every ingest loudly.
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
                # METHODS ARE A SEPARATE NODE TYPE IN GO, and this query used to
                # match only `function_declaration`. A method --
                # `func (h *RepositoriesHandler) Connect(...)` -- is a
                # `method_declaration`, whose name is a `field_identifier`, not an
                # `identifier`. Measured on this repository's backend: 85 of 186
                # Go callables were methods and none were indexed, including
                # every HTTP handler and every method on the GitHub client.
                "functions": Query(
                    self.languages["go"],
                    """
                    [
                        (function_declaration
                            name: (identifier) @name
                            body: (block)? @body) @function
                        (method_declaration
                            name: (field_identifier) @name
                            body: (block)? @body) @function
                    ]
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
        }

        # The three ECMAScript grammars: one compiled set EACH, never shared.
        for grammar, classes in (
            ("typescript", _TYPESCRIPT_CLASSES),
            ("tsx", _TYPESCRIPT_CLASSES),
            ("javascript", _JAVASCRIPT_CLASSES),
        ):
            self.queries[grammar] = {
                "functions": Query(self.languages[grammar], _ECMASCRIPT_FUNCTIONS),
                "classes": Query(self.languages[grammar], classes),
            }

    def grammar_for(self, language: str, path: Optional[str] = None) -> str:
        """The grammar that parses a file: the ONE place it is chosen.

        Args:
            language: The file's stored language (python, go, typescript,
                javascript)
            path: The file's path. Required for TypeScript, whose grammar
                depends on the extension: `.tsx` is TSX, anything else is
                TypeScript.

        Returns:
            A key of `self.languages`, `self.parsers` and `self.queries`.

        Raises:
            ValueError: If language is not supported, or is TypeScript with no
                path to choose its grammar by
        """
        if language == "typescript":
            if path is None:
                raise ValueError(
                    "TypeScript's grammar is chosen by the file's extension (.tsx is TSX); "
                    "pass the file's path"
                )
            return "tsx" if path.lower().endswith(".tsx") else "typescript"
        if language in ("python", "go", "javascript"):
            return language
        raise ValueError(
            f"Unsupported language: {language}. "
            f"Supported: ['python', 'go', 'typescript', 'javascript']"
        )

    def parse_file(self, content: str, language: str, path: Optional[str] = None) -> Tree:
        """
        Parse file content and return AST tree.

        Args:
            content: Source code content as string
            language: Language name (python, go, typescript, javascript)
            path: The file's path, which chooses TypeScript's grammar
                (`grammar_for`)

        Returns:
            Tree-sitter Tree object, parsed with `grammar_for(language, path)`

        Raises:
            ValueError: If language is not supported
        """
        parser = self.parsers[self.grammar_for(language, path)]
        return parser.parse(bytes(content, "utf8"))

    def _queries_for(self, tree: Tree, language: str, path: Optional[str]) -> Dict[str, Query]:
        """The query set of the grammar `tree` was parsed with, chosen by the
        same `grammar_for` as `parse_file`; never another grammar's."""
        grammar = self.grammar_for(language, path)
        if tree.language != self.languages[grammar]:
            raise GrammarMismatch(
                f"the tree was not parsed with the {grammar!r} grammar that {language!r} "
                f"and {path!r} choose; refusing to query it with {grammar!r}'s queries"
            )
        return self.queries[grammar]

    def extract_functions(
        self, tree: Tree, content: str, language: str, path: Optional[str] = None
    ) -> List[Dict]:
        """
        Extract function definitions from AST.

        Args:
            tree: Tree-sitter Tree object
            content: Original source code content
            language: Language name
            path: The file's path (chooses TypeScript's grammar, as in
                `parse_file`)

        Returns:
            List of dictionaries with function metadata:
            - name: Function name (for a const-bound function, the variable's)
            - start_byte: Start byte position
            - end_byte: End byte position
            - start_line: Start line number (1-indexed)
            - end_line: End line number (1-indexed)
            - docstring: Docstring if present (Python only)
        """
        content_bytes = bytes(content, "utf8")
        functions = []

        query = self._queries_for(tree, language, path)["functions"]
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

    def extract_classes(
        self, tree: Tree, content: str, language: str, path: Optional[str] = None
    ) -> List[Dict]:
        """
        Extract class definitions from AST.

        Args:
            tree: Tree-sitter Tree object
            content: Original source code content
            language: Language name
            path: The file's path (chooses TypeScript's grammar, as in
                `parse_file`)

        Returns:
            List of dictionaries with class metadata:
            - name: Class name
            - start_byte: Start byte position
            - end_byte: End byte position
            - start_line: Start line number (1-indexed)
            - end_line: End line number (1-indexed)
            - docstring: Docstring if present (Python only)
        """
        content_bytes = bytes(content, "utf8")
        classes = []

        query = self._queries_for(tree, language, path)["classes"]
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
