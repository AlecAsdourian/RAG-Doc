"""Metadata extraction for code chunks."""

import logging
from typing import List, Optional

from tree_sitter import Node, Tree

logger = logging.getLogger(__name__)


class MetadataBuilder:
    """Extracts metadata from AST nodes for context-enriched chunking."""

    def __init__(self, language: str):
        """
        Initialize metadata builder for a specific language.

        Args:
            language: Programming language (python, go, typescript, javascript)
        """
        self.language = language

    def build_ancestor_chain(self, node: Node, content: bytes) -> List[str]:
        """
        Build ancestor chain from node to root.

        Walks up the AST tree collecting ancestor names (classes, modules, namespaces).

        Args:
            node: Current AST node
            content: Source code as bytes

        Returns:
            List of ancestor names from root to current node
            Example: ["UserService", "authenticate"] for a method
        """
        ancestors = []

        # A Go method's syntactic parent is the file, so walking parents alone
        # yields no ancestor and a bare breadcrumb like "Connect". Its owner is
        # named in the receiver instead: `func (h *RepositoriesHandler) Connect`
        # belongs to RepositoriesHandler, and the breadcrumb should say so.
        if self.language == "go" and node.type == "method_declaration":
            receiver_type = self._go_receiver_type(node, content)
            if receiver_type:
                ancestors.append(receiver_type)

        current = node.parent

        while current:
            ancestor_name = self._extract_node_name(current, content)
            if ancestor_name:
                ancestors.insert(0, ancestor_name)  # Prepend to maintain order
                # The same ownership rule applies when the walk passes THROUGH a
                # Go method: a type declared inside `func (h *Handler) Serve()`
                # belongs to Handler.Serve, not to a bare Serve.
                if self.language == "go" and current.type == "method_declaration":
                    receiver_type = self._go_receiver_type(current, content)
                    if receiver_type:
                        ancestors.insert(0, receiver_type)
            current = current.parent

        return ancestors

    def generate_breadcrumb(self, ancestor_chain: List[str], current_name: str) -> str:
        """
        Generate human-readable breadcrumb from ancestor chain.

        Args:
            ancestor_chain: List of ancestor names
            current_name: Name of current element

        Returns:
            Dot-separated breadcrumb like "UserService.authenticate"
            Maximum 4 levels to keep concise
        """
        # Combine ancestors with current name
        full_chain = ancestor_chain + [current_name]

        # Keep only last 4 levels for readability
        if len(full_chain) > 4:
            full_chain = full_chain[-4:]

        # Join with dots, filtering out empty strings
        breadcrumb = ".".join(filter(None, full_chain))

        return breadcrumb if breadcrumb else current_name

    def extract_parent_scope(self, node: Node, content: bytes) -> Optional[str]:
        """
        Extract immediate parent scope context.

        For methods, this would be the class signature.
        For nested functions, the outer function signature.

        Args:
            node: Current AST node
            content: Source code as bytes

        Returns:
            Parent scope string or None if no meaningful parent
        """
        if not node.parent:
            return None

        parent = node.parent

        # Find the first meaningful parent (class or function)
        while parent:
            if self._is_scope_node(parent):
                # Get the signature/header of the parent
                return self._extract_scope_signature(parent, content)
            parent = parent.parent

        return None

    def extract_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """
        Extract docstring or documentation comment for a node.

        Language-specific extraction:
        - Python: Triple-quoted strings after def/class
        - Go: Comment block before function
        - TypeScript: JSDoc block before function

        Args:
            node: AST node (function or class definition)
            content: Source code as bytes

        Returns:
            Docstring text or None if not found
        """
        if self.language == "python":
            return self._extract_python_docstring(node, content)
        elif self.language == "go":
            return self._extract_go_docstring(node, content)
        elif self.language in ["typescript", "javascript"]:
            return self._extract_js_docstring(node, content)

        return None

    def extract_go_type_docstring(
        self, declaration: Node, type_name: str, content: bytes
    ) -> Optional[str]:
        """
        Extract the doc comment for one type in a Go `type` declaration.

        A grouped `type ( ... )` holds several types, each documented by the
        comment directly above its own spec, while the comment above `type (`
        documents the group. go/doc uses a type's own comment and falls back to
        the group's only when the type has none, and so does this. Reading the
        declaration alone gave every type in a group the group's comment and
        dropped their own.

        Args:
            declaration: The `type_declaration` node
            type_name: Name of the type being chunked
            content: Source code as bytes

        Returns:
            Docstring text or None if not found
        """
        for spec in declaration.named_children:
            if spec.type != "type_spec":
                continue
            name = spec.child_by_field_name("name")
            if name is not None and self._get_node_text(name, content) == type_name:
                own = self._extract_go_docstring(spec, content)
                if own:
                    return own
                break
        return self._extract_go_docstring(declaration, content)

    # Private helper methods

    def _extract_node_name(self, node: Node, content: bytes) -> Optional[str]:
        """Extract the name of a node if it's a named scope."""
        # Language-specific node types that have names
        named_types = {
            "python": ["class_definition", "function_definition"],
            "go": ["function_declaration", "method_declaration", "type_declaration", "type_spec"],
            "typescript": ["class_declaration", "function_declaration", "method_definition"],
            "javascript": ["class_declaration", "function_declaration", "method_definition"],
        }

        lang_types = named_types.get(self.language, [])

        if node.type not in lang_types:
            return None

        # A Go method's name is a `field_identifier`, which the child scan below
        # does not look for -- so use the grammar's `name` field directly.
        if node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            return self._get_node_text(name_node, content) if name_node else None

        # Find the name child node
        for child in node.children:
            if child.type in ["identifier", "type_identifier", "property_identifier"]:
                return self._get_node_text(child, content)

            # For Go type_spec, look deeper
            if node.type == "type_spec" and child.type == "type_identifier":
                return self._get_node_text(child, content)

        return None

    def _is_scope_node(self, node: Node) -> bool:
        """Check if node represents a meaningful scope (class, function)."""
        scope_types = {
            "python": ["class_definition", "function_definition"],
            "go": ["function_declaration", "method_declaration", "type_declaration"],
            "typescript": ["class_declaration", "function_declaration", "method_definition"],
            "javascript": ["class_declaration", "function_declaration", "method_definition"],
        }

        lang_types = scope_types.get(self.language, [])
        return node.type in lang_types

    def _go_receiver_type(self, node: Node, content: bytes) -> Optional[str]:
        """Return the base type a Go method is declared on.

        `(h *RepositoriesHandler)` -> "RepositoriesHandler"
        `(c Client)`               -> "Client"
        `(s *Stack[T])`            -> "Stack"

        The first `type_identifier` in document order within the receiver is the
        base type; any later ones are type arguments.
        """
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            return None
        stack = [receiver]
        while stack:
            current = stack.pop()
            if current.type == "type_identifier":
                return self._get_node_text(current, content)
            stack.extend(reversed(current.children))
        return None

    def _extract_scope_signature(self, node: Node, content: bytes) -> str:
        """Extract the signature line of a scope node."""
        # Get first line of the node (the signature)
        lines = self._get_node_text(node, content).split("\n")
        if lines:
            # Return first line, stripped and truncated if too long
            signature = lines[0].strip()
            if len(signature) > 100:
                signature = signature[:97] + "..."
            return signature
        return ""

    def _extract_python_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """Extract docstring from Python function or class."""
        # Look for string literal as first statement in body
        for child in node.children:
            if child.type == "block":
                for statement in child.children:
                    if statement.type == "expression_statement":
                        for expr_child in statement.children:
                            if expr_child.type == "string":
                                docstring = self._get_node_text(expr_child, content)
                                # Remove quotes
                                docstring = docstring.strip('"""').strip("'''")
                                docstring = docstring.strip('"').strip("'")
                                return docstring.strip()
                        break
                break
        return None

    def _extract_go_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """Extract the doc comment directly above a Go declaration.

        Tree-sitter makes each `//` line its own `comment` node, so a doc comment
        is a run of comment siblings, each ending on the line directly above the
        next. The earlier version read only the single previous sibling and so
        kept the LAST line of a multi-line comment -- for AppJWT,
        "installation tokens." instead of "AppJWT mints a short-lived RS256
        token identifying the App itself."

        Stops at a blank line, since Go does not treat a separated comment as
        attached. Skips `//go:` directive lines, which are compiler instructions
        rather than documentation. A comment trailing code on the same line is
        not documentation either.
        """
        lines: List[str] = []
        expected_end_row = node.start_point[0] - 1
        prev = node.prev_sibling
        while (
            prev is not None
            and prev.type == "comment"
            and prev.end_point[0] == expected_end_row
        ):
            before = prev.prev_sibling
            if before is not None and before.end_point[0] == prev.start_point[0]:
                break  # trails a line of code; not a doc comment
            lines[:0] = self._clean_go_comment(self._get_node_text(prev, content))
            expected_end_row = prev.start_point[0] - 1
            prev = before

        text = "\n".join(line for line in lines if not line.startswith("go:")).strip()
        return text or None

    @staticmethod
    def _clean_go_comment(raw: str) -> List[str]:
        """Strip comment markers from one `//` line or one `/* */` block."""
        if raw.startswith("//"):
            body = raw[2:]
            return [body[1:] if body.startswith(" ") else body]
        if raw.startswith("/*"):
            inner = raw[2:-2] if raw.endswith("*/") else raw[2:]
            return [ln.strip().lstrip("*").strip() for ln in inner.splitlines()]
        return [raw]

    def _extract_js_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """Extract JSDoc comment before TypeScript/JavaScript function."""
        # JSDoc comments are previous siblings with type "comment"
        if not node.prev_sibling:
            return None

        prev = node.prev_sibling

        # JSDoc comments look like /** ... */
        if prev.type == "comment":
            comment_text = self._get_node_text(prev, content)
            if comment_text.startswith("/**"):
                # Clean up JSDoc formatting
                comment_text = comment_text.strip("/**").strip("*/")
                # Remove leading * from each line
                lines = [line.strip().lstrip("*").strip() for line in comment_text.split("\n")]
                return " ".join(filter(None, lines))

        return None

    def _get_node_text(self, node: Node, content: bytes) -> str:
        """Extract text from a node."""
        return content[node.start_byte:node.end_byte].decode("utf8")
