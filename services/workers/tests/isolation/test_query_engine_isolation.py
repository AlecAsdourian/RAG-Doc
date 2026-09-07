"""Isolation tests for the read path (`FTSRetriever`).

Scope note: the plan named this file `test_query_engine_isolation.py`
because the plan was written expecting a direct QueryEngine harness.
Constructing QueryEngine in a test requires a live Qdrant and an OpenAI
key (VectorRetriever tries to connect on __init__), neither of which the
17-04 harness provisions. The Postgres-touching part of the pipeline is
FTSRetriever plus QueryEngine._enrich_results_with_metadata; both go
through workers.db.require_tenant, so testing FTSRetriever directly
covers the tenant-scoping semantics that matter here. If a later phase
adds a Qdrant test fixture, the QueryEngine-level orchestration test
lives in the same pattern.

Three scenarios per the plan:

1. FTS as tenant A returns only A's chunks (tenant B's identically-worded
   query in a separate run does not surface A's rows).
2. FTS as tenant B for the same query text returns only B's chunks.
3. FTS with app.current_tenant unset returns empty via RLS silent-filter
   (documenting the safety property that the read path never leaks — it
   just silently returns nothing).
"""

from __future__ import annotations

import psycopg2
import pytest

from workers.chunker.models import Chunk
from workers.retrieval.fts_retriever import FTSRetriever
from workers.storage.postgres_writer import PostgresWriter


def _make_chunk(content: str, file_path: str = "probe.md") -> Chunk:
    return Chunk(
        content=content,
        file_path=file_path,
        start_line=1,
        end_line=1,
        language="markdown",
        chunk_type="doc",
        metadata={},
    )


def _seed_chunk(dsn: str, org, content: str) -> None:
    """Write one completed ingestion run + one chunk for `org`."""
    writer = PostgresWriter(dsn)
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()
    try:
        run_id = writer.create_ingestion_run(
            organization_id=org.id,
            repository_id=org.repo_id,
            commit_sha="a" * 40,
            branch="main",
        )
        writer.insert_chunks(
            organization_id=org.id,
            chunks=[_make_chunk(content)],
            ingestion_run_id=run_id,
            repository_id=org.repo_id,
        )
        writer.complete_ingestion_run(
            organization_id=org.id,
            ingestion_run_id=run_id,
            chunks_count=1,
        )
    finally:
        writer.close()


def _fresh_retriever(dsn: str) -> FTSRetriever:
    retriever = FTSRetriever(dsn)
    retriever.connect()
    with retriever.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    retriever.conn.commit()
    return retriever


@pytest.fixture
def dsn(test_db_container) -> str:
    return test_db_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def test_fts_as_org_a_returns_only_org_a_chunks(dsn, with_two_orgs):
    org_a, org_b = with_two_orgs
    _seed_chunk(dsn, org_a, "orange marmalade recipe")
    _seed_chunk(dsn, org_b, "purple velvet cake recipe")

    retriever = _fresh_retriever(dsn)
    try:
        results = retriever.search(
            query="marmalade",
            organization_id=org_a.id,
            repository_id=org_a.repo_id,
            limit=10,
        )
    finally:
        retriever.close()

    assert len(results) == 1, "org A must see its one marmalade chunk"
    assert "marmalade" in results[0]["content_preview"]


def test_fts_as_org_b_same_query_returns_only_org_b_chunks(dsn, with_two_orgs):
    org_a, org_b = with_two_orgs
    _seed_chunk(dsn, org_a, "orange marmalade recipe")
    _seed_chunk(dsn, org_b, "purple velvet cake recipe")

    # Query "recipe" — matches both content strings, but RLS filters to
    # each org's own chunks. Org B must not see A's row.
    retriever = _fresh_retriever(dsn)
    try:
        results = retriever.search(
            query="recipe",
            organization_id=org_b.id,
            repository_id=org_b.repo_id,
            limit=10,
        )
    finally:
        retriever.close()

    assert len(results) == 1, "org B must see only its own recipe chunk"
    assert "velvet" in results[0]["content_preview"], (
        "org B must NOT see org A's marmalade chunk — cross-tenant leak"
    )


def test_fts_without_tenant_scope_returns_empty(dsn, with_two_orgs):
    """RLS silent-filter: reading `chunks` without app.current_tenant set
    yields zero rows regardless of what's in the table. The retriever
    always takes organization_id, so the only way to hit this code path
    from Python is a raw psycopg2 SELECT — but proving the DB behavior
    here documents the safety property.
    """
    org_a, _ = with_two_orgs
    _seed_chunk(dsn, org_a, "orange marmalade recipe")

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET ROLE rag_doc_app")
            cur.execute("SELECT COUNT(*) FROM chunks WHERE content ILIKE %s", ("%marmalade%",))
            count_without_tenant = cur.fetchone()[0]
    finally:
        conn.close()

    assert count_without_tenant == 0, (
        "RLS must silently filter chunks to zero rows when app.current_tenant is unset; "
        f"got {count_without_tenant} — potential leak surface for any caller that skips require_tenant"
    )
