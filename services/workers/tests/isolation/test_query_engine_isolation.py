"""Isolation and plan-shape tests for the read path: both legs in Postgres (22-03).

Until 22-03 this file tested the keyword leg only: the vector leg lived in
Qdrant, with no tenant on its points, and constructing a QueryEngine needed a
live Qdrant and an OpenAI key. Now both legs read `chunks` under row-level
security, so the vector leg's isolation can finally be a test, and it is the
first one below.

THE APP-ROLE PREMISE. Every test that builds a retriever or a QueryEngine from
a DSN uses `app_dsn` (conftest.py): the container superuser bypasses row-level
security even under FORCE, so a passing isolation test on a superuser DSN
proves nothing. Each such test reads its own connection's `rolsuper` and
`rolbypassrls` and asserts both are false, rather than trusting the fixture.

NO OPENAI CALL IS MADE. Vectors are fixed lists written through
PostgresWriter; a query's vector is injected by replacing the generator
client's `generate_embeddings_batch`, which is the retriever's own call path.

WHAT THE VECTOR-LEG FIXTURE CAN DISTINGUISH. The other tenant's chunk carries
the query vector itself, so it is the NEAREST chunk in the table. A leak would
therefore show as that chunk ranking first, not as silence; and the test
asserts, as the superuser with RLS bypassed, that it really is nearer.

TWO PLAN-SHAPE PROOFS live here because 22-03 owns them: that the HNSW index
can serve VECTOR_SEARCH_SQL (with what that does and does not prove, in the
test's docstring), and that the breadcrumb GIN index serves the expression
FTS_SEARCH_SQL is composed from.
"""

from __future__ import annotations

import hashlib
import random
import re
from contextlib import contextmanager
from typing import Dict, List, Optional

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from workers.chunker.models import Chunk
from workers.db import require_tenant
from workers.retrieval import vector_retriever as vector_retriever_module
from workers.retrieval.fts_retriever import BREADCRUMB_TSVECTOR, FTS_SEARCH_SQL, FTSRetriever
from workers.retrieval.query_engine import QueryEngine
from workers.retrieval.vector_retriever import VECTOR_SEARCH_SQL, vector_literal
from workers.storage.postgres_writer import PostgresWriter

DIMENSIONS = 1536
# The keyword-leg tests are about text; their vector is a fixed non-zero fill
# and their model a name no real generator has.
_TEST_MODEL = "test-fixed"

