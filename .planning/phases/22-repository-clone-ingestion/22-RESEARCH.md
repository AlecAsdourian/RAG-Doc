# Phase 22: Repository Clone → Ingestion Orchestration — Research

**Researched:** 2026-09-17, on `docs/22-research` from `RAG-Doc/main` at `11c3a56`
**Domain:** the phase that writes the first real indexed rows — pgvector and
partitioned `chunks` (D2), symbol identity (D1), graph edges (D3), tenancy (D5),
retiring Qdrant, and then clone → ingest → incremental → progress on top.
**Companion:** `22-CONTEXT.md` (the draft decisions, the proposed split, and the
questions that are the user's to answer).
**Confidence:** HIGH on everything tagged *measured* — it was run against
`pgvector/pgvector:pg16` scratch containers, the real migrations, the real test
suites or the real chunker over the three benchmark corpora. MEDIUM on the
proposed split, which is a sequencing judgement. LOW on anything tagged
*inferred* or *not verified*, and those tags are used deliberately.

> **How to read the evidence tags.**
> **[measured]** — I ran it and read the output; the command or script is named.
> **[read]** — I read it in code or a document, at the file:line given.
> **[verified source]** — an external page I fetched, with the claim confirmed.
> **[inferred]** — reasoning from the above; not run.
> **[not verified]** — stated so a reader knows it is open.
>
> Scratch scripts live in the session scratchpad, not the repository. Every
> scratch container (`rag22r-pgv`, `rag22r-redis`, `rag22r-isolation-pgv`,
> `rag22r-reuse`) was removed afterwards. The shared `rag-doc-isolation-tests`
> container was never touched. The docker-compose Postgres was started once,
> queried with `default_transaction_read_only=on`, and stopped again; nothing
> was written to it, and Qdrant was never started (its volume was listed
> read-only).

<research_summary>
## Summary

The roadmap entry says "Research: Unlikely (ingestion pipeline exists; clone
strategy is standard)". That was written before D1–D5. The research found the
pipeline does exist and works, the storage decisions are sound, and **three
things in `DECISIONS.md` do not survive contact with the real schema**:

1. **Partitioning `chunks` without per-partition RLS is a cross-tenant leak.**
   [measured] Row-level security enabled and forced on the partitioned parent
   is not inherited by its partitions. As the `NOSUPERUSER NOBYPASSRLS` app role
   with tenant A set, `SELECT` and `UPDATE` on tenant B's partition both
   succeeded — `UPDATE 1`, B's row overwritten. With no tenant set at all the
   read still succeeded. Enabling RLS and creating the policy on every partition
   closes it (`0` rows visible, `UPDATE 0`) and pruning still fires.
2. **`retrievals.chunk_id REFERENCES chunks(id)` cannot survive D2.** [measured]
   A foreign key into a table whose primary key is `(organization_id, id)` fails:
   `there is no unique constraint matching given keys for referenced table`.
   D2 is silent on it. Nothing writes `retrievals` today, so the choice is cheap —
   but it is a choice about user feedback, and it is surfaced as one.
3. **Partitioning by organization does not remove the selective-filter problem,
   because every query is also filtered by repository.** [measured, synthetic
   vectors] One tenant with 20 repositories of 3,000 rows in one partition,
   queried for one repository: 20 of 20 queries returned fewer than 10 rows and
   recall@10 was 0.26 under default HNSW settings; with
   `hnsw.iterative_scan = relaxed_order` recall was 1.00 with no short results.
   `DECISIONS.md` §5 demoted iterative scan to "not the fix"; for the
   repository filter it is the fix, and it is load-bearing.

**pgvector itself is a non-event for the harnesses.** [measured] On
`pgvector/pgvector:pg16` (PostgreSQL 16.15, pgvector 0.8.6) all fifteen
migrations apply through golang-migrate and through the Python harness's raw
path with byte-identical resulting schemas; the full Python suite passes
284/284 (284/284 on `postgres:16-alpine`, same count); the Go suite passes except
the one CRLF-artefact test 21-07 already records failing on `main`. One real
trap: the Go harness reuses its container **by name without checking the
image** [read, then measured], so a developer's existing
`rag-doc-isolation-tests` container would be reused and the first
`CREATE EXTENSION vector` migration would fail.

**The data question is easy.** [measured] The compose database holds only
harness and benchmark data — 4 repositories, 4 runs, 5,421 chunks, and zero
rows in `queries`, `retrievals`, `feedback`, `users`, memberships and
installations — and it is still at **migration 10**. Its vectors live only in
Qdrant, so D2's `embedding NOT NULL` cannot be satisfied by moving rows.
Re-ingestion from source is the path; the whole corpus re-embeds for about
**$0.09 on ada-002** and about **3.5 minutes** of measured ingest time.

That last number changes how D1–D5 should be sequenced. `DECISIONS.md` justifies
doing them first because "reversing any of them afterwards means re-ingesting
every repository we have indexed". Until launch that re-ingest costs cents and
minutes. The real argument for putting storage first is L1 — the handler writes
chunks and vectors in the completion transaction, which only pgvector allows —
and that argument puts **storage** first, not **all five decisions** first.

**Primary recommendation:** split into a vertical first slice that ends with a
real repository indexed end to end on pgvector (Phase 22, five plans, ~57–80 h),
then symbols, incremental, progress, the tier-1 graph and the recall test
(Phase 22.1, five plans, ~60–88 h), with retrieval-quality changes — the
embedding model among them — decided separately under the benchmark protocol.
Details and the alternative in `22-CONTEXT.md`.
</research_summary>

---

## Q1 — Where the phase divides

### What depends on what

| Work | Needs first | Evidence |
|---|---|---|
| pgvector image in compose, both harnesses and CI | nothing | [measured] suites pass on the new image (Q2) |
| D2 `chunks` rebuild, per-partition RLS, `symbols` + `symbol_edges` DDL | the image | [measured] DDL executed on the real schema (Q2) |
| Pipeline and retrievers on pgvector; Qdrant retired | the D2 table | [read] `ingestion_pipeline.py:179-181`, `vector_retriever.py:59-65` |
| Fetching a repository (token, archive, caps, filters) | **nothing in storage** | [read] a separate concern; the job payload carries no repository detail (`producer.go:220-224`) |
| `full_ingest` handler, worker switched on | pipeline on pgvector + fetch | [read] `complete()` runs `write_results` in its transaction (`transitions.py:809-897`) — only possible once vectors are in Postgres |
| D1 symbol identity emitted by the chunker | nothing in storage (it is parser work) | [measured] the census below ran with no database |
| Incremental ingest | D1 (archive-not-delete is about symbols) | `DECISIONS.md` D1 |
| D3 tier-1 resolver | D1 ids; candidates persisted | `DECISIONS.md` D3 |
| D2 multi-tenant recall test | pgvector storage; realistic embeddings | Q5 |
| Progress UI contract (ISS-034) | a worker that reports progress | `docs/api-ingestion-jobs.md` |

Two things fall out of that table.

**Fetching is independent of storage.** It can be built in either order, which
matters with a one-worker fleet only as flexibility: if a storage plan stalls in
review, fetch work is not blocked by it.

