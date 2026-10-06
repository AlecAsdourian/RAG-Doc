"""D2's recall test (22.1-05): the production vector search against exact
search, on REAL ada-002 vectors, multi-tenant and multi-repository.

The rule is `22.1-05-recall.md`, "Rule" (R1-R6, LOCKED by the user on
2026-10-06), committed before any query here ran. This file asserts it.

WHY IT EXISTS (`DECISIONS.md` D2, the 2026-09-17 correction). Small tenants
get exact search, so recall from them is 1.000 by construction; absolute
recall depends on the data, so the vectors must be real; and the product
filters by repository, so iterative scan is load-bearing (P5). 22-03 proved
HNSW COULD serve the query (`test_the_hnsw_index_can_serve_the_vector_leg`,
with the alternatives switched off), never that it DOES at size.

THE VECTORS are 22.1-05's export (`RECALL_VECTORS_PATH`): the real chunk
vectors of the three benchmark corpora and of the large repository L
(django), written by Task 2's real runs. No OpenAI call is made here: the
queries are the 130 committed benchmark question vectors
(`22-03-records/vecs.json.gz`) and 100 of L's chunk vectors, chosen with a
fixed seed and HELD OUT of every seeded copy.

THE LAYOUT. Organization ids are chosen with `satisfies_hash_partition`
(as `chunks_partition_test.go` does) and every placement is read back
through `tableoid`:
- **H**, the HNSW tenant: L, plus copies of mealie and miniflux -- a
  repository filter inside one partition;
- the SHARED partition holds H and three co-tenants: **S1** (RAG-Doc and
  miniflux), **S2** (mealie) and **T**, the twin, a second copy of L, so the
  HNSW walk meets another tenant's identical vectors, which row-level
  security must filter out;
- **Z**, in another partition: disjoint slices of L as repositories of
  5,000, 10,000 and 20,000 rows and the rest -- the planner's flip point is
  read from where the plan changes;
- **A** and **B**, in two more partitions, one corpus copy each.

THE TWO SEARCHES, for every scope (tenant x repository x query):
- production: `VectorRetriever(app_dsn, fake).search(key, org, repo,
  limit=50)`, the code the product runs (iterative scan, relaxed order,
  re-sorted by exact distance);
- exact: the IMPORTED `VECTOR_SEARCH_SQL` in a `require_tenant`
  transaction with `SET LOCAL enable_indexscan = off`. Never a hand-written
  query: a helper with its own copy of a predicate is blind to mutations of
  the original.

AS THE APP ROLE. Seeding (through `PostgresWriter.insert_chunks_on`, the
production writer) and every query run as `rag_doc_app`, probed for
`rolsuper` and `rolbypassrls` first. Only `ANALYZE` runs privileged: it
needs the table's owner, as autovacuum is in production.

RUN IT on a FRESH, migrated scratch database (`scripts/measure/
scratch_db.py up`); it refuses one that already holds chunks, so the
placements and counts it asserts are exactly its own. It leaves its rows;
discard the database afterwards.

    RECALL_VECTORS_PATH=<export dir> DATABASE_TEST_URL=<scratch superuser DSN> \
      [RECALL_RESULTS_PATH=<file.json>] pytest -m recall tests/recall -q
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest

VECTORS_ENV = "RECALL_VECTORS_PATH"
DATABASE_ENV = "DATABASE_TEST_URL"
RESULTS_ENV = "RECALL_RESULTS_PATH"

pytestmark = [
    pytest.mark.recall,
    pytest.mark.skipif(
        not (os.environ.get(VECTORS_ENV) and os.environ.get(DATABASE_ENV)),
        reason=(
            f"D2's recall test (22.1-05) runs on demand: set {VECTORS_ENV} (22.1-05's "
            f"vector export) and {DATABASE_ENV} (a fresh, migrated scratch database's "
            "superuser DSN). See 22.1-05-recall.md."
        ),
    ),
]

MODEL = "text-embedding-ada-002"
LIMIT = 50  # QueryEngine's limit for the vector leg (query_engine.py)
TIE_TOLERANCE = 2e-6  # QD2's tolerance; mealie's duplicate chunks share vectors
HELD_OUT = 100
SEED = 2215
PARTITIONS = 64
Z_SLICES = (5_000, 10_000, 20_000)

# The rule's thresholds, LOCKED by the user on 2026-10-06 (22.1-05-recall.md).
R3_MEAN_AT_10 = 0.95
R3_EACH_AT_10 = 0.70
R3_MEAN_AT_50 = 0.90
R4_AT_50 = 1.0

QUESTION_VECTORS = (
    pathlib.Path(__file__).resolve().parents[4]
    / ".planning" / "phases" / "22-repository-clone-ingestion" / "22-03-records" / "vecs.json.gz"
)

HNSW_SCAN = re.compile(r"Index Scan using (chunks_p\d+_embedding_idx) on (chunks_p\d+)")


# =====================================================================
# Helpers
# =====================================================================


class _FakeClient:
    def __init__(self, vectors: Dict[str, List[float]]) -> None:
        self.vectors = vectors

    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.vectors[t] for t in texts]


class FakeEmbeddingGenerator:
    """What `VectorRetriever` reads: `.model` and `.client.generate_embeddings_batch`."""

    model = MODEL

    def __init__(self, vectors: Dict[str, List[float]]) -> None:
        self.client = _FakeClient(vectors)


def _partition_of(cur: Any, org_id: str) -> int:
    cur.execute(
        "SELECT r FROM generate_series(0, %s) r "
        "WHERE satisfies_hash_partition('public.chunks'::regclass, %s, r, %s::uuid)",
        (PARTITIONS - 1, PARTITIONS, org_id),
    )
    return int(cur.fetchone()[0])


def _org_in(cur: Any, wanted: Optional[int], avoid: set) -> Tuple[str, int]:
    for _ in range(100_000):
        candidate = str(uuid.uuid4())
        r = _partition_of(cur, candidate)
        if (wanted is None and r not in avoid) or r == wanted:
            return candidate, r
    raise AssertionError("no organization id found for the partition")


def _plan(cur: Any, sql: str, params: dict) -> str:
    cur.execute("EXPLAIN (COSTS OFF) " + sql, params)
    return "\n".join(row[0] for row in cur.fetchall())


def _recall(prod: List[Tuple[str, float]], exact: List[Tuple[str, float]], k: int,
            scope_ids: set) -> Optional[float]:
    """Tie-aware recall@k: a returned chunk is a hit if it is in scope and its
    exact distance is within TIE_TOLERANCE of the exact k-th distance."""
    k_eff = min(k, len(exact))
    if k_eff == 0:
        return None
    kth = exact[k_eff - 1][1]
    hits = sum(1 for cid, d in prod[:k_eff] if cid in scope_ids and d <= kth + TIE_TOLERANCE)
    return hits / k_eff


# =====================================================================
# The test
# =====================================================================


def test_d2_recall_on_real_vectors_under_the_locked_rule():  # noqa: C901 - one rule, in order
    import numpy as np
    import psycopg2

    from workers.chunker.models import Chunk
    from workers.db import require_tenant
    from workers.jobs.runtime import _merged_options_dsn
    from workers.jobs.transitions import resolve_ingestion_run
    from workers.retrieval.vector_retriever import VECTOR_SEARCH_SQL, VectorRetriever, vector_literal
    from workers.storage.postgres_writer import PostgresWriter, content_hash

    export = pathlib.Path(os.environ[VECTORS_ENV])
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    arrays = {e["label"]: np.load(export / e["file"]) for e in manifest["repositories"]}
    for label in ("django", "mealie", "miniflux", "rag-doc"):
        assert label in arrays, f"the export has no {label!r} vectors: {sorted(arrays)}"
    assert {m for e in manifest["repositories"] for m in e["models"]} == {MODEL}, (
        "premise: every exported vector is ada-002"
    )

    super_dsn = os.environ[DATABASE_ENV]
    app_dsn = _merged_options_dsn(super_dsn, "-c role=rag_doc_app")
    results: Dict[str, Any] = {"written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "rule": "22.1-05-recall.md, Rule (R1-R6), locked 2026-10-06"}

    # ---- premises: the role, a fresh database --------------------------
    app = psycopg2.connect(app_dsn)
    with app.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        probe = cur.fetchone()
    app.rollback()
    assert probe == ("rag_doc_app", False, False), f"not the unprivileged app role: {probe}"
    privileged = psycopg2.connect(super_dsn)
    privileged.autocommit = True
    with privileged.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks")
        assert cur.fetchone()[0] == 0, "run on a FRESH scratch database: chunks already holds rows"
        cur.execute("SHOW server_version")
        results["postgres"] = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        results["pgvector"] = cur.fetchone()[0]

    # ---- the queries and L's split ---------------------------------------
    rng = np.random.default_rng(SEED)
    L = arrays["django"]
    held = np.sort(rng.choice(len(L), HELD_OUT, replace=False))
    keep = np.setdiff1d(np.arange(len(L)), held)
    L_seeded = L[keep]
    questions = json.load(gzip.open(QUESTION_VECTORS, "rt", encoding="utf-8"))
    queries: Dict[str, List[float]] = {}
    for qid, entry in sorted(questions.items()):
        assert entry["model"] == MODEL
        queries[f"q:{qid}"] = entry["vector"]
    for i, idx in enumerate(held):
        queries[f"h:{i:03d}"] = L[idx].astype(float).tolist()
    assert len(queries) == 130 + HELD_OUT, len(queries)
    perm = rng.permutation(len(L_seeded))
    cuts = np.cumsum((0,) + Z_SLICES)
    z_parts = {f"L-slice-{n}": L_seeded[perm[cuts[i]:cuts[i + 1]]] for i, n in enumerate(Z_SLICES)}
    z_parts["L-slice-rest"] = L_seeded[perm[cuts[-1]:]]

    # ---- the tenants and their partitions --------------------------------
    with app.cursor() as cur:
        h_org, shared = _org_in(cur, None, set())
        s1_org, _ = _org_in(cur, shared, set())
        s2_org, _ = _org_in(cur, shared, set())
        t_org, _ = _org_in(cur, shared, set())
        z_org, z_part = _org_in(cur, None, {shared})
        a_org, a_part = _org_in(cur, None, {shared, z_part})
        b_org, b_part = _org_in(cur, None, {shared, z_part, a_part})
    app.rollback()
    tenants = {"H": (h_org, shared), "S1": (s1_org, shared), "S2": (s2_org, shared),
               "T": (t_org, shared), "Z": (z_org, z_part), "A": (a_org, a_part), "B": (b_org, b_part)}
    layout = {
        "H": [("L", L_seeded), ("mealie", arrays["mealie"]), ("miniflux", arrays["miniflux"])],
        "S1": [("rag-doc", arrays["rag-doc"]), ("miniflux", arrays["miniflux"])],
        "S2": [("mealie", arrays["mealie"])],
        "T": [("L-twin", L_seeded)],
        "Z": list(z_parts.items()),
        "A": [("miniflux", arrays["miniflux"])],
        "B": [("rag-doc", arrays["rag-doc"])],
    }

    # ---- seeding, as rag_doc_app, through the production writer ----------
    seed_started = time.monotonic()
    repos: Dict[Tuple[str, str], str] = {}
    seed_seconds: Dict[str, float] = {}
    for tenant, (org, _r) in tenants.items():
        slug = f"recall-{tenant.lower()}-{uuid.uuid4().hex[:6]}"
        with app.cursor() as cur:
            cur.execute("INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
                        (org, f"Recall {tenant}", slug))
            cur.execute("INSERT INTO projects (organization_id, name, slug) VALUES (%s, %s, %s) "
                        "RETURNING id::text", (org, "Default", f"{slug}-proj"))
            project = cur.fetchone()[0]
        app.commit()
        for label, vectors in layout[tenant]:
            started = time.monotonic()
            with require_tenant(app, org) as cur:
                cur.execute("INSERT INTO repositories (project_id, name, git_url) VALUES (%s, %s, %s) "
                            "RETURNING id::text", (project, label, f"https://example.test/{slug}/{label}.git"))
                repo = cur.fetchone()[0]
                run = resolve_ingestion_run(cur, repo, "a" * 40, "main")
                chunks, embeddings = [], {}
                for i in range(len(vectors)):
                    text = f"{tenant}/{label} chunk {i}"  # a placeholder: the vector leg reads no content
                    chunks.append(Chunk(content=text, file_path=f"{label}/{i:06d}.py", start_line=1,
                                        end_line=1, language="python", chunk_type="fixed_size", metadata={}))
                    embeddings[content_hash(text)] = vectors[i]
                PostgresWriter.insert_chunks_on(cur, org, chunks, run, repo, embeddings=embeddings,
                                                embedding_model=MODEL)
            repos[(tenant, label)] = repo
            seed_seconds[f"{tenant}/{label}"] = round(time.monotonic() - started, 2)
    results["seed_seconds"] = seed_seconds
    results["seed_total_seconds"] = round(time.monotonic() - seed_started, 1)
    results["seed_rows"] = int(sum(len(v) for t in layout for _, v in layout[t]))

    analyze_started = time.monotonic()
    with privileged.cursor() as cur:
        cur.execute("ANALYZE chunks")
    results["analyze"] = {"seconds": round(time.monotonic() - analyze_started, 1),
                          "by": "the superuser: ANALYZE needs the table's owner; autovacuum does "
                                "this in production, and the planner's choice depends on it"}

    # ---- settings ------------------------------------------------------
    with privileged.cursor() as cur:
        settings = {}
        for name in ("hnsw.ef_search", "hnsw.max_scan_tuples", "hnsw.scan_mem_multiplier",
                     "hnsw.iterative_scan", "enable_seqscan", "enable_indexscan",
                     "enable_bitmapscan", "random_page_cost", "work_mem"):
            try:
                cur.execute(f"SHOW {name}")
                settings[name] = cur.fetchone()[0]
            except psycopg2.Error:
                settings[name] = None
        cur.execute("SELECT relname, reloptions FROM pg_class WHERE relname = %s",
                    (f"chunks_p{shared}_embedding_idx",))
        settings["shared_index_reloptions"] = [list(r) for r in cur.fetchall()]
    results["settings"] = settings

    # ---- premises: placements and row counts ----------------------------
    scope_ids: Dict[Tuple[str, str], set] = {}
    placements: Dict[str, List[str]] = {}
    for tenant, (org, r) in tenants.items():
        with require_tenant(app, org) as cur:
            cur.execute("SELECT DISTINCT tableoid::regclass::text FROM chunks")
            placements[tenant] = sorted(row[0] for row in cur.fetchall())
            for label, vectors in layout[tenant]:
                cur.execute("SELECT id::text FROM chunks WHERE repository_id = %s", (repos[(tenant, label)],))
                ids = {row[0] for row in cur.fetchall()}
                assert len(ids) == len(vectors), f"{tenant}/{label}: {len(ids)} rows, seeded {len(vectors)}"
                scope_ids[(tenant, label)] = ids
        assert placements[tenant] == [f"chunks_p{r}"], (
            f"tenant {tenant} is not where satisfies_hash_partition put it: {placements[tenant]} vs p{r}"
        )
    results["placements"] = placements
    results["partitions"] = {"shared": shared, "Z": z_part, "A": a_part, "B": b_part}
    with privileged.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks_p%s" % shared)
        results["shared_partition_rows"] = cur.fetchone()[0]

    # ---- the searches --------------------------------------------------
    fake = FakeEmbeddingGenerator(queries)
    retriever = VectorRetriever(app_dsn, fake)
    exact_conn = psycopg2.connect(app_dsn)
    scopes: Dict[str, Any] = {}
    per_query: Dict[str, Dict[str, float]] = {}
    try:
        for (tenant, label), repo in repos.items():
            org = tenants[tenant][0]
            ids = scope_ids[(tenant, label)]
            key = f"{tenant}/{label}"
            started = time.monotonic()
            first = next(iter(queries.values()))
            params0 = {"q": vector_literal(first), "repo": repo, "model": MODEL, "limit": LIMIT}
            with require_tenant(exact_conn, org) as cur:
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                prod_plan = _plan(cur, VECTOR_SEARCH_SQL, params0)
            with require_tenant(exact_conn, org) as cur:
                cur.execute("SET LOCAL enable_indexscan = off")
                exact_plan = _plan(cur, VECTOR_SEARCH_SQL, params0)
            served = "hnsw" if HNSW_SCAN.search(prod_plan) else "exact"
            r10, r50, short, foreign = [], [], 0, 0
            qrec: Dict[str, float] = {}
            for qkey, qvec in queries.items():
                prod_rows = retriever.search(qkey, org, repo, limit=LIMIT)
                prod = [(r["chunk_id"], 1.0 - r["vector_score"]) for r in prod_rows]
                params = {"q": vector_literal(qvec), "repo": repo, "model": MODEL, "limit": LIMIT}
                with require_tenant(exact_conn, org) as cur:
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute(VECTOR_SEARCH_SQL, params)
                    exact = sorted(((row[0], float(row[4])) for row in cur.fetchall()), key=lambda x: x[1])
                expected = min(LIMIT, len(ids))
                if len(prod) != expected:
                    short += 1
                foreign += sum(1 for cid, _ in prod if cid not in ids) + sum(1 for cid, _ in exact if cid not in ids)
                a10, a50 = _recall(prod, exact, 10, ids), _recall(prod, exact, 50, ids)
                r10.append(a10)
                r50.append(a50)
                qrec[qkey] = a10
            per_query[key] = qrec
            scopes[key] = {
                "tenant": tenant, "repository": label, "rows": len(ids), "served_by": served,
                "partition": tenants[tenant][1],
                "production_plan": prod_plan, "exact_plan": exact_plan,
                "recall_at_10": {"mean": float(np.mean(r10)), "min": float(np.min(r10))},
                "recall_at_50": {"mean": float(np.mean(r50)), "min": float(np.min(r50))},
                "short_results": short, "foreign_results": foreign, "queries": len(queries),
                "seconds": round(time.monotonic() - started, 1),
            }
    finally:
        retriever.close()
        exact_conn.close()
    results["scopes"] = scopes

    # The flip point, read from Z's slices and the shared partition's L.
    results["flip"] = {k: {"rows": v["rows"], "served_by": v["served_by"]}
                       for k, v in scopes.items() if k.startswith("Z/") or k in ("H/L", "T/L-twin")}
    # The twin's effect: L under H (the twin T shares the partition) beside the
    # same held-out and question queries in Z's largest slices.
    results["twin"] = {k: {"recall_at_10_mean": scopes[k]["recall_at_10"]["mean"],
                           "recall_at_10_min": scopes[k]["recall_at_10"]["min"],
                           "served_by": scopes[k]["served_by"], "rows": scopes[k]["rows"]}
                       for k in ("H/L", "T/L-twin", "Z/L-slice-20000", "Z/L-slice-rest") if k in scopes}

    # R3's exploration, only if R3 fails: the ef_search curve. Nothing tuned.
    r3_failing = [k for k, v in scopes.items() if v["served_by"] == "hnsw" and (
        v["recall_at_10"]["mean"] < R3_MEAN_AT_10 or v["recall_at_10"]["min"] < R3_EACH_AT_10
        or v["recall_at_50"]["mean"] < R3_MEAN_AT_50)]
    if r3_failing:
        curve: Dict[str, Any] = {}
        conn = psycopg2.connect(app_dsn)
        try:
            for key in r3_failing:
                tenant, label = key.split("/", 1)
                org, repo, ids = tenants[tenant][0], repos[(tenant, label)], scope_ids[(tenant, label)]
                curve[key] = {}
                for ef in (None, 80, 100, 200):
                    vals10, vals50 = [], []
                    for qkey, qvec in queries.items():
                        params = {"q": vector_literal(qvec), "repo": repo, "model": MODEL, "limit": LIMIT}
                        with require_tenant(conn, org) as cur:
                            cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                            if ef is not None:
                                cur.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")
                            cur.execute(VECTOR_SEARCH_SQL, params)
                            prod = sorted(((row[0], float(row[4])) for row in cur.fetchall()), key=lambda x: x[1])
                        with require_tenant(conn, org) as cur:
                            cur.execute("SET LOCAL enable_indexscan = off")
                            cur.execute(VECTOR_SEARCH_SQL, params)
                            exact = sorted(((row[0], float(row[4])) for row in cur.fetchall()), key=lambda x: x[1])
                        vals10.append(_recall(prod, exact, 10, ids))
                        vals50.append(_recall(prod, exact, 50, ids))
                    curve[key][str(ef or "default")] = {"r10_mean": float(np.mean(vals10)),
                                                        "r10_min": float(np.min(vals10)),
                                                        "r50_mean": float(np.mean(vals50))}
        finally:
            conn.close()
        results["ef_search_exploration"] = curve

    out = os.environ.get(RESULTS_ENV)
    if out:
        pathlib.Path(out).write_text(json.dumps({**results, "per_query_recall_at_10": per_query},
                                                indent=1), encoding="utf-8")
    app.close()
    privileged.close()

    # ---- the rule, in order ------------------------------------------------
    # R2 first: the baseline must be exact before any recall means anything.
    for key, s in scopes.items():
        assert "embedding_idx" not in s["exact_plan"], f"R2: {key}'s baseline used the HNSW index:\n{s['exact_plan']}"
    # R1: the HNSW tenant's largest scope is served by the HNSW index, under
    # default planner settings with iterative scan set as production sets it.
    assert all(settings[n] == "on" for n in ("enable_seqscan", "enable_indexscan", "enable_bitmapscan")), settings
    h_largest = max((k for k in scopes if k.startswith("H/")), key=lambda k: scopes[k]["rows"])
    assert scopes[h_largest]["served_by"] == "hnsw", (
        f"R1: the HNSW tenant's largest scope ({h_largest}, {scopes[h_largest]['rows']} rows) is not "
        f"served by the HNSW index:\n{scopes[h_largest]['production_plan']}"
    )
    # R3: every HNSW-served scope.
    for key, s in scopes.items():
        if s["served_by"] != "hnsw":
            continue
        assert s["recall_at_10"]["mean"] >= R3_MEAN_AT_10, f"R3: {key} mean recall@10 {s['recall_at_10']}"
        assert s["recall_at_10"]["min"] >= R3_EACH_AT_10, f"R3: {key} a query's recall@10 {s['recall_at_10']}"
        assert s["recall_at_50"]["mean"] >= R3_MEAN_AT_50, f"R3: {key} mean recall@50 {s['recall_at_50']}"
    # R4: every exactly-served scope is exact.
    for key, s in scopes.items():
        if s["served_by"] == "exact":
            assert s["recall_at_50"]["min"] >= R4_AT_50, f"R4: {key} recall@50 {s['recall_at_50']}"
    # R5 and R6, everywhere.
    for key, s in scopes.items():
        assert s["short_results"] == 0, f"R5: {key} returned short results for {s['short_results']} queries"
        assert s["foreign_results"] == 0, f"R6: {key} returned {s['foreign_results']} rows from another scope"
