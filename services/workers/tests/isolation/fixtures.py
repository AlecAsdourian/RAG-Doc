"""Two-tenant fixture and cross-tenant assertion helper.

Mirrors services/backend/pkg/testing/isolation/{fixtures,tenants}.go. Any
Python test that exercises tenant boundaries builds its scaffold via
`create_org` (or the `with_two_orgs` pytest fixture) and asserts
isolation via `assert_no_cross_tenant_leak`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from workers.db import require_tenant


@dataclass
class TestOrg:
    """One isolated test tenant with three role-tagged users and a repo.

    Mirrors the Go `TestOrg` struct; field names line up so the same
    scenarios can be ported across languages without translation.
    """

    id: str
    slug: str
    owner_id: str
    admin_id: str
    member_id: str
    project_id: str
    repo_id: str


def create_org(conn: Any, prefix: str) -> TestOrg:
    """Insert one full org scaffold (org, users, memberships, project, repo).

    Non-tenant-scoped tables (organizations, users, projects, memberships)
    are inserted with plain autocommit-style calls. The repository insert
    goes through `require_tenant` because migration 000009's trigger fires
    on it and RLS requires `app.current_tenant` for the WITH CHECK.
    """
    slug = f"{prefix}-{_short_token()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (name, slug) VALUES (%s, %s) RETURNING id",
            (slug, slug),
        )
        org_id = str(cur.fetchone()[0])
    conn.commit()

    owner_id = _create_user_with_membership(conn, org_id, "owner", slug)
    admin_id = _create_user_with_membership(conn, org_id, "admin", slug)
    member_id = _create_user_with_membership(conn, org_id, "member", slug)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (organization_id, name, slug) VALUES (%s, %s, %s) RETURNING id",
            (org_id, f"{slug}-proj", f"{slug}-proj"),
        )
        project_id = str(cur.fetchone()[0])
    conn.commit()

    with require_tenant(conn, org_id) as cur:
        cur.execute(
            "INSERT INTO repositories (project_id, name, git_url) VALUES (%s, %s, %s) RETURNING id",
            (project_id, f"{slug}-repo", f"https://example.test/{slug}.git"),
        )
        repo_id = str(cur.fetchone()[0])

    return TestOrg(
        id=org_id,
        slug=slug,
        owner_id=owner_id,
        admin_id=admin_id,
        member_id=member_id,
        project_id=project_id,
        repo_id=repo_id,
    )


def _create_user_with_membership(conn: Any, org_id: str, role: str, slug: str) -> str:
    email = f"{role}-{slug}-{_short_token()}@iso-test.local"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (supabase_user_id, email, full_name) VALUES (%s, %s, %s) RETURNING id",
            (str(uuid.uuid4()), email, f"{role} of {slug}"),
        )
        user_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO organization_memberships (user_id, organization_id, role) VALUES (%s, %s, %s)",
            (user_id, org_id, role),
        )
    conn.commit()
    return user_id


def cleanup_org(conn: Any, org: TestOrg) -> None:
    """Reverse-order delete under a SAVEPOINT-per-statement wrapper.

    Mirrors Go's `cleanupOrg`: one aborted DELETE (e.g., an unknown FK a
    future migration adds) does not silently skip every subsequent
    delete — savepoint isolates each statement so the outer transaction
    stays usable.
    """
    stmts = [
        (
            "DELETE FROM feedback WHERE retrieval_id IN ("
            "SELECT r.id FROM retrievals r JOIN queries q ON r.query_id = q.id "
            "WHERE q.project_id = %s)",
            (org.project_id,),
        ),
        (
            "DELETE FROM retrievals WHERE query_id IN (SELECT id FROM queries WHERE project_id = %s)",
            (org.project_id,),
        ),
        ("DELETE FROM queries WHERE project_id = %s", (org.project_id,)),
        (
            "DELETE FROM chunks WHERE repository_id IN (SELECT id FROM repositories WHERE project_id = %s)",
            (org.project_id,),
        ),
        (
            "DELETE FROM ingestion_runs WHERE repository_id IN (SELECT id FROM repositories WHERE project_id = %s)",
            (org.project_id,),
        ),
        ("DELETE FROM repositories WHERE project_id = %s", (org.project_id,)),
        ("DELETE FROM projects WHERE id = %s", (org.project_id,)),
        ("DELETE FROM organization_memberships WHERE organization_id = %s", (org.id,)),
        (
            "DELETE FROM users WHERE id = ANY(%s)",
            ([org.owner_id, org.admin_id, org.member_id],),
        ),
        ("DELETE FROM organizations WHERE id = %s", (org.id,)),
    ]
    try:
        with require_tenant(conn, org.id) as cur:
            for sql, args in stmts:
                cur.execute("SAVEPOINT cleanup_stmt")
                try:
                    cur.execute(sql, args)
                    cur.execute("RELEASE SAVEPOINT cleanup_stmt")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT cleanup_stmt")
    except Exception:
        # Cleanup must never mask the test's original failure — swallow
        # any residual error the savepoint loop couldn't handle.
        pass


def assert_no_cross_tenant_leak(
    conn: Any,
    tenant_a: str,
    tenant_b: str,
    action: Callable[[Any], None],
    observe: Callable[[Any], bool],
) -> None:
    """Run `action` as tenant A (auto-committed), then run `observe` as tenant B.

    Raises `AssertionError` if `observe` returns truthy — i.e., tenant B
    was able to see something tenant A wrote. Mirrors Go's
    `AssertNoCrossTenantLeak`.
    """
    if action is None or observe is None:
        raise ValueError("action and observe must be non-None")

    with require_tenant(conn, tenant_a) as cur:
        action(cur)

    leaked: Optional[bool] = None
    with require_tenant(conn, tenant_b) as cur:
        leaked = bool(observe(cur))

    if leaked:
        raise AssertionError(
            f"cross-tenant leak: tenant B ({tenant_b}) observed an effect written by tenant A ({tenant_a})"
        )


def _short_token() -> str:
    return uuid.uuid4().hex[:8]
