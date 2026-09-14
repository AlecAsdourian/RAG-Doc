"""Route tests: a query containing control characters is a 422 (PR #34 review).

U+0000 reached psycopg2, which cannot bind it ("A string literal cannot contain
NUL (0x00) characters"). Keyword search raised, QueryEngine reported it as a
keyword-search outage, and the client got a 503 "please retry" that no retry
could fix, with an ERROR traceback logged on every request.

The request models now reject U+0000 and every other C0 control character
except tab, newline and carriage return, which pasted code contains. The
rejection happens before the route runs, so nothing downstream sees the query.

The TestClient fixture pattern follows test_routes_organization_id_propagation.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app

ROUTES = ["/search", "/chat", "/chat/stream"]

DISALLOWED = {
    "nul": "marmalade\x00",
    "nul-alone": "\x00",
    "soh": "marmalade\x01recipe",
    "vertical-tab": "marmalade\x0brecipe",
    "form-feed": "marmalade\x0crecipe",
    "escape": "\x1b[31mmarmalade",
    "unit-separator": "marmalade\x1f",
    "nul-after-newline": "line one\nline two\x00",
}

ALLOWED = {
    "tab": "func\tmarmalade()",
    "newline": "func marmalade() {\n\treturn nil\n}",
    "carriage-return": "line one\r\nline two",
}


@pytest.fixture
def client_with_mock_engines():
    """Yield a TestClient with mocked query_engine and answer_generator."""
    query_engine = MagicMock()
    query_engine.query.return_value = {"results": [], "metadata": {}}
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


def _body(query: str) -> dict:
    return {
        "query": query,
        "organization_id": str(uuid4()),
        "repository_id": str(uuid4()),
        "top_k": 3,
    }


def _engine_call(route, query_engine, answer_generator):
    return query_engine.query if route == "/search" else answer_generator.generate


@pytest.mark.parametrize("route", ROUTES)
def test_nul_in_query_is_422_and_never_reaches_the_engine(client_with_mock_engines, route):
    tc, query_engine, answer_generator = client_with_mock_engines

    resp = tc.post(route, json=_body("marmalade\x00"))

    # /chat/stream included: validation runs before the stream starts, so this
    # is a real 422 rather than a 200 with an error frame.
    assert resp.status_code == 422, resp.text
    assert any(
        err["loc"] == ["body", "query"] and "query contains invalid characters" in err["msg"]
        for err in resp.json()["detail"]
    ), resp.text
    _engine_call(route, query_engine, answer_generator).assert_not_called()


@pytest.mark.parametrize("name", sorted(DISALLOWED))
@pytest.mark.parametrize("route", ROUTES)
def test_other_control_characters_are_422(client_with_mock_engines, route, name):
    tc, query_engine, answer_generator = client_with_mock_engines

    resp = tc.post(route, json=_body(DISALLOWED[name]))

    assert resp.status_code == 422, resp.text
    _engine_call(route, query_engine, answer_generator).assert_not_called()


@pytest.mark.parametrize("name", sorted(ALLOWED))
@pytest.mark.parametrize("route", ROUTES)
def test_tab_newline_and_carriage_return_are_accepted(client_with_mock_engines, route, name):
    tc, query_engine, answer_generator = client_with_mock_engines
    query = ALLOWED[name]

    if route == "/chat/stream":
        with tc.stream("POST", route, json=_body(query)) as resp:
            assert resp.status_code == 200
            text = "".join(resp.iter_text())
        assert '"type": "error"' not in text, text
    else:
        resp = tc.post(route, json=_body(query))
        assert resp.status_code == 200, resp.text

    call = _engine_call(route, query_engine, answer_generator)
    call.assert_called_once()
    _, kwargs = call.call_args
    forwarded = kwargs["query_text"] if route == "/search" else kwargs["query"]
    assert forwarded == query, "the query must reach the engine unchanged"
