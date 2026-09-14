"""Chunk-level behaviour for Go methods.

`test_parser.py` proves methods are extracted from the tree. These prove they
become searchable chunks whose breadcrumb names the type that owns them --
which is what the embedding text leads with, and what an agent reads to know
where it is.
"""

from workers.chunker.semantic_chunker import SemanticChunker

GO_SOURCE = '''package handlers

type RepositoriesHandler struct {
    db string
}

func (h *RepositoriesHandler) Connect() error {
    return nil
}

func (s *Stack[T]) Push(v T) {
}

func helper() int {
    return 1
}
'''


def _function_chunks(source=GO_SOURCE):
    chunks = SemanticChunker().chunk_file("pkg/api/handlers/repositories.go", source, "go")
    return {c.metadata.get("function_name"): c for c in chunks if c.chunk_type == "function"}


def test_go_method_becomes_a_function_chunk():
    fns = _function_chunks()
    assert "Connect" in fns, f"method missing from chunks; got {sorted(fns)}"
    assert fns["Connect"].content.lstrip().startswith("func (h *RepositoriesHandler) Connect")


def test_go_method_breadcrumb_names_the_receiver_type():
    """Without the receiver the breadcrumb would be a bare "Connect"."""
    fns = _function_chunks()
    assert fns["Connect"].metadata["breadcrumb"] == "RepositoriesHandler.Connect"


def test_generic_receiver_breadcrumb_drops_type_arguments():
    fns = _function_chunks()
    assert fns["Push"].metadata["breadcrumb"] == "Stack.Push"


def test_plain_function_breadcrumb_is_unchanged():
    fns = _function_chunks()
    assert fns["helper"].metadata["breadcrumb"] == "helper"


def test_type_declared_inside_a_method_names_the_method_and_its_receiver():
    """A struct declared in a method body is owned by Receiver.Method.

    This is the only path that reads a method's name through
    MetadataBuilder._extract_node_name -- the chunker takes a method's own name
    from the parser -- so without this test that branch was untested. A
    mutation check removing it left every other test green.
    """
    source = '''package handlers

func (h *Handler) Serve() {
    type response struct {
        ok bool
    }
}
'''
    chunks = SemanticChunker().chunk_file("h.go", source, "go")
    classes = {c.metadata.get("class_name"): c for c in chunks if c.chunk_type == "class"}
    assert "response" in classes, f"local struct not chunked; got {sorted(classes)}"
    assert classes["response"].metadata["breadcrumb"] == "Handler.Serve.response"


# ---------------------------------------------------------------------------
# Go doc comments. The parser extracts docstrings only for Python, and a Go doc
# comment sits above `func`, outside the chunk's line range -- so no Go doc
# comment had ever reached an embedding.
# ---------------------------------------------------------------------------
DOC_SOURCE = '''package github

// Client talks to the GitHub API as an App.
type Client struct {
    id int
}

// AppJWT mints a short-lived RS256 token identifying the App itself.
//
// The token is valid for ten minutes.
func (c *Client) AppJWT() (string, error) {
    return "", nil
}

// Detached comment, separated by a blank line.

func (c *Client) Detached() {}

//go:generate stringer -type=Kind
// Kind names a thing.
func Kind() {}

/* Block doc
   spanning lines. */
func Block() {}

var x = 1 // trailing, not a doc comment
func AfterTrailing() {}

func NoDoc() {}
'''


def _chunks_by_name(source=DOC_SOURCE):
    chunks = SemanticChunker().chunk_file("github/client.go", source, "go")
    named = {}
    for c in chunks:
        name = c.metadata.get("function_name") or c.metadata.get("class_name")
        if name:
            named[name] = c
    return named


def test_multiline_doc_comment_is_captured_whole():
    """The first line carries the meaning; the old extractor kept only the last."""
    doc = _chunks_by_name()["AppJWT"].metadata.get("docstring", "")
    assert doc.startswith("AppJWT mints a short-lived RS256 token"), doc
    assert "valid for ten minutes" in doc


def test_type_doc_comment_is_captured():
    assert _chunks_by_name()["Client"].metadata.get("docstring") == (
        "Client talks to the GitHub API as an App."
    )


def test_blank_line_detaches_a_comment():
    assert "docstring" not in _chunks_by_name()["Detached"].metadata


def test_go_directive_lines_are_not_documentation():
    assert _chunks_by_name()["Kind"].metadata.get("docstring") == "Kind names a thing."


def test_block_comment_markers_are_stripped():
    assert _chunks_by_name()["Block"].metadata.get("docstring") == "Block doc\nspanning lines."


def test_trailing_comment_on_code_is_not_documentation():
    assert "docstring" not in _chunks_by_name()["AfterTrailing"].metadata


def test_undocumented_function_has_no_docstring():
    assert "docstring" not in _chunks_by_name()["NoDoc"].metadata


def test_doc_comment_reaches_the_embedding_text():
    """The point of the fix: the English above `func` is what gets embedded."""
    from workers.embeddings.embedding_generator import EmbeddingGenerator

    generator = EmbeddingGenerator.__new__(EmbeddingGenerator)  # no API client needed
    generator.client = type("NoTokenizer", (), {"count_tokens": lambda self, text: 0})()
    generator.max_tokens_per_chunk = 8000

    text = generator._prepare_text_for_embedding(_chunks_by_name()["AppJWT"])
    assert "Client.AppJWT" in text
    assert "AppJWT mints a short-lived RS256 token identifying the App itself." in text
