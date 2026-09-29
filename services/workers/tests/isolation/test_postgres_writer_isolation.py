"""Isolation tests for `workers.storage.postgres_writer.PostgresWriter`.

Covers the three scenarios the 17-04 plan calls out for writer coverage,
adapted to the actual (sync psycopg2) code shape:

1. Writer under correct tenant scope: chunks written for org A are
   invisible to org B under a proper `require_tenant` read.
2. Writer that bypasses `require_tenant`: a raw INSERT on the chunks
   table without app.current_tenant set raises SQLSTATE 42501 from
   migration 000009's trigger. This is the cross-language proof that
   the Go-side trigger enforces on Python callers too.
3. Cross-tenant write: writing under org A's scope with a repository_id
   that belongs to org B is refused by RLS (the row is invisible under
   A's scope, so the FK-satisfying INSERT is silently filtered out; the
   chunk cannot be seen from either tenant).

And, since migration 000017 (22-02), what the writer stores with each
chunk: its tenant, its vector and the model that produced it. Those tests
are at the bottom. Every writer here CONNECTS AS THE APP ROLE (`SET ROLE
rag_doc_app` right after connecting): the container user is a superuser,
and a superuser bypasses row-level security even under FORCE, so an
isolation assertion on a superuser connection passes whatever the policy
does. `_app_role_writer` is the one place that switch happens.

No OpenAI call is made anywhere in this file. Vectors are fixed lists;
the one EmbeddingGenerator constructed here is never asked to embed.
"""

from __future__ import annotations

import hashlib
import random
from typing import Dict, List, Sequence

import numpy as np
import psycopg2
import pytest

from workers.chunker.models import Chunk
from workers.db import require_tenant
from workers.embeddings.embedding_generator import EmbeddingGenerator
from workers.storage.postgres_writer import PostgresWriter

DIMENSIONS = 1536
TEST_MODEL = "test-fixed"


def _make_chunk(text: str, file_path: str = "probe.md", start_line: int = 1) -> Chunk:
    """Build a minimal Chunk instance matching the writer's expected fields."""
    return Chunk(
        content=text,
        file_path=file_path,
        start_line=start_line,
        end_line=start_line,
        language="markdown",
        chunk_type="doc",
        metadata={"probe": True},
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixed_vector(fill: float = 0.01) -> List[float]:
    """A non-zero vector: the cosine distance of a zero vector is undefined."""
    return [fill] * DIMENSIONS


def _embeddings_for(*chunks: Chunk, fill: float = 0.01) -> Dict[str, List[float]]:
    """content_hash -> vector, the shape EmbeddingGenerator returns."""
    return {_hash(c.content): _fixed_vector(fill) for c in chunks}


def _dsn(test_db_container) -> str:
    return test_db_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _app_role_writer(dsn: str) -> PostgresWriter:
    """A PostgresWriter whose connection runs as rag_doc_app.

    The writer connects as the container superuser; SET ROLE is what makes
    RLS and trg_assert_tenant apply to everything it then does.
    """
    writer = PostgresWriter(dsn)
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()
    return writer


def _parse_vector(text: str) -> List[float]:
    """pgvector's text output, `[x,y,z]`, back to the float32 values it holds.

    MEASURED, and the reason for the float32 step: pgvector prints each
    single-precision element as the SHORTEST decimal that identifies it as a
    float32, not as a double. The stored value -0.99951171875 (exact in
    float32) comes back as the text `-0.9995117`, which read as a double is
    a different number, and read as a float32 is exactly the stored one.
    So the text is decoded the way it was encoded, through float32.
    """
    return [float(np.float32(x)) for x in text.strip("[]").split(",")]


def _write_one(writer: PostgresWriter, org, chunks: Sequence[Chunk], embeddings, model=TEST_MODEL, sha="0"):
    run_id = writer.create_ingestion_run(
        organization_id=org.id,
        repository_id=org.repo_id,
        commit_sha=sha * 40,
        branch="main",
    )
    ids = writer.insert_chunks(
        organization_id=org.id,
        chunks=list(chunks),
        ingestion_run_id=run_id,
        repository_id=org.repo_id,
        embeddings=embeddings,
        embedding_model=model,
    )
    writer.complete_ingestion_run(
        organization_id=org.id,
        ingestion_run_id=run_id,
        chunks_count=len(chunks),
    )
    return run_id, ids


def test_writer_under_correct_tenant_is_visible_only_to_its_org(
    test_db_container, db_conn, with_two_orgs
):
    """Scenario 1 — chunks written for org A are invisible under org B."""
    org_a, org_b = with_two_orgs
    chunk = _make_chunk("orange marmalade recipe")

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _write_one(writer, org_a, [chunk], _embeddings_for(chunk))
    finally:
        writer.close()

    # Read under org A's scope — must see the marmalade chunk.
    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chunks WHERE content = %s",
            ("orange marmalade recipe",),
        )
        count_a = cur.fetchone()[0]
    assert count_a == 1, "org A must see its own chunk"

    # Read under org B's scope — must NOT see it (RLS filter).
    with require_tenant(db_conn, org_b.id) as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chunks WHERE content = %s",
            ("orange marmalade recipe",),
        )
        count_b = cur.fetchone()[0]
    assert count_b == 0, "org B must NOT see org A's chunk — cross-tenant leak"