IDENTITY_SQL = (
    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(conn) -> tuple:
    """(current_user, rolsuper, rolbypassrls) of `conn`, leaving it idle."""
    with conn.cursor() as cur:
        cur.execute(IDENTITY_SQL)
        row = cur.fetchone()
    if not conn.autocommit:
        conn.rollback()
    return tuple(row)


def _assert_app_role(conn) -> None:
    current_user, rolsuper, rolbypassrls = _identity(conn)
    assert current_user == "rag_doc_app", current_user
    assert rolsuper is False, "a superuser bypasses row-level security; this test would prove nothing"
    assert rolbypassrls is False


def _unit(seed: int) -> List[float]:
    """A deterministic unit vector."""
    rng = random.Random(seed)
    values = [rng.gauss(0.0, 1.0) for _ in range(DIMENSIONS)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def _mix(a: List[float], b: List[float], weight: float) -> List[float]:
    """A unit vector between `a` and `b`: nearer to `a` for a small `weight`."""
    values = [(1.0 - weight) * x + weight * y for x, y in zip(a, b)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def _make_chunk(content: str, file_path: str = "probe.md", breadcrumb: Optional[str] = None) -> Chunk:
    return Chunk(
        content=content,
        file_path=file_path,
        start_line=1,
        end_line=1,
        language="markdown",
        chunk_type="doc",
        metadata={"breadcrumb": breadcrumb} if breadcrumb else {},
    )


def _seed_chunk(
    app_dsn: str,
    org,
    content: str,
    vector: Optional[List[float]] = None,
    model: str = _TEST_MODEL,
    file_path: str = "probe.md",
    breadcrumb: Optional[str] = None,
) -> str:
    """Write one completed ingestion run + one chunk for `org`, as the app role; return its id."""
    writer = PostgresWriter(app_dsn)
    writer.connect()
    try:
        _assert_app_role(writer.conn)
        chunk = _make_chunk(content, file_path=file_path, breadcrumb=breadcrumb)
        content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        # One run per chunk; ingestion_runs is unique on (repository, commit).
        run_id = writer.create_ingestion_run(
            organization_id=org.id, repository_id=org.repo_id, commit_sha=content_hash[:40], branch="main"
        )
        ids = writer.insert_chunks(
            organization_id=org.id,
            chunks=[chunk],
            ingestion_run_id=run_id,
            repository_id=org.repo_id,
            embeddings={content_hash: vector if vector is not None else [0.01] * DIMENSIONS},
            embedding_model=model,
        )
        writer.complete_ingestion_run(organization_id=org.id, ingestion_run_id=run_id, chunks_count=1)
    finally:
        writer.close()
    return str(next(iter(ids.values())))


def _engine(app_dsn: str, query_vector: List[float]) -> QueryEngine:
    """A real QueryEngine on the app role, whose generator answers every query with `query_vector`.

    The replacement sits on the client's `generate_embeddings_batch`, the exact
    call the vector leg makes, so the injected vector travels the production
    path; nothing else about the engine is patched.
    """
    engine = QueryEngine(postgres_conn=app_dsn, openai_api_key="test-not-used")
    engine.embedding_generator.client.generate_embeddings_batch = lambda texts: [
        list(query_vector) for _ in texts
    ]
    return engine


def _superuser_distances(superuser_dsn: str, query_vector: List[float], chunk_ids: List[str]) -> Dict[str, float]:
    """Cosine distance of each chunk to the query, read with RLS bypassed."""
    conn = psycopg2.connect(superuser_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, embedding <=> %s::vector FROM chunks WHERE id = ANY(%s::uuid[])",
                (vector_literal(query_vector), chunk_ids),
            )
            return {chunk_id: float(distance) for chunk_id, distance in cur.fetchall()}
    finally:
        conn.close()


@pytest.fixture
def superuser_dsn(test_db_container) -> str:
    """The container superuser: for premise checks with RLS bypassed, never for the code under test."""
    return test_db_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


# ---------------------------------------------------------------------------
# The vector leg
# ---------------------------------------------------------------------------


def test_vector_leg_returns_only_the_tenants_chunks_even_when_the_other_tenants_is_nearer(
    app_dsn, superuser_dsn, with_two_orgs
):
    """Org A's search never returns org B's chunk, although B's is the nearest one.

    B's chunk carries the query vector itself (distance 0); A's is a mix, a
    little further away. If the vector leg ran outside the tenant scope, B's
    chunk would rank first. Searching as A with B's repository id returns
    nothing from either leg: the tenant, not the caller's claim, decides.
    """
    org_a, org_b = with_two_orgs
    query = _unit(1)
    engine = _engine(app_dsn, query)
    model = engine.embedding_generator.model
    a_id = _seed_chunk(app_dsn, org_a, "tenant a's chunk", _mix(query, _unit(2), 0.4), model)
    b_id = _seed_chunk(app_dsn, org_b, "tenant b's chunk", list(query), model)

    # The premise, with RLS bypassed: B's chunk really is the nearer one.
    distances = _superuser_distances(superuser_dsn, query, [a_id, b_id])
    assert distances[b_id] < distances[a_id], distances
    assert distances[b_id] < 1e-6

    try:
        results = engine.vector_retriever.search(
            query="probe", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
        _assert_app_role(engine.vector_retriever.conn)
        assert [r["chunk_id"] for r in results] == [a_id], results
        assert b_id not in {r["chunk_id"] for r in results}, "cross-tenant leak through the vector leg"
        assert 0.0 < results[0]["vector_score"] < 1.0

        # As A, asking for B's repository: nothing, from either leg.
        assert engine.vector_retriever.search(
            query="probe", organization_id=org_a.id, repository_id=org_b.repo_id, limit=10
        ) == []
        assert engine.fts_retriever.search(
            query="chunk", organization_id=org_a.id, repository_id=org_b.repo_id, limit=10
        ) == []
        _assert_app_role(engine.fts_retriever.conn)
        assert engine.query(query_text="probe", organization_id=org_a.id, repository_id=org_b.repo_id)["results"] == []

        # The whole pipeline, as A in A's repository: A's chunk, from the vector leg.
        full = engine.query(query_text="probe", organization_id=org_a.id, repository_id=org_a.repo_id)
        assert [r["chunk_id"] for r in full["results"]] == [a_id]
        assert full["results"][0]["sources"] == ["vector"]
    finally:
        engine.vector_retriever.close()
        engine.fts_retriever.close()


def test_a_chunk_embedded_with_another_model_is_never_returned(app_dsn, superuser_dsn, with_two_orgs):
    """Vectors from two models share a dimension and nothing else (P4).

    The refused chunk carries the query vector itself, so it is the nearest
    row in the tenant's repository; only the model predicate keeps it out.
    """
    org_a, _ = with_two_orgs
    query = _unit(3)
    engine = _engine(app_dsn, query)
    same_model = engine.embedding_generator.model
    kept_id = _seed_chunk(app_dsn, org_a, "embedded with the engine's model", _mix(query, _unit(4), 0.4), same_model)
    refused_id = _seed_chunk(app_dsn, org_a, "embedded with another model", list(query), "other-model")

    distances = _superuser_distances(superuser_dsn, query, [kept_id, refused_id])
    assert distances[refused_id] < distances[kept_id], "the premise: the other model's chunk is nearer"

    try:
        results = engine.vector_retriever.search(
            query="probe", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
        _assert_app_role(engine.vector_retriever.conn)
    finally:
        engine.vector_retriever.close()

    assert [r["chunk_id"] for r in results] == [kept_id], results
    assert refused_id not in {r["chunk_id"] for r in results}, (
        "a chunk embedded with another model was compared against this query"
    )


def test_iterative_scan_is_set_inside_the_retrievers_own_transaction(app_dsn, with_two_orgs, monkeypatch):
    """`hnsw.iterative_scan = relaxed_order` is in force where the statement runs (P5).

    The setting is read in the retriever's own transaction, after its
    statement and before the commit, by wrapping the `require_tenant` the
    retriever module imported. Outside that transaction the server default
    is what a fresh transaction sees, so the SET LOCAL is what the test reads.
    """
    org_a, _ = with_two_orgs
    query = _unit(5)
    engine = _engine(app_dsn, query)
    _seed_chunk(app_dsn, org_a, "any chunk", _mix(query, _unit(6), 0.4), engine.embedding_generator.model)

    real_require_tenant = vector_retriever_module.require_tenant
    seen: List[str] = []

    @contextmanager
    def spying_require_tenant(conn, tenant_id, cursor_factory=None):
        with real_require_tenant(conn, tenant_id, cursor_factory=cursor_factory) as cur:
            yield cur
            cur.execute("SELECT current_setting('hnsw.iterative_scan') AS setting")
            row = cur.fetchone()
            seen.append(row["setting"] if isinstance(row, dict) else row[0])

    monkeypatch.setattr(vector_retriever_module, "require_tenant", spying_require_tenant)
    try:
        results = engine.vector_retriever.search(
            query="probe", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
        assert len(results) == 1
        assert seen == ["relaxed_order"], seen

        # And it was local: a fresh transaction on the same connection sees the default.
        with engine.vector_retriever.conn.cursor() as cur:
            cur.execute("SELECT current_setting('hnsw.iterative_scan')")
            after = cur.fetchone()[0]
        engine.vector_retriever.conn.rollback()
        assert after != "relaxed_order", after
    finally:
        engine.vector_retriever.close()


def test_the_hnsw_index_can_serve_the_vector_leg(app_dsn, with_two_orgs):
    """HNSW eligibility for VECTOR_SEARCH_SQL, on the production statement.

    WHAT THIS PROVES. With sequential scans, bitmap scans and sorts all
    disabled, and the query vector bound as a parameter, the planner's only
    remaining path for `ORDER BY embedding <=> $q LIMIT n` is the partition's
    HNSW index, and it takes it: the operator class matches (`<=>` against
    `vector_cosine_ops`), the vector is a constant rather than a subquery, and
    the scan is on the one partition the policy pruned to (`Subplans
    Removed: 63`).

    WHAT IT DOES NOT PROVE. That the planner CHOOSES the index at production
    sizes. At benchmark sizes it rightly chooses exact search (the 22-03
    equivalence records show `Seq Scan on chunks_p0`), and the plan at scale
    is 22.1-05's recall test, which seeds a tenant large enough for HNSW to
    win and asserts the plan.
    """
    org_a, _ = with_two_orgs
    query = _unit(7)
    _seed_chunk(app_dsn, org_a, "a chunk in the partition", _mix(query, _unit(8), 0.4), "text-embedding-ada-002")

    conn = psycopg2.connect(app_dsn)
    try:
        _assert_app_role(conn)
        params = {
            "q": vector_literal(query),
            "repo": str(org_a.repo_id),
            "model": "text-embedding-ada-002",
            "limit": 50,
        }
        with require_tenant(conn, org_a.id) as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
            cur.execute("SET LOCAL enable_sort = off")
            cur.execute("EXPLAIN (COSTS OFF) " + VECTOR_SEARCH_SQL, params)
            forced = "\n".join(row[0] for row in cur.fetchall())
        with require_tenant(conn, org_a.id) as cur:
            cur.execute("EXPLAIN (COSTS OFF) " + VECTOR_SEARCH_SQL, params)
            vector_plan = "\n".join(row[0] for row in cur.fetchall())
            cur.execute("EXPLAIN (COSTS OFF) " + FTS_SEARCH_SQL, {"q": "chunk", "repo": str(org_a.repo_id), "limit": 50})
            keyword_plan = "\n".join(row[0] for row in cur.fetchall())
    finally:
        conn.close()

    assert re.search(r"Index Scan using chunks_p\d+_embedding_idx on chunks_p\d+", forced), forced
    assert "Order By: (embedding <=>" in forced, forced
    assert "Subplans Removed: 63" in forced, forced
    # A5, on the production statements with default settings: both legs prune
    # to one partition from the policy alone; there is no organization_id in
    # either statement.
    for label, plan in (("vector", vector_plan), ("keyword", keyword_plan)):
        assert "Subplans Removed: 63" in plan, f"{label} leg:\n{plan}"
        assert len(re.findall(r"Scan (?:using \S+ )?on chunks_p\d+", plan)) == 1, f"{label} leg:\n{plan}"
    assert "organization_id" not in VECTOR_SEARCH_SQL and "organization_id" not in FTS_SEARCH_SQL


# ---------------------------------------------------------------------------
# The breadcrumb GIN index, owned by 22-03
# ---------------------------------------------------------------------------


def test_the_breadcrumb_gin_index_serves_the_keyword_legs_expression(superuser_dsn):
    """Migration 000017's GIN index on `to_tsvector('english', COALESCE(breadcrumb, ''))`
    serves the keyword leg's breadcrumb predicate, and a mismatched expression does not.

    An expression index serves only a query that uses the same expression, so
    FTS_SEARCH_SQL is composed from BREADCRUMB_TSVECTOR and this test plans that
    constant. The shape that makes the planner's choice visible: one partition,
    as the superuser (no row-level-security predicate to offer a btree
    alternative), only the breadcrumb predicate, sequential and index scans
    off. GIN supports bitmap scans only, so the matching expression gives a
    Bitmap Index Scan on the COALESCE index, and the bare `breadcrumb`
    expression falls back to a Seq Scan. The index is identified by its
    `indexdef`, not its name: both GIN indexes on a partition get generated
    names.
    """
    assert BREADCRUMB_TSVECTOR in FTS_SEARCH_SQL, "the keyword leg must be composed from the constant"
    mismatched = "to_tsvector('english', breadcrumb)"
    assert mismatched != BREADCRUMB_TSVECTOR

    conn = psycopg2.connect(superuser_dsn)
    try:
        current_user, rolsuper, _ = _identity(conn)
        assert rolsuper is True, "this proof runs as the superuser, so no policy predicate is in the plan"
        plans = {}
        for label, expression in (("matching", BREADCRUMB_TSVECTOR), ("mismatched", mismatched)):
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL enable_seqscan = off")
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute(
                        f"EXPLAIN (COSTS OFF) SELECT id FROM chunks_p0 "
                        f"WHERE {expression} @@ plainto_tsquery('english', 'x')"
                    )
                    plans[label] = "\n".join(row[0] for row in cur.fetchall())
        match = re.search(r"Bitmap Index Scan on (\S+)", plans["matching"])
        assert match, plans["matching"]
        index_name = match.group(1)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'chunks_p0' AND indexname = %s",
                (index_name,),
            )
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()

    assert row is not None, index_name
    indexdef = row[0]
    assert "USING gin" in indexdef and "COALESCE(breadcrumb" in indexdef, indexdef
    assert "Seq Scan" in plans["mismatched"] and "Bitmap Index Scan" not in plans["mismatched"], plans["mismatched"]


# ---------------------------------------------------------------------------
# The keyword leg (from before 22-03, now on the app-role DSN)
# ---------------------------------------------------------------------------


def _fresh_retriever(app_dsn: str) -> FTSRetriever:
    retriever = FTSRetriever(app_dsn)
    retriever.connect()
    _assert_app_role(retriever.conn)
    return retriever


def test_fts_as_org_a_returns_only_org_a_chunks(app_dsn, with_two_orgs):
    org_a, org_b = with_two_orgs
    _seed_chunk(app_dsn, org_a, "orange marmalade recipe")
    _seed_chunk(app_dsn, org_b, "purple velvet cake recipe")

    retriever = _fresh_retriever(app_dsn)
    try:
        results = retriever.search(
            query="marmalade", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
    finally:
        retriever.close()

    assert len(results) == 1, "org A must see its one marmalade chunk"
    assert "marmalade" in results[0]["content_preview"]


def test_fts_as_org_b_same_query_returns_only_org_b_chunks(app_dsn, with_two_orgs):
    org_a, org_b = with_two_orgs
    _seed_chunk(app_dsn, org_a, "orange marmalade recipe")
    _seed_chunk(app_dsn, org_b, "purple velvet cake recipe")

    # Query "recipe" matches both content strings, but RLS filters to each
    # org's own chunks. Org B must not see A's row.
    retriever = _fresh_retriever(app_dsn)
    try:
        results = retriever.search(
            query="recipe", organization_id=org_b.id, repository_id=org_b.repo_id, limit=10
        )
    finally:
        retriever.close()

    assert len(results) == 1, "org B must see only its own recipe chunk"
    assert "velvet" in results[0]["content_preview"], (
        "org B must NOT see org A's marmalade chunk -- cross-tenant leak"
    )


def test_fts_searches_the_repository_not_the_latest_run(app_dsn, with_two_orgs):
    """Two completed runs, one chunk each: both are found (ISS-027, 22-03).

    Until 22-03 the keyword leg filtered to the latest completed run and
    would have returned only the second chunk; with incremental indexing an
    unchanged file's chunks legitimately belong to an earlier run.
    """
    org_a, _ = with_two_orgs
    first = _seed_chunk(app_dsn, org_a, "marmalade from the first run", file_path="first.md")
    second = _seed_chunk(app_dsn, org_a, "marmalade from the second run", file_path="second.md")

    retriever = _fresh_retriever(app_dsn)
    try:
        results = retriever.search(
            query="marmalade", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
    finally:
        retriever.close()

    assert {r["chunk_id"] for r in results} == {first, second}


def test_fts_without_tenant_scope_returns_empty(app_dsn, with_two_orgs):
    """RLS silent-filter: reading `chunks` without app.current_tenant set
    yields zero rows regardless of what's in the table. The retriever
    always takes organization_id, so the only way to hit this code path
    from Python is a raw psycopg2 SELECT -- but proving the DB behavior
    here documents the safety property.
    """
    org_a, _ = with_two_orgs
    _seed_chunk(app_dsn, org_a, "orange marmalade recipe")

    conn = psycopg2.connect(app_dsn)
    conn.autocommit = True
    try:
        _assert_app_role(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM chunks WHERE content ILIKE %s", ("%marmalade%",))
            count_without_tenant = cur.fetchone()[0]
    finally:
        conn.close()

    assert count_without_tenant == 0, (
        "RLS must silently filter chunks to zero rows when app.current_tenant is unset; "
        f"got {count_without_tenant} -- potential leak surface for any caller that skips require_tenant"
    )


def test_fts_finds_a_chunk_by_its_qualified_name(app_dsn, with_two_orgs):
    """A qualified name that appears only in the breadcrumb must be findable.

    Postgres's parser reads `MarmaladeHandler.Connect` as a single token, so
    this matches the whole qualified name, not its parts (ISS-028).
    """
    org_a, _ = with_two_orgs
    _seed_chunk(app_dsn, org_a, "return nil", file_path="pkg/api/handlers/marmalade.go",
                breadcrumb="MarmaladeHandler.Connect")

    retriever = _fresh_retriever(app_dsn)
    try:
        results = retriever.search(
            query="MarmaladeHandler.Connect", organization_id=org_a.id, repository_id=org_a.repo_id, limit=10
        )
    finally:
        retriever.close()

    assert len(results) == 1, "the qualified name is only in the breadcrumb column"
    assert results[0]["breadcrumb"] == "MarmaladeHandler.Connect"


def test_query_results_carry_the_breadcrumb(app_dsn, with_two_orgs):
    """Results are rebuilt from Postgres after ranking, breadcrumb included,
    and only under the tenant that owns the chunk.

    With the column empty, every result -- and so every cited source in a
    generated answer -- came back with breadcrumb "" whatever the chunk's
    metadata held.
    """
    org_a, org_b = with_two_orgs
    chunk_id = _seed_chunk(app_dsn, org_a, "return nil", file_path="pkg/api/handlers/marmalade.go",
                           breadcrumb="MarmaladeHandler.Connect")
    engine = _engine(app_dsn, _unit(9))

    results = engine._enrich_results_with_metadata([{"chunk_id": chunk_id}], org_a.id, org_a.repo_id)
    assert len(results) == 1
    assert results[0]["breadcrumb"] == "MarmaladeHandler.Connect"

    leaked = engine._enrich_results_with_metadata([{"chunk_id": chunk_id}], org_b.id, org_a.repo_id)
    assert leaked == [], "org B must not read org A's chunk through result enrichment"