**The smallest slice that ends with a real repository indexed end to end** is:
image → D2 storage → pipeline and retrieval on pgvector → fetch → `full_ingest`
handler and worker activation. D1 emission, incremental, progress and the graph
are not on that path. D1's *table* is (it is DDL in the same migration), its
*emission* is not.

### Why "all five decisions before any real row" is weaker than it reads

`DECISIONS.md` opens: *"Phase 22 writes the first real rows and reversing any of
them afterwards means re-ingesting every repository we have indexed."* True, and
until Phase 25 ships the repositories we have indexed are the three benchmark
corpora and this repository.

[measured] Historic ingest durations from the compose database (read-only), the
harness path with no clone:

| Corpus | Chunks | Ingest |
|---|---|---|
| RAG-Doc (`self`) | 452 | 13 s |
| miniflux | 2,134 | 81 s |
| mealie | 2,816 | 120 s |

[measured] Total content across all 5,421 chunks is 3,541,668 characters —
roughly 0.9M tokens at ~4 characters a token [inferred], so a full re-embed is
about **$0.09 on ada-002 or $0.02 on text-embedding-3-small** at the
[verified source] prices of $0.10 and $0.02 per million tokens.

So pre-launch, the cost of changing a stored shape is a few dollars of time, not
a migration project. That does not make the decisions unimportant — it makes
their *order* flexible. The proposed split in `22-CONTEXT.md` uses that freedom
to reach a real end-to-end ingest early, which is `DESIGN.md` §9's own "real
gate": *"Prove retrieval quality on one real repository … the easiest to skip
and most expensive to have skipped."*

The expected shape — every decision first, then clone/ingest — is laid out as
the alternative with its cost. Both build the same things.

---

## Q2 — pgvector availability, measured

### The current images do not have it

| Image | Where | pgvector? | Evidence |
|---|---|---|---|
| `postgres:16-alpine` (16.15) | compose `docker-compose.yml:5`, Go harness `container.go:25`, Python conftest `conftest.py:43-48`, CI `backend-ci.yml:46` | **no** | [measured] no `vector.control` in `/usr/local/share/postgresql/extension/` |
| the compose container's actual image (16.11, untagged `23e88eb049fd`) | compose | **no** | [measured] `pg_available_extensions` has no `vector` row |
| `pgvector/pgvector:pg16` (16.15, digest `sha256:ccc6e83d…`) | proposed | **yes, 0.8.6** | [measured] |

[measured] On `pgvector/pgvector:pg16`:

- `pg_available_extension_versions`: `vector | 0.8.6 | superuser=t | trusted=f`.
  **Creating it needs a superuser** (or a provider's admin role). No
  `shared_preload_libraries` entry is needed.
- The log line the Go harness waits for, `database system is ready to accept
  connections`, appears **twice**, so `wait.ForLog(...).WithOccurrence(2)`
  (`container.go:103-105`) holds for the Debian-based image too.

### Every migration applies, through both harness paths

[measured] Two databases in one scratch container:

- `gomig`: `migrate -path migrations up` — all fifteen, `1/u` … `15/u`.
- `pymig`: the Python conftest's exact method (`conftest.py:98-110`: sorted
  glob, one `execute` per file, autocommit).

Schema diff between them (`py_migrate.py`): **columns 122/122, indexes 60/60,
constraints 58/58, triggers 12/12, policies 7/7 — identical.**

### Both suites pass on the new image

[measured] Scratch copies of `services/backend` and `services/workers` with only
the image constant changed (and, for Go, the reuse name, so the shared container
was never touched):

| Suite | `postgres:16-alpine` | `pgvector/pgvector:pg16` |
|---|---|---|
| Python, `pytest tests/ workers/` | **284 passed** | **284 passed** |
| Go, `go test ./... -p 1` | (21-07: all ok except one known failure) | all packages `ok` except `pkg/api/handlers`, whose only failure is `TestSignatureComparisonIsConstantTime` |
| Go `pkg/api/handlers` with that test skipped | — | **149 passing tests and subtests, 0 failing** |

`TestSignatureComparisonIsConstantTime` is the CRLF artefact 21-07 measured
failing on `main` ("could not find the end of verifySignature") — the same
message here. `DATABASE_TEST_URL` and `REDIS_URL` pointed at scratch containers
so `pkg/auth`'s helpers never reached port 5434.

### What breaks: the harness reuses a container by name, whatever its image

[read] `testcontainers-go` v0.44.0, `docker.go:1424-1441`:
`ReuseOrCreateContainer` calls `findContainerByName` and, if it finds one,
**uses it** — there is no image comparison. The Go harness uses
`WithReuseByName("rag-doc-isolation-tests")` with Ryuk disabled
(`container.go:93-101`), so that container outlives every run.

[measured] Reproduced with a scratch name: a `postgres:16-alpine` container
named `rag22r-reuse`, then the harness with `postgresImage =
"pgvector/pgvector:pg16"`, `containerName = "rag22r-reuse"` and a sixteenth
migration `CREATE EXTENSION IF NOT EXISTS vector`:

```
apply migrations: run migrations up: migration failed: extension "vector" is
not available, Could not open extension control file
"/usr/local/share/postgresql/extension/vector.control"
```

**CI is unaffected** (always cold). Every developer machine with the old
container is affected on first run after the change. **The fix is one line:
rename the reuse container whenever the image changes** (e.g.
`rag-doc-isolation-tests-pgv16`), so a stale container is simply not found. The
Python harness has no cross-session reuse (`conftest.py:36-41`) and is
unaffected.

### A second, smaller trap: Docker's 64 MB shared memory

[measured] Building an HNSW index over 95,000 rows with
`maintenance_work_mem = 1GB` in a default container failed:
`could not resize shared memory segment … No space left on device`. Parallel
index builds use `/dev/shm`, which Docker caps at 64 MB by default. Serial build
(`max_parallel_maintenance_workers = 0`) succeeded in 11–26 s. Irrelevant to the
migration (the table is created empty); relevant to Phase 24 and to any
`REINDEX` on a large table — the container needs `--shm-size`.

### The production target

Not chosen — Phase 24 owns it. The application database is **separate from
Supabase** (project memory; `STATE.md` "Environment note"), so Supabase's own
pgvector support is irrelevant.

[verified source] pgvector on candidate hosts, fetched 2026-09-17:

| Host | pgvector | Note |
|---|---|---|
| AWS RDS for PostgreSQL 16 | **0.8.2** on 16.15 | extension table in the RDS release notes |
| Google Cloud SQL for PostgreSQL | **0.8.5** on PG 13+ | installing any extension needs `cloudsqlsuperuser` |
| Render Postgres | supported on PG 13+ | "Enable this extension with `CREATE EXTENSION vector;`" |
| Fly.io Managed Postgres | included | "The third party `pgvector` extension" |

[verified source] Iterative index scans — which Q5 shows are load-bearing —
arrived in **pgvector 0.8.0** (CHANGELOG, 2024-10-30). Every host above ships
0.8.x. [not verified] Railway, Neon, Azure and Crunchy Bridge were not checked.

**Consequence for the migration runner** [measured + inferred]: `vector` is not
a trusted extension, so the production migration role must be able to create
it, or an operator creates it once and the migration's
`CREATE EXTENSION IF NOT EXISTS vector` becomes a no-op. [not verified] whether
`IF NOT EXISTS` on an already-installed extension skips the privilege check —
check on the chosen host in Phase 24.

