"""The quality harness's measurement plumbing for the 22-03 equivalence gate.

No database, no OpenAI: the compose guard, the query-vector cache, the pin that
makes an engine answer a question with its cached vector, and the hash that
compare_runs.py refuses on.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_quality_harness.py"
_spec = importlib.util.spec_from_file_location("rag_quality_harness", _SCRIPT)
harness = importlib.util.module_from_spec(_spec)
sys.modules["rag_quality_harness"] = harness
_spec.loader.exec_module(harness)

SCRATCH_PG = "postgresql://user:hunter2@127.0.0.1:55432/scratch"
COMPOSE_PG = "postgresql://coderag:hunter2@127.0.0.1:5434/coderag"


NO_ENV: dict = {}


class TestComposeGuard:
    """--ingest and --clear refuse compose's port without --allow-compose (A7).

    Every check passes an explicit environment, so the developer's own PGPORT
    cannot make these pass or fail; and no refusal may echo the DSN.
    """

    @pytest.mark.parametrize("action", ["ingest", "clear"])
    def test_refuses_compose_postgres_by_port(self, action):
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose(action, COMPOSE_PG, allow_compose=False, env=NO_ENV)
        message = str(raised.value)
        assert f"--{action} refused" in message
        assert "5434" in message and "--allow-compose" in message
        assert "hunter2" not in message, "the refusal must not echo the DSN"

    def test_the_harness_default_is_compose_and_is_refused(self):
        """The default DSN points at compose (port 5434); the guard must catch exactly that."""
        assert harness.dsn_port(harness.DEFAULT_PG, NO_ENV) == 5434
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("ingest", harness.DEFAULT_PG, allow_compose=False, env=NO_ENV)
        assert "5434" in str(raised.value)

    def test_allow_compose_lets_it_through(self):
        harness.refuse_compose("ingest", COMPOSE_PG, allow_compose=True, env=NO_ENV)

    def test_scratch_ports_pass(self):
        harness.refuse_compose("ingest", SCRATCH_PG, allow_compose=False, env=NO_ENV)
        harness.refuse_compose("clear", SCRATCH_PG + "?options=-c%20role%3Drag_doc_app", allow_compose=False,
                               env=NO_ENV)

    def test_port_parsing_handles_url_and_keyword_forms(self):
        assert harness.dsn_port("postgresql://u:p@h:5434/db?options=-c%20role%3Drag_doc_app", NO_ENV) == 5434
        assert harness.dsn_port("host=h port=5434 dbname=db user=u", NO_ENV) == 5434
        assert harness.dsn_port("postgresql://u:p@h/db", NO_ENV) == 5432
        assert harness.compose_targets("postgresql://u:p@h:5434/db", NO_ENV) == ["Postgres on port 5434"]
        assert harness.compose_targets("postgresql://u:p@h:5433/db", NO_ENV) == []

    # libpq's environment fills in what the DSN omits (reviewer B, PR #53).

    @pytest.mark.parametrize("dsn", ["postgresql://u:hunter2@127.0.0.1/db", "host=127.0.0.1 dbname=db user=u password=hunter2"])
    def test_a_port_less_dsn_takes_its_port_from_pgport(self, dsn):
        assert harness.dsn_port(dsn, {"PGPORT": "5434"}) == 5434
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("ingest", dsn, allow_compose=False, env={"PGPORT": "5434"})
        message = str(raised.value)
        assert "5434" in message and "PGPORT" in message and "hunter2" not in message
        # The same DSN with PGPORT pointing elsewhere, or unset, is a scratch target.
        harness.refuse_compose("ingest", dsn, allow_compose=False, env={"PGPORT": "55432"})
        harness.refuse_compose("ingest", dsn, allow_compose=False, env=NO_ENV)

    def test_a_host_less_dsn_reports_pghost_for_context(self):
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("clear", "dbname=db user=u password=hunter2", allow_compose=False,
                                   env={"PGPORT": "5434", "PGHOST": "localhost"})
        message = str(raised.value)
        assert "PGHOST=localhost" in message and "hunter2" not in message

    def test_a_service_dsn_is_refused_because_its_port_cannot_be_known(self):
        for dsn in ("service=compose", "postgresql://u:hunter2@/db?service=compose"):
            with pytest.raises(harness.UnknownTarget):
                harness.dsn_port(dsn, NO_ENV)
            with pytest.raises(SystemExit) as raised:
                harness.refuse_compose("ingest", dsn, allow_compose=True, env=NO_ENV)
            message = str(raised.value)
            assert "--ingest refused" in message and "service" in message and "hunter2" not in message

    def test_pgservice_with_a_port_less_dsn_is_refused_but_an_explicit_port_wins(self):
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("ingest", "postgresql://u:hunter2@127.0.0.1/db", allow_compose=True,
                                   env={"PGSERVICE": "compose"})
        assert "PGSERVICE" in str(raised.value) and "hunter2" not in str(raised.value)
        # An explicit port in the DSN overrides any service file, so it is decidable.
        harness.refuse_compose("ingest", SCRATCH_PG, allow_compose=False, env={"PGSERVICE": "compose"})

    @pytest.mark.parametrize("dsn", [
        "postgresql://u:hunter2@a:5434,b:5432/db",
        "host=a,b port=5434,5432 dbname=db user=u password=hunter2",
        "postgresql://u:hunter2@a,b/db",
    ])
    def test_a_multi_host_dsn_is_refused_not_crashed(self, dsn):
        with pytest.raises(harness.UnknownTarget):
            harness.dsn_port(dsn, NO_ENV)
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("clear", dsn, allow_compose=True, env=NO_ENV)
        message = str(raised.value)
        assert "more than one host" in message and "hunter2" not in message

    def test_a_dsn_that_does_not_parse_is_refused(self):
        with pytest.raises(SystemExit) as raised:
            harness.refuse_compose("ingest", "this is not a dsn = = hunter2", allow_compose=True, env=NO_ENV)
        assert "could not be parsed" in str(raised.value) and "hunter2" not in str(raised.value)


class TestQueryVectors:
    def test_a_missing_question_is_embedded_once_and_read_back_from_disk(self, tmp_path):
        path = tmp_path / "vecs.json"
        calls = []

        def embed(texts):
            calls.append(list(texts))
            return [[0.1 * (i + 1), -0.5, 1e-7] for i, _ in enumerate(texts)]

        cache = harness.QueryVectors(path)
        questions = [{"id": "q1", "question": "first?"}, {"id": "q2", "question": "second?"}]
        assert cache.ensure(questions, embed, "text-embedding-ada-002") == 2
        assert calls == [["first?", "second?"]]
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["q1"] == {"question": "first?", "model": "text-embedding-ada-002", "vector": [0.1, -0.5, 1e-7]}
        assert cache.vector("q2") == on_disk["q2"]["vector"]

        # A second run reads the file and embeds nothing.
        again = harness.QueryVectors(path)
        assert again.ensure(questions, embed, "text-embedding-ada-002") == 0
        assert calls == [["first?", "second?"]]
        assert again.vector("q1") == cache.vector("q1")

    def test_only_the_missing_questions_are_embedded(self, tmp_path):
        path = tmp_path / "vecs.json"
        cache = harness.QueryVectors(path)
        cache.ensure([{"id": "q1", "question": "first?"}], lambda texts: [[1.0]], "m")
        calls = []

        def embed(texts):
            calls.append(list(texts))
            return [[2.0]]

        assert cache.ensure([{"id": "q1", "question": "first?"}, {"id": "q2", "question": "second?"}], embed, "m") == 1
        assert calls == [["second?"]]

    def test_a_changed_question_text_or_model_is_refused(self, tmp_path):
        path = tmp_path / "vecs.json"
        cache = harness.QueryVectors(path)
        cache.ensure([{"id": "q1", "question": "first?"}], lambda texts: [[1.0]], "m")
        with pytest.raises(SystemExit, match="different question text"):
            cache.ensure([{"id": "q1", "question": "changed?"}], lambda texts: [[1.0]], "m")
        with pytest.raises(SystemExit, match="never compared across models"):
            cache.ensure([{"id": "q1", "question": "first?"}], lambda texts: [[1.0]], "other-model")


class TestPin:
    @staticmethod
    def _engine():
        client = SimpleNamespace(generate_embeddings_batch=lambda texts: [[9.9] for _ in texts])
        return SimpleNamespace(
            vector_retriever=SimpleNamespace(embedding_generator=SimpleNamespace(client=client))
        )

    def test_the_pinned_question_gets_its_cached_vector(self):
        engine = self._engine()
        harness.pin_query_vector(engine, "how is a query embedded", [0.25, 0.5])
        client = engine.vector_retriever.embedding_generator.client
        assert client.generate_embeddings_batch(["how is a query embedded"]) == [[0.25, 0.5]]

    def test_any_other_text_raises_instead_of_embedding(self):
        engine = self._engine()
        harness.pin_query_vector(engine, "the pinned question", [0.25])
        client = engine.vector_retriever.embedding_generator.client
        with pytest.raises(RuntimeError, match="unexpected embedding call"):
            client.generate_embeddings_batch(["another question"])
        with pytest.raises(RuntimeError):
            client.generate_embeddings_batch(["the pinned question", "and one more"])


def test_vector_hash_is_over_the_json_float_list():
    vector = [0.1, -0.5, 1e-7]
    expected = __import__("hashlib").sha256(b"[0.1,-0.5,1e-07]").hexdigest()
    assert harness.vector_sha256(vector) == expected
    assert harness.vector_sha256(json.loads(json.dumps(vector))) == expected
    assert harness.vector_sha256([0.1, -0.5, 2e-7]) != expected


def test_vector_literal_is_the_retrievers_one_and_is_pgvectors_input_form():
    from workers.retrieval import vector_retriever

    assert harness.vector_literal is vector_retriever.vector_literal, "one function, not a copy"
    assert harness.vector_literal([0.1, -0.5, 1e-7]) == "[0.1,-0.5,1e-07]"


# ---------------------------------------------------------------------------
# 22.2-01: --self-root, the record header, and the tie tail's tolerance
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def self_tree(tmp_path):
    """A tiny `self` tree, its own git checkout, with one Go and one Python file."""
    root = tmp_path / "tree"
    (root / "services" / "backend" / "pkg" / "db").mkdir(parents=True)
    (root / "services" / "workers" / "workers" / "chunker").mkdir(parents=True)
    (root / "services" / "backend" / "pkg" / "db" / "tenant.go").write_text(
        "package db\n\nfunc Scope() {}\n", encoding="utf-8")
    (root / "services" / "backend" / "pkg" / "db" / "tenant_test.go").write_text(
        "package db\n", encoding="utf-8")
    (root / "services" / "workers" / "workers" / "chunker" / "split.py").write_text(
        "def split(text):\n    return [text]\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root, _git(root, "rev-parse", "HEAD")


class TestSelfRoot:
    def test_self_root_changes_which_files_self_collects(self, self_tree, tmp_path):
        root, head = self_tree
        corpus = harness.load_corpus("self", tmp_path, self_root=root)
        assert corpus.root == root
        assert [p for p, _, _ in harness.collect_files(corpus)] == [
            "services/backend/pkg/db/tenant.go", "services/workers/workers/chunker/split.py"]
        default = harness.load_corpus("self", tmp_path)
        assert default.root == harness.REPO_ROOT
        assert len(harness.collect_files(default)) > 50, "the default is this checkout's own self corpus"

    def test_the_commit_is_the_trees_head_or_self_commit(self, self_tree, tmp_path):
        root, head = self_tree
        assert harness.load_corpus("self", tmp_path, self_root=root).commit == head
        assert harness.load_corpus("self", tmp_path, self_root=root, self_commit="abc").commit == "abc"
        # An export (no .git) and a directory merely inside a checkout name no commit.
        export = tmp_path / "export"
        export.mkdir()
        assert harness.load_corpus("self", tmp_path, self_root=export).commit is None
        assert harness.tree_commit(root / "services") is None

    def test_the_header_records_the_self_roots_commit_and_what_was_measured(self, self_tree, tmp_path):
        root, head = self_tree
        corpus = harness.load_corpus("self", tmp_path, self_root=root)
        measured = {"database": {}, "connections": {}, "chunks_visible": 3, "explain": None,
                    "chunk_set_digest": "d" * 64, "chunk_rows": 3, "chunk_models": {"text-embedding-ada-002": 3}}
        header = harness.run_header(corpus, "all", 5, None, "text-embedding-ada-002", 2e-6, measured)
        assert header["corpus_commit"] == head and header["commit"] == head
        assert header["chunker_version"] == harness.chunk_digest.chunker_version()
        assert header["vector_tolerance"] == 2e-6
        assert header["embedding_model"] == "text-embedding-ada-002"
        for key, value in measured.items():
            assert header[key] == value
        other = harness.run_header(harness.load_corpus("self", tmp_path, self_root=tmp_path, self_commit="x"),
                                   "all", 5, None, "m", 1e-5, measured)
        assert other["corpus_commit"] == "x"

    def test_the_offline_digest_is_the_census_definition(self, self_tree, tmp_path):
        root, _ = self_tree
        files = harness.collect_files(harness.load_corpus("self", tmp_path, self_root=root))
        chunker = harness.SemanticChunker()
        chunks = [c for p, content, lang in files for c in chunker.chunk_file(p, content, lang)]
        assert harness.offline_chunk_set(files) == harness.chunk_digest.digest_of_chunks(chunks)


def test_the_database_digest_reads_the_runs_model_under_the_tenant():
    """The header's digest is over the rows with the run's model only, read
    through chunk_digest's one statement (no copy of it here)."""
    import inspect

    source = inspect.getsource(harness.database_chunk_set)
    assert "chunk_digest.DB_ROWS_SQL" in source and "require_tenant(conn, ORG)" in source
    assert "(str(corpus.repository_id), model)" in source


class TestExactTieTail:
    @staticmethod
    def _rows(extra):
        rows = [(f"c{i:02d}", 0.1 + i * 1e-3) for i in range(harness.EXACT_LIMIT)]
        last = rows[-1][1]
        return rows + [(cid, last + d) for cid, d in extra]

    def test_the_tail_follows_the_tolerance(self):
        rows = self._rows([("near", 1.5e-6), ("mid", 5e-6), ("far", 5e-5)])
        wide = harness.exact_list(rows, 1e-5)
        narrow = harness.exact_list(rows, 2e-6)
        assert len(wide["top"]) == harness.EXACT_LIMIT
        assert [e["chunk_id"] for e in wide["tail_ties"]] == ["near", "mid"]
        assert [e["chunk_id"] for e in narrow["tail_ties"]] == ["near"]
        assert wide["tail_complete"] and narrow["tail_complete"]

    def test_the_default_is_22_03s(self):
        assert harness.DEFAULT_VECTOR_TOLERANCE == 1e-5
