---
phase: 22-repository-clone-ingestion
plan: 03
subsystem: workers, retrieval
tags: [pgvector, retrieval, rls, equivalence-gate, protocol, qdrant-retired, mutation-testing, benchmark]

requires:
  - phase: 22-02
    provides: "chunks.embedding and embedding_model on every row under per-partition RLS; PostgresWriter writing every vector; the Qdrant upsert kept for this gate (P15's transition); the float32 read-back rule; the breadcrumb GIN index"
  - phase: 22-01
    provides: "pgvector on every Postgres; ScratchDatabase; the app role"
  - phase: 17-01
    provides: "the testcontainers harness and the two-tenant fixtures"
provides:
  - "VectorRetriever on pgvector: VECTOR_SEARCH_SQL under require_tenant, the query vector bound, hnsw.iterative_scan = relaxed_order per transaction, re-sorted by exact distance, filtered to EmbeddingGenerator.model"
  - "FTSRetriever filtered by repository (the latest-completed-run filter and _get_latest_run_id are gone), FTS_SEARCH_SQL composed from BREADCRUMB_TSVECTOR"
  - "QueryEngine(postgres_conn, openai_api_key, boost_config=None) with one EmbeddingGenerator, both legs under one tenant scope, a trace hook, and run_id refused"
  - "tests/isolation/conftest.py: app_dsn; the vector-leg isolation, model-refusal, iterative-scan, HNSW-eligibility and breadcrumb-GIN tests"
  - "rag_quality_harness.py: --query-vectors, --record (with exact_paths and every measuring connection in the header), --exact, --allow-compose and the compose guard (which reads libpq's PGPORT/PGSERVICE and refuses service= and multi-host DSNs); scripts/rag_benchmarks/compare_runs.py, the gate's arbiter, and scoring.py, the one scoring rule both use"
  - "22-03-equivalence.md: the rule committed before measurement, the baseline, the verdict; 22-03-records/: every record, gzipped"
  - "Qdrant retired: qdrant_writer.py, qdrant-client, QDRANT_URL, the compose service, the harness's Qdrant paths, the frontend copy, the docs"
