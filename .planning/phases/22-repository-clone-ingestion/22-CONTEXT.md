# Phase 22: Repository Clone → Ingestion Orchestration — Context (DRAFT)

**Written:** 2026-09-17, as a research and scoping pass. **Nothing here is
locked.** Every decision is marked **PROPOSED**; the user locks them, and the
questions in [Open questions for the user](#open-questions-for-the-user) are the
ones that are genuinely theirs.
**Research:** `22-RESEARCH.md` in this directory — every claim below points at
the question there that carries its evidence, and the evidence is tagged
measured / read / verified source / inferred.
**Not edited:** `ROADMAP.md` and `STATE.md`. The proposed roadmap change is
[at the end of this file](#proposed-roadmap-change), for the user to approve
first.

## Objective

Turn the queue Phase 21 built into indexed repositories: fetch a connected
repository, parse it, embed it and store it — on the storage `DECISIONS.md`
settled (pgvector, partitioned `chunks`, symbol identity, graph edges,
trigger- or key-maintained tenancy) — then keep it current on every push, and
report progress a UI can read.

It is also the phase where D1–D5 stop being documents. Three of them needed
correcting on contact with the real schema (`22-RESEARCH.md` Summary), and the
corrections are folded into the proposals below.

---

## What this phase inherits

**From Phase 21**, the authority is `docs/api-ingestion-jobs.md#the-phase-22-hand-off`.
Where each item lands in the proposed split:

| Hand-off item | Plan |
|---|---|
| Register `full_ingest` and `incremental` | 22-05 |
| `DATABASE_URL` for the compose `workers` service | 22-05 |
| Set `max_job_duration` | 22-05 (initial), 22.1-05 (from measurements) |
| Write handlers against the three endings | 22-05 |
| Measure the pool | 22.1-05 |
| Wire the pipeline to runs (`resolve_ingestion_run` + `attach_ingestion_run`) | 22-05 |
| Idempotent chunk writes per run (ISS-027) | 22-05 (full), 22.1-02 (incremental) |
| `chunks.organization_id` under D5 | 22-02 |
| Distinguish `incremental` from `full_ingest` | 22.1-02 |
| `statement_timeout` on the heartbeat connection | 22-05 |
| A way to find a repository's job id (ISS-034) | 22.1-03 |
| Re-parent drift | **not re-opened** — 21-07 ruled it settled, and P3's composite key makes a chunk's tenant unable to drift at all |

**From `DECISIONS.md`**, every "Verification required in Phase 22" item:

| Decision | Verification | Plan |
|---|---|---|
| D1 | re-ingesting an unchanged commit gives identical `symbol_id`s | 22.1-01 |
| D1 | adding an unrelated line does not change ids below it | 22.1-01 |
| D1 | a re-export resolves to its leaf's id | **22.1-04** (moved — see P8) |
| D2 | a multi-tenant recall test against exact search | 22.1-05 |
| D2 | `EXPLAIN` shows `Subplans Removed` | 22-02 |
| D3 | a traversal over a cyclic fixture terminates | 22.1-04 |
| D3 | tier 2 upgrades tier 1 in place; tier 1 never downgrades tier 2 | 22-02 (the SQL rule, tested before tier 2 exists) |
| D5 | cross-organization re-parent rejected | already built (000013, 21-01) |
| D5 | a child row whose tenant disagrees with its repository is rejected at write | 22-02 |
| D5 | a trigger-disabled bulk load does not leave drift, or is documented forbidden | 22-02 |
| D5 | a drift-detection query runs in CI | 22-02 |
| `REWORK.md` open item | `chunks.symbol_id`: one FK or a join table | P7 |

---

## Proposed decisions

Each is **PROPOSED**. The reasoning is short here and long in `22-RESEARCH.md`.

### P1 — Drop and recreate `chunks`; re-ingest from source. No data migration.

Every existing row is harness or benchmark data, re-creatable from pinned
sources; no user data exists anywhere; and the existing rows' vectors live only
in Qdrant, so D2's `embedding NOT NULL` could not be met by moving them anyway.
Re-embedding everything costs about $0.09 and 3.5 minutes (RESEARCH Q1, Q4).
The migration therefore carries **no DML**.

### P2 — Every partition gets row-level security of its own.

`ENABLE` and `FORCE ROW LEVEL SECURITY` and the `tenant_isolation` policy on
**each of the 64 partitions**, not only the parent. Without it the app role read
and **overwrote** another tenant's rows through the partition directly (RESEARCH
Summary, item 1). Guarded by two tests: every partition carries RLS, FORCE and
the policy; and a direct cross-tenant read and write through a partition returns
nothing. `trg_assert_tenant` on the parent is inherited by the partitions
(measured: 64 of 64) and stays.

### P3 — Tenancy on `chunks`, `symbols` and `symbol_edges` by composite foreign key.

`FOREIGN KEY (repository_id, organization_id) REFERENCES repositories (id, organization_id)`
— the 21-01/21-02 pattern, which makes a misfiled row unrepresentable rather
than rejected (measured: the misfiled insert fails on the key). A `BEFORE INSERT`
trigger stays optional, for a readable message, exactly as 21-02 kept one. This
refines D5's "maintained by trigger": the codebase's own later precedent is
stronger. The drift query joins through `repositories` and runs in CI.

### P4 — Record the embedding model on every chunk.

`chunks.embedding_model TEXT NOT NULL`, written from the generator's model, and a
retriever that refuses to compare a query embedded with one model against rows
embedded with another. Two models at the same dimension produce vectors in
different spaces, and mixing them fails silently. This is what lets the storage
migration keep **ada-002** and the model question be decided on its own
(RESEARCH Q3, question U3).

### P5 — `hnsw.iterative_scan = relaxed_order` is load-bearing, with a re-sort.

Every product query filters by repository inside the tenant's partition, and
without iterative scan that filter produced short results in 16/20 and 20/20
queries (RESEARCH Q5). Set it per transaction in the vector leg, re-sort the
candidates by exact distance, and pass the query vector as a bound parameter so
the HNSW index is eligible. This corrects `DECISIONS.md` §5's framing that
iterative scan "is not the fix".

### P6 — Both retrieval legs in Postgres under one tenant scope; fusion stays in Python.

The vector leg becomes SQL under `require_tenant`; the keyword leg drops its
latest-run filter (ISS-027); RRF and the booster keep running in Python so the
migration changes **storage and nothing else**. A one-statement hybrid query was
measured feasible under RLS with both legs pruned, and is recorded as a later
latency option — moving fusion into SQL changes tie-breaking, which is a ranking
change and belongs to the protocol.

### P7 — `symbols` stays unpartitioned; `chunks.symbol_id` stays one nullable FK.

Partitioning `symbols` would turn every foreign key into it — from `chunks`,
`symbol_edges` and D4's `memory_anchors` — into a composite key, the same break
measured for `retrievals`. `symbols` carries no vector index, so D2's runway
argument does not apply to it. One nullable FK suffices because chunking is
symbol-aligned where it matters: function and class chunks map one-to-one;
`class_summary` → its class; `file_summary` and fixed-size chunks → the file's
`module` symbol (P9), or `NULL`; a large function split into several chunks is
many-to-one, which a chunk-side FK already supports. Closes `REWORK.md`'s open
item by stating the limitation: a chunk spanning several symbols points at the
module.

### P8 — Mint symbol identities only at definition sites; never for an alias.

A Go `type A = B`, a TypeScript `export { X as Y }` and a Python `Y = X` are
recorded as `imports` edge candidates, not as symbols. With that rule a re-export
cannot create a second identity, so **D1 emission does not wait for the D3
resolver** — a refinement of D1's "cannot be sequenced apart". Resolving a *name*
to its leaf still needs the import graph, so D1's third verification criterion
moves to the D3 plan (RESEARCH Q6).

Also part of D1, decided with the chunker work in 22.1-01:

- **`symbol_path` is the full ancestor chain**, never the four-level display
  breadcrumb (`metadata_builder.py:80-82`);
- **`kind`** maps from the parse: `function`, `method`, `class`, `type`, `const`,
  `module`;
- **`ordinal`** covers the four measured collision shapes, `@typing.overload`
  added to D1's three;
- **the span includes decorators and leading doc comments**, so `span_digest`
  sees a route decorator or a doc comment change (measured: both are outside the
  span today) — [open for execution] whether a doc-comment-only change should
  mark an anchored memory `stale`;
- **Go non-struct types and package-level constants become symbols without
  becoming chunks**, so identity is complete without changing ranking;
- **one `module` symbol per file**, the `from_symbol_id` for top-level code.

### P9 — D3: omit `edge_kind` from the tier-2 reconciliation.

SCIP has no notion of a call (verified in `scip.proto`), so tier 1's `calls`
and SCIP's classifications would disagree every time, not occasionally. Tier 1
keeps its vocabulary and emits `calls` and `imports` first; tier 2 retires every
unresolved tier-1 edge for `(organization_id, from_symbol_id, to_symbol_name)`
in the transaction that inserts its own. Every graph query joins `symbols` and
excludes archived rows (D1's stated requirement). Traversal uses `CYCLE`.

### P10 — Fetch through GitHub's tarball API, with a repository-scoped read-only token.

No `git` in the worker image (measured: `python:3.11-slim` has none), the token
in one request header, extraction with `tarfile`'s `data` filter, symlinks never
followed, caps checked while streaming, a per-job temporary directory removed in
`finally` and swept at worker start. The roadmap's "respect `.gitignore`" is
replaced by filters for vendored, generated, binary, oversized and secret-looking
files (U6, U7), because an archive holds only tracked files. Measure on the
largest benchmark repository before relying on it: GitHub documents no size
limit.

### P11 — Incremental by content-addressed file manifest, not by commit diff.

Per repository: `(file_path, content_sha256, indexer_version)`. Each job hashes
the current tree, re-parses only added and changed files, deletes the chunks of
changed and removed files, inserts the new ones, upserts-and-unarchives present
symbols and archives vanished ones — all in `complete()`'s transaction.
`full_ingest` is the same algorithm ignoring the manifest. It survives
force-pushes and missed webhooks, which a commit diff (GitHub's compare API caps
at 300 files) does not, and `indexer_version` lets a chunker fix roll out file
by file after launch. Embeddings for unchanged chunk text are reused, keyed on
the hash of the **embedded text and the model** (RESEARCH Q3, Q9).

### P12 — Progress by polling the job row; ISS-034 in the same plan.

The row already carries `last_stage`, `progress` and a computed `stalled`; a UI
polls it every few seconds. SSE, Redis pub/sub and a streaming endpoint are
deferred. The roadmap's v2 breadcrumb — a future graph worker "subscribing" to
chunk events — is served better by what D3 persists in tables than by an
ephemeral stream (RESEARCH Q10, question U8).

### P13 — The seeded-migration CI check lands first (ISS-031).

In the plan that swaps the image, before the storage migration. The compose
database is itself a seeded database at migration 10, so taking it to 16 is the
exact scenario ISS-031 describes (RESEARCH Q13).

### P14 — `pgvector/pgvector:pg16` everywhere, and rename the harness's reuse container.

Compose, the Go harness, the Python conftest and `backend-ci.yml`'s service.
The Go harness reuses its container by name without checking the image, so the
name must change with it — `rag-doc-isolation-tests-pgv16` — or every developer
machine fails on the first `CREATE EXTENSION`. Pin the image by digest in CI.
Document `--shm-size` for large index builds.

### P15 — Qdrant leaves in the storage plans, not after.

`qdrant_writer.py`, the Qdrant path of `vector_retriever.py`, the compose
service, `QDRANT_URL` in `api/main.py`, the harness's Qdrant-based clear and
state checks, `qdrant-client`, and `pkg/vectordb` with its `go.mod` dependency
(K2 — dead code regardless). Keeping both stores for any stretch is the
consistency hazard D2 exists to remove.

### P16 — Initial operating numbers, to be replaced by measurements.

`max_job_duration` **2 hours** (about three times the extrapolated end-to-end
time for a 50,000-chunk repository, under twice U6's proposed 100,000-chunk
cap, and sixty times the largest benchmark). If U6 lands higher than that cap,
this number moves with it.
Heartbeat `statement_timeout` **15 seconds** (a quarter of the beat interval),
with a test that blocks a beat on the job row's lock. **Two** worker processes
(four connections) until 22.1-05 measures. All three are revisited in 22.1-05.

---

## The proposed split

### Recommended: a vertical first slice (Option A)

The dependency analysis (RESEARCH Q1) says only storage has to come before the
pipeline — the handler writes chunks and vectors in the completion transaction,
which pgvector allows and Qdrant does not. Symbol emission, incremental ingest,
progress and the graph resolver are not on the path to a first real repository.
So the first phase ends with one real repository indexed end to end, and the
second builds the substrate onto a pipeline already proven.

#### Phase 22 — pgvector storage and the first real repository (~57–80 h)

| Plan | Scope | Est. |
|---|---|---|
| **22-01** | **pgvector everywhere, and the seeded-migration gate.** Image swap in compose, both harnesses (reuse container renamed) and CI, pinned by digest; ISS-031's seeded-database migration check in `backend-ci.yml` (seed fixture covering every tenant table, applied at N−1, then `up`, then assertions); `--shm-size` documented. | 5–8 h |
| **22-02** | **The storage migration.** `CREATE EXTENSION vector`; the `retrievals` FK per U9; `chunks` dropped and recreated partitioned by `HASH (organization_id)` `MODULUS 64` with `organization_id`, the composite tenant key (P3), `embedding vector(1536)`, `embedding_model` (P4), nullable `symbol_id`; RLS, FORCE and the policy on the parent **and all 64 partitions** (P2); `trg_assert_tenant`; indexes (HNSW, `organization_id`, `(repository_id, file_path)`, `content_hash`, keyword GIN with the breadcrumb expression matching the query); `symbols` (D1, with `archived_at`, RLS, trigger, composite key) and `symbol_edges` (D3, likewise); tests: partition-RLS guard, direct cross-tenant partition read and write refused, `Subplans Removed`, misfiled row rejected, drift query in CI, the D3 upgrade/no-downgrade SQL rule; `protectedTables` updated; `pkg/vectordb` deleted. | 12–16 h |
| **22-03** | **Pipeline and retrieval on pgvector; Qdrant retired.** The writer takes a caller's cursor and writes chunk + vector rows (the `write_results` shape); every chunk gets its vector (the duplicate-hash gap closes); the vector leg becomes SQL (P5, P6); the keyword leg drops the latest-run filter; Qdrant removed from code, compose, API and harness (P15); the vector-leg isolation test that could never be written against Qdrant; **the equivalence check** — re-ingest the three corpora on pgvector with ada-002 and explain every rank that differs from the Qdrant-era run. | 14–20 h |
| **22-04** | **Fetching a repository safely.** Per U4: the token broker (an internal-only backend route minting a one-hour, one-repository, `contents: read` token for the holder of a live lease) or the key mounted into the worker; the tarball fetcher (P10) with caps (U6) and filters (U7); hostile-archive fixtures (traversal, symlink out, oversized file, bomb); a redaction test pushing a realistic fetch failure through `sanitize_error`; temporary-directory lifecycle. | 14–20 h |
| **22-05** | **The `full_ingest` handler and the worker switched on.** Stages `fetch → parse → embed → store` reported through `report_progress`; run resolved and attached; `write_results` deletes the repository's chunks and inserts, in `complete()`'s transaction; `Unfinished` only on shutdown; `REGISTRY` filled (both keys, `incremental` running the full path until 22.1-02); compose `workers` gets `DATABASE_URL`, the OpenAI key and the broker address; P16's numbers; an end-to-end test through the real worker against a fake GitHub serving an archive; **then a real repository connected through the development App, indexed, and answered from `/api/search`.** | 12–16 h |

**Phase 22 ends with a real GitHub repository indexed end to end** — connect →
queue → worker → pgvector → search — under tenant isolation on both legs.

#### Phase 22.1 — Symbols, incremental updates, progress and the code graph (~60–88 h)

| Plan | Scope | Est. |
|---|---|---|
| **22.1-01** | **D1 symbol identity from the chunker** (P8): full-chain `symbol_path`, `kind`, `ordinal`, span with decorators and doc comments, `span_digest`, `module` symbols, Go non-struct types and constants as symbols, alias rule; upsert-and-unarchive; chunks linked to symbols; D1's first two verification criteria. Python and Go; TypeScript waits for U10. | 14–20 h |
| **22.1-02** | **Incremental ingestion** (P11): the file manifest (a small migration), per-file delete-and-insert, symbol archival, embedding reuse, `incremental` distinct from `full_ingest`; tests for a force-push, a missed push and a file deleted and restored (D1's resurrection path). **Closes ISS-027.** | 12–16 h |
| **22.1-03** | **Progress contract and ISS-034** (P12): a documented `progress` schema; the repository's current or last job reachable from the repository API (ISS-034's two shapes — decided in the plan); a deliberately written, mutation-checked isolation test, since `ingestion_jobs` has no RLS and the CI gate ignores `GET`s. | 6–10 h |
| **22.1-04** | **D3 tier 1** (P9): call-site and import candidates from the parser; the resolver (imports plus scope matching) writing `symbol_edges` with `to_symbol_name` always set; the reconciliation rule; a `CYCLE`-safe traversal helper and a cyclic fixture; archived symbols excluded; D1's re-export criterion. | 20–30 h |
| **22.1-05** | **D2's recall test and the operating numbers.** A multi-tenant, multi-repository recall test seeded with the benchmark corpora's **real** embeddings copied into synthetic tenants — at least one tenant large enough that the plan is HNSW (asserted), repositories filtered inside a partition, a partition shared by several tenants, exact baseline in the same scope, assertions on recall **and** on short results; per-stage ingest timings over the three corpora and one large public repository, full and incremental; pool size, `max_job_duration` and the OpenAI throughput ceiling set from them. | 8–12 h |

### The alternative: the expected shape, foundation first (Option B)

Phase 22 = 22-01, 22-02, 22-03 **and 22.1-01** (every stored shape decided
before any pipeline code); Phase 22.1 = fetch, the handler, incremental,
progress, tier 1, the recall test.

| | A — vertical (recommended) | B — foundation first |
|---|---|---|
| First real repository indexed | end of Phase 22 | second plan of Phase 22.1 — **~14–20 h later** |
| Rework | `write_results` touched again in 22.1-01 and 22.1-02: **~2–4 h** | none |
| Real rows without symbol ids | yes, briefly; re-ingested in 22.1 for cents | never |
| Where the risk surfaces | fetch, the worker's first real run and pgvector at scale surface first — the things never exercised end to end | identity work (well measured already, RESEARCH Q6) comes before the untested parts |

Both build the same things in the same total time. A spends a few hours of
rework to find out sooner whether the untested half works; B spends nothing on
rework and finds out later. Question U1.

### What can run in parallel

With one worker in the fleet, parallelism means **order flexibility**, not
simultaneous work. Three tracks do not depend on each other and can be taken in
whatever order review throughput allows: **storage** (22-02 → 22-03), **fetch**
(22-04) and **identity** (22.1-01, offline parser work that can start as soon as
22-01 lands). They converge at 22-05 and 22.1-02.

### The retrieval-quality track (protocol-gated, placement is U10)

Not a phase of its own in the recommendation unless the user wants one. Each
item is a decision under `boost-defaults-protocol.md`'s method — rule committed
before fresh blind questions exist — in this order, so each is measured on the
chunks and vectors it will ship with:

| Decision | Needs first | Est. |
|---|---|---|
| Chunker: ISS-026 (class chunks without method bodies) | 22-03's equivalence check | 8–12 h |
| Chunker: TypeScript grammar, **after adding a TS/JS benchmark corpus** — the benchmark has none | a TS corpus with blind questions | 10–16 h |
| Embedding model (U3) | the chunker decisions | 6–10 h |
| Ranking: ISS-024, 025, 028, 029 | the model decision | 12–20 h |

---

## Open questions for the user

Plain-language versions; the evidence is in `22-RESEARCH.md`.

### U1 — Which order: prove it end to end first, or lay every foundation first?

Think of it as building a house: either put up one finished room first to prove
the plumbing and wiring work, then build the rest; or pour every foundation
before any room goes up.

- **A — vertical first (recommended).** One real repository indexed end to end
  after ~57–80 h. Costs ~2–4 h of rework later. The parts that have never run for
  real (fetching, the worker's first job, pgvector with real data) fail early if
  they are going to fail.
- **B — foundation first.** Every stored shape decided before any pipeline code.
  No rework; the first real ingest arrives ~14–20 h later.

*Why A:* the untested half is the risky half, and the "every decision before the
first row" argument assumes re-ingesting is expensive — it is about $0.09 and
four minutes until launch.

### U2 — What to call the second half

- **A — Phase 22 and Phase 22.1 (recommended).** Nothing renumbers. ISS-034,
  `docs/api-ingestion-jobs.md` and the roadmap all say "Phase 23" for the
  frontend and keep meaning it.
- **B — renumber** 23 → 24 onward. Cleaner numbers; every existing "Phase 23"
  reference has to be found and changed (~1 h, and easy to miss one).

### U3 — The embedding model

Today every vector comes from ada-002; the design comment says
text-embedding-3-small. Same size, but they "speak different languages" — you
cannot mix them, and swapping is a retrieval change, which your rule says must be
decided on the benchmark.

- **A — keep ada-002 through the storage move, then decide 3-small under the
  protocol (recommended).** The storage change is measured on its own first.
  Cost: ~6–10 h, almost all of it writing 30 fresh blind questions, plus under
  $0.10 of embeddings. Needs you to fix the pass/fail rule before the questions
  are written.
- **B — switch during the storage move.** Saves one re-ingest (~4 minutes), but
  two changes land together and neither can be measured alone — and it skips the
  protocol.
- **C — stay on ada-002.** Nothing to do now. It costs five times as much per
  token ($0.10 vs $0.02 per million) and OpenAI labels it an older model.

*Either way,* P4 records the model on every row so a mix-up is refused rather
than silent.

### U4 — Where the GitHub App's private key lives

The key is a master key: it can open every customer's repository. The worker
needs to read one repository at a time.

- **A — the backend keeps the key and hands the worker a one-hour, one-repository,
  read-only token (recommended).** Like a hotel front desk issuing a key card for
  one room for one night. ~6–8 h more than B; adds an internal-only route that
  Phase 24's deployment has to keep private. The worker proves it is working on
  that repository with its job lease; no new shared secret.
- **B — mount the key into the worker too.** ~3–4 h; two new Python packages. Every
  worker process — the part that parses untrusted customer code — then holds the
  master key.

*Why A:* the worker is exactly the process a hostile repository gets to talk to.

### U5 — Git clone or GitHub's download-archive API

Proposed as P10 (the archive API) — listed here because it reverses the
roadmap's "shallow clone" and you may prefer to overrule it.

- **A — archive API (recommended).** No `git` in the worker image, no git attack
  surface, token in one header. Unknown: how GitHub behaves on very large
  repositories; the plan measures it first.
- **B — `git` clone.** Adds `git` to the image and its clone-time CVE history;
  more control over very large repositories.

Equal effort (~2 h difference either way).

### U6 — How big a repository v1 accepts

Nothing today stops a 5 GB monorepo from being queued. Proposed caps, as a
starting point: **archive ≤ 500 MB, ≤ 20,000 indexable files, ≤ 1 MB per file,
≤ 100,000 chunks.** Above a hard cap the job ends `dead` with a plain reason;
oversized single files are skipped and counted.

- **A — those numbers (recommended)**, revisited after 22.1-05 measures real
  ingests. A 100,000-chunk repository is roughly an hour of ingest and ~1.6 GB of
  storage.
- **B — smaller**, e.g. 200 MB / 25,000 chunks: faster, cheaper, turns away some
  real customers.
- **C — no caps in v1.** One large repository can hold a worker for hours and
  fill the disk.

This is a product call as much as a technical one.

### U7 — Secret-looking files inside customer repositories

People commit `.env` files and keys. Indexing them sends them to OpenAI and makes
them searchable by everyone in the organization.

- **A — skip a deny-list of obvious secret files** (`.env*`, `*.pem`, `*.key`,
  `id_rsa*`, and similar) **(recommended).** ~1–2 h. Misses secrets pasted into
  ordinary source files.
- **B — A plus scanning file contents for secret patterns.** ~6–10 h; catches
  more, with false positives to tune.
- **C — index everything.** No work; the exposure above.

### U8 — Live progress: polling or streaming

- **A — the page asks every few seconds (recommended).** The data already exists
  on the job row; ~0 h beyond ISS-034's 4–6 h.
- **B — server push (SSE), as the roadmap sketched.** Smoother, ~10–16 h more:
  a message bus, a long-lived endpoint, reconnect logic and its own isolation
  test. Can be added later without changing what the page receives.

### U9 — The link between search logs and chunks

`retrievals` (which search result was shown) points at a chunk, and `feedback`
hangs off `retrievals`. Partitioning makes that link impossible as written, and
incremental updates would delete the feedback every time a file changed. Nothing
writes either table today (0 rows; no code path).

- **A — drop the link now; decide properly when feedback ships (recommended).**
  ~0.5 h. Logged results keep a chunk id that may later point at nothing.
- **B — point results at the symbol instead of the chunk.** ~2–3 h; survives
  re-indexing; feedback then follows the function, not the text snapshot.
- **C — keep a link with `organization_id` added to `retrievals`.** ~3–4 h;
  still deletes feedback when a file changes unless it nulls instead.

### U10 — When the retrieval-quality decisions happen

The chunker issues (duplicate class chunks, TypeScript barely parsing), the
embedding model and the ranking fixes each need a protocol run with fresh blind
questions. None blocks indexing; all affect answer quality.

- **A — a protocol-gated track after 22-03 and before Phase 23 (recommended)**,
  ~36–58 h in total, including adding a TypeScript benchmark corpus. The frontend
  then shows answers from the chunker and model we mean to launch with.
- **B — after launch.** Phase 23 sooner; the quality gate `DESIGN.md` §9 calls
  "the real gate" slips past launch.
- **C — only the chunker and model now, ranking later.** ~24–38 h now.

---

## Boundaries

**In scope:** everything in the two plan tables; the storage migration and its
tests; retiring Qdrant; fetch, ingest, incremental, progress; D1's identity, D3's
tier 1 and D2's recall test; the operating numbers.

**Not in scope:**

- **Retrieval-quality changes** — the chunker, the model and ranking are decided
  under the protocol (U10), not inside these plans. The storage migration's
  equivalence check is not a quality decision and is in scope.
- **SCIP tier 2** — customer CI upload, later (K3).
- **D4's `memories` and `memory_anchors` tables** — later; only `span_digest`
  is needed now, and it is in 22.1-01.
- **ISS-023** (retrying a `dead` repository through the API) — stays with Phase 23.
- **ISS-021** (the semantic cache) — unchanged; when it is repaired, its key must
  include the embedding model (P4's logic applies to cached query vectors too).
- **Single-statement hybrid fusion** — measured feasible, deferred (P6).
- **SSE** — deferred (P12, U8).

---

## Corrections to the roadmap sketch

1. **"Research: Unlikely"** — D1–D5 all land here and three needed correcting.
2. **22-01 "shallow clone … respect `.gitignore`"** — an archive (P10); `.gitignore`
   governs untracked files, so the rule protects nothing; tracked vendored,
   generated, binary and secret files are what need filtering.
3. **22-02 "Postgres + Qdrant writers"** — one store, one transaction (P15).
4. **22-03 "diff previous commit vs new HEAD … delete removed files"** — a content
   manifest (P11); removed files' chunks are deleted and their **symbols archived**.
5. **22-04 "Redis pub/sub … SSE"** — polling (P12).
6. **Phase 24** — drop "Qdrant persistence and snapshot strategy" and 24-04's
   Qdrant snapshots; add pgvector availability on the chosen host,
   `CREATE EXTENSION` privilege for the migration role, and container
   `--shm-size`.
7. **Phase 25-03** — drop "Qdrant down" from the runbook.

---

## Open questions for execution

Technical, for the plans to settle; none needs the user.

- **Doc-comment-only changes and staleness.** P8 puts doc comments in the span,
  so editing a comment changes `span_digest`. Whether that should make an anchored
  memory `stale` is a D4 question; the span rule should be written so either
  answer is possible.
- **The tarball redirect URL.** Its private-repository link carries a short-lived
  credential of its own [not verified]; confirm its shape and make sure nothing
  logs it.
- **`CREATE EXTENSION IF NOT EXISTS` on a host where the migration role is not a
  superuser** — confirm on the Phase 24 host whether it skips the privilege check
  when the extension already exists.
- **The breadcrumb GIN index.** The query's `COALESCE(breadcrumb, '')` does not
  match the index expression; make them agree in 22-02 and prove it with
  `EXPLAIN`.
- **OpenAI throughput.** The key's tokens-per-minute limit may cap the pool
  before Postgres does; 22.1-05 measures it.
- **Re-sorting under `relaxed_order`.** Choose between a materialized CTE and an
  over-fetch-then-sort in Python; test that the final order is by exact distance.

---

## Proposed roadmap change

For the user to approve before `ROADMAP.md` is edited. Written as it would
appear there under Option A of U1 and U2.

> ### Phase 22: pgvector Storage & the First Real Repository
>
> **Goal:** Move vectors into Postgres under tenant isolation, retire Qdrant, and
> index one real GitHub repository end to end through the Phase 21 queue.
> **Depends on:** Phase 21
> **Research:** Complete — `22-RESEARCH.md`; decisions proposed in `22-CONTEXT.md`
> **Plans:** 5
>
> - [ ] 22-01: pgvector image everywhere (compose, both harnesses with the reuse container renamed, CI) and ISS-031's seeded-migration gate
> - [ ] 22-02: the storage migration — partitioned `chunks` with per-partition RLS and a composite tenant key, `embedding_model`, `symbols`, `symbol_edges`; `pkg/vectordb` deleted
> - [ ] 22-03: pipeline and both retrieval legs on pgvector, iterative scan, Qdrant removed, the benchmark equivalence check
> - [ ] 22-04: fetching a repository safely — scoped installation tokens, the archive fetcher, caps, filters, hostile-archive tests
> - [ ] 22-05: the `full_ingest` handler, the worker switched on, a real repository indexed and searchable
>
> ### Phase 22.1: Symbols, Incremental Updates, Progress & the Code Graph
>
> **Goal:** Stable symbol identity, push-driven incremental ingest, a progress
> contract a UI can poll, tier-1 graph edges, and D2's recall test.
> **Depends on:** Phase 22
> **Plans:** 5
>
> - [ ] 22.1-01: D1 symbol identity from the chunker
> - [ ] 22.1-02: incremental ingestion by file manifest; closes ISS-027
> - [ ] 22.1-03: progress contract and ISS-034, with a deliberate isolation test
> - [ ] 22.1-04: D3 tier 1 — candidates, resolver, reconciliation, cycle-safe traversal
> - [ ] 22.1-05: the multi-tenant recall test and the measured operating numbers
>
> **Retrieval-quality track (U10):** chunker (ISS-026; TypeScript after a TS
> corpus exists), embedding model, ranking (ISS-024/025/028/029) — each decided
> under `boost-defaults-protocol.md`'s method.
>
> **Phase 24 research topics:** replace "Qdrant persistence and snapshot
> strategy" with "pgvector availability and `CREATE EXTENSION` privilege on the
> chosen host; container `--shm-size` for index builds". 24-04: drop the Qdrant
> snapshot schedule. **Phase 25-03:** drop "Qdrant down".