def test_raw_insert_without_require_tenant_fires_the_17_03_trigger(
    test_db_container, with_two_orgs
):
    """Scenario 2 — cross-language proof that migration 000009's trigger
    refuses Python-side raw INSERTs. Bypass require_tenant entirely, use
    a plain psycopg2 connection under the rag_doc_app role, attempt an
    INSERT on chunks. Postgres must raise SQLSTATE 42501 with the
    tenant-isolation message.
    """
    org_a, _ = with_two_orgs

    conn = psycopg2.connect(_dsn(test_db_container))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET ROLE rag_doc_app")
        conn.autocommit = False

        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            # A raw INSERT with no SET LOCAL — trigger must fire before any
            # FK checks (BEFORE triggers run before constraints).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (org_a.repo_id, "0" * 40, "main", "completed"),
                )

        pg_err = excinfo.value
        assert pg_err.pgcode == "42501", (
            f"expected SQLSTATE 42501 (assert_tenant_scoped trigger); got {pg_err.pgcode}"
        )
        assert "tenant isolation violated" in str(pg_err), (
            f"expected trigger message; got {pg_err}"
        )
    finally:
        conn.close()


def test_writer_with_repo_id_from_other_org_writes_nothing_visible(
    test_db_container, db_conn, with_two_orgs
):
    """Scenario 3 — writer scoped to org A but pointed at org B's
    repo_id ends up with a chunk that no tenant can see. Under org A's
    RLS the chunk's FK to org B's repo makes it invisible; under org B's
    RLS the chunk's own row is invisible because it was inserted under
    A's tenant scope. This documents the safety property: cross-tenant
    payloads produce write-nothing-observable rather than leaking.
    """
    org_a, org_b = with_two_orgs

    writer = _app_role_writer(_dsn(test_db_container))
    marker = f"cross-tenant-payload-{org_a.id[:8]}"

    try:
        # A run pointing at org B's repo while scoped to org A. RLS on
        # ingestion_runs's WITH CHECK requires the run's repo belong to
        # the current tenant, so this row cannot be inserted at all.
        with pytest.raises(psycopg2.Error):
            writer.create_ingestion_run(
                organization_id=org_a.id,
                repository_id=org_b.repo_id,
                commit_sha="0" * 40,
                branch="main",
            )
    finally:
        writer.close()

    # Neither tenant sees any row with our marker text.
    for org in (org_a, org_b):
        with require_tenant(db_conn, org.id) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM chunks WHERE content = %s", (marker,)
            )
            assert cur.fetchone()[0] == 0, (
                f"cross-tenant payload must be unwritable, but tenant {org.id} sees it"
            )


def test_writer_scoped_to_org_a_cannot_file_a_chunk_under_org_bs_repository(
    test_db_container, db_conn, with_two_orgs
):
    """Since 000017 the writer names the tenant on every row and the
    composite key chunks_repo_tenant_fk refuses a pair that is not the
    repository's own. A chunk for org B's repository written under org A's
    scope, with a run that IS org A's, is refused with 23503 on that key —
    the row is unrepresentable, not merely invisible (22-CONTEXT P3).
    """
    org_a, org_b = with_two_orgs
    chunk = _make_chunk("misfiled marmalade")

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        run_id = writer.create_ingestion_run(
            organization_id=org_a.id,
            repository_id=org_a.repo_id,
            commit_sha="5" * 40,
            branch="main",
        )
        with pytest.raises(psycopg2.errors.ForeignKeyViolation) as excinfo:
            writer.insert_chunks(
                organization_id=org_a.id,
                chunks=[chunk],
                ingestion_run_id=run_id,
                repository_id=org_b.repo_id,
                embeddings=_embeddings_for(chunk),
                embedding_model=TEST_MODEL,
            )
        assert excinfo.value.diag.constraint_name == "chunks_repo_tenant_fk"
    finally:
        writer.close()

    for org in (org_a, org_b):
        with require_tenant(db_conn, org.id) as cur:
            cur.execute("SELECT COUNT(*) FROM chunks WHERE content = %s", ("misfiled marmalade",))
            assert cur.fetchone()[0] == 0


