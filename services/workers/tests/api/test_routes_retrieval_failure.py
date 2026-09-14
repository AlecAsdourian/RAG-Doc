"""Route tests for ISS-030: a failed retriever fails the request, loudly and
without leaking the underlying error.

Before the fix QueryEngine swallowed a retriever's exception. `/search` answered
200 with empty or partial results, and `/chat` answered "I don't have enough
information" to a question it never managed to search. Now:

- `/search` and `/chat` map RetrievalError to 503, with a fixed, retryable
  detail naming the failed retriever.
- `/chat/stream` sends an SSE error frame with the same message.
- Any other exception is a 500 with a fixed detail.

No response carries the underlying exception text. An OpenAI authentication
error contains a masked fragment of the API key, so every mocked error below
embeds a sentinel that must never appear in a response body.

The TestClient fixture pattern follows test_routes_organization_id_propagation.py.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from workers.generation.answer_generator import AnswerGenerator
from workers.retrieval import RetrievalError
from workers.retrieval.query_engine import QueryEngine

SENTINEL = "sk-SENTINEL-must-not-leak"

# The client-facing messages are a contract, so they are spelled out here
# rather than imported from api.routes.
VECTOR_UNAVAILABLE = "Search is temporarily unavailable (vector search failed); please retry"
KEYWORD_UNAVAILABLE = "Search is temporarily unavailable (keyword search failed); please retry"
SEARCH_FAILED = "Search failed due to an internal error"
CHAT_FAILED = "Chat failed due to an internal error"


@contextmanager
def _client(query_engine, answer_generator):
    with TestClient(app) as tc:
        # Assign AFTER TestClient enters: the lifespan handler sets
        # app.state.query_engine = None on startup, clobbering anything
        # assigned earlier. Assigning here overrides that reset.
        app.state.query_engine = query_engine
        app.state.answer_generator = answer_generator
        try:
            yield tc
        finally:
            app.state.query_engine = None
            app.state.answer_generator = None


@pytest.fixture
def client_with_mock_engines():
    """Yield a TestClient with mocked query_engine and answer_generator."""
    query_engine = MagicMock()
    answer_generator = MagicMock()
    with _client(query_engine, answer_generator) as tc:
        yield tc, query_engine, answer_generator


@pytest.fixture
def client_with_real_pipeline():
    """Yield a TestClient over the real QueryEngine and AnswerGenerator.

    Only the retriever classes, the OpenAI client and tiktoken are patched. So
    everything from a retriever raising to the HTTP response is production
    code: QueryEngine.query, AnswerGenerator.generate and the routes.
    """
    with patch("workers.retrieval.query_engine.FTSRetriever"), patch(
        "workers.retrieval.query_engine.VectorRetriever"
    ):
        query_engine = QueryEngine(
            postgres_conn="postgresql://unused.invalid/unused",
            qdrant_url="http://unused.invalid:6333",
            openai_api_key="unused",
            boost_config={},
        )
    with patch("workers.generation.answer_generator.OpenAI"), patch(
        "workers.generation.answer_generator.tiktoken"
    ):
        answer_generator = AnswerGenerator(query_engine=query_engine, openai_api_key="unused")
    with _client(query_engine, answer_generator) as tc:
        yield tc, query_engine


def _body():
    return {
        "query": "how does the parser work",
        "organization_id": str(uuid4()),
        "repository_id": str(uuid4()),
        "top_k": 3,
    }


def _retrieval_error(retriever: str) -> RetrievalError:
    cause = RuntimeError(f"Error code: 401 - Incorrect API key provided: {SENTINEL}")
    error = RetrievalError({retriever: cause})
    error.__cause__ = cause
    return error


def _stream(tc, body) -> str:
    with tc.stream("POST", "/chat/stream", json=body) as resp:
        assert resp.status_code == 200, resp.status_code
        return "".join(resp.iter_text())


def _frames(stream_text: str) -> list:
    return [
        json.loads(line[len("data: "):])
        for line in stream_text.splitlines()
        if line.startswith("data: ")
    ]


class TestRetrievalFailureIsServiceUnavailable:
    def test_search_returns_503_naming_the_retriever_without_leaking(
        self, client_with_mock_engines, caplog
    ):
        tc, query_engine, _ = client_with_mock_engines
        query_engine.query.side_effect = _retrieval_error("vector")

        with caplog.at_level(logging.ERROR):
            resp = tc.post("/search", json=_body())

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": VECTOR_UNAVAILABLE}
        assert SENTINEL not in resp.text
        assert any(SENTINEL in record.getMessage() for record in caplog.records), (
            "the full error must still be logged server-side"
        )

    def test_search_names_keyword_search_when_it_fails(self, client_with_mock_engines):
        tc, query_engine, _ = client_with_mock_engines
        query_engine.query.side_effect = _retrieval_error("fts")

        resp = tc.post("/search", json=_body())

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": KEYWORD_UNAVAILABLE}
        assert SENTINEL not in resp.text

    def test_chat_returns_503_without_leaking(self, client_with_mock_engines):
        tc, _, answer_generator = client_with_mock_engines
        answer_generator.generate.side_effect = _retrieval_error("vector")

        resp = tc.post("/chat", json=_body())

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": VECTOR_UNAVAILABLE}
        assert SENTINEL not in resp.text

    def test_chat_stream_sends_generic_error_frame_without_leaking(self, client_with_mock_engines):
        tc, _, answer_generator = client_with_mock_engines
        answer_generator.generate.side_effect = _retrieval_error("vector")

        stream_text = _stream(tc, _body())

        assert _frames(stream_text) == [{"type": "error", "error": VECTOR_UNAVAILABLE}], stream_text
        assert SENTINEL not in stream_text


class TestUnexpectedErrorIsInternalWithoutItsText:
    def test_search_returns_500_without_the_exception_text(self, client_with_mock_engines):
        tc, query_engine, _ = client_with_mock_engines
        query_engine.query.side_effect = RuntimeError(f"unexpected failure {SENTINEL}")

        resp = tc.post("/search", json=_body())

        assert resp.status_code == 500, resp.text
        assert resp.json() == {"detail": SEARCH_FAILED}
        assert SENTINEL not in resp.text

    def test_chat_returns_500_without_the_exception_text(self, client_with_mock_engines):
        tc, _, answer_generator = client_with_mock_engines
        answer_generator.generate.side_effect = RuntimeError(f"unexpected failure {SENTINEL}")

        resp = tc.post("/chat", json=_body())

        assert resp.status_code == 500, resp.text
        assert resp.json() == {"detail": CHAT_FAILED}
        assert SENTINEL not in resp.text

    def test_chat_stream_error_frame_omits_the_exception_text(self, client_with_mock_engines):
        tc, _, answer_generator = client_with_mock_engines
        answer_generator.generate.side_effect = RuntimeError(f"unexpected failure {SENTINEL}")

        stream_text = _stream(tc, _body())

        assert _frames(stream_text) == [{"type": "error", "error": CHAT_FAILED}], stream_text
        assert SENTINEL not in stream_text


class TestUnchangedBehaviour:
    def test_search_is_503_when_the_query_engine_is_not_initialised(self, client_with_mock_engines):
        tc, _, _ = client_with_mock_engines
        app.state.query_engine = None

        resp = tc.post("/search", json=_body())

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Query engine not initialized"}

    def test_chat_is_503_when_the_answer_generator_is_not_initialised(self, client_with_mock_engines):
        tc, _, _ = client_with_mock_engines
        app.state.answer_generator = None

        resp = tc.post("/chat", json=_body())

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Answer generator not initialized"}

    def test_chat_rejects_a_request_without_a_query(self, client_with_mock_engines):
        tc, _, answer_generator = client_with_mock_engines
        body = _body()
        del body["query"]

        resp = tc.post("/chat", json=body)

        assert resp.status_code == 422, resp.text
        answer_generator.generate.assert_not_called()


@pytest.mark.parametrize("failing", ["fts", "vector"])
@pytest.mark.parametrize("route", ["/search", "/chat", "/chat/stream"])
def test_iss030_a_failing_retriever_cannot_produce_a_silent_200(
    client_with_real_pipeline, route, failing
):
    """The regression test for ISS-030.

    One retriever raises and the other finds nothing. That is what a rejected
    OpenAI key looked like in practice, because keyword search returns nothing
    for most questions (ISS-029). Before the fix this was a 200 with no results
    on /search, and a 200 "I don't have enough information" on /chat and
    /chat/stream.
    """
    tc, query_engine = client_with_real_pipeline
    retrievers = {"fts": query_engine.fts_retriever, "vector": query_engine.vector_retriever}
    for name, retriever in retrievers.items():
        if name == failing:
            retriever.search.side_effect = RuntimeError(f"Error code: 401 - {SENTINEL}")
        else:
            retriever.search.return_value = []
    expected = VECTOR_UNAVAILABLE if failing == "vector" else KEYWORD_UNAVAILABLE

    if route == "/chat/stream":
        text = _stream(tc, _body())
        assert _frames(text) == [{"type": "error", "error": expected}], text[:400]
    else:
        resp = tc.post(route, json=_body())
        text = resp.text
        assert resp.status_code == 503, f"{route} answered {resp.status_code}: {text[:400]}"
        assert resp.json() == {"detail": expected}

    assert SENTINEL not in text
    assert "I don't have enough information" not in text
