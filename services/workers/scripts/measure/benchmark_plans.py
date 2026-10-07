"""Is the retrieval benchmark served by exact search? `EXPLAIN` says (22.1-05).

The user's decision of 2026-10-06 set `hnsw.ef_search` on the vector leg
(`HNSW_EF_SEARCH`). That setting only matters where the planner chooses the
HNSW index; where it chooses an exact scan, rankings cannot change. This script
shows which the planner chooses for the benchmark's layout:

- the harness's own organization id (`rag_quality_harness.ORG`, so the same
  `chunks` partition) and its repository ids (`uuid5(BENCHMARK_NAMESPACE,
  name)` for mealie and miniflux, `REPO` for self);
- each corpus seeded with 22.1-05's exported REAL vectors of the WHOLE
  repository -- a superset of the benchmark's scoped corpora, so if the whole
  repository plans exact, the smaller scoped one does too;
- seeded as `rag_doc_app` through `PostgresWriter.insert_chunks_on`, then
  `ANALYZE` (privileged: it needs the table's owner);
- `EXPLAIN (COSTS OFF)` of the imported `VECTOR_SEARCH_SQL` with exactly what
  `VectorRetriever.search` sets first (`ITERATIVE_SCAN_SQL`, `EF_SEARCH_SQL`).

No OpenAI call. Run on a scratch database (`scratch_db.py up`).

    python scripts/measure/benchmark_plans.py --state <state.json> [--inside] \
        --export <export dir> --out <plans.json>
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from uuid import UUID, uuid5

import numpy as np
import psycopg2

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from workers.chunker.models import Chunk  # noqa: E402
from workers.db import require_tenant  # noqa: E402
from workers.jobs.transitions import resolve_ingestion_run  # noqa: E402
from workers.retrieval.vector_retriever import (  # noqa: E402
    EF_SEARCH_SQL,
    ITERATIVE_SCAN_SQL,
    VECTOR_SEARCH_SQL,
    vector_literal,
)
from workers.storage.postgres_writer import PostgresWriter, content_hash  # noqa: E402

# rag_quality_harness.py's ids (its ORG, REPO and BENCHMARK_NAMESPACE),
# restated because importing the harness reads its environment. Compared by
# hand with the harness on 2026-10-06; re-check them if the harness changes.
ORG = UUID("0ca11117-0000-4000-8000-00000000f001")
REPO_SELF = UUID("0ca11117-0000-4000-8000-00000000f003")
BENCHMARK_NAMESPACE = UUID("0ca11117-0000-4000-8000-00000000f0b0")
CORPORA = {"self": ("rag-doc", REPO_SELF), "mealie": ("mealie", uuid5(BENCHMARK_NAMESPACE, "mealie")),
           "miniflux": ("miniflux", uuid5(BENCHMARK_NAMESPACE, "miniflux"))}
MODEL = "text-embedding-ada-002"
HNSW = re.compile(r"Index Scan using chunks_p\d+_embedding_idx")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True)
    p.add_argument("--inside", action="store_true")
    p.add_argument("--export", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    state = json.loads(pathlib.Path(args.state).read_text(encoding="utf-8"))
    app = psycopg2.connect(state["app_dsn_inside" if args.inside else "app_dsn"])
    su = psycopg2.connect(state["super_dsn_inside" if args.inside else "super_dsn"])
    su.autocommit = True
    export = pathlib.Path(args.export)

    with app.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        probe = cur.fetchone()
        assert probe == ("rag_doc_app", False, False), probe
        cur.execute("INSERT INTO organizations (id, name, slug) VALUES (%s, 'Benchmark plans', %s)",
                    (str(ORG), f"bench-plans-{ORG.hex[:6]}"))
        cur.execute("INSERT INTO projects (organization_id, name, slug) VALUES (%s, 'Default', %s) "
                    "RETURNING id::text", (str(ORG), f"bench-plans-{ORG.hex[:6]}-proj"))
        project = cur.fetchone()[0]
    app.commit()

    seeded = {}
    for corpus, (label, repo) in CORPORA.items():
        vectors = np.load(export / f"{label}.npy")
        with require_tenant(app, str(ORG)) as cur:
            cur.execute("INSERT INTO repositories (id, project_id, name, git_url) VALUES (%s, %s, %s, %s)",
                        (str(repo), project, corpus, f"https://example.test/{corpus}.git"))
            run = resolve_ingestion_run(cur, str(repo), "b" * 40, "main")
            chunks, emb = [], {}
            for i, v in enumerate(vectors):
                text = f"{corpus} chunk {i}"
                chunks.append(Chunk(content=text, file_path=f"{corpus}/{i:06d}.py", start_line=1, end_line=1,
                                    language="python", chunk_type="fixed_size", metadata={}))
                emb[content_hash(text)] = v
            PostgresWriter.insert_chunks_on(cur, str(ORG), chunks, run, str(repo),
                                            embeddings=emb, embedding_model=MODEL)
        seeded[corpus] = len(vectors)
    with su.cursor() as cur:
        cur.execute("ANALYZE chunks")
        cur.execute("SELECT DISTINCT tableoid::regclass::text FROM chunks WHERE organization_id = %s", (str(ORG),))
        partitions = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM chunks WHERE organization_id = %s", (str(ORG),))
        org_rows = cur.fetchone()[0]

    query = np.load(export / "django.npy")[0]  # any real vector; the plan does not depend on its value
    out = {"organization": str(ORG), "partitions": partitions, "organization_rows": org_rows,
           "production_sets": [ITERATIVE_SCAN_SQL, EF_SEARCH_SQL], "corpora": {}}
    for corpus, (_label, repo) in CORPORA.items():
        with require_tenant(app, str(ORG)) as cur:
            cur.execute(ITERATIVE_SCAN_SQL)
            cur.execute(EF_SEARCH_SQL)
            cur.execute("EXPLAIN (COSTS OFF) " + VECTOR_SEARCH_SQL,
                        {"q": vector_literal(query), "repo": str(repo), "model": MODEL, "limit": 50})
            plan = "\n".join(r[0] for r in cur.fetchall())
        plan = re.sub(r"'\[[-0-9.,e]{200,}\]'", "'[<the query vector>]'", plan)
        out["corpora"][corpus] = {"rows": seeded[corpus], "served_by": "hnsw" if HNSW.search(plan) else "exact",
                                  "plan": plan}
        print(json.dumps({corpus: out["corpora"][corpus]["served_by"], "rows": seeded[corpus]}))
    pathlib.Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    app.close()
    su.close()
    return 0 if all(c["served_by"] == "exact" for c in out["corpora"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
