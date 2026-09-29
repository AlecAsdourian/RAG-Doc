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