def test_writer_stores_the_chunk_breadcrumb_in_its_own_column(
    test_db_container, db_conn, with_two_orgs
):
    """The breadcrumb must reach `chunks.breadcrumb`, not only `metadata`.

    Keyword search matches that column and query results are rebuilt from it.
    Until 2026-09-13 the insert never wrote it, so it was NULL for every chunk
    ever indexed. A chunk without a breadcrumb stores NULL, not an empty string.
    """
    org_a, _ = with_two_orgs

    named = Chunk(
        content="breadcrumb probe with a name",
        file_path="pkg/api/handlers/repositories.go",
        start_line=1,
        end_line=3,
        language="go",
        chunk_type="function",
        metadata={"breadcrumb": "RepositoriesHandler.Connect"},
    )
    unnamed = _make_chunk("breadcrumb probe without a name")

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _write_one(writer, org_a, [named, unnamed], _embeddings_for(named, unnamed), sha="b")
    finally:
        writer.close()

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute(
            "SELECT content, breadcrumb FROM chunks WHERE content LIKE %s",
            ("breadcrumb probe%",),
        )
        stored = dict(cur.fetchall())

    assert stored["breadcrumb probe with a name"] == "RepositoriesHandler.Connect"
    assert stored["breadcrumb probe without a name"] is None


# ---------------------------------------------------------------------------
# Migration 000017 (22-02): the tenant, the vector and the model on every row.
# ---------------------------------------------------------------------------


def test_writer_records_the_generators_model_on_every_chunk(
    test_db_container, db_conn, with_two_orgs
):
    """`embedding_model` is whatever the generator that produced the vectors
    says it is (22-CONTEXT P4). The generator is constructed with a model
    that is NOT the writer's or the pipeline's default, so a writer that
    restated a name instead of storing the one it was given would fail
    here. It is never asked to embed: no OpenAI call.
    """
    org_a, _ = with_two_orgs
    generator = EmbeddingGenerator(api_key="test-key-never-used", model="text-embedding-3-small")
    assert generator.model == "text-embedding-3-small"

    chunk = _make_chunk("model probe")
    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _write_one(writer, org_a, [chunk], _embeddings_for(chunk), model=generator.model, sha="6")
    finally:
        writer.close()

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute("SELECT embedding_model FROM chunks WHERE content = %s", ("model probe",))
        rows = cur.fetchall()
    assert rows == [("text-embedding-3-small",)]


def test_writer_stores_the_tenant_and_org_b_sees_neither_the_chunk_nor_its_vector(
    test_db_container, db_conn, with_two_orgs
):
    """Every row carries organization_id, and it is the tenant the writer
    was scoped to. Under org B's scope the chunk, its vector and its model
    are all absent — the row is in A's partition under A's policy.
    """
    org_a, org_b = with_two_orgs
    chunk = _make_chunk("tenant probe")

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _write_one(writer, org_a, [chunk], _embeddings_for(chunk, fill=0.25), sha="7")
    finally:
        writer.close()

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute(
            "SELECT organization_id::text, embedding IS NOT NULL, embedding_model "
            "FROM chunks WHERE content = %s",
            ("tenant probe",),
        )
        assert cur.fetchall() == [(org_a.id, True, TEST_MODEL)]

    with require_tenant(db_conn, org_b.id) as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chunks WHERE content = %s OR embedding_model = %s",
            ("tenant probe", TEST_MODEL),
        )
        assert cur.fetchone()[0] == 0, "org B must see neither org A's chunk nor its vector"


