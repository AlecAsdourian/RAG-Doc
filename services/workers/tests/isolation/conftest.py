"""Session-scoped Postgres container + per-test connection fixtures.

Mirror of services/backend/pkg/testing/isolation/container.go. Reads the
Go migrations directory as the single source of truth for schema, applies
every `*.up.sql` in order, and creates a NOSUPERUSER `rag_doc_app` role
so RLS actually enforces (superusers bypass it).
"""

from __future__ import annotations

import pathlib
from typing import Iterator, Tuple

import psycopg2
import pytest
from testcontainers.community.postgres import PostgresContainer

from tests.isolation.fixtures import TestOrg, cleanup_org, create_org

# services/workers/tests/isolation/conftest.py → services/backend/migrations
_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "backend" / "migrations"
)

_APP_ROLE = "rag_doc_app"

# Test superuser account inside the container. Matches the Go harness so a
# developer switching between the two doesn't have to relearn conventions.
_PG_USER = "isolation"
_PG_PASS = "isolation"
_PG_DB = "isolation"


@pytest.fixture(scope="session")
def test_db_container() -> Iterator[PostgresContainer]:
    """Spin up Postgres once per pytest session and apply migrations.

    Container lifetime is the session; teardown removes it. Cross-session
    reuse (as Go's harness does via `WithReuseByName`) is not attempted
    because testcontainers-python does not expose an equivalent knob and
    per-session startup at ~5s is already tolerable.
    """
    container = PostgresContainer(
        "postgres:16-alpine",
        username=_PG_USER,
        password=_PG_PASS,
        dbname=_PG_DB,
    )
    container.start()
    try:
        dsn = _psycopg2_dsn(container)
        _apply_migrations(dsn)
        _ensure_app_role(dsn)
        yield container
    finally:
        container.stop()


@pytest.fixture
def db_conn(test_db_container: PostgresContainer) -> Iterator:
    """Fresh psycopg2 connection per test, SET ROLE to rag_doc_app.

    Connecting as the app role rather than the superuser is load-bearing
    for RLS: Postgres superusers bypass RLS even with FORCE, so every
    isolation assertion would silently pass on a superuser connection.
    """
    conn = psycopg2.connect(_psycopg2_dsn(test_db_container))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET ROLE {_APP_ROLE}")
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def with_two_orgs(db_conn) -> Iterator[Tuple[TestOrg, TestOrg]]:
    """Create two independent test tenants; clean both up on teardown."""
    org_a = create_org(db_conn, "iso-a")
    org_b = create_org(db_conn, "iso-b")
    try:
        yield org_a, org_b
    finally:
        cleanup_org(db_conn, org_a)
        cleanup_org(db_conn, org_b)


def _psycopg2_dsn(container: PostgresContainer) -> str:
    """Return a plain-psycopg2 DSN, stripping the SQLAlchemy driver hint."""
    raw = container.get_connection_url()
    # testcontainers-python returns `postgresql+psycopg2://...`; strip
    # the +psycopg2 driver hint so psycopg2.connect accepts the URL.
    return raw.replace("postgresql+psycopg2://", "postgresql://")


def _apply_migrations(dsn: str) -> None:
    up_files = sorted(_MIGRATIONS_DIR.glob("*.up.sql"))
    if not up_files:
        raise RuntimeError(f"no migrations found under {_MIGRATIONS_DIR}")

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for path in up_files:
                cur.execute(path.read_text(encoding="utf-8"))
    finally:
        conn.close()


def _ensure_app_role(dsn: str) -> None:
    """Create the NOSUPERUSER app role and grant it CRUD + sequence access.

    Idempotent — safe under any test setup ordering.
    """
    stmts = [
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                CREATE ROLE {_APP_ROLE} NOSUPERUSER NOBYPASSRLS INHERIT;
            END IF;
        END $$;
        """,
        f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_APP_ROLE};",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE};",
        f"GRANT {_APP_ROLE} TO {_PG_USER};",
    ]
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
    finally:
        conn.close()
