"""The quality harness's measurement plumbing for the 22-03 equivalence gate.

No database, no OpenAI: the compose guard, the query-vector cache, the pin that
makes an engine answer a question with its cached vector, and the hash that
compare_runs.py refuses on.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
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

# The worker images (python:3.11-slim) have no git; these tests build a git
# checkout, so they skip there and run wherever git is installed (ISS-041).
NO_GIT = "git is not installed (e.g. python:3.11-slim); this test builds a git checkout"


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
    if shutil.which("git") is None:
        if os.environ.get("CI"):
            # On CI a silent skip would keep the job green (PR #67, review B, m6).
            pytest.fail(NO_GIT + "; CI must have git, so this is not skipped there")
        pytest.skip(NO_GIT)
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
        # A claim is taken only where there is no HEAD to check it against (TestSelfCommitIsChecked).
        assert harness.load_corpus("self", tmp_path, self_root=tmp_path / "x", self_commit="abc").commit == "abc"
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
        assert header["corpus_dirty"] is False
        assert header["retrieval_code_version"] == harness.chunk_digest.retrieval_code_version()
        # The chunker version is not run_header's to state: chunk_provenance
        # decides it from the stored rows (review A, finding 1).
        assert "chunker_version" not in header
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


class TestSelfCommitIsChecked:
    """--self-commit and --harness-commit are claims; a checkout's HEAD is a fact (review A, finding 3)."""

    def test_a_claim_the_checkout_contradicts_is_refused(self, self_tree, tmp_path):
        root, head = self_tree
        with pytest.raises(SystemExit, match="refusing a commit the tree contradicts"):
            harness.load_corpus("self", tmp_path, self_root=root, self_commit="0" * 40)
        with pytest.raises(SystemExit, match="--harness-commit"):
            harness.checked_commit(root, "f" * 12, "--harness-commit")

    def test_the_head_in_full_or_a_prefix_is_accepted(self, self_tree, tmp_path):
        root, head = self_tree
        assert harness.load_corpus("self", tmp_path, self_root=root, self_commit=head).commit == head
        assert harness.load_corpus("self", tmp_path, self_root=root, self_commit=head[:7]).commit == head
        assert harness.checked_commit(tmp_path / "no-such-export", "abc", "x") == "abc"

    def test_a_dirty_tree_is_flagged_and_a_clean_one_is_not(self, self_tree, tmp_path):
        root, _ = self_tree
        assert harness.load_corpus("self", tmp_path, self_root=root).tree_dirty is False
        target = root / "services" / "workers" / "workers" / "chunker" / "split.py"
        target.write_text(target.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        assert harness.load_corpus("self", tmp_path, self_root=root).tree_dirty is True
        export = tmp_path / "export"
        export.mkdir()
        assert harness.load_corpus("self", tmp_path, self_root=export, self_commit="x").tree_dirty is None


class TestChunkProvenance:
    """The header's chunker_version names the stored rows' chunker only when
    re-chunking now gives the stored set (review A, finding 1)."""

    def test_equal_digests_verify_the_running_version(self):
        p = harness.chunk_provenance(("d" * 64, 10), "d" * 64, "abcdef0123456789")
        assert p == {"offline_chunk_set_digest": "d" * 64, "offline_chunk_rows": 10,
                     "running_chunker_version": "abcdef0123456789", "chunker_version_verified": True,
                     "chunker_version": "abcdef0123456789"}

    def test_different_digests_record_no_chunker_version(self):
        p = harness.chunk_provenance(("d" * 64, 10), "e" * 64, "abcdef0123456789")
        assert p["chunker_version"] is None and p["chunker_version_verified"] is False
        assert p["running_chunker_version"] == "abcdef0123456789"


def test_exact_passes_the_vector_tolerance_through(tmp_path, monkeypatch):
    """--exact's tie tail is cut at --vector-tolerance (review B, finding 4)."""
    seen = {}

    def fake_do_exact(corpus, questions, vectors, model, out, tolerance=None):
        seen.update(tolerance=tolerance, out=out, questions=len(questions), model=model)

    client = SimpleNamespace(generate_embeddings_batch=lambda texts: [[0.5, 0.25] for _ in texts])
    engine = SimpleNamespace(vector_retriever=SimpleNamespace(
        embedding_generator=SimpleNamespace(model="m-test", client=client)))
    monkeypatch.setattr(harness, "QueryEngine", lambda **kwargs: engine)
    monkeypatch.setattr(harness, "do_exact", fake_do_exact)
    monkeypatch.setattr(harness, "OPENAI", "not-a-real-key")
    out = tmp_path / "exact.json"
    harness.main(["--corpus", "self", "--corpora-dir", str(tmp_path), "--set", "all",
                  "--query-vectors", str(tmp_path / "vecs.json"), "--exact", str(out),
                  "--vector-tolerance", "2e-6"])
    assert seen == {"tolerance": 2e-6, "out": out, "questions": 40, "model": "m-test"}


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


# ---------------------------------------------------------------------------
# 22.2-07: the decision sets, their independence, and --list-targets
# ---------------------------------------------------------------------------


def _spec(questions):
    return {"repository": "https://example.invalid/toy", "commit": "0" * 40,
            "roots": [{"path": ".", "extensions": [".go"], "language": "go"}], "questions": questions}


def _q(qid, set_name, path, symbol=None):
    q = {"id": qid, "set": set_name, "question": f"the text of {qid}, which no target list may show", "path": path}
    if symbol:
        q["symbol"] = symbol
    return q


class TestDecisionSets:
    def test_the_sets_are_named(self):
        assert harness.DECISION_SETS == ("confirm", "shape-model", "keyword-leg")
        assert harness.QUESTION_SETS == ("tuning", "holdout", "confirm", "shape-model", "keyword-leg")

    def test_the_new_sets_validate(self):
        harness.validate_spec(_spec([
            _q("t1", "tuning", "a.go", "A.run"), _q("h1", "holdout", "b.go", "B.run"),
            _q("c1", "confirm", "c.go", "C.run"), _q("s1", "shape-model", "d.go", "D.run"),
            _q("k1", "keyword-leg", "e.go", "E.run"),
        ]), "toy")

    def test_an_unknown_set_is_still_refused(self):
        with pytest.raises(SystemExit) as raised:
            harness.validate_spec(_spec([_q("x1", "shape_model", "a.go", "A.run")]), "toy")
        assert "x1: set must be one of tuning, holdout, confirm, shape-model, keyword-leg" in str(raised.value)

    @pytest.mark.parametrize("first, second", [
        (("t1", "tuning", "a.go", "Foo.bar"), ("s1", "shape-model", "a.go", "Foo.bar")),
        (("h1", "holdout", "a.go", "Foo.bar"), ("k1", "keyword-leg", "a.go", "bar")),   # Class.method and method
        (("s1", "shape-model", "a.go", "bar"), ("k1", "keyword-leg", "a.go", "Foo.bar")),  # two decision sets
        (("c1", "confirm", "a.go", "Foo.bar"), ("s1", "shape-model", "a.go", "Foo.bar")),
        (("t1", "tuning", "a.go", None), ("s1", "shape-model", "a.go", "Foo.bar")),     # no symbol: the file
    ])
    def test_a_decision_question_on_another_questions_target_is_refused_naming_both(self, first, second):
        with pytest.raises(SystemExit) as raised:
            harness.validate_spec(_spec([_q(*first), _q(*second)]), "toy")
        message = str(raised.value)
        assert f"{first[0]} ({first[1]}) and {second[0]} ({second[1]}) target one answer" in message, message
        assert "a decision set's targets are its own" in message

    def test_tuning_and_holdout_may_share_a_target(self):
        harness.validate_spec(_spec([_q("t1", "tuning", "a.go", "Foo.bar"), _q("h1", "holdout", "a.go", "Foo.bar")]),
                              "toy")

    def test_the_same_name_in_another_file_is_another_target(self):
        harness.validate_spec(_spec([_q("t1", "tuning", "app/api/a/route.ts", "handler"),
                                     _q("s1", "shape-model", "app/api/b/route.ts", "handler")]), "toy")

    def test_another_symbol_in_the_same_file_is_another_target(self):
        harness.validate_spec(_spec([_q("t1", "tuning", "a.go", "Foo.bar"),
                                     _q("s1", "shape-model", "a.go", "Foo.baz")]), "toy")

    def test_every_committed_spec_validates_with_the_check_on(self, tmp_path):
        names = sorted(p.stem for p in harness.BENCHMARKS_DIR.glob("*.json") if not p.stem.endswith("-rule"))
        assert {"miniflux", "mealie"} <= set(names)
        for name in names:
            corpus = harness.load_corpus(name, tmp_path)
            assert corpus.questions

    def test_a_rule_file_is_not_listed_as_a_corpus(self, tmp_path):
        with pytest.raises(SystemExit) as raised:
            harness.load_corpus("no-such-corpus", tmp_path)
        assert "embedding-model-rule" not in str(raised.value)


class TestListTargets:
    def test_it_prints_each_sets_targets_and_no_question_text(self, tmp_path, monkeypatch, capsys):
        questions = [_q("t1", "tuning", "a.go", "Foo.bar"), _q("t2", "tuning", "b.go"),
                     _q("s1", "shape-model", "c.go", "Baz.qux")]
        (tmp_path / "toy.json").write_text(json.dumps(_spec(questions)), encoding="utf-8")
        monkeypatch.setattr(harness, "BENCHMARKS_DIR", tmp_path)
        harness.main(["--corpus", "toy", "--corpora-dir", str(tmp_path), "--list-targets"])
        out = capsys.readouterr().out
        assert "[tuning] 2\n  a.go :: Foo.bar\n  b.go :: (the file)\n[shape-model] 1\n  c.go :: Baz.qux" in out, out
        assert "the text of" not in out and "usage:" not in out

    def test_a_committed_spec_lists_every_target_and_no_text(self, tmp_path, capsys):
        harness.main(["--corpus", "miniflux", "--corpora-dir", str(tmp_path), "--list-targets"])
        out = capsys.readouterr().out
        spec = json.loads((harness.BENCHMARKS_DIR / "miniflux.json").read_text(encoding="utf-8"))
        for q in spec["questions"]:
            assert f"  {q['path']} :: {q['symbol']}" in out
            assert q["question"] not in out
        assert sum(1 for line in out.splitlines() if " :: " in line) == len(spec["questions"])


# ---------------------------------------------------------------------------
# 22.2-07: --embedding-model reaches the pipeline, the engine and --exact
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for IngestionPipeline or QueryEngine: records its keyword arguments, connects to nothing."""

    def __init__(self, calls, **kwargs):
        calls.append(kwargs)
        model = kwargs.get("embedding_model")
        generator = SimpleNamespace(model=model, client=SimpleNamespace(
            generate_embeddings_batch=lambda texts: [[0.5] for _ in texts]))
        self.embedding_gen = generator
        self.vector_retriever = SimpleNamespace(embedding_generator=generator)

    def process_files(self, **kwargs):
        return {"status": "success"}

    def query(self, **kwargs):
        return {"results": []}


class TestEmbeddingModelArgument:
    @pytest.fixture
    def recorded(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(harness, "IngestionPipeline", lambda **kw: _Recorder(calls, **kw))
        monkeypatch.setattr(harness, "QueryEngine", lambda **kw: _Recorder(calls, **kw))
        monkeypatch.setattr(harness, "OPENAI", "not-a-real-key")
        monkeypatch.setattr(harness, "PG", SCRATCH_PG)
        monkeypatch.setattr(harness, "require_fetched", lambda corpus: None)
        monkeypatch.setattr(harness, "collect_files", lambda corpus: [("a.py", "def f():\n    return 1\n", "python")])
        monkeypatch.setattr(harness, "ensure_fixtures", lambda corpus: None)
        monkeypatch.setattr(harness, "indexed_state", lambda corpus: (0, 0))
        monkeypatch.setattr(harness, "do_exact", lambda corpus, questions, vectors, model, out, tolerance=None:
                            calls.append({"do_exact_model": model}))
        return calls

    def _run(self, tmp_path, *flags):
        harness.main(["--corpus", "self", "--corpora-dir", str(tmp_path), "--set", "tuning", *flags])

    def test_with_no_flag_the_resolved_model_is_ada_002(self, recorded, tmp_path):
        self._run(tmp_path, "--ingest", "--measure", "--query-vectors", str(tmp_path / "v.json"),
                  "--exact", str(tmp_path / "x.json"))
        models = [c.get("embedding_model", c.get("do_exact_model")) for c in recorded]
        assert len(recorded) == 4 and set(models) == {"text-embedding-ada-002"}, recorded

    def test_the_flag_reaches_the_pipeline_the_engine_and_exact(self, recorded, tmp_path):
        self._run(tmp_path, "--ingest", "--measure", "--query-vectors", str(tmp_path / "v.json"),
                  "--exact", str(tmp_path / "x.json"), "--embedding-model", "text-embedding-3-small")
        models = [c.get("embedding_model", c.get("do_exact_model")) for c in recorded]
        assert len(recorded) == 4 and set(models) == {"text-embedding-3-small"}, recorded

    def test_the_query_vector_cache_refuses_the_other_models_vector(self, recorded, tmp_path):
        """QueryVectors.ensure's refusal is unchanged: an ada-002 vector is not reused for 3-small."""
        vectors = tmp_path / "v.json"
        self._run(tmp_path, "--measure", "--query-vectors", str(vectors))
        assert json.loads(vectors.read_text(encoding="utf-8"))["self-t01"]["model"] == "text-embedding-ada-002"
        with pytest.raises(SystemExit) as raised:
            self._run(tmp_path, "--measure", "--query-vectors", str(vectors),
                      "--embedding-model", "text-embedding-3-small")
        assert "was embedded with text-embedding-ada-002 and the engine uses text-embedding-3-small" in str(raised.value)
