"""22-05 live proof, the checks. Run from services/workers.

Subcommands:
  wait JOB            poll the job row (as rag_doc_app) until it settles
  record JOB          items 1-4 and the connection identities
  search              items 5-6 through the RAG API's /search
  enqueue             enqueue another full_ingest for A (item 7); prints the job id
  idempotency J1 J2   item 7: the second ingest replaced the first, no duplicates
  logs                item 8: scan the three logs for secret shapes

Never prints a DSN, a password, a key, a token or a lease owner: worker ids
in quoted log lines are replaced with <worker>.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, List

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.fetch.filters import classify_path, is_secret_name
from workers.jobs.transitions import ENQUEUE_UPSERT_SQL

LIVE = pathlib.Path(r"C:\Users\Alec\AppData\Local\Temp\rag2205-ScfByQaa\live")
STATE = json.loads((LIVE / "state.json").read_text(encoding="utf-8"))
PORTS = dict(line.split("=", 1) for line in (LIVE / "ports.env").read_text().split())
REPO = "AlecAsdourian/ES-SC-API-Navigator"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

QUESTIONS = [
    ("Q1", "How does a free-text search term get matched to the facet and field it belongs to?",
     "scicrunch_poc_v5_column_filters.py"),
    ("Q2", "Which function pages through every matching record with a scroll_id and reports "
           "progress through a callback?", "scicrunch_gui_v5_column_filters.py"),
    ("Q3", "Which operating-system credential stores keep the SciCrunch API key on Windows, "
           "macOS and Linux?", "README.md"),
]


def app() -> Any:
    return psycopg2.connect(STATE["app_dsn"])


def job_row(conn: Any, job_id: str) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, job_type, state, attempts, max_attempts, last_stage, progress, "
            "last_error, lease_owner IS NOT NULL AS leased, ingestion_run_id::text AS run, "
            "created_at, updated_at, run_after, "
            "state = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < NOW()) AS stalled "
            "FROM ingestion_jobs WHERE id = %s",
            (job_id,),
        )
        row = dict(cur.fetchone())
    conn.commit()
    return row


def cmd_wait(job_id: str, timeout: float = 900.0) -> None:
    conn = app()
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        row = job_row(conn, job_id)
        seen = (row["state"], row["last_stage"], row["attempts"])
        if seen != last:
            print(time.strftime("%H:%M:%S"), "state=%s last_stage=%s attempts=%s" % seen, flush=True)
            last = seen
        if row["state"] in ("completed", "dead", "superseded") or (
            row["state"] == "queued" and row["last_error"]
        ):
            break
        time.sleep(1.0)
    print(json.dumps({k: (str(v) if not isinstance(v, (int, bool, dict, type(None))) else v)
                      for k, v in job_row(conn, job_id).items()}, indent=2, default=str))


def gh(path: str, jq: str) -> str:
    out = subprocess.run(["gh", "api", path, "--jq", jq], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"gh api {path} exited {out.returncode}"
    return out.stdout.strip()


def cmd_record(job_id: str) -> None:
    conn = app()
    row = job_row(conn, job_id)
    duration = (row["updated_at"] - row["created_at"]).total_seconds()
    print("## item 1: the job row")
    print(f"state={row['state']} attempts={row['attempts']}/{row['max_attempts']} "
          f"last_stage={row['last_stage']} last_error={row['last_error']!r} leased={row['leased']} "
          f"stalled={row['stalled']}")
    print(f"created_at -> updated_at: {duration:.1f} s (max_job_duration 7200 s)")
    print("progress:", json.dumps(row["progress"], sort_keys=True))

    print("## item 2: the projection")
    with require_tenant(conn, STATE["org_a"], cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT sync_state, last_synced_at FROM repositories WHERE id = %s", (STATE["repo_a"],)
        )
        repo = cur.fetchone()
    print(f"sync_state={repo['sync_state']} last_synced_at={repo['last_synced_at']}")

    print("## item 3: the run's commit against GitHub's, read now")
    with require_tenant(conn, STATE["org_a"], cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, commit_sha, branch, status, chunks_processed FROM ingestion_runs "
            "WHERE repository_id = %s ORDER BY started_at", (STATE["repo_a"],)
        )
        runs = [dict(r) for r in cur.fetchall()]
    head = gh(f"repos/{REPO}/commits/main", ".sha")
    for run in runs:
        print(f"run {run['id']} commit_sha={run['commit_sha']} branch={run['branch']} "
              f"status={run['status']} chunks_processed={run['chunks_processed']} "
              f"attached={run['id'] == row['run']}")
    print(f"gh api repos/{REPO}/commits/main --jq .sha -> {head}")
    print("run commit == GitHub head:", runs[-1]["commit_sha"] == head)

    print("## item 4: the chunks")
    with require_tenant(conn, STATE["org_a"], cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT file_path, count(*) AS n, bool_and(embedding IS NOT NULL) AS all_vectors, "
            "min(vector_dims(embedding)) AS dims_min, max(vector_dims(embedding)) AS dims_max, "
            "array_agg(DISTINCT embedding_model) AS models, array_agg(DISTINCT chunk_type) AS types "
            "FROM chunks WHERE repository_id = %s GROUP BY file_path ORDER BY file_path",
            (STATE["repo_a"],),
        )
        per_file = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM chunks WHERE repository_id = %s", (STATE["repo_a"],))
        total = cur.fetchone()["count"]
    print(f"total chunks: {total}")
    for f in per_file:
        print(f"  {f['file_path']}: {f['n']} chunks, all vectors={f['all_vectors']}, "
              f"dims={f['dims_min']}..{f['dims_max']}, models={f['models']}, types={f['types']}")
    sha = runs[-1]["commit_sha"]
    tree = gh(f"repos/{REPO}/git/trees/{sha}?recursive=1", '.tree[] | select(.type=="blob") | .path')
    tracked = set(tree.splitlines())
    paths = {f["file_path"] for f in per_file}
    print("every file_path tracked at the SHA:", paths <= tracked, f"({len(paths)} paths, {len(tracked)} blobs)")
    print("deny-listed names among chunk paths:",
          sorted(p for p in paths if is_secret_name(p.rsplit('/', 1)[-1])) or "none")
    print("tracked files the name filters index:",
          sorted(p for p in tracked if classify_path(p).indexable))

    print("## connection identities (pg_stat_activity, read as the scratch superuser)")
    sup = psycopg2.connect(STATE["super_dsn"])
    sup.autocommit = True
    with sup.cursor() as cur:
        cur.execute(
            "SELECT usename, COALESCE(NULLIF(application_name, ''), '(none)'), count(*) "
            "FROM pg_stat_activity WHERE datname = 'ragproof' AND pid <> pg_backend_pid() "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        )
        for usename, application, n in cur.fetchall():
            print(f"  usename={usename} application_name={application} connections={n}")
        cur.execute("SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'rag_doc_app'")
        print("  role:", cur.fetchone())
    sup.close()


def search(query: str, organization: str, top_k: int = 10) -> Dict[str, Any]:
    response = httpx.post(
        f"http://127.0.0.1:{PORTS['RAG_PORT']}/search",
        json={"query": query, "organization_id": organization,
              "repository_id": STATE["repo_a"], "top_k": top_k},
        timeout=120,
    )
    return {"status": response.status_code, "body": response.json()}


def cmd_search() -> None:
    conn = app()
    print("## item 5: /search as A, for each pre-registered question")
    for qid, question, expected in QUESTIONS:
        result = search(question, STATE["org_a"])
        results = result["body"].get("results", [])
        files = [r["file_path"] for r in results]
        rank = files.index(expected) + 1 if expected in files else None
        ids = [r["chunk_id"] for r in results]
        with require_tenant(conn, STATE["org_a"]) as cur:
            cur.execute(
                "SELECT c.id::text, ir.commit_sha FROM chunks c "
                "JOIN ingestion_runs ir ON ir.id = c.ingestion_run_id WHERE c.id = ANY(%s::uuid[])",
                (ids,),
            )
            commits = dict(cur.fetchall())
        print(f"{qid}: HTTP {result['status']}, {len(results)} results; expected {expected} at rank {rank}")
        for i, r in enumerate(results[:5], start=1):
            print(f"   {i}. {r['file_path']}:{r['start_line']}-{r['end_line']} "
                  f"{r.get('chunk_type')} score={r['score']:.4f} "
                  f"breadcrumb={r.get('breadcrumb')!r} commit={commits.get(r['chunk_id'], '?')[:12]}")
        print(f"   distinct commits among results: {sorted(set(commits.values()))}")
        print(f"   response metadata: {json.dumps(result['body'].get('metadata'), default=str)}")

    print("## item 6: /search as B, with A's repository id")
    for qid, question, _ in QUESTIONS:
        result = search(question, STATE["org_b"])
        print(f"{qid} as B: HTTP {result['status']}, total_results={result['body'].get('total_results')}, "
              f"results={len(result['body'].get('results', []))}")


def cmd_enqueue() -> None:
    conn = app()
    with require_tenant(conn, STATE["org_a"]) as cur:
        cur.execute(ENQUEUE_UPSERT_SQL, (STATE["org_a"], STATE["repo_a"], "full_ingest"))
        job_id, was_existing = cur.fetchone()
        if not was_existing:
            cur.execute(
                "UPDATE repositories SET sync_state = 'pending', updated_at = NOW() WHERE id = %s",
                (STATE["repo_a"],),
            )
    print(f"enqueued {job_id} was_existing={was_existing}")


def chunk_keys(conn: Any) -> List[tuple]:
    with require_tenant(conn, STATE["org_a"]) as cur:
        cur.execute(
            "SELECT id::text, file_path, start_line, end_line, content_hash, ingestion_run_id::text "
            "FROM chunks WHERE repository_id = %s", (STATE["repo_a"],)
        )
        return cur.fetchall()


def cmd_idempotency(first_count: str) -> None:
    conn = app()
    rows = chunk_keys(conn)
    keys = [(r[1], r[2], r[3], r[4]) for r in rows]
    print("## item 7: idempotency")
    print(f"chunks after the second ingest: {len(rows)} (after the first: {first_count})")
    print("duplicate (file_path, start_line, end_line, content_hash):", len(keys) - len(set(keys)))
    print("runs referenced by the chunks:", sorted({r[5] for r in rows}))


def cmd_snapshot_ids() -> None:
    conn = app()
    rows = chunk_keys(conn)
    (LIVE / "first_chunk_ids.json").write_text(json.dumps(sorted(r[0] for r in rows)))
    print(f"recorded {len(rows)} chunk ids from the first ingest")


def cmd_replaced() -> None:
    conn = app()
    before = set(json.loads((LIVE / "first_chunk_ids.json").read_text()))
    after = {r[0] for r in chunk_keys(conn)}
    print(f"ids surviving from the first ingest: {len(before & after)} of {len(before)} (0 means replaced)")


def cmd_logs() -> None:
    print("## item 8: secret shapes in the three logs")
    patterns = {
        "ghs_": re.compile(r"ghs_"),
        "-----BEGIN": re.compile(r"-----BEGIN"),
        "sk- (key-shaped)": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        "sk- (substring)": re.compile(r"sk-"),
        "Authorization:": re.compile(r"Authorization:", re.IGNORECASE),
        "Bearer": re.compile(r"Bearer\s+\S"),
    }
    for name in ("backend.log", "worker.log", "rag.log"):
        text = (LIVE / name).read_text(encoding="utf-8", errors="replace")
        counts = {label: len(p.findall(text)) for label, p in patterns.items()}
        print(f"{name}: {len(text.splitlines())} lines; " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if counts["sk- (substring)"] and not counts["sk- (key-shaped)"]:
            ctx = sorted({text[max(0, m.start() - 12):m.start() + 3] for m in patterns["sk- (substring)"].finditer(text)})
            print(f"   'sk-' substrings are not key-shaped: {ctx[:6]}")


def cmd_quote(which: str, pattern: str) -> None:
    """Print matching log lines with worker ids (lease owners) replaced."""
    text = (LIVE / which).read_text(encoding="utf-8", errors="replace")
    worker_ids = set(re.findall(r"worker[= ](" + UUID.pattern + ")", text))
    for line in text.splitlines():
        if re.search(pattern, line):
            for wid in worker_ids:
                line = line.replace(wid, "<worker>")
            print(line)


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    {
        "wait": lambda: cmd_wait(*args),
        "record": lambda: cmd_record(*args),
        "search": cmd_search,
        "enqueue": cmd_enqueue,
        "idempotency": lambda: cmd_idempotency(*args),
        "snapshot": cmd_snapshot_ids,
        "replaced": cmd_replaced,
        "logs": cmd_logs,
        "quote": lambda: cmd_quote(*args),
    }[command]()