---

## Q3 — The embedding model

### What is true today

- [read] The pipeline, the retriever and the semantic cache all use
  `EmbeddingGenerator`'s default, `text-embedding-ada-002`
  (`embedding_generator.py:20`, `openai_client.py:19`). `EMBEDDING_MODEL` in
  `services/workers/.env.example` is **read by nothing** (grep over
  `services/workers` finds no reader).
- [read] D2's DDL comment says `text-embedding-3-small`. It is a comment;
  `vector(1536)` holds either.
- [verified source] Both are 1,536 dimensions by default; 3-small accepts a
  `dimensions` parameter to shorten; both take 8,192 input tokens. Price: ada-002
  **$0.10**, 3-small **$0.02** per million tokens. OpenAI labels ada-002 an
  "Older embedding model". OpenAI's own MTEB figures: ada-002 61.0%, 3-small
  62.3% — a general benchmark, **not** evidence about code retrieval on our
  corpora, and cited only as the vendor's claim.

### Why this is not a free swap even at equal dimension

[inferred, standard] The two models produce vectors in **different spaces**.
Mixing them — ada-002 chunks queried with a 3-small query vector, or half a
repository re-embedded — returns nonsense with no error. Equal dimension is
exactly what lets that happen silently.

So the storage change and the model change **can be decoupled safely only if
the model is recorded**: an `embedding_model` column on `chunks` (or on the
run), and a retriever that refuses to compare a query embedded with one model
against rows embedded with another. Proposed in `22-CONTEXT.md` (P4).

### How it would be decided — not whether

This is a retrieval-quality change, so under the user's rule it is decided on
the benchmark with the rule committed before the deciding questions exist
(`boost-defaults-protocol.md`). Not a recommendation — a proposed protocol:

- **Candidate:** `text-embedding-3-small` at 1,536 dimensions, everything else
  unchanged (same chunker, same boosts, same fusion).
- **Questions:** the existing `confirm` sets were consulted for the boost
  decision, and the `self` holdout set is already spent (ISS-029). A decision
  needs **fresh blind questions** — 15 per corpus, written without retrieval
  access, committed before any result is seen, as for boost defaults.
- **Rule** (to be fixed by the user before the questions are written): e.g.
  symbol-level MRR higher on both corpora and file-level MRR not lower — the
  boost protocol's shape.
- **Cost:** [inferred from the measured totals above] under $0.10 of embeddings
  per full re-ingest of all three corpora, about 3.5 minutes of ingest each way,
  and about **6–10 hours**, almost all of it writing and verifying 30 blind
  questions.
- **Ordering:** after the storage migration's equivalence check (Q5), so the
  storage change and the model change are measured separately; and after any
  chunker change the user decides to make first (Q11), so the model is chosen
  on the chunks it will actually embed.

**Flagged as a user decision** — question U3 in `22-CONTEXT.md`.

### A latent bug the new storage fixes, and one it does not

[measured] Duplicate chunk content never got a vector. The compose database's
chunk counts per repository against distinct `content_hash` values:

| Corpus | Chunks | Distinct hashes | Recorded Qdrant vectors (`boost-defaults-protocol.md`) |
|---|---|---|---|
| miniflux | 2,134 | 2,122 | 2,122 |
| mealie | 2,816 | 2,736 | 2,736 |

The match is exact. [read] Mechanism: `PostgresWriter.insert_chunks` maps
`content_hash → chunk_id`, keeping only the last chunk per hash
(`postgres_writer.py:148`), and the pipeline gives vectors only to ids in that
map (`ingestion_pipeline.py:147-165`). So 12 miniflux and 80 mealie chunks were
findable by keyword only. With `embedding NOT NULL` every chunk carries its
vector. **This will move a few benchmark ranks on its own** — duplicates now tie
in the vector leg — and the equivalence check in Q5 has to expect that.

[read] Not fixed by storage: the embedding cache keys on the hash of the **raw
content** (`embedding_generator.py:126`) while the text actually embedded
includes the breadcrumb and docstring (`:124`, `_prepare_text_for_embedding`
`:61-97`). Two chunks with identical bodies but different breadcrumbs share one
embedding. Any cross-run embedding reuse (Q9) must key on the hash of the
**embedded text plus the model**.

---

## Q4 — What data exists

[measured] Compose Postgres (`testtgsd-postgres-1`, PostgreSQL 16.11), read with
`default_transaction_read_only=on`:

| | |
|---|---|
| `schema_migrations` | **version 10**, not dirty — migrations 11–15 have never been applied here |
| organizations / projects | 2 / 2 (`rag-quality` harness org; a 2026-01 `test-org`) |
| repositories | 4 — `RAG-Doc`, `miniflux`, `mealie` (harness org), `testtGSD` (test org), all `never_synced` |
| ingestion_runs | 4, all `completed` |
| chunks | **5,421** — go 2,422, python 2,997, markdown 2 |
| queries / retrievals / feedback | **0 / 0 / 0** |
| users / memberships / github_installations | **0 / 0 / 0** |

[measured, read-only] The Qdrant volume holds one collection, `code_embeddings`
(`size 1536, distance Cosine`, `hnsw m 16, ef_construct 100`), 90.9 MB on disk.
Qdrant was not started, so its point count was not read; the benchmark
protocol records 2,122 + 2,736 vectors for the two corpora.

**Conclusion:** every row is re-creatable from pinned sources (the corpora are
fetched at pinned commits — [read] `.git/shallow` holds `76889f08…` and
`84b2677f…`, matching the specs) plus this repository. No user-authored data
exists anywhere. **Re-ingestion from source is the path; no data migration.**
The D2 migration can therefore drop and recreate `chunks` rather than rewrite
it — which also means it carries **no DML**, sidestepping ISS-031's hazard for
this particular migration (Q13).

[inferred] Nothing is deployed to production (`ROADMAP.md`: Phase 24 is
deployment and is not started).

---

## Q5 — Retrieval after Qdrant

### What changes in the query engine

[read] Today:

- the vector leg calls Qdrant filtered by `repository_id` only
  (`vector_retriever.py:59-65`, `qdrant_writer.py:162-169`); the Qdrant payload has no
  `organization_id` (`qdrant_writer.py:96-107`) and no run id, so the `run_id` argument is a placeholder (`vector_retriever.py:81-87`);
- the keyword leg filters to the *latest completed run*
  (`fts_retriever.py:106`, `:128`), so the two legs already disagree about what
  is current (ISS-027);
- enrichment re-reads each result under RLS, one query per result
  (`query_engine.py:303-358`).

After D2:

1. **Both legs run in Postgres under one tenant scope.** The vector leg becomes
   `ORDER BY embedding <=> %s::vector LIMIT 50` inside `require_tenant`, and the
   `DESIGN.md` §2.1 asymmetry — the org-unscoped Qdrant leg — is gone rather than
   patched. [read] `search.go:31` still accepts any well-formed
   `repository_id`; with the vector leg under RLS, another tenant's id returns
   nothing from either leg.
2. **The keyword leg must drop the latest-run filter.** With incremental ingest,
   unchanged files legitimately keep chunks from earlier runs (ISS-027's own
   warning); currency becomes per file (Q9).
