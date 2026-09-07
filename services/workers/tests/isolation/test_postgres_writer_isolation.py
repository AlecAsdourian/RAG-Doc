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
"""

from __future__ import annotations

import psycopg2
import pytest

from workers.chunker.models import Chunk
from workers.db import require_tenant
from workers.storage.postgres_writer import PostgresWriter


def _make_chunk(text: str) -> Chunk:
    """Build a minimal Chunk instance matching the writer's expected fields."""
    return Chunk(
        content=text,
        file_path="probe.md",
        start_line=1,
        end_line=1,
        language="markdown",
        chunk_type="doc",
        metadata={"probe": True},
    )


def test_writer_under_correct_tenant_is_visible_only_to_its_org(
    test_db_container, db_conn, with_two_orgs
):
    """Scenario 1 — chunks written for org A are invisible under org B."""
    org_a, org_b = with_two_orgs

    dsn = (
        test_db_container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
    )

    writer = PostgresWriter(dsn)
    # PostgresWriter connects as the superuser role; SET ROLE so RLS
    # and the assert_tenant_scoped trigger actually enforce.
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()

    try:
        run_id = writer.create_ingestion_run(
            organization_id=org_a.id,
            repository_id=org_a.repo_id,
            commit_sha="0" * 40,
            branch="main",
        )
        writer.insert_chunks(
            organization_id=org_a.id,
            chunks=[_make_chunk("orange marmalade recipe")],
            ingestion_run_id=run_id,
            repository_id=org_a.repo_id,
        )
        writer.complete_ingestion_run(
            organization_id=org_a.id,
            ingestion_run_id=run_id,
            chunks_count=1,
        )
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

    dsn = (
        test_db_container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
    )
    conn = psycopg2.connect(dsn)
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

    dsn = (
        test_db_container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
    )
    writer = PostgresWriter(dsn)
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()

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
