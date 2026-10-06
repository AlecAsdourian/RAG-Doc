"""The chunk census tool, the chunk-set digest and the chunker version (22.2-01).

No database, no OpenAI. Every fixture is small and in-memory, run through the
repository's own `SemanticChunker`, so a test reads what the census reads.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import sys
from importlib import metadata

import pytest
from tree_sitter import Query

_RAG = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_benchmarks"
_spec = importlib.util.spec_from_file_location("chunk_census", _RAG / "chunk_census.py")
chunk_census = importlib.util.module_from_spec(_spec)
sys.modules["chunk_census"] = chunk_census
_spec.loader.exec_module(chunk_census)
chunk_digest = chunk_census.chunk_digest

from workers.chunker.models import Chunk  # noqa: E402
from workers.embeddings.embedding_generator import EmbeddingGenerator  # noqa: E402

WORKERS = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tools():
    return chunk_census.make_tools()


def _census(tools, files, name="fixture"):
    chunker, enc, grammars = tools
    r, _ = chunk_census.census(name, files, {"commit": "fixture"}, chunker, enc, grammars)
    return r


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------

ROW = ("pkg/a.py", 3, 9, "function", "def a():\n    return 1", "a", "Return one.")


def _digest(*rows):
    return chunk_digest.chunk_set_digest(chunk_digest.chunk_row(*r) for r in rows)


class TestDigest:
    def test_row_order_does_not_matter(self):
        other = ("pkg/b.py", 1, 2, "class", "class B: pass", "B", None)
        assert _digest(ROW, other) == _digest(other, ROW)

    @pytest.mark.parametrize("field, value", [
        (4, "def a():\n    return 2"),   # one content byte
        (1, 4),                           # a line
        (2, 10),
        (3, "class"),                     # the type
        (5, "pkg.a"),                     # the breadcrumb
        (6, "Return two."),               # the docstring
        (0, "pkg/b.py"),
    ])
    def test_any_one_field_changes_the_digest(self, field, value):
        changed = list(ROW)
        changed[field] = value
        assert _digest(tuple(changed)) != _digest(ROW)

    def test_the_docstring_alone_changes_the_digest(self):
        """Two arms with equal content and different docstrings embed different
        text (breadcrumb, then docstring, then content), so they must not pass as
        identical (QD8)."""
        assert _digest(ROW[:6] + ("another docstring",)) != _digest(ROW)
        assert _digest(ROW[:6] + (None,)) != _digest(ROW)

    def test_none_is_hashed_as_empty(self):
        assert _digest(ROW[:5] + (None, None)) == _digest(ROW[:5] + ("", ""))

    def test_two_identical_rows_do_not_digest_as_one(self):
        assert _digest(ROW, ROW) != _digest(ROW)

    def test_chunks_and_a_database_read_of_them_give_one_digest(self, tools):
        chunker, _, _ = tools
        source = 'class A:\n    """Doc A."""\n\n    def run(self):\n        """Runs."""\n        return 1\n\n\ndef helper():\n    return 2\n'
        chunks = chunker.chunk_file("pkg/a.py", source, "python")
        assert any(c.metadata.get("docstring") for c in chunks), "the fixture must carry a docstring"
        # The writer stores the chunker's metadata whole as jsonb; the read takes
        # metadata->>'breadcrumb' and metadata->>'docstring' (NULL when absent).
        db_rows = []
        for c in chunks:
            stored = json.loads(json.dumps(c.metadata))
            db_rows.append((c.file_path, c.start_line, c.end_line, c.chunk_type, c.content,
                            stored.get("breadcrumb"), stored.get("docstring")))
        assert chunk_digest.digest_of_chunks(chunks) == chunk_digest.digest_of_db_rows(reversed(db_rows))
        assert chunk_digest.digest_of_chunks(chunks)[1] == len(chunks)

    def test_the_database_read_names_the_fields_the_digest_hashes(self):
        sql = " ".join(chunk_digest.DB_ROWS_SQL.split())
        assert ("SELECT file_path, start_line, end_line, chunk_type, content, "
                "metadata->>'breadcrumb', metadata->>'docstring'") in sql
        assert "WHERE repository_id = %s AND embedding_model = %s" in sql


# ---------------------------------------------------------------------------
# The chunker version
# ---------------------------------------------------------------------------


class TestChunkerVersion:
    @staticmethod
    def _copy(tmp_path):
        for rel in chunk_digest.CHUNKER_DIRS:
            shutil.copytree(WORKERS / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
        return tmp_path

    def test_it_is_sixteen_hex_characters_and_stable(self):
        v = chunk_digest.chunker_version()
        assert re.fullmatch(r"[0-9a-f]{16}", v)
        assert chunk_digest.chunker_version() == v

    def test_a_copy_names_the_same_version(self, tmp_path):
        assert chunk_digest.chunker_version(self._copy(tmp_path)) == chunk_digest.chunker_version()

    @pytest.mark.parametrize("rel", ["workers/chunker/semantic_chunker.py", "workers/parser/tree_sitter_parser.py"])
    def test_one_byte_of_a_parser_or_chunker_file_changes_it(self, tmp_path, rel):
        root = self._copy(tmp_path)
        before = chunk_digest.chunker_version(root)
        path = root / rel
        path.write_bytes(path.read_bytes() + b"#")
        assert chunk_digest.chunker_version(root) != before

    @pytest.mark.parametrize("rel", ["workers/chunker/test_semantic_chunker_go.py", "workers/parser/test_parser.py"])
    def test_a_test_file_does_not_change_it(self, tmp_path, rel):
        root = self._copy(tmp_path)
        before = chunk_digest.chunker_version(root)
        path = root / rel
        assert path.exists(), f"{rel} must exist for this test to mean anything"
        path.write_bytes(path.read_bytes() + b"# a test changed\n")
        assert chunk_digest.chunker_version(root) == before

    def test_line_endings_do_not_change_it(self, tmp_path):
        """An all-LF copy and an all-CRLF copy name one version, the checkout's own.

        Both copies are written explicitly, so the test can fail on any checkout:
        comparing a copy with itself converted would pass on a CRLF checkout
        whatever the code did (review A, finding 4)."""
        lf = self._copy(tmp_path / "lf")
        crlf = self._copy(tmp_path / "crlf")
        for root, ending in ((lf, b"\n"), (crlf, b"\r\n")):
            for _, path in chunk_digest.chunker_files(root):
                data = path.read_bytes().replace(b"\r\n", b"\n")
                path.write_bytes(data.replace(b"\n", ending))
        some = chunk_digest.chunker_files(lf)[0][1]
        assert b"\r\n" not in some.read_bytes()
        assert b"\r\n" in (crlf / some.relative_to(lf)).read_bytes()
        assert chunk_digest.chunker_version(lf) == chunk_digest.chunker_version(crlf) == chunk_digest.chunker_version()

    def test_a_parser_package_version_changes_it(self, monkeypatch):
        before = chunk_digest.chunker_version()
        real = metadata.version
        monkeypatch.setattr(metadata, "version",
                            lambda name: "0.0.1" if name == "tree-sitter-go" else real(name))
        assert chunk_digest.chunker_version() != before


# ---------------------------------------------------------------------------
# The embedded text: a copy, pinned to the generator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metadata_", [
    {"breadcrumb": "Service.run", "docstring": "Runs the service."},
    {"breadcrumb": "Service.run"},
    {"docstring": "Runs the service."},
    {},
    {"breadcrumb": "", "docstring": ""},
])
def test_embed_text_is_the_generators_rule(metadata_):
    chunk = Chunk(content="def run(self):\n    return 1", file_path="svc.py", start_line=1, end_line=2,
                  language="python", chunk_type="function", metadata=dict(metadata_))
    generator = EmbeddingGenerator(api_key="offline")
    assert chunk_census.embed_text(chunk) == generator._prepare_text_for_embedding(chunk)


# ---------------------------------------------------------------------------
# Fallback reasons, and the fields that catch a silent query
# ---------------------------------------------------------------------------

PY_PLAIN = "X = 1\nY = 2\n"
PY_FUNC = "def run():\n    return 1\n"


def test_a_parser_that_raises_is_counted_as_raised(tools, monkeypatch):
    chunker, _, _ = tools

    def broken(content, language):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(chunker.parser, "extract_functions", lambda *a, **k: broken(None, None))
    r = _census(tools, [("pkg/a.py", PY_FUNC, "python")])
    assert r["fallback_reasons"]["python"] == {"raised": 1, "no_chunk": 0, "unsupported": 0}
    assert r["fallback_files_fixed_size_only"] == 1


def test_a_file_with_no_function_or_class_is_counted_as_no_chunk(tools):
    r = _census(tools, [("pkg/consts.py", PY_PLAIN, "python"), ("pkg/a.py", PY_FUNC, "python")])
    assert r["fallback_reasons"]["python"] == {"raised": 0, "no_chunk": 1, "unsupported": 0}


def test_a_census_leaves_logging_as_it_found_it(tools, caplog):
    """Run inside a test session, the census must not quiet anyone else's logs:
    a later test reading a workers.* warning would otherwise see nothing."""
    import logging

    chunker_logger = logging.getLogger(chunk_census.CHUNKER_LOGGER)
    _census(tools, [("pkg/consts.py", PY_PLAIN, "python")])
    # The ABSOLUTE pristine state, not a before/after pair: earlier tests in this
    # file run censuses too, so a leak would already be in a "before" (review B,
    # finding 2). Nothing in the suite calls attach_capture(), the CLI's
    # process-wide variant.
    assert logging.getLogger("workers").level == logging.NOTSET
    assert chunker_logger.level == logging.NOTSET
    assert chunker_logger.propagate is True
    assert not [h for h in chunker_logger.handlers if isinstance(h, chunk_census.FallbackCapture)]
    with caplog.at_level(logging.WARNING, logger="workers"):
        tools[0].chunk_file("pkg/consts.py", PY_PLAIN, "python")
    assert any("produced no chunks" in r.getMessage() for r in caplog.records)


def test_an_unsupported_language_is_counted_as_unsupported(tools):
    r = _census(tools, [("src/lib.rs", "fn main() {}\n", "rust")])
    assert r["fallback_reasons"]["rust"] == {"raised": 0, "no_chunk": 0, "unsupported": 1}


TS_SOURCE = """\
export const handler = async (req: Request) => {
  return req;
};