3. **`hnsw.iterative_scan = relaxed_order` is required, not optional** — see the
   measurement below — with a re-sort, because relaxed order can return rows
   slightly out of distance order ([verified source] the pgvector README
   recommends a materialized CTE for strict ordering).
4. **The query vector must be a bound parameter.** [measured] With sequential
   and bitmap scans disabled, both `ORDER BY embedding <=> :'v'::vector` and the
   same constant through an inlined CTE used
   `Index Scan using chunks_v2_p12_embedding_idx`. In the hybrid prototype below,
   a query vector drawn from a table via a subquery produced a sequential scan.
   psycopg2 interpolates parameters client-side, so passing `%s::vector` is the
   safe shape.
5. **The vector-leg isolation test can finally exist.** [read]
   `test_query_engine_isolation.py:5-11` explains it never tested the vector leg
   because that needed a live Qdrant and an OpenAI key. With vectors in the
   testcontainers database, a test can insert fixed vectors and inject the query
   vector — no OpenAI call.

### Can the hybrid search be one SQL statement under RLS?

**Yes — measured.** A single statement with a keyword leg (`ts_rank_cd`, top 50),
a vector leg (`<=>`, top 50), `UNION ALL`, and RRF with k = 60
(`sum(1.0/(60 + r))`, matching `rrf_fusion.py`) ran as `rag_doc_app` under the
RLS policy on 5,001 rows. `EXPLAIN ANALYZE` showed **`Subplans Removed: 63` on
both legs** — each leg pruned to the tenant's one partition from the policy
alone, with no `organization_id` predicate in the SQL.

**It should not be the first step.** [inferred] `RRFFusion` keeps metadata from
a chunk's first occurrence (ISS-025 defect 2) and ranks ties in dictionary
order; `MetadataBooster` runs between fusion and the cut. Moving fusion into SQL
changes tie-breaking and ordering — a ranking change, which the protocol
governs. The migration should keep fusion and boosts in Python, run both legs
against Postgres, and prove equivalence; single-statement fusion is a later
latency optimisation, now known to be feasible.

### The equivalence check the migration owes

[inferred] At benchmark scale both stores do exact search: [measured] a 3,000-row
tenant got a **sequential scan** of its partition (below), and Qdrant's
collection has `full_scan_threshold: 10000` [measured, config read] with no
payload index on `repository_id`. So retrieval over the same chunks with the same
ada-002 vectors should rank identically, **except** where a previously
vector-less duplicate chunk now appears (Q3). The migration plan should re-ingest
the three corpora on pgvector with ada-002 and compare every question's rank
against the Qdrant-era run, and explain each difference. That is an equivalence
check, not a quality decision — it does not need fresh questions.

### What the D2 multi-tenant recall test needs

Two probes on `pgvector/pgvector:pg16`, 256-dimensional vectors, `MODULUS 64`,
RLS on the parent **and** every partition, queries as the app role inside a
tenant transaction, exact baseline computed in the same scope with the vector
index disabled (`recall_probe.py`, `recall_probe_clustered.py`,
`repo_filter_probe.py`).

**Probe 1 — tenant size decides whether HNSW is used at all.** [measured]

| Tenant | Data | Plan | recall@10 |
|---|---|---|---|
| 50,000 rows (own partition) | uniform random | HNSW | 0.145 (ef_search 40) / 0.290 (100) |
| 50,000 rows | clustered, queries near data | HNSW | 0.810 / 0.700 (iterative) / 0.820 (ef 100) |
| 3,000 rows | either | **sequential scan + sort** | 1.000 |

**Probe 2 — the repository filter reproduces the failure D2 set out to remove.**
[measured, clustered data] One tenant, all rows in its one partition, query one
repository:

| Repositories × rows | Setting | recall@10 | Short results (<10 rows) |
|---|---|---|---|
| 5 × 10,000 | default | 0.690 | **16 / 20** |
| 5 × 10,000 | `iterative_scan = relaxed_order` | 0.835 | 0 / 20 |
| 20 × 3,000 | default | 0.260 | **20 / 20** |
| 20 × 3,000 | `iterative_scan = relaxed_order` | **1.000** | 0 / 20 |

Read those carefully — they are synthetic, and the **absolute** recall is a
property of the data (0.145 on uniform noise, 0.81 on clustered, same table,
same size). What is robust is the **shape**:

1. **Below some partition size the planner does exact search**, so a recall test
   seeded with small tenants measures nothing about HNSW. The test must seed at
   least one tenant large enough (tens of thousands of rows) that the plan is
   HNSW, and assert the plan.
2. **The repository filter is a selective filter inside the partition.**
   `DECISIONS.md` §5 found `iterative_scan` "changed nothing" and that short
   results did *not* appear — true at its tenant selectivity with no repository
   filter. With the repository filter every product query carries, short results
   appeared in 16/20 and 20/20 queries, and iterative scan fixed both. **This
   corrects §5**: keep partitioning for index-size runway, and treat iterative
   scan as the mitigation for the repository filter.
3. **Co-tenancy will arrive by pigeonhole** above 64 organizations, adding a
   second selective filter (tenant) inside each partition. `DECISIONS.md`
   already names the ~1,000-organization revisit trigger; the test should
   include a partition shared by several tenants now so the trigger is watched.
4. **Realistic vectors.** Random vectors understate recall badly. The test should
   seed synthetic tenants with **real embeddings** — the benchmark corpora's own
   chunk vectors, copied (with small perturbation) into many organizations and
   repositories. Real vectors, synthetic tenancy.

**Relation to the benchmark harness:** they measure different things and neither
substitutes for the other. [read] The harness puts every corpus under one
organization (`ORG`, `rag_quality_harness.py:133`, passed at `:511`) — three
repositories, ~5,400 rows, one partition — where [measured] the planner does
exact search. So the harness measures ranking quality and can never see an HNSW
recall regression; the recall test measures HNSW recall under tenancy and says
nothing about ranking quality. The recall test can reuse the harness's corpora as
its source of real embeddings.

### Storage cost per chunk, and the insert inside the completion transaction

[measured] Inserting 5,000 rows of `vector(1536)` into the HNSW-indexed
partitioned table in one transaction took **26.2 s** (≈ 5 ms/row, including
generating the random vectors server-side). The partition then held **43 MB of
table and 39 MB of HNSW index** for 5,001 rows — about **16 KB per chunk**, half
of it index. [inferred] A 50,000-chunk repository is ~800 MB and ~4 minutes of
insert inside `complete()`'s transaction; the benchmark corpora are 10–15 s.

---

## Q6 — D1 symbol identity against the current chunker

[measured] `chunk_census.py` runs the real `SemanticChunker` over the `self` Go
and Python code, miniflux, mealie and this repository's frontend TypeScript,
with no database and no OpenAI. Its chunk counts reproduce the compose
database's exactly (miniflux 2,134, mealie 2,816), so it measures what the
pipeline actually stores.

### What exists per language