affects: [retrieval-quality track (U10, unblocked), 22-05 (IngestionPipeline(postgres_conn, openai_api_key); QueryEngine without qdrant_url), 22.1-02 (per-file currency, ISS-027's remainder), 22.1-05 (the HNSW plan at scale), ISS-021 (the SemanticCache call still broken, argument list changed)]

tech-stack:
  added: []
  removed: [qdrant-client]
  patterns:
    - "A retrieval change is judged by a rule committed before the first measurement, on records both sides can be re-judged from; the arbiter is a script's exit code"
    - "Query vectors for a comparison are embedded once, cached, hashed per record, and the comparison refuses on a hash mismatch"
    - "Two rankings agree up to the order of tied chunks; scores are compared only within a stated tolerance, never for equality"
    - "A measuring connection records its own rolsuper and rolbypassrls; a superuser measurement is refused as evidence"
    - "A plan-shape proof states what it proves (eligibility) and what it does not (the planner's choice at scale)"

key-files:
  created:
    - .planning/phases/22-repository-clone-ingestion/22-03-equivalence.md
    - .planning/phases/22-repository-clone-ingestion/22-03-records/ (baseline-*, pgvector-*, exact-*, qdrant_ids-*, vecs.json.gz, the *.summary.json, compare_runs.txt, the mutation and check logs as .txt, explain_fts_shapes.py)
    - services/workers/scripts/rag_benchmarks/compare_runs.py
    - services/workers/scripts/rag_benchmarks/scoring.py
    - services/workers/tests/test_compare_runs.py
    - services/workers/tests/test_rag_quality_harness.py
  modified:
    - services/workers/workers/retrieval/vector_retriever.py (rewritten)
    - services/workers/workers/retrieval/fts_retriever.py
    - services/workers/workers/retrieval/query_engine.py
    - services/workers/workers/retrieval/test_query_engine.py
    - services/workers/workers/pipeline/ingestion_pipeline.py
    - services/workers/workers/pipeline/test_pipeline.py
    - services/workers/workers/storage/__init__.py
    - services/workers/scripts/rag_quality_harness.py
    - services/workers/scripts/test_answer_generation.py
    - services/workers/tests/isolation/conftest.py
    - services/workers/tests/isolation/test_query_engine_isolation.py (rewritten)
    - services/workers/tests/isolation/test_job_worker_runtime.py
    - services/workers/tests/api/test_routes_retrieval_failure.py
    - services/workers/api/main.py
    - services/workers/api/routes.py
    - services/workers/requirements.txt
    - services/workers/README.md
    - services/backend/.env.example
    - services/frontend/src/pages/LivingDocsPage.tsx
    - docker-compose.yml
    - docs/local-development.md
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md
    - .planning/phases/22-repository-clone-ingestion/22-CONTEXT.md (the tolerance note for the quality track)
    - scripts/dev-tmux.sh
  deleted:
    - services/workers/workers/storage/qdrant_writer.py
    - services/workers/workers/storage/test_qdrant_writer.py
    - services/workers/scripts/test_ingestion.py
    - services/workers/scripts/test_query_engine.py

key-decisions:
  - "The rule's precision (tolerances, 'agree' as identical up to tied chunks, the order the classes are tested in) was written into 22-03-equivalence.md and committed as e87cbf1 before the instrumentation and before any measurement; the classes then went unused because no question differed"
  - "Class (c) is defined as ties broken differently anywhere they can be (at fusion output or within a leg), with the fusion recomputed from each run's own legs as the consistency check; the vector tolerance is 1e-5, the keyword 1e-6 relative, the fused/boosted 1e-9 (below the 2.147e-8 smallest gap between distinct RRF sums)"
  - "relaxed_order with a Python re-sort by the exact distance the SELECT list computes (P5's open choice), not strict_order and not a materialized CTE"
  - "QueryEngine.query refuses run_id (ValueError) rather than half-apply it: before 22-03 only the keyword leg honoured it; nothing in production passes one; currency is per file (ISS-027, 22.1-02)"
  - "The records are committed gzipped (2.9 MB) so the verdict can be re-judged without a container; the Qdrant point set can never be recorded again"
  - "scripts/test_ingestion.py and scripts/test_query_engine.py deleted: the harness's --ingest and --measure cover them and they were never evidence; scripts/test_answer_generation.py kept (nothing else exercises the answer generator with a cache) and updated"

issues-closed: []
issues-updated: [ISS-021, ISS-027]
issues-filed: []
review: "PR #53 — reviewer B (Qdrant retirement, harness, tests, docs): approve with changes, nothing critical or important, A7 holding clause by clause; reviewer A (protocol integrity and retrieval SQL): nothing blocking, the order of operations confirmed from git, the RLS SQL and A5 holding. Three mediums, four lows and six nits from B and two mediums and two lows from A, all applied (the last section of this summary); A's L4 (A6's wording in 22-ACCEPTANCE.md) applied by the coordinator."
completed: 2026-09-29
---

# Phase 22 Plan 03: both retrieval legs on pgvector, proven to change no ranking, and Qdrant retired

**The vector leg reads `chunks.embedding` in Postgres, under the same
row-level security as the text, as `rag_doc_app`, with `hnsw.iterative_scan =
relaxed_order` set per transaction and the rows filtered to the generator's
model (P4, P5, P6).** The keyword leg searches the repository instead of its
latest completed run (ISS-027). Fusion and boosts are untouched. **The
storage move was judged under a rule committed before the first
measurement** (`22-03-equivalence.md`, `e87cbf1`), on one scratch database
ingested once and one set of query vectors embedded once: `compare_runs.py`
found **no question of 130 differing** in file rank or symbol rank, 0
UNEXPLAINED, exit 0, every aggregate identical. In the terms of what was
measured: **the storage move, judged by exact search on both sides, changed
no ranking on 130 vector-leg questions** (the keyword leg was empty for 120
of them; HNSW-served rankings are 22.1-05's). **Only then was Qdrant retired
everywhere.** The retrieval-quality track (U10) is unblocked.

**The order of record** (one branch, one PR):

| commit | what |
|---|---|
| `e87cbf1` | the rule, verbatim from the plan, with its precision |
| `9bd404b` | the instrumentation: trace hook, `--query-vectors`, `--record`, `--exact`, the compose guard, `compare_runs.py` and its tests |
| `9083479` | the Qdrant-era baseline, recorded completely |
| `f946240` | both legs in Postgres, the tests, both plan-shape proofs |
| `4fd8f80` | the harness building `QueryEngine` without `qdrant_url` |
| `8c1dd36` | the verdict, the pgvector-side records, the mutation log |
| `ee22f44` | Qdrant retired everywhere |
| `c5ce8e8`, `78e8271`, `6bc6012` | ASCII comments; the summary, the ROADMAP tick, the STATE line; the PR number |
| `296233a`, `e0c9fe2`, `bfe407b`, and the closing docs commit | PR #53's review applied (the last section) |

## The instrumentation (Task 1)

**`QueryEngine.query(…, trace=None)`.** When a dict is given, the pipeline
writes into it, in order and from inside the real code path: `fts` and
`vector` (chunk ids with scores as the retrievers returned them), `fused`
(with `rrf_score` and `sources`), `boosted` (in the final sorted order, with
`rrf_score`, `boost_multiplier`, `boosted_score`) and `top` (the enriched
results). With `None`, the default, nothing is recorded and nothing else
changes; `TestTrace` in `test_query_engine.py` asserts the response with and
without the argument is identical, the five keys in pipeline order, the
scores being the pipeline's own, and that the trace holds copies.

**The harness** (`rag_quality_harness.py`):
- `--query-vectors FILE`: question id → vector. A missing question is
  embedded once through the engine's own client, the file is written and
  read back, and the run uses what is on disk. A cached entry for a
  different question text or model is refused (P4).
- The pin: before each question, the harness replaces
  `engine.vector_retriever.embedding_generator.client.generate_embeddings_batch`
  on the instance with a function returning the cached vector for that text
  and **raising on any other text**. That is the exact call path the
  retriever uses, before and after the move, which is why one pin worked on
  both sides.
- `--record FILE.jsonl`: a header (corpus, set, top_k, boost config,
  `exact_paths` — how a result's path is matched to a question's, since the
  review — the harness commit, the vector backend, the model, the database's
  host, port, dbname and options — never the password — its server version
  and pgvector settings, the identity of every measuring connection, the
  visible chunk count, and from Task 2 the `EXPLAIN` of both production
  statements), then per question the ranks, the SHA-256 of the query vector
  actually used (over its JSON float list) and the trace. **It refuses to
  record on a connection whose `rolsuper` or `rolbypassrls` is true.** The
  ranks come from `scripts/rag_benchmarks/scoring.py`, the one module the
  judge recomputes them with.
- `--exact FILE`: the exact-search reference per question, as the DSN's role
  under the harness tenant, with `enable_indexscan` and `enable_bitmapscan`
  off, filtered by repository and model, from the cached vector: the top 50
  plus every chunk tied with the 50th, with the recorded plan.
- `--qdrant-ids FILE` (Task 1 only, removed in Task 3): every point Qdrant
  held for the corpus, and the Postgres chunk count beside it.
- **The compose guard:** `--ingest` and `--clear` refuse port 5434 without
  `--allow-compose`, before touching anything. Shown from the real CLI before
  the first ingest: `--ingest refused: Postgres on port 5434 …` exit 1, and
  the same for `--clear` with the harness's defaults. `TestComposeGuard`
  pins it, including that the message never echoes the DSN. Since the
  review the guard reads libpq's environment the way libpq does: a port-less
  DSN takes its port from `PGPORT` (so `PGPORT=5434` is compose), a
  `service=` DSN or a `PGSERVICE` filling in the port is refused because the
  port cannot be known, and a multi-host DSN is refused rather than crashed;
  none of it with `--allow-compose` as an escape, which means "compose,
  deliberately", not "somewhere, deliberately".

**`compare_runs.py`** reads the four files per corpus, **refuses before
comparing anything** if any question's hash differs, the question sets
differ, `top_k`, the boost config, the repository or the model differ, a
query failed, a question has no exact list, a measuring connection was a
superuser or is not recorded, a question is recorded twice, a record lacks
what the comparison reads, **a record's ranks are not what its own final
list gives under `scoring.py`'s rule**, the Qdrant point set is empty, or
the exact list was produced by a superuser, for another model or another
repository; classifies every differing question as (a), (b), (c) or
UNEXPLAINED in the rule's order, (c) only when the final lists themselves
differ and only by tied chunks; prints the table, the counts, the aggregates
under both runs and the information lines; exits 1 on any UNEXPLAINED, 2 on
a refusal, 0 otherwise. Its tests (`test_compare_runs.py`, 38) build
hand-made records with one difference of each class plus one UNEXPLAINED
and assert each is classified as such and the exit is 1; every refusal
above has a test that shows exit 2 and no table; a rank edited by hand with
the trace untouched (reviewer A's reproduction, which used to come out (c)
and PASS) is refused; a real tied swap in the final list is (c) and an
untied one UNEXPLAINED; the fusion is recomputed with an independent RRF in
the fixtures, and the judge's `ranks` is asserted to be the harness's own
object.

## The baseline, then the candidate, on one database

`22-03-equivalence.md` carries the detail. In short: a fresh
`pgvector/pgvector:pg16` (pinned digest) migrated to 17 with `rag_doc_app`
created as the Python harness creates it, and a scratch `qdrant/qdrant`,
both on Docker-assigned free ports; `self` (74 files, 534 chunks), miniflux
(335, 2,134 chunks / 2,122 embeddings, **12** duplicates) and mealie (390,
2,816 / 2,736, **80** duplicates) ingested once as the superuser, each vector
written to both stores from one embedding call; the point sets scrolled; 130
questions embedded once (three batch calls); the exact lists recorded (plan:
`Seq Scan on chunks_p0`, `Subplans Removed: 63`); the baseline measured on
the Qdrant read path as `rag_doc_app`. Then the code of `f946240`, the same
database, the same `vecs.json` (`0 embedded now` on every run), as
`rag_doc_app`. The OpenAI spend was the ingest of ~5,500 chunks plus 130
question embeddings, once.

## The vector leg (Task 2)

```sql
SELECT id::text AS chunk_id, file_path, breadcrumb, chunk_type,
       embedding <=> %(q)s::vector AS distance
FROM chunks
WHERE repository_id = %(repo)s AND embedding_model = %(model)s
ORDER BY embedding <=> %(q)s::vector LIMIT %(limit)s
```

run inside `require_tenant` after `SET LOCAL hnsw.iterative_scan =
relaxed_order`; the rows re-sorted in Python by the exact `distance` the
SELECT list computes (P5's choice: relaxed order with a re-sort);
`vector_score = 1 - distance`; `%(model)s` is `EmbeddingGenerator.model`,
read at query time and never copied. No `organization_id` in the statement:
the partition is pruned from the policy alone. The query is embedded through
`self.embedding_generator.client.generate_embeddings_batch([query])[0]`, the
same path as before, which the harness's pin depends on.

**`FTSRetriever`:** `FTS_SEARCH_SQL` is composed from `BREADCRUMB_TSVECTOR =
"to_tsvector('english', COALESCE(breadcrumb, ''))"` and `CONTENT_TSVECTOR`,
filters `repository_id = %(repo)s` where it used to filter to the
repository's **latest completed run**, and `_get_latest_run_id` is gone.
Measured before anything changed: the two predicate shapes plan identically
on the scratch data (`Limit → Sort → Append, Subplans Removed: 63 → Bitmap
Heap Scan on chunks_p0 → Bitmap Index Scan` on the run or repository btree),
so the sort saw the same rows in the same order
(`22-03-records/explain_fts_shapes.py` and its output
`explain_fts_shapes-baseline.txt`).

**`QueryEngine(postgres_conn, openai_api_key, boost_config=None)`:** one
`EmbeddingGenerator`, shared with the vector leg; the vector leg receives
`organization_id`; fusion, boosts, enrichment and ISS-030's behaviour are
unchanged (`test_both_legs_receive_the_tenant`, the ISS-030 suite); the trace
hook stays. `run_id` is refused with `ValueError` (see key decisions).

## The tests, and their app-role premise

`app_dsn` is lifted into `tests/isolation/conftest.py` from
`test_job_worker_runtime.py`'s `dsn` (which now uses it): the container DSN
with `options=-c role=rag_doc_app`, for code that opens its own connections.
Every test in `test_query_engine_isolation.py` that builds a retriever, a
writer or a `QueryEngine` from a DSN uses it, and asserts on its own
connection `current_user = rag_doc_app`, `rolsuper = false`, `rolbypassrls =
false`. No OpenAI call: a `QueryEngine` is built with a throwaway key and its
client's `generate_embeddings_batch` replaced to return a fixed vector, the
retriever's own path.

| Test | What it pins | Premise it asserts |
|---|---|---|
| `test_vector_leg_returns_only_the_tenants_chunks_even_when_the_other_tenants_is_nearer` | as A in A's repository the vector leg returns A's chunk and never B's; as A with B's repository id both legs return nothing and `QueryEngine.query` returns `[]`; the whole pipeline as A returns A's chunk from `sources == ["vector"]` | **B's chunk carries the query vector itself** (distance < 1e-6), checked as the superuser with RLS bypassed: a leak would show as B ranking first, not as silence |
| `test_a_chunk_embedded_with_another_model_is_never_returned` | a chunk with `embedding_model = 'other-model'` is never returned; the same-model chunk is | the other model's chunk is the nearest row, checked with RLS bypassed |
| `test_iterative_scan_is_set_inside_the_retrievers_own_transaction` | `current_setting('hnsw.iterative_scan') = 'relaxed_order'`, read by wrapping the `require_tenant` the retriever module imported, after its statement and before its commit | a fresh transaction on the same connection sees the default (`off`), so the SET LOCAL is what was read |
| `test_the_hnsw_index_can_serve_the_vector_leg` | below | — |
| `test_the_breadcrumb_gin_index_serves_the_keyword_legs_expression` | below | — |
| `test_fts_searches_the_repository_not_the_latest_run` | two completed runs, one chunk each: both found (before 22-03 only the second would have been) | — |
| the five keyword-leg tests from before | unchanged assertions, now on `app_dsn` with the app-role identity asserted | — |

Unit level, no database: `TestTrace` (three tests; the first now also shows
a recorded trace changes nothing), `test_both_legs_receive_the_tenant`,
`test_a_run_id_is_refused_rather_than_half_applied`, the 22 harness tests
(14 before Task 3 removed the Qdrant-port guard test; the review round added
the libpq-environment ones) and the 38 `compare_runs.py` tests.

## The two plan-shape proofs, with what they do and do not prove

**HNSW eligibility** (`test_the_hnsw_index_can_serve_the_vector_leg`), as
the app role under the tenant, with `enable_seqscan`, `enable_bitmapscan`
**and `enable_sort`** off, `EXPLAIN (COSTS OFF)` of `VECTOR_SEARCH_SQL` with
the vector bound as a parameter: `Index Scan using chunks_pNN_embedding_idx
on chunks_pNN`, `Order By: (embedding <=> …)`, `Subplans Removed: 63`.
**What it proves:** the index can serve this exact statement: the operator
class matches, the vector is a constant rather than a subquery, the scan is
on the one partition the policy pruned to. **What it does not prove:** that
the planner chooses it at production sizes. It did not here: with default
settings the same statement planned as a Bitmap Heap Scan through the
repository btree (`self`, miniflux) or a Seq Scan (mealie), in both the test
and the gate's recorded plans. The plan at scale is 22.1-05's recall test.
The same test asserts A5 on both production statements with default
settings: `Subplans Removed: 63` and exactly one `chunks_p` scan each, and
that neither statement mentions `organization_id`.

**The breadcrumb GIN index**
(`test_the_breadcrumb_gin_index_serves_the_keyword_legs_expression`), owned
here. As the superuser (no policy predicate offers a btree alternative), on
one partition (`chunks_p0`), with only the breadcrumb predicate,
`enable_seqscan` and `enable_indexscan` off: the expression
`BREADCRUMB_TSVECTOR` gives `Bitmap Index Scan on chunks_p0_to_tsvector_idx1`,
whose `pg_indexes.indexdef` contains `USING gin` and `COALESCE(breadcrumb`
(the index is identified by its definition, never its generated name); the
bare `to_tsvector('english', breadcrumb)` gives `Seq Scan` and no bitmap
scan. The test also asserts `FTS_SEARCH_SQL` is composed from the constant,
so the constant it plans is the one the query uses. **What it proves:** the
index matches the query's expression and the planner can use it for that
predicate. **What it does not prove:** that the planner picks it in the full
keyword statement, where on this data the repository btree wins (recorded
above).

## The equivalence gate

`compare_runs.py --records 22-03-records`, exit code **0**, verbatim:

```
corpus   id         set      file         symbol       class       note
---------------------------------------------------------------------------------------------------------------
(no question differs in file rank or symbol rank)
---------------------------------------------------------------------------------------------------------------
differing questions: 0   (a)=0   (b)=0   (c)=0   UNEXPLAINED=0

Aggregates, reported and not judged (recall@k = found/questions, rank-1, MRR):
corpus    set      side      file recall  file #1  file MRR  sym recall  sym #1  sym MRR 
self      holdout  qdrant    12/15        7        0.633     0/0        0       0.000   
self      tuning   qdrant    20/25        14       0.649     0/0        0       0.000   
self      holdout  pgvector  12/15        7        0.633     0/0        0       0.000   
self      tuning   pgvector  20/25        14       0.649     0/0        0       0.000   
miniflux  confirm  qdrant    10/15        6        0.494     8/15       3       0.319   
miniflux  holdout  qdrant    11/15        5        0.489     11/15       5       0.478   
miniflux  tuning   qdrant    9/15        6        0.483     6/15       3       0.263   
miniflux  confirm  pgvector  10/15        6        0.494     8/15       3       0.319   
miniflux  holdout  pgvector  11/15        5        0.489     11/15       5       0.478   
miniflux  tuning   pgvector  9/15        6        0.483     6/15       3       0.263   
mealie    confirm  qdrant    14/15        7        0.629     10/15       4       0.376   
mealie    holdout  qdrant    11/15        5        0.489     6/15       2       0.239   
mealie    tuning   qdrant    8/15        3        0.307     8/15       3       0.290   
mealie    confirm  pgvector  14/15        7        0.629     10/15       4       0.376   
mealie    holdout  pgvector  11/15        5        0.489     6/15       2       0.239   
mealie    tuning   pgvector  8/15        3        0.307     8/15       3       0.290   

For information only:
  self: 40/40 questions with fully agreeing boosted rankings; max |delta similarity| over 2000 chunk scores both legs returned = 4.66e-07; chunks without a Qdrant point: 0 (534 in Postgres, 534 points)
  miniflux: 39/45 questions with fully agreeing boosted rankings; max |delta similarity| over 2243 chunk scores both legs returned = 4.85e-07; chunks without a Qdrant point: 12 (2134 in Postgres, 2122 points)
  mealie: 25/45 questions with fully agreeing boosted rankings; max |delta similarity| over 2152 chunk scores both legs returned = 6.03e-07; chunks without a Qdrant point: 80 (2816 in Postgres, 2736 points)

VERDICT: PASS (0 UNEXPLAINED)
```

**Read, not judged:** the Qdrant and pgvector scores of the same chunk for
the same query differ by at most 6.03e-07 across 6,395 pairs, none above
1e-6, three orders inside the pre-committed 1e-5 (about 16× generous, and
harmless here: no adjacent pair within it changed a rank); the 6 miniflux
and 20 mealie questions whose *full* boosted rankings differ are the ones
where duplicate-content chunks entered the vector top 50 (class (a)'s
mechanism) below the top-5 cut — the one difference P6 said to expect, seen
where it was expected; the keyword leg is empty for 30 of 40 `self`
questions and all 90 benchmark questions (ISS-029), so the gate was almost
entirely a test of the vector leg, which is its subject.

**Re-judged after the review** with the hardened `compare_runs.py`
(`22-03-records/compare_runs-rejudged-after-review.txt`): the same 260
records, no record refused (every recorded rank is what its own final list
gives), the same line — `differing questions: 0   (a)=0   (b)=0   (c)=0
UNEXPLAINED=0`, `VERDICT: PASS (0 UNEXPLAINED)`, exit 0.

## Qdrant retired (Task 3)

Removed: `qdrant_writer.py`, `test_qdrant_writer.py`, `qdrant-client`, the
pipeline's Qdrant step with its `[DEBUG]` prints and `qdrant_url`
(`IngestionPipeline(postgres_conn, openai_api_key)`), `storage/__init__`'s
export, the harness's Qdrant imports, `QDRANT_URL`, `--qdrant-ids`, the
Qdrant halves of `indexed_state`, `do_clear` and the guard, `api/main.py`'s
`QDRANT_URL` and its guard, `api/routes.py`'s comment, the backend
`.env.example` block, the frontend copy (now "Postgres, with pgvector" and
ada-002), compose's `qdrant` service, its volume declaration and `rag-api`'s
`QDRANT_URL` and `depends_on`, `docs/local-development.md`'s mention (with a
new "Qdrant is gone" section saying the user may remove
`testtgsd_qdrant_data`), and the workers README. `scripts/test_ingestion.py`
and `scripts/test_query_engine.py` were deleted (the harness covers them);
`scripts/test_answer_generation.py` lost `qdrant_url=`. The `self` tuning
question "how are vectors stored for similarity search" now expects
`postgres_writer` (it was measured with `qdrant_writer` on both sides of the
gate; it is a tuning question, so no decision rests on it).

**`api/main.py`'s `SemanticCache(...)` call lost only `qdrant_url=`** and is
still broken, deliberately, with a comment saying so; ISS-021 records that
the argument list changed and the bug did not.

**The compose volume `testtgsd_qdrant_data` was not touched.** `docker
compose config` validates: services `workers, postgres, backend, frontend,
redis, rag-api`, volume `postgres_data`; no compose service was started.
(Compose warns that the `version` attribute is obsolete; pre-existing, not
changed here.)

**The grep gate, over the whole repository** (`git grep -n -i qdrant`, every
tracked file; the plan's `services/ docker-compose.yml docs/` scope let
`scripts/dev-tmux.sh` hide, which reviewer B found and this round fixed)
finds only these survivors, each with its reason:

| Where | Why it stays |
|---|---|
| `.planning/` (phases 03, 05 and 07's plans and summaries; `v2-substrate/`'s DECISIONS, DESIGN, RESEARCH and REWORK; ISSUES, ROADMAP, STATE; this phase's files) | the record of choosing Qdrant, of deciding to leave it (D2, K1), and of leaving it; history by design |
| `.github/workflows/backend-ci.yml:91` | a comment recounting how a `go.sum` gap first surfaced, on the qdrant bump; history |
| `scripts/dev-tmux.sh` | **fixed in this round**: it asked compose for `postgres qdrant redis backend rag-api`, which failed with "no such service"; it asks for `postgres redis backend rag-api` now |
| `compare_runs.py`, `test_compare_runs.py`, `rag_quality_harness.py:121` | the gate's arbiter reads the recorded Qdrant point set, `qdrant_ids-<c>.json`, which defines class (a) and can never be recorded again |
| `rag_quality_harness.py:229, :549, :684` | comments saying what changed and why the pin worked on both sides |
| `migrations/000017_partitioned_chunks.up.sql:15, :115, :234` | the migration's history: why duplicates had no vector and P15's transition |
| `postgres_writer.py:154`, `test_postgres_writer_isolation.py:390` | the same history at the writer |
| `test_job_transitions.py:1362` | a 21-05 docstring narrating ISS-027's failure mode as it was analysed |
| `test_query_engine_isolation.py:4-5`, `vector_retriever.py:6`, `query_engine.py:45`, `ingestion_pipeline.py:29`, `test_pipeline.py:28` | say that it is gone (the pipeline test asserts `not hasattr(pipeline, "qdrant")`) |
| `api/main.py:67`, `README.md:49`, `docker-compose.yml:78-80`, `docs/local-development.md:160-174` | the retirement notes and ISS-021's |

No import of `qdrant_client`, no `QDRANT_URL`, no service, no package: a
clean venv from `requirements.txt` installs, `pip show qdrant-client` reports
not found, and `workers.retrieval`, `workers.pipeline`, `workers.storage` and
`api.main` import.

## Mutations

Committed code, one mutation at a time in the working tree, each proven to
have landed by a tool printing the original text's count (1→0) and the
mutated text's (0→1), the target test run, the file restored with `git
checkout --`, the tree shown clean after each (`22-03-records/mutations-*.txt`;
`.txt` because `.gitignore` ignores `*.log`, which is how the first commit
titled as adding them added nothing — reviewer B's finding).

| # | Mutation | Expected | Result |
|---|---|---|---|
| M1 | the model predicate neutered to `AND %(model)s IS NOT NULL` | the refusal test fails | **killed**: the other model's chunk ranked first |
| M2 | `cur.execute(ITERATIVE_SCAN_SQL)` removed | the setting test fails | **killed**: `seen == ['off']`, read inside the transaction |
| M3 | `BREADCRUMB_TSVECTOR` bare | the GIN test sees `Seq Scan` | **killed**: `Seq Scan on chunks_p0 / Filter: (to_tsvector('english'::regconfig, breadcrumb) @@ …)` (the first run of M3 was killed at the test's own premise line; the premise check was moved after the plan assertions and M3 re-run, `mutations-task2-m3-rerun.log`) |
| M4 | `require_tenant` bypassed in the vector leg (`with self.conn.cursor(...)`) | the isolation test fails; record empty or `22P02` | **killed, as empty** (`[] == [a_id]`): a fresh connection with no tenant ever set reads `current_setting(..., true)` as NULL, so the policy is false for every row; `22P02` is the committed-`SET LOCAL` shape (ISS-013), which this path never produces |
| G1 | the compose guard's `if targets and not allow_compose` neutered | the refusal tests fail | **killed**: three `DID NOT RAISE SystemExit` |
| C1 | `compare_runs.py` step 2 forced (`if True:`) | the each-class test fails | **killed**: (a) and (b) come out UNEXPLAINED |
| C2 | the hash refusal neutered | the refusal test fails | **killed**: the script compared and printed its table instead of refusing |
| T1 | the trace's `boosted` recorded from the unsorted fused list | the stage-order test fails | **killed**: `TypeError` on the descending-order check (no boosted scores) |
| T2 | the trace recorded when `trace is None` | the `trace=None` test fails | **killed**: `TypeError: 'NoneType' object does not support item assignment` in every trace-less call |
| M0 | committed code | pass | 11/11 isolation tests; 38/38 unit tests; tree clean |

**The review round's mutations** (`22-03-records/mutations-review-round.txt`),
same discipline, on `e0c9fe2`:

| # | Mutation | Expected | Result |
|---|---|---|---|
| R1 | the duplicate-id refusal neutered | the duplicated-record test fails | **killed**: the script compared and the (a) row vanished, exactly reviewer B's reproduction |
| R2 | the rank recomputation neutered | the rank-tamper tests fail | **killed**: the tampered `q-same` came out `#1->#2 UNEXPLAINED "the final lists are identical, yet the ranks differ"` — so even without step 0, reviewer A's reproduction no longer yields (c); with step 0 it is refused |
| R3 | the missing-connections refusal neutered | the role-unknown tests fail | **killed** (as a crash, `TypeError` on the `None` header, rather than as a refusal message: the guard was the only thing standing between a missing header and the code that reads it) |
| R4 | the empty-point-set refusal neutered | the point-set tests fail | **killed** (as a crash, `TypeError` on the missing `ids`; same shape as R3) |
| R5 | the exact-list checks neutered | the untrusted-exact-list tests fail | **killed**: a superuser exact list compared and printed its table |
| R6 | `PGPORT` ignored | the libpq-environment tests fail | **killed**: `5432 == 5434` on both DSN forms and `DID NOT RAISE SystemExit` |
| R7 | the class-(c) final-list tie check forced true | **expected to survive** | **survived, as predicted**: step 0 already requires `top` to be the boosted list cut at `top_k`, the boosted list descending and consistent, and the ranks to be what `top` gives, which implies the final-list check; it stays as defence in depth, as reviewer A asked, and this row is the honest record that no test can reach it |
| M0 | committed code | pass | 72/72 across the three unit files; tree clean |

## Verification

| Check | Result |
|---|---|
| `pytest tests/test_compare_runs.py tests/test_rag_quality_harness.py workers/retrieval -q` (Task 1) | 66 passed |
| `pytest tests/isolation/test_query_engine_isolation.py -q` (Task 2, testcontainers) | 11 passed |
| `pytest tests/ workers/ -q` as CI runs it (`REDIS_URL` on a scratch Redis db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no reachable `.env`, fresh venv from `requirements.txt`, Python 3.13.7), after Task 2 | **330 passed** (main: 292) |
| the same after Task 3 (Qdrant gone) | **322 passed** (the 8 deleted Qdrant-writer tests and the Qdrant-port guard test) |
| the same after PR #53's review round | **356 passed** (the 25 new `compare_runs.py` tests and the 9 new harness tests) |
| `compare_runs.py` on the real records | exit 0, 0 UNEXPLAINED, 0 differing |
| the guard from the real CLI on port 5434 and on the defaults | refused, exit 1, before any connection |
| `--ingest` on an indexed corpus; `--clear self` on the scratch data as the superuser | refused (`runs=1, chunks=2134`); `runs=0 chunks=0`, the other corpora's 4,950 chunks untouched (`harness-clear-check.txt`) |
| `compare_runs.py` re-run on the committed records with the hardened script (review round) | exit 0, 0 differing, 0 refused; verdict unchanged |
| a clean venv from `requirements.txt` | installs; `qdrant-client` not found; the packages import |
| `docker compose config` | valid; no service started; the Qdrant volume untouched |
| the grep gate | survivors listed above, each with a reason |
| `scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD` | PASS (re-run after the review round: PASS) |
| the branch's added lines scanned for cp1252-undecodable bytes and curly quotes | 0 and 0 (2,850 lines before the review round; 3,954 after) |
| Go | untouched; no Go change was needed (nothing in Go referenced Qdrant after 22-01) |
| port 5434, compose's Postgres, the compose volumes | never touched; every run used `rag2203-pg` (127.0.0.1:61797), `rag2203-qdrant` (61799) and `rag2203-redis`, all removed |

## Deviations from the plan

1. **The rule's precision is written into the doc** (tolerances; "agree" as
   identical up to tied chunks; (c) covering ties broken differently within
   a leg; the exact list extended by its tie tail; the classification
   order). Definition before measurement, as the plan and PR #49's ruling
   allow; recorded here so it is not mistaken for tuning. The classes went
   unused.
2. **`run_id` is refused, not honoured or silently ignored.** The plan says
   only to drop the latest-run filter. Keeping an explicit run filter on one
   leg would recreate ISS-027's disagreement; ignoring it would be a silent
   filter loss. Nothing in production passes one.
3. **The records are committed** (gzipped, 2.9 MB; the six
   `baseline-<c>.summary.json` / `pgvector-<c>.summary.json` files, which are
   the harness's `--json-out` per-question ranks and summary for each run,
   readable without gunzip; the mutation and check logs as `.txt`; the
   EXPLAIN script), which the plan did not ask for; without them the verdict
   could not be re-judged, and reviewer B re-judged it byte-identically from
   them.
4. **The FTS predicate-shape EXPLAIN** was measured before the baseline as a
   check that the keyword leg's tie order could not change with the filter;
   not in the plan, recorded in the equivalence doc.
5. **The scratch Postgres outlived the verdict by a few minutes** so the
   rewritten `--clear` and `--ingest` refusal could be checked on real data
   (the plan tears both containers down once the verdict is recorded; Qdrant's
   was removed at that point).
6. **The harness records the connection identity of both legs and both
   production plans in the candidate header** (the baseline's header could
   record only the keyword leg's connection; the Qdrant leg had none), and
   `hnsw.*` settings read as unset in the baseline headers because the
   pgvector library had not loaded on that fresh session — fixed for the
   candidate, explained in the doc.
7. **STATE and ROADMAP edits are minimal** (22-03's own entry and the one
   Last-activity line); the phase table row and the stale "22-02 in review"
   text elsewhere were left for the merge of the parallel plans.

## What the next plans inherit

- **22-05:** `IngestionPipeline(postgres_conn, openai_api_key)` and
  `QueryEngine(postgres_conn, openai_api_key)`; `write_results` decides what
  happens to a replaced chunk's retrievals (ISS-027's note).
- **22.1-02:** per-file currency on re-index; until then `--ingest` refuses
  an indexed corpus without `--clear`.
- **22.1-05:** the HNSW plan at scale; here the planner chose exact scans and
  eligibility is the test's proof. **The gate judged exact search on both
  sides, on repositories of at most 2,816 chunks**; HNSW-served rankings have
  no equivalence evidence from 22-03.
- **The retrieval-quality track (U10):** unblocked. Its runs use
  `--query-vectors` and `--record`, and `compare_runs.py` can judge any two
  recorded runs on the same database; its protocol commits its rule first,
  as this one did. **Two things it inherits from the measurement, also
  written into `22-CONTEXT.md`'s track section:**
  - **The gate's reach was the vector leg.** The keyword leg was empty for
    120 of the 130 questions (all 90 benchmark questions and 30 of 40
    `self`, ISS-029), so a keyword-leg change — ISS-029's own fix first of
    all — has no equivalence evidence from 22-03 and needs the protocol's
    fresh questions.
  - **The tolerance.** 1e-5 on vector similarity was committed before
    measuring; the measured cross-store difference was at most 6.03e-07 over
    6,395 pairs, none above 1e-6, so 1e-5 was about 16× generous (harmless
    here). Reviewer A's recommendation, adopted as the hand-off: **fix the
    track's tolerance at about 2e-6 from this measurement, dated, before its
    next rule is written.**
- **ISS-021:** the `SemanticCache` call still passes the wrong arguments;
  when repaired, its key must carry the model (P4).

## PR #53's review, applied 2026-09-29

Two reviewers. **Reviewer B** (Qdrant retirement, harness, tests, docs):
approve with changes, nothing critical or important; A7 holds clause by
clause; the 322 count and the byte-identical re-judge from the committed
records confirmed. **Reviewer A** (protocol integrity and retrieval SQL):
nothing blocking; the order of operations confirmed from git (the rule file's
diffs across history fill only the "baseline" and "candidate and verdict"
sections, never the rule); the precision definitions did no work in the
verdict; both legs inside `require_tenant` with no `organization_id` in
either statement; M4's "empty, not `22P02`" confirmed as ISS-013's
fresh-connection shape; A5 holds. Everything below was applied on the branch,
in `296233a` (B-M1, B-M2), `e0c9fe2` (B-M3, B-L1, B-L2, A-M2, A-L3, the
nits), `bfe407b` (B-L3, B-L4, A-L4, the tolerance note) and the closing docs
commit; A-L4's qualification of A6 in `22-ACCEPTANCE.md` by the coordinator.

**Reviewer B.**
- **M1, `scripts/dev-tmux.sh`** still asked compose for `qdrant` (the one
  live reference left, outside the plan's grep scope): fixed, and the grep
  gate widened to every tracked file (above).
- **M2, the evidence was not in the PR:** `.gitignore` ignores `*.log`, so
  the mutation and clear-check logs never landed and `explain_fts_shapes.py`
  was untracked. The logs are committed as `.txt` (`.gitignore` untouched)
  and the script beside its output.
- **M3, `load_run` overwrote a duplicated question id** (last wins; a second
  `q-a` agreeing with the candidate made the (a) row vanish, PASS): it raises
  `ValueError`, reported as REFUSED — `test_a_duplicated_question_record_is_refused`.
- **L1, refusals that did not fire:** a header with no `connections` (or
  `{}`) skipped the superuser refusal; an empty or missing `qdrant_ids.ids`
  made (b) trivially available. Both refuse now, and a pgvector run must
  record both legs' connections —
  `test_a_run_whose_measuring_role_is_unknown_is_refused` (four shapes),
  `test_a_pgvector_run_must_record_the_vector_legs_connection`,
  `test_an_empty_or_inconsistent_qdrant_point_set_is_refused` (three shapes).
- **L2, the guard could be bypassed through libpq's environment:** a
  port-less DSN with `PGPORT=5434`, a `service=` DSN, and a multi-host DSN
  that crashed. `dsn_port` reads `PGPORT` (and reports `PGHOST`) when the DSN
  omits the port, refuses `service=` and a port-filling `PGSERVICE`, and
  refuses multi-host DSNs explicitly; nothing echoes the DSN —
  `test_a_port_less_dsn_takes_its_port_from_pgport`,
  `test_a_host_less_dsn_reports_pghost_for_context`,
  `test_a_service_dsn_is_refused_because_its_port_cannot_be_known`,
  `test_pgservice_with_a_port_less_dsn_is_refused_but_an_explicit_port_wins`,
  `test_a_multi_host_dsn_is_refused_not_crashed`,
  `test_a_dsn_that_does_not_parse_is_refused`. Every guard test passes an
  explicit environment, so the developer's own `PGPORT` cannot decide them.
- **L3, `STATE.md`:** 22-02's block is restored under "Previously", as
  22-01's is.
- **L4:** the ISS-029 caveat is in the hand-off above, and "latest run" reads
  "latest **completed** run" here and in ISS-027.
- **Nits:** the harness test count corrected; the harness USAGE shows a
  scratch `DATABASE_URL` first; `vector_literal` is one function (the
  harness imports the retriever's, asserted by identity); a record missing
  `trace` or a rank is REFUSED, not a `KeyError`
  (`test_an_incomplete_record_is_refused_rather_than_crashing`);
  `test_trace_none_changes_nothing` also compares against a recorded trace;
  the six `*.summary.json` files are listed in deviation 3.
- **Rulings recorded, no action:** deleting the eight Qdrant-writer tests,
  the 6333 guard twin and the two smoke scripts loses nothing (reviewer B
  checked each); committing the 2.9 MB gzipped records was the right call,
  and flake8, mypy and the isolation scanner do not see them.

**Reviewer A.**
- **M1:** the same evidence finding as B-M2.
- **M2, `compare_runs.py` could be made to lie about a fabricated
  difference:** step 0 never recomputed `file_rank`/`symbol_rank` from
  `trace.top`, and step 2 returned (c) whenever the legs and the fused and
  boosted lists agreed, without checking that the final lists differed or
  that the crossing chunks were tied; a candidate `file_rank` changed from 1
  to 2 with the trace untouched came out "(c) … PASS", and applied to all 130
  non-differing questions, (c) was the default output. No effect on this
  verdict (the reviewer recomputed all 260 records' ranks: 0 inconsistent),
  but the quality track will judge real differences with the tool. Now: the
  harness records `exact_paths` in the header and scores with
  `scoring.py`; `refusals()` recomputes every record's ranks from its own
  `trace.top` with the same module and **refuses on mismatch**; step 2
  requires the final lists to differ, and only by tied chunks, before (c).
  Tests: `test_a_rank_edited_without_its_final_list_is_refused_not_classified`
  (file rank and symbol rank), `test_a_real_tied_swap_in_the_final_list_is_class_c`,
  `test_an_untied_swap_in_the_final_list_is_unexplained`,
  `test_a_missing_exact_paths_is_assumed_by_corpus_and_said_so`,
  `test_the_judge_scores_with_the_harnesss_own_rule`. The committed records
  re-judged: unchanged (above).
- **L3, the exact list was not checked:** `exact-<c>.json`'s connection
  identity, model and repository are now held to a run's standard —
  `test_an_exact_list_that_cannot_be_trusted_is_refused` (five shapes),
  `test_a_question_with_no_exact_list_is_refused`.
- **L4, A6's wording:** the verdict is stated in the terms measured, here
  and in `22-03-equivalence.md`: the storage move, judged by exact search on
  both sides, changed no ranking on 130 vector-leg questions.
- **The tolerance for the track:** written into the hand-off above,
  `22-03-equivalence.md` and `22-CONTEXT.md`'s track section.