class Store {
  /** Saves it. */
  @Log()
  save(item: string): void {
    console.log(item);
  }
}

/** Loads it. */
export function load(id: string): string {
  return id;
}

/** Not adjacent. */

function unload(): void {}
"""

PY_DECORATED = """\
class Service:
    @property
    def name(self):
        return "x"
"""

GO_GROUPED = """\
package model

type (
\tUser struct {
\t\tID int
\t}
\tTeam struct {
\t\tName string
\t}
)

func Run() {}
"""


def _swap_queries_for_ones_that_match_nothing(chunker):
    """A test double for a query that compiles and matches nothing (22.2-02 found one)."""
    for lang, queries in chunker.parser.queries.items():
        language = chunker.parser.languages[lang]
        for kind in ("functions", "classes"):
            queries[kind] = Query(language, "(ERROR) @function")


def test_silent_queries_show_up_as_zero_named_chunks_and_as_files_with_declarations(monkeypatch):
    chunker, enc, grammars = chunk_census.make_tools()
    _swap_queries_for_ones_that_match_nothing(chunker)
    files = [("web/a.ts", TS_SOURCE, "typescript"), ("svc/b.py", PY_DECORATED, "python"),
             ("model/c.go", GO_GROUPED, "go")]
    r, _ = chunk_census.census("silent", files, {"commit": "x"}, chunker, enc, grammars)
    assert r["named_chunks_by_extension"] == {ext: {"function": 0, "class": 0} for ext in (".go", ".py", ".ts")}
    silent = r["files_with_declarations_but_no_named_chunk"]
    assert silent["count"] == 3
    assert silent["files"] == ["model/c.go", "svc/b.py", "web/a.ts"]
    # Every one of them is a "no chunk" fallback, which on its own says nothing.
    assert sum(v["no_chunk"] for v in r["fallback_reasons"].values()) == 3


def test_with_working_queries_no_file_is_counted_as_silent(tools):
    files = [("svc/b.py", PY_DECORATED, "python"), ("model/c.go", GO_GROUPED, "go")]
    r = _census(tools, files)
    assert r["files_with_declarations_but_no_named_chunk"]["count"] == 0
    assert r["named_chunks_by_extension"][".py"] == {"function": 1, "class": 1}
    assert r["named_chunks_by_extension"][".go"]["function"] == 1


# ---------------------------------------------------------------------------
# Small fixtures per language count what the records count
# ---------------------------------------------------------------------------


def test_typescript_fixture_counts(tools):
    r = _census(tools, [("web/a.ts", TS_SOURCE, "typescript")])
    ts = r["typescript"]
    assert ts["real_top_level_const_function"] == 1
    # const handler, class Store, method save, function load, function unload
    assert ts["sim_chunkable_declarations"] == 5
    # load (its export is directly below the block) and save (its first decorator
    # is); unload's block is a blank line away.
    assert ts["sim_chunkable_with_jsdoc_directly_above"] == 2
    # The research's upper bound: a `/**` previous sibling, adjacent or not, and
    # not looking past a decorator: load and unload.
    assert ts["real_declarations_with_jsdoc_above"] == 2
    assert ts["real_decorators"] == 1
    assert r["parse_errors_with_typescript_grammar"]["typescript.ts"]["files_with_errors"] == 0


def test_python_decorated_method_counts(tools):
    r = _census(tools, [("svc/b.py", PY_DECORATED, "python")])
    py = r["python"]
    assert py["decorated_function_definition"] == 1
    assert py["decorators_only_inside_a_class_chunk"] == 1
    assert py["decorators_in_no_chunk"] == 0


def test_go_grouped_type_counts(tools):
    r = _census(tools, [("model/c.go", GO_GROUPED, "go")])
    assert r["go_package_types_total"] == 2
    assert r["go_package_types_without_chunk"] == 0
    assert r["duplication"]["go_struct_chunks_in_groups"] == 2
    assert r["chunks_by_type"]["class"] == 2


def test_the_census_carries_the_digest_and_the_version(tools):
    chunker, _, _ = tools
    files = [("svc/b.py", PY_DECORATED, "python")]
    r = _census(tools, files)
    chunks = chunker.chunk_file("svc/b.py", PY_DECORATED, "python")
    assert (r["chunk_set_digest"], r["chunk_rows"]) == chunk_digest.digest_of_chunks(chunks)
    assert r["chunker_version"] == chunk_digest.chunker_version()


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def test_requirements_pin_every_package_the_census_measures_with():
    pins = chunk_census.required_pins()
    assert set(pins) == set(chunk_census.PINNED_PACKAGES)
    assert all(pins.values()), pins
    assert chunk_census.pin_mismatches() == [], "this venv must be built from requirements.txt"


def test_a_different_installed_version_is_refused_with_exit_2(monkeypatch, tmp_path, capsys):
    real = metadata.version
    monkeypatch.setattr(metadata, "version",
                        lambda name: "9.9.9" if name == "tree-sitter-typescript" else real(name))
    code = chunk_census.main(["--corpora", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 2
    assert "REFUSED" in out and "tree-sitter-typescript 9.9.9" in out
    assert not (tmp_path / "out").exists(), "nothing may be measured after a refusal"


# ---------------------------------------------------------------------------
# The self split
# ---------------------------------------------------------------------------


def test_self_go_plus_self_py_is_exactly_self():
    h = chunk_census.harness()
    # A checkout names its own HEAD (a different claim would be refused); a
    # clean export of the tests has none, and takes the claim.
    commit = h.tree_commit(h.REPO_ROOT) or "export-commit"
    args = (WORKERS, h.REPO_ROOT, commit)
    self_files, meta = chunk_census.corpus_files("self", *args)
    go, _ = chunk_census.corpus_files("self-go", *args)
    py, _ = chunk_census.corpus_files("self-py", *args)
    assert meta["commit"] == commit
    assert sorted(f[0] for f in go + py) == sorted(f[0] for f in self_files)
    assert len(go) + len(py) == len(self_files)
    assert {f[2] for f in go} == {"go"} and {f[2] for f in py} == {"python"}
    # The research's self-go excluded only `_test\.go$`; the harness's SELF_EXCLUDE
    # adds `(^|/)test_[^/]*$`. The split reproduces it while no Go file is named so.
    assert not [f[0] for f in go if re.search(r"(^|/)test_[^/]*$", f[0])]
    assert h.SELF_EXCLUDE == [r"(^|/)test_[^/]*$", r"_test\.go$"]


def test_self_is_read_from_the_self_root(tmp_path):
    (tmp_path / "services" / "workers" / "workers").mkdir(parents=True)
    only = tmp_path / "services" / "workers" / "workers" / "only.py"
    only.write_text("def only():\n    return 1\n", encoding="utf-8")
    files, meta = chunk_census.corpus_files("self", WORKERS, tmp_path, "abc123")
    assert [f[0] for f in files] == ["services/workers/workers/only.py"]
    assert meta == {"commit": "abc123", "corpus_dirty": None}
    with pytest.raises(SystemExit, match="--self-commit"):
        chunk_census.corpus_files("self", WORKERS, tmp_path, None)


# ---------------------------------------------------------------------------
# The corpus tree digest and the retrieval code version (review A3, A8)
# ---------------------------------------------------------------------------

FILES = [("pkg/a.py", "def a():\n    return 1\n", "python"), ("pkg/b.go", "package b\n", "go")]


class TestTreeDigest:
    def test_it_is_independent_of_order_and_line_endings(self):
        crlf = [(p, c.replace("\n", "\r\n"), lang) for p, c, lang in reversed(FILES)]
        assert chunk_digest.tree_digest(crlf) == chunk_digest.tree_digest(FILES)

    @pytest.mark.parametrize("change", ["content", "path", "dropped"])
    def test_one_file_changes_it(self, change):
        files = list(FILES)
        if change == "content":
            files[0] = (files[0][0], files[0][1] + "#", files[0][2])
        elif change == "path":
            files[0] = ("pkg/a2.py", files[0][1], files[0][2])
        else:
            files = files[1:]
        assert chunk_digest.tree_digest(files) != chunk_digest.tree_digest(FILES)

    def test_the_census_records_it(self, tools):
        r = _census(tools, FILES)
        assert r["corpus_tree_digest"] == chunk_digest.tree_digest(FILES)


def test_the_retrieval_code_version_changes_with_its_code_not_its_tests(tmp_path):
    for rel in chunk_digest.RETRIEVAL_DIRS:
        shutil.copytree(WORKERS / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    before = chunk_digest.retrieval_code_version(tmp_path)
    assert before == chunk_digest.retrieval_code_version()
    test_file = tmp_path / "workers" / "retrieval" / "test_rrf_fusion.py"
    assert test_file.exists()
    test_file.write_bytes(test_file.read_bytes() + b"# a test changed\n")
    assert chunk_digest.retrieval_code_version(tmp_path) == before
    engine = tmp_path / "workers" / "retrieval" / "query_engine.py"
    engine.write_bytes(engine.read_bytes() + b"#")
    assert chunk_digest.retrieval_code_version(tmp_path) != before
