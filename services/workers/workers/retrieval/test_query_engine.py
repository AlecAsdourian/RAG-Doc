"""Tests for how QueryEngine.query handles a failing retriever (ISS-030).

QueryEngine used to catch either retriever's exception and fuse whatever the
other one returned. The failure survived only as `metadata.fts_error` or
`metadata.vector_error`, which nothing read. With a rejected OpenAI key it raised
nothing and returned 0 results, so `/search` answered 200 with nothing and
`/chat` answered "I don't have enough information". Now any retriever failure
raises RetrievalError, and no partial result is assembled.

The engine is built with both retriever classes and the embedding generator
patched, so these tests need no Postgres or OpenAI.
"""

import logging
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from workers.retrieval import RetrievalError
from workers.retrieval.query_engine import QueryEngine

# Stands in for the masked API key fragment an OpenAI 401 carries.
SENTINEL = "sk-SENTINEL-must-not-leak"


def _hit(chunk_id: str, **scores) -> dict:
    return {
        "chunk_id": chunk_id,
        "file_path": f"pkg/{chunk_id}.go",
        "breadcrumb": "",
        "chunk_type": "function",
        "content_preview": f"func {chunk_id}() {{}}",
        **scores,
    }


def _enriched(results, organization_id, repository_id):
    """Stand-in for the Postgres read that rebuilds results after ranking."""
    return [
        {
            "chunk_id": r["chunk_id"],
            "file_path": r["file_path"],
            "score": r.get("boosted_score", 0.0),
        }
        for r in results
    ]


@pytest.fixture
def engine():
    with patch("workers.retrieval.query_engine.FTSRetriever"), patch(
        "workers.retrieval.query_engine.VectorRetriever"
    ), patch("workers.retrieval.query_engine.EmbeddingGenerator"):
        engine = QueryEngine(
            postgres_conn="postgresql://unused.invalid/unused",
            openai_api_key="unused",
            boost_config={},
        )
    # Result enrichment opens its own Postgres connection. Replacing it keeps
    # these tests offline and shows whether a result was assembled at all.
    engine._enrich_results_with_metadata = MagicMock(side_effect=_enriched)
    return engine


def _query(engine):
    return engine.query(
        query_text="how does the parser work",
        organization_id=uuid4(),
        repository_id=uuid4(),
        top_k=5,
    )


