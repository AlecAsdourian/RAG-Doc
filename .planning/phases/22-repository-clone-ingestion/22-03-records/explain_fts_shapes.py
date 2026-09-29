"""EXPLAIN the keyword leg's two predicate shapes on the scratch data, as rag_doc_app
under the harness tenant: 22-02's `ingestion_run_id = <latest completed run>` and
22-03's `repository_id = <repo>`. Same rows on a one-run corpus; the question is
whether the plan (and so the input order to the top-N sort, which decides the
order of equal ts_rank_cd scores) is the same shape. Read-only.

Run in 22-03 before the baseline was measured; its output is
explain_fts_shapes-baseline.txt beside this file, and the conclusion is in
22-03-equivalence.md ("The baseline"). The SELECT below is 22-02's
FTSRetriever statement with the predicate parameterised, kept here verbatim so
the measurement can be re-read.

Environment:
    WORKERS   path to services/workers (for `workers.db`)
    PG_APP    a DSN carrying `options=-c role=rag_doc_app` on the scratch database
"""
import os
import sys
from uuid import UUID, uuid5

import psycopg2

sys.path.insert(0, os.environ["WORKERS"])
from workers.db import require_tenant  # noqa: E402

ORG = UUID("0ca11117-0000-4000-8000-00000000f001")
REPOS = {
    "self": UUID("0ca11117-0000-4000-8000-00000000f003"),
    "miniflux": uuid5(UUID("0ca11117-0000-4000-8000-00000000f0b0"), "miniflux"),
    "mealie": uuid5(UUID("0ca11117-0000-4000-8000-00000000f0b0"), "mealie"),
}
QUESTION = "how are keyword results and vector results combined into one ranking"

SELECT = """
    SELECT id::text as chunk_id, file_path, start_line, end_line, breadcrumb, chunk_type,
        LEFT(content, 200) as content_preview,
        GREATEST(
            ts_rank_cd(to_tsvector('english', content), plainto_tsquery('english', %(q)s)),
            ts_rank_cd(to_tsvector('english', COALESCE(breadcrumb, '')), plainto_tsquery('english', %(q)s))
        ) as fts_score
    FROM chunks
    WHERE {predicate}
        AND (
            to_tsvector('english', content) @@ plainto_tsquery('english', %(q)s)
            OR to_tsvector('english', COALESCE(breadcrumb, '')) @@ plainto_tsquery('english', %(q)s)
        )
    ORDER BY fts_score DESC
    LIMIT 50
"""

conn = psycopg2.connect(os.environ["PG_APP"])
with conn.cursor() as cur:
    cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    print("connection:", cur.fetchone())
conn.rollback()
for name, repo in REPOS.items():
    with require_tenant(conn, ORG) as cur:
        cur.execute("SELECT id FROM ingestion_runs WHERE repository_id = %s AND status = 'completed' "
                    "ORDER BY completed_at DESC LIMIT 1", (str(repo),))
        row = cur.fetchone()
        if not row:
            print(f"{name}: no completed run yet")
            continue
        run_id = row[0]
        cur.execute("SELECT count(*) FROM chunks WHERE repository_id = %s", (str(repo),))
        n = cur.fetchone()[0]
        print(f"\n=== {name}: {n} chunks visible, run {str(run_id)[:8]} ===")
        for label, predicate, params in (
            ("22-02 shape: ingestion_run_id = latest completed run", "ingestion_run_id = %(run)s",
             {"q": QUESTION, "run": str(run_id)}),
            ("22-03 shape: repository_id = repo", "repository_id = %(repo)s", {"q": QUESTION, "repo": str(repo)}),
        ):
            cur.execute("EXPLAIN (COSTS OFF) " + SELECT.format(predicate=predicate), params)
            print(f"--- {label}")
            for (line,) in cur.fetchall():
                print("   ", line)
            cur.execute(SELECT.format(predicate=predicate), params)
            rows = cur.fetchall()
            print(f"    rows: {len(rows)}; first: {[(r[0][:8], round(r[7], 6)) for r in rows[:5]]}")
conn.close()
