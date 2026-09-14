"""Tests for how QueryEngine.query handles a failing retriever (ISS-030).

QueryEngine used to catch either retriever's exception and fuse whatever the
other one returned. The failure survived only as `metadata.fts_error` or
`metadata.vector_error`, which nothing read. With a rejected OpenAI key it raised
nothing and returned 0 results, so `/search` answered 200 with nothing and
`/chat` answered "I don't have enough information". Now any retriever failure
raises RetrievalError, and no partial result is assembled.

The engine is built with both retriever classes patched, so these tests need no
Postgres, Qdrant or OpenAI.
"""

import logging
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from workers.retrieval import RetrievalError
from workers.retrieval.query_engine import QueryEngine

# Stands in for the masked API key fragment an OpenAI 401 carries.
SENTINEL = "sk-SENTINEL-must-not-leak"


def _hit(chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "file_path": f"pkg/{chunk_id}.go",
        "breadcrumb": "",
        "chunk_type": "function",
        "content_preview": f"func {chunk_id}() {{}}",
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
    ):
        engine = QueryEngine(
            postgres_conn="postgresql://unused.invalid/unused",
            qdrant_url="http://unused.invalid:6333",
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


class TestRetrievalError:
    def test_requires_at_least_one_failure(self):
        with pytest.raises(ValueError):
            RetrievalError({})

    def test_failed_description_carries_no_exception_text(self):
        error = RetrievalError({"vector": RuntimeError(SENTINEL)})

        assert SENTINEL in str(error), "the full message is for server-side logs"
        assert SENTINEL not in error.failed_description
