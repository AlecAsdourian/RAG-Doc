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