class TestQueryEngineRetrieverFailure:
    """A failing retriever raises RetrievalError instead of a partial result."""

    def test_vector_search_failure_raises_retrieval_error_naming_vector(self, engine, caplog):
        cause = RuntimeError(f"Error code: 401 - Incorrect API key provided: {SENTINEL}")
        engine.fts_retriever.search.return_value = [_hit("a")]
        engine.vector_retriever.search.side_effect = cause

        with caplog.at_level(logging.ERROR), pytest.raises(RetrievalError) as raised:
            _query(engine)

        error = raised.value
        assert error.retrievers == ("vector",)
        assert error.failed_description == "vector search"
        assert "vector search" in str(error)
        assert "keyword search" not in str(error)
        assert error.__cause__ is cause, "the original exception must be chained"
        assert error.failures == {"vector": cause}
        # Keyword search succeeded, but its results must not be returned alone.
        engine._enrich_results_with_metadata.assert_not_called()
        assert any(
            record.levelno == logging.ERROR and "vector search failed" in record.getMessage()
            for record in caplog.records
        ), "the failure must be logged at error level"

    def test_keyword_search_failure_raises_retrieval_error_naming_fts(self, engine):
        cause = RuntimeError("could not connect to server: Connection refused")
        engine.fts_retriever.search.side_effect = cause
        engine.vector_retriever.search.return_value = [_hit("a")]

        with pytest.raises(RetrievalError) as raised:
            _query(engine)

        error = raised.value
        assert error.retrievers == ("fts",)
        assert error.failed_description == "keyword search"
        assert "keyword search" in str(error)
        assert "vector search" not in str(error)
        assert error.__cause__ is cause
        # Vector search succeeded, but its results must not be returned alone.
        engine._enrich_results_with_metadata.assert_not_called()

    def test_both_failures_are_named(self, engine):
        fts_cause = RuntimeError("keyword side down")
        vector_cause = RuntimeError("vector side down")
        engine.fts_retriever.search.side_effect = fts_cause
        engine.vector_retriever.search.side_effect = vector_cause

        with pytest.raises(RetrievalError) as raised:
            _query(engine)

        error = raised.value
        assert error.retrievers == ("fts", "vector")
        assert error.failed_description == "keyword search and vector search"
        assert error.failures == {"fts": fts_cause, "vector": vector_cause}
        assert "keyword side down" in str(error) and "vector side down" in str(error)

    def test_both_retrievers_succeeding_returns_fused_results(self, engine):
        engine.fts_retriever.search.return_value = [_hit("a"), _hit("b")]
        engine.vector_retriever.search.return_value = [_hit("b"), _hit("c")]

        result = _query(engine)

        assert {r["chunk_id"] for r in result["results"]} == {"a", "b", "c"}
        metadata = result["metadata"]
        assert (metadata["fts_results"], metadata["vector_results"], metadata["fused_results"]) == (2, 2, 3)
        assert "fts_error" not in metadata and "vector_error" not in metadata, (
            "failures are raised, not recorded in metadata that nothing reads"
        )
        engine._enrich_results_with_metadata.assert_called_once()

    def test_a_retriever_that_finds_nothing_has_not_failed(self, engine):
        engine.fts_retriever.search.return_value = []
        engine.vector_retriever.search.return_value = []

        result = _query(engine)

        assert result["results"] == []

    def test_both_legs_receive_the_tenant(self, engine):
        """The vector leg runs under the same tenant scope as the keyword leg (22-03)."""
        engine.fts_retriever.search.return_value = []
        engine.vector_retriever.search.return_value = []
        organization_id, repository_id = uuid4(), uuid4()

        engine.query(
            query_text="how does the parser work",
            organization_id=organization_id,
            repository_id=repository_id,
        )

        for retriever in (engine.fts_retriever, engine.vector_retriever):
            retriever.search.assert_called_once_with(
                query="how does the parser work",
                organization_id=organization_id,
                repository_id=repository_id,
                limit=50,
            )

    def test_a_run_id_is_refused_rather_than_half_applied(self, engine):
        """Search is not run-scoped (ISS-027). Before 22-03 only the keyword leg
        honoured run_id, so a caller asking for a run got two legs that disagreed."""
        engine.fts_retriever.search.return_value = []
        engine.vector_retriever.search.return_value = []

        with pytest.raises(ValueError, match="run_id is not supported"):
            engine.query(
                query_text="how does the parser work",
                organization_id=uuid4(),
                repository_id=uuid4(),
                run_id=uuid4(),
            )
        engine.fts_retriever.search.assert_not_called()
        engine.vector_retriever.search.assert_not_called()


class TestTrace:
    """`trace=` records every stage from inside the real pipeline (22-03).

    The harness's `--record` feeds the storage-move equivalence gate from it,
    so the trace must be the pipeline's own numbers in the pipeline's own
    order, and its absence must change nothing.
    """

    @staticmethod
    def _seed(engine):
        engine.fts_retriever.search.return_value = [_hit("a", fts_score=0.5), _hit("b", fts_score=0.25)]
        engine.vector_retriever.search.return_value = [_hit("b", vector_score=0.9), _hit("c", vector_score=0.8)]

    def test_trace_none_changes_nothing(self, engine):
        """The response is the same with no trace, with trace=None and with a trace
        being recorded: recording is a side channel, never a change of result."""
        self._seed(engine)

        def query(**kwargs):
            return engine.query(
                query_text="how does the parser work",
                organization_id=uuid4(),
                repository_id=uuid4(),
                top_k=5,
                **kwargs,
            )

        without = _query(engine)
        explicit = query(trace=None)
        recorded_trace = {}
        recorded = query(trace=recorded_trace)

        for response in (without, explicit, recorded):
            response["metadata"].pop("duration_ms")
            response.pop("organization_id")
            response.pop("repository_id")
        assert explicit == without
        assert recorded == without, "recording a trace must not change the response"
        assert list(recorded_trace) == ["fts", "vector", "fused", "boosted", "top"]
        assert [r["chunk_id"] for r in without["results"]] == ["b", "a", "c"]
        assert "trace" not in without and "trace" not in without["metadata"]

    def test_trace_records_every_stage_in_pipeline_order(self, engine):
        self._seed(engine)
        trace = {}

        result = engine.query(
            query_text="how does the parser work",
            organization_id=uuid4(),
            repository_id=uuid4(),
            top_k=2,
            trace=trace,
        )

        assert list(trace) == ["fts", "vector", "fused", "boosted", "top"]
        # The legs, as the retrievers returned them, with their scores.
        assert [(e["chunk_id"], e["score"]) for e in trace["fts"]] == [("a", 0.5), ("b", 0.25)]
        assert [(e["chunk_id"], e["score"]) for e in trace["vector"]] == [("b", 0.9), ("c", 0.8)]
        assert trace["fts"][0]["file_path"] == "pkg/a.go"
        # Fusion: b is in both legs (1/62 + 1/61), a is fts #1 (1/61), c is vector #2 (1/62).
        assert [e["chunk_id"] for e in trace["fused"]] == ["b", "a", "c"]
        assert [e["rrf_score"] for e in trace["fused"]] == [1 / 62 + 1 / 61, 1 / 61, 1 / 62]
        assert trace["fused"][0]["sources"] == ["fts", "vector"]
        # Boosted, in the final sorted order, is what the top_k cut is taken from.
        assert [e["chunk_id"] for e in trace["boosted"]] == ["b", "a", "c"]
        boosted_scores = [e["boosted_score"] for e in trace["boosted"]]
        assert boosted_scores == sorted(boosted_scores, reverse=True)
        for entry in trace["boosted"]:
            assert entry["boosted_score"] == entry["rrf_score"] * entry["boost_multiplier"]
        # `top` is the enriched top_k, and its scores are the response's.
        assert [e["chunk_id"] for e in trace["top"]] == ["b", "a"]
        assert [e["chunk_id"] for e in result["results"]] == ["b", "a"]
        assert [e["score"] for e in trace["top"]] == [r["score"] for r in result["results"]]
        assert [e["score"] for e in trace["top"]] == boosted_scores[:2]

    def test_trace_holds_copies_not_the_pipeline_objects(self, engine):
        self._seed(engine)
        trace = {}

        result = engine.query(
            query_text="how does the parser work",
            organization_id=uuid4(),
            repository_id=uuid4(),
            top_k=5,
            trace=trace,
        )
        trace["boosted"][0]["boosted_score"] = -1.0
        trace["top"][0]["chunk_id"] = "tampered"

        assert result["results"][0]["chunk_id"] == "b"
        assert result["results"][0]["score"] > 0