| | Python | Go | TypeScript |
|---|---|---|---|
| Grammar | `tree_sitter_python` | `tree_sitter_go` | **the JavaScript grammar** (`tree_sitter_parser.py:19`) |
| Functions | `function_definition` | `function_declaration` + `method_declaration` (since PR #27) | `function_declaration`, `method_definition` |
| Types | `class_definition` | **`type_spec` with `struct_type` only** (`:74-82`) | `class_declaration` |
| Breadcrumb | ancestor classes/functions + name | receiver type + name for methods (`metadata_builder.py:43-46`) | ancestors + name |
| Docstring | in metadata | doc comment in metadata, **outside the span** (`semantic_chunker.py:179-186`) | JSDoc in metadata |

### What D1's `(repository, file_path, symbol_path, kind, ordinal)` is missing

1. **`symbol_path` cannot be the breadcrumb.** [read] `generate_breadcrumb`
   keeps only the last four levels (`metadata_builder.py:80-82`). [measured] It
   truncated **0** breadcrumbs across all five corpora, so it is a latent
   identity collision rather than a live one — but an identity must use the full
   ancestor chain.
2. **There is no `kind` taxonomy.** [read] `chunk_type` is `function` for both
   Go functions and Go methods, and `class` for Go structs. D1 needs
   function / method / class / type / const / module; a mapping, not a new parse.
3. **`ordinal` is needed, and there is a fourth collision case.** [measured]
   Collisions on `(file, full path, kind)`: **0** in self-Go, self-Python and
   miniflux; **2 keys / 5 extra rows** in mealie — `UnitConverter.parse` ×3 and
   `PlaceholderKeyword.parse_value` ×4, both **`@typing.overload`** stubs above
   their implementation. D1 lists Go `init()`, Python property/setter and TS
   declaration merging; `@overload` is a fourth. [measured] A property and its
   setter do collide as D1 predicts (`Api.name` twice, same kind). Ordinal
   handles all of them, with D1's stated limitation — adding an overload above
   the implementation renumbers it.
4. **The span is not what `span_digest` should cover.** [measured]
   `span_probe.py`: a Python method decorated `@router.get("/recipes")` is
   chunked from the `def` line (lines 3–4; the decorator is line 2). A
   decorator change — `get` to `post` — would not change `span_digest`, so D4
   would call a changed route handler *unaffected*. Go doc comments are likewise
   outside the span. Deciding what the span includes is part of D1.
5. **Go types other than structs have no identity.** [measured] Non-struct type
   specs never chunked: **9 of 71** in self-Go (6 interfaces, 3 named types),
   **41 of 370** in miniflux (26 slice, 8 named, 3 interface, 2 function,
   2 map). Package-level `const` and `var` declarations are never chunked either
   (`span_probe.py`: `type State string`, `type Store interface`, `const Max`
   produced no chunk).
6. **TypeScript barely parses.** [measured] With the JavaScript grammar, **39 of
   43** frontend files have parse errors (327 `ERROR` nodes); **29 of 105**
   chunks are fixed-size fallbacks; interfaces, type aliases and enums are
   invisible. D1's own TypeScript example (declaration merging) cannot be
   parsed today.
7. **No module symbol.** Top-level code — Python module statements, Go
   initialisers — has no enclosing symbol. D3 needs a `from_symbol_id` for calls
   made there, so each file needs a `module` symbol.
8. **`first_seen_commit` / `last_seen_commit`** need the commit passed to the
   chunker; the pipeline already has it (`ingestion_pipeline.py:44`).

### Aliases — a refinement to D1's dependency on D3

`DECISIONS.md` D1: *"the tier-1 import resolution has to land before, or with,
symbol identity."* [inferred] That holds only if identities are minted for
aliases. The chunker mints nothing for imports or re-exports; it emits
definitions. [measured] mealie has 58 of 89 `__init__.py` files re-exporting
(168 relative-import lines), and **0** Go type aliases appear in miniflux or
self-Go. If the rule is **mint identities only at definition sites, and never
for an alias declaration** (a Go `type A = B`, a TS `export { X as Y }`, a Python
`Y = X`), a re-export cannot create a second identity. Resolving a *name* to its
leaf — which is what D4 anchoring by name and D3 edges need — still needs the
import graph. So D1 emission can precede the D3 resolver, and D1's third
verification criterion ("a re-export resolves to the same id as its leaf")
belongs to the D3 plan.

### Which benchmark findings interact with identity

| Finding | Interaction with D1 |
|---|---|
| **ISS-026** duplicate class chunks | [measured] class chunks duplicate their methods: **93%** of class characters in self-Python (182,289 / 196,582) and **77%** in mealie (659,442 / 860,234); **0%** in Go (methods sit outside the struct). Identity is unaffected — one class symbol either way — but the class *chunk* changes, and `chunks.symbol_id` must still point at the class |
| oversized chunks | [measured] chunks over 5,000 characters: 3 self-Go, 22 self-Python, 15 miniflux, 32 mealie, 6 TS; the largest is mealie's `RecipeController` at 33,348. Splitting a large function into several chunks is many chunks → one symbol, which D1's single nullable FK supports |
| Go types never indexed | the identity gap in item 5; adding them as **symbols** need not add **chunks**, so identity can be fixed without a ranking change |
| nested functions | [measured] 2 in self-Python, 39 in mealie, each chunked and also inside its parent's chunk — a symbol-under-symbol, which the full-chain `symbol_path` handles |

---

## Q7 — D3 tier 1

### What the parser can emit today

[read] Nothing: the only queries are functions and classes
(`tree_sitter_parser.py:34-107`). [measured] The trees already contain the raw
material, counted by `chunk_census.py`:

| | call sites | imports |
|---|---|---|
| self-Go | 1,721 `call_expression` | 284 `import_spec` |
| miniflux | 11,371 | 1,852 |
| self-Python | 1,175 `call` | 120 `import_from_statement` + 56 `import_statement` |
| mealie | 9,386 | 2,661 + 237 |

Emitting candidates — callee text, line, the enclosing symbol, and each file's
imports — is one extra query per language and per kind. [inferred] Cheap, as
`DECISIONS.md` says; the resolver is the expensive part (R-A's 20–30 h).

### The `edge_kind` choice

`DECISIONS.md` D3 requires Phase 22 to pick one `edge_kind` vocabulary for both
tiers, or omit `edge_kind` from the tier-2 reconciliation `DELETE`.

[verified source] SCIP's `scip.proto`: `SymbolRole` is `Definition, Import,
WriteAccess, ReadAccess, Generated, Test, ForwardDefinition`, and
`Relationship` carries `is_reference, is_implementation, is_type_definition,
is_definition`. **There is no notion of a call.**

So the disagreement `DECISIONS.md` describes as a hazard — tier 1 says `calls`,
SCIP says `references` — is **systematic**: SCIP can never say `calls`. A shared
vocabulary would mean tier 1 giving up `calls`, which is the edge behind
`who_calls`, `DESIGN.md`'s headline structural query.

**Recommendation (P9 in `22-CONTEXT.md`): omit `edge_kind` from the
reconciliation.** Tier 1 keeps its own vocabulary (emit `calls` and `imports`
first); tier 2 retires every unresolved tier-1 edge for
`(organization_id, from_symbol_id, to_symbol_name)` in the same transaction as
its insert, and derives its own `edge_kind` from SCIP roles plus the tier-1 call
site it resolved. The cost is the one `DECISIONS.md` names — tier 2 retires all
tier-1 guesses between that pair — and it is the right cost: tier 2 is the
authority for that pair.

---

## Q8 — The clone worker's security

### Where the Python worker would get the App private key

[read] Today only the Go backend holds it: `GITHUB_APP_ID` +
`GITHUB_APP_PRIVATE_KEY_PATH` (`router.go:179-187`), loaded by
`github.NewClient` (`client.go:109-156`), minting App JWTs (`:185-218`) and
installation tokens (`:227-283`). The documented rule is an absolute path
outside the repository (`docs/github-app-setup.md:172-179`). The compose
`backend` service passes none of the GitHub variables (`STATE.md`), and the
`workers` service passes only `ENV` (`docker-compose.yml:60-65`).

[measured] The worker's virtualenv has neither `PyJWT` nor `cryptography`
(`pip list`), so minting in Python is a new dependency.

Two shapes:

| | A — token broker | B — key in the worker |
|---|---|---|
| Where the key lives | backend only | backend **and every worker process** |
| What a worker holds | a 1-hour token for **one repository, `contents: read`** | the App's full authority over **every installation of every tenant** |
| New surface | an internal-only endpoint on the backend | a mounted secret file and two Python dependencies |
| How the key stays out of the repository | unchanged | a host path mounted read-only, e.g. `${GITHUB_APP_PRIVATE_KEY_PATH}:/run/secrets/github-app.pem:ro` |

[verified source] GitHub supports exactly the narrowing A needs: the access-token
request accepts `repositories` / `repository_ids` ("up to 500") and a
`permissions` object; tokens expire one hour after creation. [read] The Go client
mints **unscoped** tokens today — the `POST` at `client.go:263-264` sends no body.

[inferred] A's endpoint can authorize without a new static secret: the worker
presents `job_id` + `lease_owner`, and the backend mints only if that job is
`running` under that lease — the same fence every terminal write uses. Its
residual risk is stated rather than hidden: `ingestion_jobs` has no RLS and the
worker's database role can read every `lease_owner`, so a compromised worker could
request tokens for any **currently running** job's repository — scoped,
read-only and hour-long, which is still far narrower than holding the key. The
endpoint must be on an internal listener, never on the public router.

This is the user's call because it shapes Phase 24's topology — U4 in
`22-CONTEXT.md`, recommending A.

### Fetch: git clone or the tarball API

[measured] The worker image is `python:3.11-slim` (`services/workers/Dockerfile:1`),
Python 3.11.16, and it has **no `git` binary**. [verified source]
`GET /repos/{owner}/{repo}/tarball/{ref}` returns a redirect to a download link
that, for private repositories, "expire[s] after five minutes"; the page states
no size limit.

| | tarball API | `git` shallow clone |
|---|---|---|
| new binary in the image | no | `git` |
| token placement | an `Authorization` header on one request; [not verified] the redirect URL carries its own short-lived token, which must never be logged | URL (lands in `.git/config` and error text) unless passed via `http.extraHeader` in environment config |
| hostile-content surface | archive extraction: traversal, symlinks, device files, bombs — Python's `tarfile` `filter="data"` refuses the first three (available from 3.11.4 [inferred from the version measured above]) | git itself, plus submodules, LFS smudge filters and the clone-time CVE history |
| incremental bandwidth | full archive per job | also a full shallow fetch per job, since workers are stateless |
| size limit | undocumented [not verified] | none from git; ours to set |

Recommendation (P10): the tarball API, with a caveat to measure on the largest
benchmark repository in the plan — GitHub documents no size limit, and behaviour
on very large repositories is unverified.

### "Respect `.gitignore`" is a no-op

[inferred] A clone or archive contains only **tracked** files; `.gitignore`
governs untracked ones. The roadmap's rule protects against nothing. What
matters is tracked content that should not be indexed: vendored and generated
code (`vendor/`, committed `node_modules/`, `dist/`, `*.min.js`, lockfiles —
[read] `MetadataBooster.NOISE_PATTERNS` already lists some, `metadata_booster.py:13-16`),
binaries, oversized files, and **committed secrets** (`.env`, `*.pem`,
`id_rsa`). The last are worth a policy: indexing them sends them to OpenAI and
makes them searchable to every member of the organization. U7.

### Threat model for a hostile repository

[inferred throughout; each row names the control the plan should test]

| Threat | Control |
|---|---|
| credentials in logs or `last_error` | token only in headers; never log URLs; [read] 21-05's redaction already covers `ghs_`, `github_pat_`, `sk-`, JWTs and PEM blocks (`transitions.py:524-530`); add a test that pushes a real-shaped fetch failure through `sanitize_error` |
| path traversal, absolute paths, device files in the archive | `tarfile` `filter="data"`; extract into a per-job temporary directory |
| **symlinks** — a tracked link to `/etc/passwd` or to the worker's secrets mount, read and **indexed for the tenant** | never follow symlinks when walking (`lstat`, skip links) |
| resource exhaustion: huge archive, many files, huge files, decompression bombs, pathological lines | caps on archive bytes, extracted bytes, file count and per-file size, checked while streaming; `max_job_duration` as the backstop |
| code execution | never run install or build steps (K3 already moved SCIP to customer CI); no git hooks, submodules or LFS on the tarball path |
| parser memory-safety bugs on hostile input | the worker holds the fewest secrets possible — the argument for A |
| crashed worker leaving files | per-job directory removed in `finally`; sweep stale directories at worker start |
| prompt injection in indexed content | already bounded by tenant isolation — it can only reach its own tenant's answers; not new in Phase 22 |

### Two repository hygiene findings

[measured] `git check-ignore` in this worktree: `services/backend/.env` is
ignored, but **`*.pem`** (`key.pem`,
`services/backend/rag-doc-dev.private-key.pem`) and **`.env.bak-19-03`** are
**not**. `docs/github-app-setup.md:152-154` says 20-02 would add `.pem` to
`.gitignore`; it did not. [read] Three `services/backend/.env.bak-*` files are
untracked in the main checkout (the session's git status); one `git add -A`
would commit them. Not Phase 22's job to fix, but Phase 22 is the phase that
starts handling the key in more places.

---

## Q9 — Incremental ingestion

### What the queue hands the worker

[read] A push enqueues an `incremental` job carrying only
`(organization_id, repository_id, job_type)` (`producer.go:220-224`,
`github_webhook_events.go:645-654`) — **no commit SHA**. That is deliberate (a push
joins a live job, L7), so the worker must decide what to index at claim time:
the default branch's current head, compared against what is already indexed.

### Diff by commit, or by content

[verified source] GitHub's compare endpoint returns "up to 300 changed files for
the entire comparison". A large push, a force-push, or a stretch of missed
webhooks would silently exceed or skip it.

Recommendation (P11): **a content-addressed file manifest, not a commit diff.**
Per repository, store `(file_path, content_sha256, indexer_version)`. Each job:

1. fetch the current tree;
2. compare every file's hash with the manifest — added, changed, unchanged,
   removed;
3. parse and embed only added and changed files;
4. in `complete()`'s transaction: delete the chunks of changed and removed
   files, insert the new ones, upsert-and-unarchive present symbols, archive
   symbols that vanished (D1 — never `DELETE`), update the manifest.

It does not need the previous commit to exist, so it survives force-pushes and
missed pushes; `full_ingest` and `incremental` become the same algorithm, the
former ignoring the manifest. `indexer_version` means a chunker fix can be rolled
out by re-indexing files whose version is old — the lazy migration path for Q11.

### What "idempotent chunk writes per run" means concretely

[read] The completion transaction removes torn writes, not duplicated work: a
crash mid-embedding re-runs the job and reuses the same `ingestion_runs` row
(`RESOLVE_RUN_SQL`, `transitions.py:297-302`). [read] `PostgresWriter.create_ingestion_run`
still raises `23505` on a repeated commit (`postgres_writer.py:80`, no
`ON CONFLICT`).

Concretely:

- the run is resolved and attached with `resolve_ingestion_run` +
  `attach_ingestion_run`, never `create_ingestion_run`;
- `write_results` is a **pure function of (manifest, tree)**: delete the chunks
  for the affected `file_path`s of that repository, then insert. Replaying it
  after a partial failure produces the same rows; replaying it after a commit
  finds nothing changed;
- chunks become **current state per file**, and `chunks.ingestion_run_id` means
  "the run that last wrote this chunk". That is what closes ISS-027 — superseded
  chunks are deleted in the transaction that writes their replacement, and there
  is no second store to disagree with;
- embeddings for unchanged chunk text in a changed file can be reused from the
  rows being replaced, keyed on the hash of the **embedded text and the model**
  (Q3).

### The `retrievals` cascade

[read] `retrievals.chunk_id … ON DELETE CASCADE` (000004) and
`feedback.retrieval_id … ON DELETE CASCADE` (000005): under per-file
delete-and-insert, every push to a file would delete the user feedback left on
answers citing it. [measured] D2 breaks the foreign key anyway (Summary, item 2),
and [measured] zero rows exist in either table; [read] no production code writes
`retrievals` (only `scripts/seed-complete.sql` and test fixtures). U9.

---

## Q10 — Progress transport

[read] Already built: the worker writes `last_stage` and `progress` to the job
row, fenced and redacted (`runtime.py:554-620`); `GET /api/admin/jobs/{id}`
returns them with a computed `stalled` (21-07). Missing: any way for a UI to
learn the job id (ISS-034).

| | Poll the job row | SSE (roadmap sketch) |
|---|---|---|
| server work | none new; a primary-key read every 2–5 s per open page | Redis pub/sub or `LISTEN/NOTIFY`, a long-lived Go handler per viewer, reconnection, backfill for late joiners |
| durability | the row is the truth | pub/sub is fire-and-forget; a UI that misses an event must still read the row |
| auth | the existing Bearer route | [read] workable — the frontend streams with `fetch` + `getReader` (`lib/api/client.ts:44-54`), not `EventSource`, so headers are available |
| isolation test | 21-07's pattern | a new streaming surface to isolation-test deliberately |
| roadmap's v2 breadcrumb | — | "subscribe to a chunk-level event stream" |

Recommendation (P12): **poll the job row for v1**, and put ISS-034 in the same
plan, since polling is what makes a job id necessary. The v2 breadcrumb — a
graph worker subscribing without pipeline changes — is better served by what D3
already persists (candidates and edges in tables, written in the completion
transaction) than by an ephemeral stream. SSE stays available later: the payload
it would carry is the same `progress` object.

---

## Q11 — Chunk quality before real ingest

The user's worry — a bad chunker makes every downstream measurement meaningless —
is right about **measurements**, and it lands on **decisions**, not on the
ingest itself. Pre-launch, re-ingesting costs cents (Q1); after launch,
`indexer_version` (Q9) rolls a chunker fix out file by file.

| Issue | Changes stored shape? | Changes ranking? | Before real ingest? | Before which decision? |
|---|---|---|---|---|
| ISS-026 duplicate/oversized class chunks | chunk rows | yes | **no** | **before the embedding-model decision** — it changes what gets embedded |
| TypeScript on the JavaScript grammar | chunks and symbols | yes | no, but **before any TS customer** | the benchmark has **no TS corpus** [read: `miniflux.json` and `mealie.json` roots are `.go` and `.py`], so it cannot be decided under the protocol until one exists |
| Go non-struct types, consts | symbols (identity) | only if chunked | identity: **with D1** | none, if added as symbols without chunks |
| decorators / doc comments outside the span | `span_digest` | only if the embedded text changes | **with D1** | none |
| ISS-024 content boosts never fire | no | [read] no at current defaults — the quoted and identifier boosts are **1.0** since PR #32 | no | ranking |
| ISS-025 stopword identifiers, first-occurrence metadata | no | at non-neutral boosts | no | ranking |
| ISS-028 breadcrumbs match only whole names | an index expression | yes | no — a generated column added later is cheap | ranking |
| ISS-029 AND keyword semantics | no | yes | no | ranking |

**Recommended order of decisions:** storage equivalence (no quality change) →
chunker changes (ISS-026, then TS once a TS corpus exists) → embedding model →
ranking (ISS-024/025/028/029). Each later decision is then made on the chunks
and vectors it will actually ship with. U10 asks where this track sits.

One ISS-029 detail found on the way: [read] the keyword leg ranks and matches
`to_tsvector('english', COALESCE(breadcrumb, ''))` (`fts_retriever.py:124`,
`:131`) while migration 000006 indexes `to_tsvector('english', breadcrumb)`
(`000006_add_fts_index.up.sql:11`). [inferred, not `EXPLAIN`ed] The expressions
differ, so the breadcrumb GIN index cannot serve that branch. The D2 table
rebuild is the natural moment to make them agree.

---

## Q12 — Phase 21's open numbers

| Number | What is known now | What only ingestion can tell | How to measure |
|---|---|---|---|
| **worker-pool size** | [read] one job per process, two connections each (`docs/api-ingestion-jobs.md`) | incremental duration (never measured) and the embedding API's throughput ceiling | per-stage timings (`fetch_ms`, `parse_ms`, `embed_ms`, `store_ms`, file and chunk counts) written into `progress`; a script aggregating completed jobs; run full and incremental on all three corpora and a large public repository |
| **`max_job_duration`** | [measured] full ingest 13–120 s for 452–2,816 chunks, ≈ 29–43 ms per chunk, embedding-dominated; [measured] ~5 ms per row of vector insert | a real large repository's end-to-end time | the same timings; [inferred] a 50,000-chunk repository is ~25–35 min of embedding plus ~4 min of insert, so a **2-hour** initial bound (P16) is ~3× a 50,000-chunk repository, under 2× the 100,000-chunk cap proposed in U6, and ~60× the largest benchmark |
| **heartbeat `statement_timeout`** | [read] no timeout on either connection; `connect_timeout` only (`runtime.py:785-797`) | nothing — a blocking beat can be provoked | a test holding `SELECT … FOR UPDATE` on the job row while the heartbeat beats, asserting it raises within the timeout and the give-up rules fire; propose 15 s, a quarter of the beat interval |

[not verified] The OpenAI rate limit for the key in use. Every worker shares it,
so tokens-per-minute may bound the useful pool size before Postgres or CPU does.

---

## Q13 — ISS-031 and migrations with DML

[read] ISS-031 recommends a CI check that applies every migration to a **seeded**
database. The D2 migration proposed here drops and recreates `chunks` (Q4), so
it has no DML of its own.

**Recommendation (P13): land the check first anyway**, in the plan that swaps the
image, because:

- the D2 migration is the largest structural change the schema has had — FK
  drops, a table rebuild under `FORCE ROW LEVEL SECURITY`, 64 partitions each
  with a policy — and "applies to an empty database" is the weakest evidence
  about it;
- **a seeded database already exists and is behind**: [measured] the compose
  database is at migration 10 with 5,421 chunks. Taking it to 16 runs 000013's
  and 000015's backfill DML on real rows for the first time — the exact scenario
  ISS-031 describes, on developer machines, with nobody watching;
- the check guards the whole class, not one migration, and the phase adds at
  least one more migration (the file manifest, Q9).

Estimated 3–5 h: a seed fixture covering every tenant table (including
`chunks`, `retrievals`, `feedback`, `ingestion_jobs`), applied at version N−1,
then `up`, then assertions that seeded rows survived or were intentionally
rebuilt, run in `backend-ci.yml` beside the existing empty-database step.

---

## Corrections to the roadmap sketch

| Roadmap says | Research found |
|---|---|
| "Research: Unlikely" | D1–D5 all land here; three of them needed correction (Summary) |
| 22-01 "shallow clone (`--depth 1`)" | tarball API proposed; the worker image has no `git` [measured] |
| 22-01 "respect `.gitignore`" | a no-op for tracked content; replace with vendored/generated/binary/secret filters |
| 22-02 "Postgres + Qdrant writers" | Qdrant retired; one transaction |
| 22-03 "diff previous commit vs new HEAD … by SHA" | content manifest; the compare API caps at 300 files [verified source]; the job carries no SHA [read] |
| 22-03 "delete removed files from indexes" | delete chunks, **archive** symbols (D1) |
| 22-04 "Redis pub/sub … SSE endpoint" | poll the job row; ISS-034 |
| Phase 24 "Qdrant persistence and snapshot strategy", 24-04 "Qdrant snapshot schedule" | no Qdrant; add pgvector availability, `CREATE EXTENSION` privilege and `--shm-size` to Phase 24's research topics |
| Phase 25-03 runbook "Qdrant down" | drop |

---

## What was measured, and what was not

**Measured:** pgvector absent from every current image and present at 0.8.6 in
`pgvector/pgvector:pg16`; its privilege flags; migrations through both harness
paths with identical schemas; both test suites on the new image (and the Python
suite on the old one); the harness reuse-by-name failure; the `retrievals` FK
error; the partition RLS leak and its fix; runtime pruning on PG16 from the
policy alone; the one-statement hybrid query under RLS; HNSW eligibility of a
bound vector; recall and plan choice on synthetic multi-tenant and
multi-repository data; the insert rate and per-chunk storage; the compose
database's contents, version and historic ingest durations; the duplicate-hash
vector gap; the chunk census (collisions, truncation, Go types, TS parse errors,
class duplication, call/import counts); decorator and doc-comment spans;
`git check-ignore` coverage; the worker image's lack of `git`.

**Not measured, and why:**

- **Recall on real embeddings.** Synthetic vectors only; the real-embedding test
  is itself a proposed deliverable.
- **Qdrant's point count.** Qdrant was deliberately not started; the counts are
  the benchmark protocol's record, cross-checked against distinct hashes.
- **The tarball endpoint on large repositories**, and the shape of its redirect
  URL.
- **Whether `CREATE EXTENSION IF NOT EXISTS` skips the privilege check** when the
  extension exists.
- **The OpenAI rate limit** of the key in use.
- **Railway, Neon, Azure, Crunchy Bridge** pgvector availability.
- **The breadcrumb GIN index mismatch** was read, not `EXPLAIN`ed.
- A **read-only copy of the compose volumes** was attempted first, to avoid
  starting the compose database at all; the permission system refused the copy,
  so the database was started and read with `default_transaction_read_only=on`
  instead, as the brief allows.

---

## Sources

Per `REWORK.md` §4: **Verified** means fetched on 2026-09-17 and the specific
claim confirmed. Nothing in this document rests on an unopened page.

### Verified — fetched, claim confirmed

- **AWS, *Extension versions for Amazon RDS for PostgreSQL*** —
  `docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html`.
  pgvector listed for PostgreSQL 16; 0.8.2 on 16.15.
- **Google Cloud, *Configure PostgreSQL extensions*** —
  `docs.cloud.google.com/sql/docs/postgres/extensions`. pgvector 0.8.5 on
  PG 13+; installation needs `cloudsqlsuperuser`.
- **Render, *PostgreSQL extensions*** — `render.com/docs/postgresql-extensions`.
  pgvector on PG 13+, enabled with `CREATE EXTENSION vector;`.
- **Fly.io, *Managed Postgres*** — `fly.io/docs/mpg/`. Fully managed; includes
  "the third party `pgvector` extension".
- **pgvector README** — `github.com/pgvector/pgvector`. Iterative scans; strict
  versus relaxed ordering; a materialized CTE for strict ordering under relaxed;
  "If filtering by many different values, consider partitioning."
- **pgvector CHANGELOG** — `github.com/pgvector/pgvector/blob/master/CHANGELOG.md`.
  0.8.0 (2024-10-30): "Added support for iterative index scans".
- **OpenAI, `text-embedding-3-small` and `text-embedding-ada-002` model pages** —
  `developers.openai.com/api/docs/models/…`. $0.02 and $0.1 per 1M tokens;
  ada-002 labelled "Older embedding model".
- **OpenAI, *Vector embeddings* guide** —
  `developers.openai.com/api/docs/guides/embeddings`. 1536 dimensions for both;
  the `dimensions` parameter; 8,192 input tokens; MTEB 61.0% (ada-002) vs 62.3%
  (3-small).
- **GitHub REST, *Create an installation access token for an app*** —
  `docs.github.com/en/rest/apps/apps`. `repositories` / `repository_ids` (up to
  500) and `permissions`; one-hour expiry.
- **GitHub REST, *Download a repository archive (tar)*** —
  `docs.github.com/en/rest/repos/contents`. `GET /repos/{owner}/{repo}/tarball/{ref}`;
  private links "expire after five minutes"; no size limit stated.
- **GitHub REST, *Compare two commits*** — `docs.github.com/en/rest/commits/commits`.
  "up to 300 changed files for the entire comparison"; 250 commits without
  paging.
- **SCIP, `scip.proto`** — `github.com/sourcegraph/scip/blob/main/scip.proto`.
  `SymbolRole` and `Relationship` as quoted; no call concept.

### Read in the repository

Cited inline at file:line. The planning inputs: `.planning/v2-substrate/`
(`REWORK.md`, `DECISIONS.md`, `DESIGN.md`, `RESEARCH.md` R-A and R-B),
`ROADMAP.md`, `STATE.md`, `ISSUES.md`, `docs/api-ingestion-jobs.md`,
`21-RESEARCH.md`, `21-CONTEXT.md`, `21-07-SUMMARY.md`,
`boost-defaults-protocol.md`; and `testcontainers-go` v0.44.0 `docker.go` in the
Go module cache.

### Scratch artefacts (session scratchpad, not committed)

`chunk_census.py`, `span_probe.py`, `py_migrate.py`, `d2_proto.sql`,
`d2_rls_probe.sql`, `d2_rls_probe2.sql`, `recall_probe.py`,
`recall_probe_clustered.py`, `repo_filter_probe.py`, `insert_rate.sql`,
`hybrid.sql`, `hnsw_eligibility.sql`, and the two test logs. The SQL files are
worth transcribing into the D2 plan's tests; they are the reproduction for every
storage claim above.
