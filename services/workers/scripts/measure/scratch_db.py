"""A SCRATCH pgvector database for 22.1-05's measurements: up, and down.

Never compose, never port 5434, never a compose volume. The image is 22-01's
pinned digest. `up` starts a container on a Docker-assigned loopback port,
migrates it with golang-migrate on the container's own network namespace,
and creates `rag_doc_app` LOGIN NOSUPERUSER NOBYPASSRLS with the isolation
harness's grants (as 22-05's `live_setup.py` did). It writes the two DSNs to
`<state-dir>/state.json` and never prints a password. `down` removes exactly
the container `up` created, and nothing else.

    python scripts/measure/scratch_db.py up   --state-dir <dir outside the repo>
    python scripts/measure/scratch_db.py down --state-dir <same dir>
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

import psycopg2

IMAGE = (
    "pgvector/pgvector:pg16"
    "@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
)
MIGRATE_IMAGE = "migrate/migrate:v4.19.1"
MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "backend" / "migrations"
APP_ROLE = "rag_doc_app"


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", **kwargs)


def up(state_dir: pathlib.Path, name_prefix: str) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True)
    name = f"{name_prefix}-{secrets.token_hex(3)}"
    super_pw = secrets.token_urlsafe(18)
    app_pw = secrets.token_urlsafe(18)
    started = run(
        ["docker", "run", "-d", "--name", name,
         "-e", f"POSTGRES_PASSWORD={super_pw}", "-e", "POSTGRES_DB=ragmeasure",
         "-p", "127.0.0.1::5432", IMAGE]
    )
    if started.returncode != 0:
        sys.exit(f"docker run failed ({started.returncode}): {started.stderr.strip()[:300]}")
    port = int(run(["docker", "port", name, "5432"]).stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    if port == 5434:
        run(["docker", "rm", "-f", name])
        sys.exit("Docker assigned port 5434, which is not this project's database; refusing")
    super_dsn = f"postgresql://postgres:{super_pw}@127.0.0.1:{port}/ragmeasure?sslmode=disable"
    for _ in range(240):
        try:
            psycopg2.connect(super_dsn, connect_timeout=2).close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.5)
    else:
        sys.exit("the scratch database never became ready")
    # The entrypoint restarts the server once after initdb; wait it out.
    time.sleep(2)
    for _ in range(240):
        try:
            psycopg2.connect(super_dsn, connect_timeout=2).close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.5)

    migrated = run(
        ["docker", "run", "--rm", "--network", f"container:{name}",
         "-v", f"{MIGRATIONS}:/migrations:ro", MIGRATE_IMAGE,
         "-path=/migrations",
         "-database", f"postgres://postgres:{super_pw}@localhost:5432/ragmeasure?sslmode=disable",
         "up"]
    )
    if migrated.returncode != 0:
        sys.exit(f"migrate up failed ({migrated.returncode})")

    conn = psycopg2.connect(super_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT version, dirty FROM schema_migrations")
        version, dirty = cur.fetchone()
        cur.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD %s", (app_pw,)
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
        cur.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
        cur.execute("SHOW server_version")
        server_version = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        pgvector = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_connections = int(cur.fetchone()[0])
        settings = {}
        for name_ in ("shared_buffers", "maintenance_work_mem", "work_mem",
                      "effective_cache_size", "max_wal_size", "synchronous_commit"):
            cur.execute(f"SHOW {name_}")
            settings[name_] = cur.fetchone()[0]
    conn.close()

    app_dsn = f"postgresql://{APP_ROLE}:{app_pw}@127.0.0.1:{port}/ragmeasure?sslmode=disable"
    probe = psycopg2.connect(app_dsn)
    with probe.cursor() as cur:
        cur.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        user, is_super, bypass = cur.fetchone()
    probe.close()
    assert (user, is_super, bypass) == (APP_ROLE, False, False), (user, is_super, bypass)

    state = {
        "container": name,
        "port": port,
        "image": IMAGE,
        # In-container DSNs, for a process sharing the container's network
        # namespace (`docker run --network container:<name>`).
        "super_dsn_inside": super_dsn.replace(f"127.0.0.1:{port}", "localhost:5432"),
        "app_dsn_inside": app_dsn.replace(f"127.0.0.1:{port}", "localhost:5432"),
        "super_dsn": super_dsn,
        "app_dsn": app_dsn,
        "schema_version": version,
        "schema_dirty": dirty,
        "server_version": server_version,
        "pgvector": pgvector,
        "max_connections": max_connections,
        "settings": settings,
        "app_role_probe": {"current_user": user, "rolsuper": is_super, "rolbypassrls": bypass},
    }
    (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    public = {k: v for k, v in state.items() if "dsn" not in k}
    print(json.dumps(public, indent=2))
    return state


def down(state_dir: pathlib.Path) -> None:
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    removed = run(["docker", "rm", "-f", "-v", state["container"]])
    print(f"removed {state['container']}: exit {removed.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["up", "down"])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--name-prefix", default="rag2215-measure-pg")
    args = parser.parse_args()
    state_dir = pathlib.Path(args.state_dir).resolve()
    repo = pathlib.Path(__file__).resolve().parents[4]
    if str(state_dir).startswith(str(repo)):
        sys.exit("--state-dir must be outside the repository: it holds passwords")
    if args.cmd == "up":
        up(state_dir, args.name_prefix)
    else:
        down(state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
