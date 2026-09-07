"""Self-tests for the Python isolation harness.

Mirrors services/backend/pkg/testing/isolation/fixtures_test.go. If these
fail, no downstream isolation test can be trusted.
"""

from __future__ import annotations

import pytest

from workers.db import require_tenant
from tests.isolation.fixtures import assert_no_cross_tenant_leak


def test_with_two_orgs_creates_two_orgs_with_distinct_ids(with_two_orgs):
    org_a, org_b = with_two_orgs
    assert org_a.id != org_b.id
    assert org_a.owner_id != org_b.owner_id
    assert org_a.repo_id != org_b.repo_id
    # Slugs share a common prefix from prefix-token but must differ overall.
    assert org_a.slug != org_b.slug


def test_require_tenant_sets_current_setting(db_conn, with_two_orgs):
    org_a, _ = with_two_orgs

    with require_tenant(db_conn, org_a.id) as cur:
        cur.execute("SELECT current_setting('app.current_tenant', true)")
        got = cur.fetchone()[0]

    assert got == org_a.id, (
        f"require_tenant must set app.current_tenant to the given tenant; "
        f"got {got!r}, want {org_a.id!r}"
    )


def test_require_tenant_rejects_non_uuid(db_conn):
    with pytest.raises(ValueError):
        with require_tenant(db_conn, "not-a-uuid"):
            pass


def test_assert_no_cross_tenant_leak_catches_a_real_leak(db_conn, with_two_orgs):
    """Synthesize a leak by writing under tenant A and reading under a
    lifted-role connection that bypasses RLS. Confirms the assertion helper
    would flag it — a critical negative test for the harness itself.
    """
    org_a, org_b = with_two_orgs

    # Insert a chunk under tenant A via a proper require_tenant scope.
    def insert_chunk_as_a(cur):
        cur.execute(
            """
            INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (org_a.repo_id, "0" * 40, "main", "completed"),
        )
        run_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO chunks (
                ingestion_run_id, repository_id, file_path, start_line, end_line,
                content, content_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, org_a.repo_id, "leak-probe.md", 1, 2, "orange marmalade", "h-leak"),
        )

    # Under tenant B, "observe" would report false (no leak) if we obey
    # RLS. To synthesize a leak, RESET the role to the container superuser
    # inside the observe transaction — superuser bypasses RLS regardless
    # of app.current_tenant.
    def observe_via_superuser_bypass(cur):
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM chunks WHERE content = %s)",
            ("orange marmalade",),
        )
        return bool(cur.fetchone()[0])

    with pytest.raises(AssertionError, match="cross-tenant leak"):
        assert_no_cross_tenant_leak(
            db_conn,
            tenant_a=org_a.id,
            tenant_b=org_b.id,
            action=insert_chunk_as_a,
            observe=observe_via_superuser_bypass,
        )


def test_assert_no_cross_tenant_leak_passes_when_isolated(db_conn, with_two_orgs):
    org_a, org_b = with_two_orgs

    def insert_chunk_as_a(cur):
        cur.execute(
            """
            INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (org_a.repo_id, "1" * 40, "main", "completed"),
        )
        run_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO chunks (
                ingestion_run_id, repository_id, file_path, start_line, end_line,
                content, content_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, org_a.repo_id, "isolated.md", 1, 2, "unique-string-42", "h-iso"),
        )

    def observe_as_b(cur):
        # Honest observation: no role bypass. RLS filters chunks to org_b's
        # tenant scope, so a's chunk is invisible.
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM chunks WHERE content = %s)",
            ("unique-string-42",),
        )
        return bool(cur.fetchone()[0])

    # Must not raise.
    assert_no_cross_tenant_leak(
        db_conn,
        tenant_a=org_a.id,
        tenant_b=org_b.id,
        action=insert_chunk_as_a,
        observe=observe_as_b,
    )
