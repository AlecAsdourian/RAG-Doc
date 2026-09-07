"""Regression tests pinning that every RAG-service route forwards
`organization_id` from the request body into the underlying engine call.

Reviewer note that spawned this suite (PR #10): the initial refactor
missed one of three call sites — the SSE `/chat/stream` handler nests
its `answer_generator.generate(...)` inside a coroutine closure, so
the `replace_all` edit that fixed `/search` and `/chat` skipped it, and
`/chat/stream` was returning `{"type":"error"}` on every request. This
suite runs the Pydantic validation + route dispatch chain against a
Mock engine and asserts `organization_id` reaches it. If a future
refactor drops the arg on any route, these tests fail loudly.

Deliberately NOT a full isolation test — the tenant enforcement is
proven end-to-end by `tests/isolation/`. This suite is purely for the
routes.py → engine wire contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client_with_mock_engines():
    """Yield a TestClient with mocked query_engine and answer_generator.

    Both mocks capture their call kwargs so the assertion targets the
    exact argument name used by the route (`organization_id=...`).
    """
    query_engine = MagicMock()
    query_engine.query.return_value = {
        "results": [],
        "query_id": "q-mock",
        "metadata": {},
    }
    answer_generator = MagicMock()
    answer_generator.generate.return_value = {
        "answer": "mock answer",
        "sources": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_cost": 0.0,
        "cache_hit": False,
        "model": "mock-model",
        "chunks_retrieved": 0,
    }

    with TestClient(app) as tc:
        # Assign AFTER TestClient enters: the lifespan handler sets
        # app.state.query_engine = None on startup, clobbering anything
        # assigned earlier. Assigning here overrides that reset.
        app.state.query_engine = query_engine
        app.state.answer_generator = answer_generator
        try:
            yield tc, query_engine, answer_generator
        finally:
            app.state.query_engine = None
            app.state.answer_generator = None


def _body():
    return {
        "query": "how does the parser work",
        "organization_id": str(uuid4()),
        "repository_id": str(uuid4()),
        "top_k": 3,
    }


def test_search_forwards_organization_id_to_query_engine(client_with_mock_engines):
    tc, query_engine, _ = client_with_mock_engines
    body = _body()

    resp = tc.post("/search", json=body)

    assert resp.status_code == 200, resp.text
    query_engine.query.assert_called_once()
    _, kwargs = query_engine.query.call_args
    assert str(kwargs["organization_id"]) == body["organization_id"]


def test_chat_forwards_organization_id_to_answer_generator(client_with_mock_engines):
    tc, _, answer_generator = client_with_mock_engines
    body = _body()

    resp = tc.post("/chat", json=body)

    assert resp.status_code == 200, resp.text
    answer_generator.generate.assert_called_once()
    _, kwargs = answer_generator.generate.call_args
    assert str(kwargs["organization_id"]) == body["organization_id"]


def test_chat_stream_forwards_organization_id_to_answer_generator(client_with_mock_engines):
    """The regression test that motivated this whole file.

    Without organization_id in the forwarded kwargs, AnswerGenerator.generate
    raises TypeError, the SSE closure catches it, and the stream emits
    `{"type":"error"}`. Assert (a) the mock was called with the right
    org id, and (b) the stream body does NOT contain an error frame.
    """
    tc, _, answer_generator = client_with_mock_engines
    body = _body()

    with tc.stream("POST", "/chat/stream", json=body) as resp:
        assert resp.status_code == 200, resp.text
        stream_text = "".join(resp.iter_text())

    answer_generator.generate.assert_called_once()
    _, kwargs = answer_generator.generate.call_args
    assert str(kwargs["organization_id"]) == body["organization_id"]
    assert '"type": "error"' not in stream_text and '"type":"error"' not in stream_text, (
        f"chat/stream emitted an SSE error frame — likely a missing kwarg on "
        f"the engine call; body: {stream_text[:400]}"
    )


def test_search_rejects_request_missing_organization_id(client_with_mock_engines):
    tc, query_engine, _ = client_with_mock_engines
    body = _body()
    del body["organization_id"]

    resp = tc.post("/search", json=body)

    assert resp.status_code == 422, resp.text
    query_engine.query.assert_not_called()
