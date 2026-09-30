"""22-05 live proof, setup: a SCRATCH pgvector database, migrated, seeded as rag_doc_app.

Never compose, never port 5434, never a compose volume. Every secret this
creates (two scratch passwords) lives in live/state.json and is never printed.
Run from services/workers so `workers` is importable.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

import psycopg2

from workers.db import require_tenant
from workers.jobs.transitions import ENQUEUE_UPSERT_SQL

SCRATCH = pathlib.Path(r"C:\Users\Alec\AppData\Local\Temp\rag2205-ScfByQaa\live")
WORKTREE = pathlib.Path(r"C:\Users\Alec\Desktop\code\testtGSD\.claude\worktrees\agent-ab4bd33d2c92fa0bd")
MIGRATIONS = WORKTREE / "services" / "backend" / "migrations"
IMAGE = (
    "pgvector/pgvector:pg16"
    "@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
)

REPO_FULL_NAME = "AlecAsdourian/ES-SC-API-Navigator"
GITHUB_REPO_ID = 1103353668
INSTALLATION_ID = 160225622


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "workdir").mkdir(exist_ok=True)
    name = f"rag2205-live-pg-{secrets.token_hex(3)}"
    super_pw = secrets.token_urlsafe(18)
    app_pw = secrets.token_urlsafe(18)

    started = run(
        ["docker", "run", "-d", "--name", name,
         "-e", f"POSTGRES_PASSWORD={super_pw}", "-e", "POSTGRES_DB=ragproof",
         "-p", "127.0.0.1::5432", IMAGE]
    )
    assert started.returncode == 0, f"docker run failed ({started.returncode})"
    port = int(run(["docker", "port", name, "5432"]).stdout.strip().rsplit(":", 1)[1])
    assert port != 5434, "port 5434 is not this project's database"
    print(f"container {name} on 127.0.0.1:{port} (image {IMAGE.split('@')[1][:19]}...)")

    super_dsn = f"postgresql://postgres:{super_pw}@127.0.0.1:{port}/ragproof?sslmode=disable"
    for _ in range(120):
        try:
            psycopg2.connect(super_dsn, connect_timeout=2).close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.5)
    else:
        sys.exit("the scratch database never became ready")

    conn = psycopg2.connect(super_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        print("pgvector", cur.fetchone()[0])
        cur.execute("SHOW server_version")
        print("postgres", cur.fetchone()[0])

    # On the scratch container's own network namespace, so the migration
    # reaches exactly this database and nothing published anywhere else.
    migrated = run(
        ["docker", "run", "--rm", "--network", f"container:{name}",
         "-v", f"{MIGRATIONS}:/migrations:ro", "migrate/migrate:v4.19.1",
         "-path=/migrations",
         "-database", f"postgres://postgres:{super_pw}@localhost:5432/ragproof?sslmode=disable",
         "up"]
    )
    print("migrate up exit", migrated.returncode)
    for line in (migrated.stdout + migrated.stderr).splitlines():
        if "/u " in line or "error" in line.lower():
            print("  ", line.split(" ", 1)[-1] if line.startswith("20") else line)
    assert migrated.returncode == 0
    with conn.cursor() as cur:
        cur.execute("SELECT version, dirty FROM schema_migrations")
        print("schema_migrations", cur.fetchone())

        # The harness's role and grants (tests/isolation/conftest.py), with LOGIN
        # so every process logs in AS it rather than switching to it.
        cur.execute(
            "CREATE ROLE rag_doc_app LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD %s", (app_pw,)
        )
        cur.execute("GRANT USAGE ON SCHEMA public TO rag_doc_app")
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_doc_app")
        cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_doc_app")
    conn.close()

    app_dsn = f"postgresql://rag_doc_app:{app_pw}@127.0.0.1:{port}/ragproof?sslmode=disable"
    app = psycopg2.connect(app_dsn)
    with app.cursor() as cur:
        cur.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        print("app role probe (current_user, rolsuper, rolbypassrls):", cur.fetchone())
    app.commit()

    # --- Seed, as rag_doc_app, under each tenant ---------------------------
    def organization(label: str) -> tuple:
        slug = f"live-proof-{label}-{secrets.token_hex(3)}"
        with app.cursor() as cur:
            cur.execute(
                "INSERT INTO organizations (name, slug) VALUES (%s, %s) RETURNING id::text",
                (f"Live proof {label.upper()}", slug),
            )
            org = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO projects (organization_id, name, slug, is_default) "
                "VALUES (%s, %s, %s, true) RETURNING id::text",
                (org, "Default", f"{slug}-default"),
            )
            project = cur.fetchone()[0]
        app.commit()
        return org, project

    org_a, project_a = organization("a")
    org_b, project_b = organization("b")

    with require_tenant(app, org_a) as cur:
        cur.execute(
            "INSERT INTO github_installations (organization_id, github_installation_id, "
            "account_login, account_type, repository_selection) "
            "VALUES (%s, %s, 'AlecAsdourian', 'User', 'selected') RETURNING id::text",
            (org_a, INSTALLATION_ID),
        )
        installation = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO repositories (project_id, name, git_url, default_branch, installation_id, "
            "github_repo_id, visibility, size_kb) "
            "VALUES (%s, %s, %s, 'main', %s, %s, 'private', 75) RETURNING id::text",
            (project_a, "ES-SC-API-Navigator", f"https://github.com/{REPO_FULL_NAME}",
             installation, GITHUB_REPO_ID),
        )
        repo_a = cur.fetchone()[0]
        # The producer's statement, then the projection, as pkg/jobs.Enqueue writes them.
        cur.execute(ENQUEUE_UPSERT_SQL, (org_a, repo_a, "full_ingest"))
        job_id, was_existing = cur.fetchone()
        cur.execute(
            "UPDATE repositories SET sync_state = 'pending', updated_at = NOW() WHERE id = %s",
            (repo_a,),
        )
    print(f"org A {org_a}, project {project_a}, installation row {installation}, repository {repo_a}")
    print(f"org B {org_b}, project {project_b}, no repository")
    print(f"enqueued job {job_id} (was_existing={was_existing})")
    app.close()

    state = {
        "container": name,
        "port": port,
        "super_dsn": super_dsn,
        "app_dsn": app_dsn,
        "org_a": org_a,
        "org_b": org_b,
        "project_a": project_a,
        "repo_a": repo_a,
        "installation_row": installation,
        "job_1": job_id,
    }
    (SCRATCH / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    (SCRATCH / "app_dsn").write_text(app_dsn, encoding="utf-8")
    print("state written (DSNs are in the scratch file, never printed)")


if __name__ == "__main__":
    main()