class _RecordingCursor:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []


class _RecordingConnection:
    """An idle psycopg2-shaped connection that records every statement and its parameters."""

    def __init__(self):
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE

        self.executed = []
        self.closed = False
        self.autocommit = True
        self.info = MagicMock(transaction_status=TRANSACTION_STATUS_IDLE)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, cursor_factory=None):
        return _RecordingCursor(self.executed)


class TestTheEmbeddingModel:
    """22.2-07: the model is an argument of QueryEngine, with ada-002 as its
    default, and it is the vector leg's filter. No Postgres, no OpenAI: the
    OpenAI client is patched and the vector leg runs on a recording connection."""

    def _engine(self, **kwargs):
        with patch("workers.embeddings.embedding_generator.OpenAIEmbeddingClient") as client_class:
            client_class.return_value.generate_embeddings_batch.return_value = [[0.1, 0.2, 0.3]]
            engine = QueryEngine(postgres_conn="postgresql://unused.invalid/unused",
                                 openai_api_key="unused", boost_config={}, **kwargs)
        return engine, client_class

    def test_the_default_is_ada_002(self):
        from workers.embeddings import DEFAULT_EMBEDDING_MODEL

        engine, client_class = self._engine()
        assert DEFAULT_EMBEDDING_MODEL == "text-embedding-ada-002"
        assert engine.embedding_generator.model == "text-embedding-ada-002"
        assert client_class.call_args.kwargs["model"] == "text-embedding-ada-002"

    def test_a_given_model_reaches_the_generator_and_the_vector_legs_filter(self):
        engine, client_class = self._engine(embedding_model="text-embedding-3-small")
        assert engine.embedding_generator.model == "text-embedding-3-small"
        assert client_class.call_args.kwargs["model"] == "text-embedding-3-small"
        assert engine.vector_retriever.embedding_generator is engine.embedding_generator

        conn = _RecordingConnection()
        engine.vector_retriever.conn = conn
        engine.vector_retriever.search("where is the parser", uuid4(), uuid4(), limit=5)
        filters = [params["model"] for _, params in conn.executed if isinstance(params, dict) and "model" in params]
        assert filters == ["text-embedding-3-small"], conn.executed


class TestRetrievalError:
    def test_requires_at_least_one_failure(self):
        with pytest.raises(ValueError):
            RetrievalError({})

    def test_failed_description_carries_no_exception_text(self):
        error = RetrievalError({"vector": RuntimeError(SENTINEL)})

        assert SENTINEL in str(error), "the full message is for server-side logs"
        assert SENTINEL not in error.failed_description
