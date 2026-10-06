#!/usr/bin/env python3
"""Prepare a scratch pgvector database the way tests/isolation/conftest.py does.

A MEASUREMENT RECORD, not product code (22.2-01 Task 2's live proof). Reads
the superuser DSN from DATABASE_URL (never printed: it carries a password),
refuses compose's port 5434, applies every `services/backend/migrations/
*.up.sql` of the tree this file is in, in order, on a FRESH database (it
refuses one that already has tables: golang-migrate never re-applies a
version, and neither does this), and creates the NOSUPERUSER NOBYPASSRLS
`rag_doc_app` role with the harness grants. Prints host:port only.

USAGE
    DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:<port>/<db> scratch_db.py
"""
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn

TREE = Path(__file__).resolve().parents[4]
MIGRATIONS = TREE / "services" / "backend" / "migrations"
APP_ROLE = "rag_doc_app"


def main() -> int:
    dsn = os.environ["DATABASE_URL"]
    parsed = parse_dsn(dsn)
    host, port, user = parsed.get("host"), int(parsed.get("port") or 5432), parsed.get("user")
    if port == 5434:
        raise SystemExit("refused: port 5434 is compose's (and another project's), not a scratch database")
    up = sorted(MIGRATIONS.glob("*.up.sql"))
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            if cur.fetchone()[0]:
                raise SystemExit("refused: the database already has tables; use a fresh container")
            for path in up:
                cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                        CREATE ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS INHERIT;
                    END IF;
                END $$;""")
            for stmt in (f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
                         f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
                         f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
                         f'GRANT {APP_ROLE} TO "{user}"'):
                cur.execute(stmt)
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            rolsuper, rolbypassrls = cur.fetchone()
            cur.execute("SHOW server_version")
            version = cur.fetchone()[0]
    finally:
        conn.close()
    print(f"scratch database at {host}:{port} (server {version}): {len(up)} migrations applied "
          f"({up[0].name} .. {up[-1].name}); {APP_ROLE} rolsuper={rolsuper} rolbypassrls={rolbypassrls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