def test_every_duplicate_content_chunk_gets_its_hashs_vector(
    test_db_container, db_conn, with_two_orgs
):
    """Two chunks with identical content share a content hash, so the
    generator produces ONE vector for them. Both rows must carry it. Before
    000017 the vectors lived in Qdrant keyed by one chunk id per hash, so
    the second of every such pair had no vector at all (12 chunks in
    miniflux, 80 in mealie; 22-RESEARCH.md Q3). The returned map still
    has one entry per hash; every row was written.
    """
    org_a, _ = with_two_orgs
    first = _make_chunk("def helper():\n    return 1", file_path="a.py", start_line=10)
    second = _make_chunk("def helper():\n    return 1", file_path="b.py", start_line=40)
    assert _hash(first.content) == _hash(second.content)
    embeddings = _embeddings_for(first, fill=0.125)
    assert len(embeddings) == 1

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _, ids = _write_one(writer, org_a, [first, second], embeddings, sha="8")
    finally:
        writer.close()
    assert list(ids) == [_hash(first.content)]

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute(
            "SELECT file_path, embedding::text, embedding_model FROM chunks "
            "WHERE content_hash = %s ORDER BY file_path",
            (_hash(first.content),),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["a.py", "b.py"], "both duplicate-content rows were written"
    for _, vector_text, model in rows:
        assert _parse_vector(vector_text) == _fixed_vector(0.125), "each row carries the hash's vector"
        assert model == TEST_MODEL


def test_writer_refuses_a_chunk_without_an_embedding_before_writing_anything(
    test_db_container, db_conn, with_two_orgs
):
    """A chunk with no vector raises first, naming its file and lines, and
    NOTHING is written: not that chunk, and not the ones before it in the
    batch either. The table would refuse the row anyway (embedding is NOT
    NULL), but part way through a batch, with a message naming a column.
    """
    org_a, _ = with_two_orgs
    embedded = _make_chunk("has a vector", file_path="ok.py", start_line=1)
    orphan = _make_chunk("has no vector", file_path="missing.py", start_line=17)
    orphan.end_line = 23

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        run_id = writer.create_ingestion_run(
            organization_id=org_a.id,
            repository_id=org_a.repo_id,
            commit_sha="9" * 40,
            branch="main",
        )
        with pytest.raises(ValueError) as excinfo:
            writer.insert_chunks(
                organization_id=org_a.id,
                chunks=[embedded, orphan],
                ingestion_run_id=run_id,
                repository_id=org_a.repo_id,
                embeddings=_embeddings_for(embedded),
                embedding_model=TEST_MODEL,
            )
    finally:
        writer.close()

    message = str(excinfo.value)
    assert "missing.py:17-23" in message, message
    assert "nothing was written" in message

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute("SELECT COUNT(*) FROM chunks WHERE ingestion_run_id = %s", (str(run_id),))
        assert cur.fetchone()[0] == 0, "the chunk that had a vector must not have been written either"


def test_writer_refuses_an_empty_model(test_db_container, with_two_orgs):
    org_a, _ = with_two_orgs
    chunk = _make_chunk("no model")
    writer = _app_role_writer(_dsn(test_db_container))
    try:
        with pytest.raises(ValueError, match="embedding_model is required"):
            writer.insert_chunks(
                organization_id=org_a.id,
                chunks=[chunk],
                ingestion_run_id="00000000-0000-0000-0000-000000000000",
                repository_id=org_a.repo_id,
                embeddings=_embeddings_for(chunk),
                embedding_model="",
            )
    finally:
        writer.close()


def test_writer_stores_the_vector_it_was_given(
    test_db_container, db_conn, with_two_orgs
):
    """The vector is passed as text (`%s::vector`) and must round-trip.

    pgvector stores single-precision elements, so the only rounding that
    can ever happen is the server's, from the double we send to the nearest
    float32. Two checks pin that the TRANSPORT loses nothing beyond that:

    - values exactly representable in float32 (dyadic rationals) come back
      EXACTLY equal;
    - realistic values (an OpenAI-like spread, deterministic seed) come back
      within float32's rounding of what was sent, element by element.

    If either failed, the fix would be the pgvector client package; both
    pass, so the text form is enough (22-02's plan asked for exactly this
    check before adding a dependency). What the first run of this test
    found instead is in _parse_vector: the OUTPUT side prints float32-
    shortest digits, which must be read back as float32.
    """
    org_a, _ = with_two_orgs

    exact = [((i % 4096) - 2048) / 2048.0 for i in range(DIMENSIONS)]
    rng = random.Random(22_02)
    realistic = [rng.uniform(-0.05, 0.05) for _ in range(DIMENSIONS)]

    exact_chunk = _make_chunk("exact vector probe", file_path="exact.md")
    realistic_chunk = _make_chunk("realistic vector probe", file_path="realistic.md")
    embeddings = {_hash(exact_chunk.content): exact, _hash(realistic_chunk.content): realistic}

    writer = _app_role_writer(_dsn(test_db_container))
    try:
        _write_one(writer, org_a, [exact_chunk, realistic_chunk], embeddings, sha="a")
    finally:
        writer.close()

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute(
            "SELECT content, embedding::text, vector_dims(embedding) FROM chunks "
            "WHERE content IN (%s, %s)",
            (exact_chunk.content, realistic_chunk.content),
        )
        stored = {content: (_parse_vector(text), dims) for content, text, dims in cur.fetchall()}

    got_exact, dims = stored[exact_chunk.content]
    assert dims == DIMENSIONS
    assert got_exact == exact, "float32-representable values must round-trip exactly"

    got_realistic, _ = stored[realistic_chunk.content]
    assert len(got_realistic) == DIMENSIONS
    for sent, got in zip(realistic, got_realistic):
        # The server's rounding to float32 is the ONLY difference allowed:
        # what came back is the float32 nearest to what was sent.
        assert got == float(np.float32(sent)), (sent, got)
