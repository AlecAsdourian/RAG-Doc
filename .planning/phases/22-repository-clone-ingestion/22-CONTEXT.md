# Phase 22: Repository Clone → Ingestion Orchestration — Context

**Written:** 2026-09-17 as a research and scoping pass (PR #45).
**Locked:** 2026-09-17. The user answered all ten open questions (U1–U10) and
took every recommendation. See [The user's answers](#the-users-answers-confirmed-2026-09-17).
**Research:** `22-RESEARCH.md` in this directory. Every claim below points at the
question there that carries its evidence, and that evidence is tagged measured,
read, verified source or inferred.
**Corrections to `.planning/v2-substrate/DECISIONS.md`:** eight, dated
2026-09-17, written inline beside the text each one corrects. Each cites
`22-RESEARCH.md`.

> **How to read the status markers.**
> **LOCKED 2026-09-17, U*n*** means settled by the user's answer to that question.
> **LOCKED 2026-09-17, correction to D*n*** means recorded as a dated correction
> in `DECISIONS.md`, at the direction that came with the answers. It was **not**
> a U question. It is marked this way so that the difference stays visible.
> **PROPOSED** means no answer covered it. Each one names who decides it and when.
>
> Do not re-litigate a LOCKED decision during execution without cause. If a
> plan finds one impossible against the real system, revise the PLAN with a
> REVISION NOTICE (the 19-03 pattern). Do not improvise.

## Objective

Turn the queue Phase 21 built into indexed repositories. That means fetching a
connected repository, then parsing, embedding and storing it on the storage
`DECISIONS.md` settled: pgvector, partitioned `chunks`, symbol identity, graph
edges and key-guaranteed tenancy. After that, keep it current on every push,
and report progress a UI can read.

This is also the phase where D1–D5 stop being documents. Three of them needed
correcting on contact with the real schema (`22-RESEARCH.md` Summary). The
corrections are in `DECISIONS.md` and folded into the decisions below.

**The work is split across two phases (U1, U2).** Phase 22 ends with one real
repository indexed and searchable end to end. Phase 22.1 builds the foundations
that do not block that: symbols, incremental updates, progress and the code
graph. Retrieval-quality decisions run as a protocol-gated track after 22-03
and before Phase 23 (U10).

---

## The user's answers (confirmed 2026-09-17)

The questions below are recorded as they were put, with their options and
costs, because the reasoning is what a later reader will want to challenge.
Each answer names what it locks. The user took the recommendation every time.

### U1 — Order: prove it end to end first, or lay every foundation first · CONFIRMED: A

**Decision: end to end first.** Phase 22 ends with a real repository indexed and
searchable. The foundations that do not block that move to Phase 22.1.
**Locks:** [the split](#the-split--locked-2026-09-17-u1), P1, P13, and the
timing half of P14 and P15.

Think of building a house. You can put up one finished room first to prove the
plumbing and wiring work, then build the rest. Or you can pour every foundation
before any room goes up.

- **A — vertical first (chosen).** One real repository is indexed end to end
  after ~57–80 h. It costs ~2–4 h of rework later. The parts that have never run
  for real (fetching, the worker's first job, pgvector with real data) fail early
  if they are going to fail.
- **B — foundation first.** Every stored shape is decided before any pipeline
  code. No rework, but the first real ingest arrives ~14–20 h later.

*Why A:* the untested half is the risky half. The "every decision before the
first row" argument assumes re-ingesting is expensive. Until launch it costs
about $0.09 and four minutes (`DECISIONS.md` now carries that as a correction).

### U2 — What to call the second half · CONFIRMED: A

**Decision: Phase 22 and Phase 22.1.** Nothing is renumbered. ISS-034,
`docs/api-ingestion-jobs.md` and the roadmap all say "Phase 23" for the frontend,
and they keep meaning it.

- **A — 22 and 22.1 (chosen).**
- **B — renumber 23 → 24 onward.** Cleaner numbers, but every existing
  "Phase 23" reference would have to be found and changed (~1 h, and easy to
  miss one).

### U3 — The embedding model · CONFIRMED: A

**Decision: keep `text-embedding-ada-002` through the storage move.**
`text-embedding-3-small` is decided later on the benchmark, in the
retrieval-quality track. The pass rule is **committed by the user before the
deciding questions are written.** Every chunk records `embedding_model`, so the
two changes stay separable.
**Locks:** P4, and together with U10, P6.

Every vector today comes from ada-002; the design comment says
text-embedding-3-small. The two produce vectors of the same size, but they
"speak different languages": you cannot mix them, and swapping one for the other
is a retrieval change, which the user's rule says must be decided on the
benchmark.

- **A — keep ada-002, then decide 3-small under the protocol (chosen).** The
  storage change is measured on its own first. Cost: ~6–10 h, almost all of it
  writing 30 fresh blind questions, plus under $0.10 of embeddings.
- **B — switch during the storage move.** It saves one re-ingest (~4 minutes),
  but two changes land together and neither can be measured alone. It also
  skips the protocol.
- **C — stay on ada-002 indefinitely.** Nothing to do now. It costs five times as
  much per token ($0.10 vs $0.02 per million), and OpenAI labels it an older
  model.

### U4 — Where the GitHub App's private key lives · CONFIRMED: A

**Decision: the backend keeps the key.** The worker receives a **one-hour,
one-repository, read-only** installation token, checked against the job's
lease. The key never enters the process that parses untrusted code.
**Locks:** P10 (its token half).

The key is a master key: it can open every customer's repository. The worker
needs to read one repository at a time.

- **A — the backend mints a scoped token for the worker (chosen).** Like a hotel
  front desk issuing a key card for one room for one night. It takes ~6–8 h more
  than B, and adds an internal-only route that Phase 24's deployment must keep
  private. The worker proves it is working on that repository with its job
  lease, so no new shared secret is needed.
- **B — mount the key into the worker too.** ~3–4 h and two new Python packages.
  But every worker process, which is exactly the part that parses untrusted
  customer code, would then hold the master key.

### U5 — Git clone or GitHub's archive API · CONFIRMED: A

**Decision: fetch the repository as an archive through the GitHub API, not
`git clone`.** **Locks:** P10 (its fetch half).

- **A — archive API (chosen).** No `git` in the worker image (measured: it has
  none), no git attack surface, and the token goes in one header. Unknown: how
  GitHub behaves on very large repositories, which 22-04 measures first.
- **B — `git` clone.** Adds `git` to the image, along with git's history of
  clone-time vulnerabilities. It gives more control over very large repositories.

The two are equal effort (~2 h difference either way).

### U6 — How big a repository v1 accepts · CONFIRMED: A

**Decision:** archive **≤ 500 MB**, **≤ 20,000** indexable files, **≤ 1 MB** per
file, **≤ 100,000** chunks. Above a hard cap the job ends `dead` with a plain
reason. Oversized single files are skipped and counted. The caps are revisited
after 22.1-05 measures real ingests.
**Locks:** P10 (its caps).

- **A — those numbers (chosen).** A 100,000-chunk repository is roughly an hour
  of ingest and ~1.6 GB of storage.
- **B — smaller**, e.g. 200 MB / 25,000 chunks. Faster and cheaper, but it turns
  away some real customers.
- **C — no caps in v1.** One large repository could hold a worker for hours and
  fill the disk.

### U7 — Secret-looking files inside customer repositories · CONFIRMED: A

**Decision: skip a deny-list of secret-looking files** (`.env*`, `*.pem`,
`*.key`, `id_rsa*` and similar), so they are neither sent to OpenAI nor made
searchable. **Locks:** P10 (its filters).

- **A — deny-list by file name (chosen).** ~1–2 h. It misses secrets pasted into
  ordinary source files.
- **B — A plus scanning file contents for secret patterns.** ~6–10 h. It catches
  more, with false positives to tune.
- **C — index everything.** No work, but it sends every committed secret to
  OpenAI and makes it searchable by everyone in the organization.

### U8 — Live progress: polling or streaming · CONFIRMED: A

**Decision: the page polls the job row for v1.** There is no server push.
**Locks:** P12.

- **A — the page asks every few seconds (chosen).** The data already exists on
  the job row. It costs ~0 h beyond ISS-034's 4–6 h.
- **B — server push (SSE), as the roadmap sketched.** Smoother, but ~10–16 h
  more: a message bus, a long-lived endpoint, reconnect logic and its own
  isolation test. It can be added later without changing what the page receives.

### U9 — The link between search logs and chunks · CONFIRMED: A

**Decision: drop the foreign key from `retrievals` to `chunks` now.** Decide the
shape when feedback ships. **Locks:** P17.

`retrievals` (which search result was shown) points at a chunk, and `feedback`
hangs off `retrievals`. Partitioning makes that link impossible as written
(measured). Incremental updates would also delete the feedback every time a
file changed. Nothing writes either table today (0 rows, and no code path does).

- **A — drop the link now (chosen).** ~0.5 h. A logged result keeps a chunk id
  that may later point at nothing.
- **B — point results at the symbol instead of the chunk.** ~2–3 h. It survives
  re-indexing, and feedback then follows the function rather than a snapshot of
  its text.
- **C — keep a link and add `organization_id` to `retrievals`.** ~3–4 h. It still
  deletes feedback when a file changes, unless it nulls instead.

### U10 — When the retrieval-quality decisions happen · CONFIRMED: A

**Decision: a protocol-gated track after 22-03 and before Phase 23.** It includes
adding a TypeScript benchmark corpus. **Locks:**
[the retrieval-quality track](#the-retrieval-quality-track--locked-2026-09-17-u10),
and together with U3, P6.

Three things each need a protocol run with fresh blind questions: the chunker
issues (duplicate class chunks, TypeScript barely parsing), the embedding model,
and the ranking fixes. None of them blocks indexing; all of them affect answer
quality.

- **A — after 22-03, before Phase 23 (chosen).** ~36–58 h in total. The frontend
  then shows answers from the chunker and model we mean to launch with.
- **B — after launch.** Phase 23 comes sooner, but the quality gate `DESIGN.md` §9
  calls "the real gate" slips past launch.
- **C — only the chunker and model now, ranking later.** ~24–38 h now.

---

## What this phase inherits

**From Phase 21**, the authority is `docs/api-ingestion-jobs.md#the-phase-22-hand-off`.
Where each item lands:

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
| Re-parent drift | **not re-opened.** 21-07 ruled it settled, and a composite tenant key (P3) would make a chunk's tenant unable to drift at all |

**From `DECISIONS.md`**, every "Verification required in Phase 22" item:

| Decision | Verification | Plan |
|---|---|---|
| D1 | re-ingesting an unchanged commit gives identical `symbol_id`s | 22.1-01 |
| D1 | adding an unrelated line does not change ids below it | 22.1-01 |
| D1 | a re-export resolves to its leaf's id | **22.1-04** (moved; see P8) |
| D2 | a multi-tenant recall test against exact search | 22.1-05 (with the 2026-09-17 correction on what it must seed) |
| D2 | `EXPLAIN` shows `Subplans Removed` | 22-02 |
| D3 | a traversal over a cyclic fixture terminates | 22.1-04 |
| D3 | tier 2 upgrades tier 1 in place; tier 1 never downgrades tier 2 | 22.1-04 (the SQL rule, tested when `symbol_edges` is created; moved from 22-02) |
| D5 | cross-organization re-parent is rejected | already built (000013, 21-01) |
| D5 | a child row whose tenant disagrees with its repository is rejected at write time | 22-02 |
| D5 | a trigger-disabled bulk load leaves no drift, or is documented as forbidden | 22-02 |
| D5 | a drift-detection query runs in CI | 22-02 |
| `REWORK.md` open item | `chunks.symbol_id`: one FK or a join table | P7 |

---

## Decisions

| | Decision | Status |
|---|---|---|
| P1 | drop and recreate `chunks`; re-ingest from source | **LOCKED** · U1 |
| P2 | every partition gets its own row-level security | **LOCKED** · correction to D2 |
| P3 | tenancy by composite foreign key rather than trigger | PROPOSED · user, at 22-02 plan approval |
| P4 | record `embedding_model` on every chunk; ada-002 stays for now | **LOCKED** · U3 |
| P5 | `hnsw.iterative_scan` is load-bearing | **LOCKED** · correction to D2 §5 |
| P6 | both retrieval legs in Postgres; fusion and boosts stay in Python | **LOCKED** · U3 + U10 |
| P7 | `symbols` unpartitioned; `chunks.symbol_id` one nullable FK | PROPOSED · user, at 22-02 plan approval |
| P8 | symbol identity rules | PROPOSED · user, at 22.1-01 plan approval |
| P9 | D3 reconciliation omits `edge_kind` | **LOCKED** · correction to D3 |
| P10 | archive fetch, scoped token, caps, secret filter | **LOCKED** · U4, U5, U6, U7 |
| P11 | incremental by content manifest | PROPOSED · user, at 22.1-02 plan approval |
| P12 | progress by polling the job row; ISS-034 alongside | **LOCKED** · U8 |
| P13 | the seeded-migration CI gate lands first | **LOCKED** · U1 |
| P14 | `pgvector/pgvector:pg16` everywhere; the harness's reuse container renamed | **LOCKED** · U1 + D2 |
| P15 | Qdrant leaves in the storage plans | **LOCKED** · U1 + D2 |
| P16 | initial operating numbers | PROPOSED · set in 22-05's plan, replaced by 22.1-05's measurements |
| P17 | drop `retrievals.chunk_id`'s foreign key | **LOCKED** · U9 |

### P1 — Drop and recreate `chunks`; re-ingest from source. No data migration.

**LOCKED 2026-09-17, U1.** Option A depends on this premise: re-ingestion is the
path, and before launch it costs cents. The matching correction to
`DECISIONS.md`'s opening says the same.

Every existing row is harness or benchmark data that can be re-created from
pinned sources, and no user data exists anywhere. The existing rows' vectors
also live only in Qdrant, so D2's `embedding NOT NULL` could not be met by
moving them anyway. Re-embedding everything costs about $0.09 and 3.5 minutes
(RESEARCH Q1, Q4). The migration therefore carries **no DML**.

### P2 — Every partition gets row-level security of its own

**LOCKED 2026-09-17, correction to D2.** D2 already decided that RLS covers the
vectors. This is what makes that true on a partitioned table.

Enable and force row-level security, and create the `tenant_isolation` policy,
on **each of the 64 partitions**, not only on the parent. Without it, the app
role read and **overwrote** another tenant's rows by addressing the partition
directly (RESEARCH Summary, item 1).

Two tests guard it:
- every partition carries RLS, FORCE and the policy;
- a direct cross-tenant read and write through a partition returns nothing.

`trg_assert_tenant` on the parent is inherited by the partitions (measured: 64
of 64) and stays.

*Left to 22-02's plan:* whether to **also** revoke the app role's direct
privileges on the partitions, as defence in depth. Grants through the parent
still reach the rows.

### P3 — Tenancy on `chunks`, `symbols` and `symbol_edges` by composite foreign key · PROPOSED

**Who decides:** the user, when approving 22-02's plan. **Why it is not locked:**
it refines D5's "maintained by trigger, everywhere", which is a locked decision,
and no answer covered it.

The key is `FOREIGN KEY (repository_id, organization_id) REFERENCES repositories (id, organization_id)`.
This is the pattern 21-01 and 21-02 used, and it makes a misfiled row
**unrepresentable** rather than rejected (measured: the misfiled insert fails on
the key). A `BEFORE INSERT` trigger stays optional, for a readable error message,
exactly as 21-02 kept one. The drift query joins through `repositories` and
runs in CI.

**If declined:** 22-02 uses D5's `BEFORE INSERT` trigger as written. Roughly equal
cost. What is lost is the guarantee that holds even when a trigger is disabled.

### P4 — Record the embedding model on every chunk

**LOCKED 2026-09-17, U3.**

Add `chunks.embedding_model TEXT NOT NULL`, written from the generator's model.
The retriever **refuses** to compare a query embedded with one model against rows
embedded with another. Two models at the same dimension produce vectors in
different spaces, and mixing them fails silently (RESEARCH Q3). ada-002 stays
through the storage move. `text-embedding-3-small` is decided in the
retrieval-quality track, under a pass rule the user commits before the deciding
questions are written.

When the semantic cache is repaired (ISS-021), its key must carry the model too.

### P5 — `hnsw.iterative_scan` is load-bearing

**LOCKED 2026-09-17, correction to D2 §5.**

Every product query filters by repository inside the tenant's partition.
Without iterative scan, that filter produced short results in 16 of 20 and 20 of
20 queries (RESEARCH Q5). Set iterative scan per transaction in the vector leg.
Pass the query vector as a bound parameter so the HNSW index is eligible, and
make the final order exact distance.

*Left to 22-03's plan:* `relaxed_order` with a re-sort (a materialized CTE, or an
over-fetch sorted in Python), or `strict_order`. The two measured the same
recall; the plan picks one and tests the final order.

### P6 — Both retrieval legs in Postgres under one tenant scope; fusion stays in Python

**LOCKED 2026-09-17, U3 + U10.** Both answers keep the storage move separate
from quality changes. This decision is the mechanism for that.

- The vector leg becomes SQL under `require_tenant`.
- The keyword leg drops its latest-run filter (ISS-027).
- RRF and the booster keep running in Python, so the migration changes
  **storage and nothing else**.

22-03 proves that with an equivalence check on the three corpora. The check
expects one known difference: duplicate-content chunks gain the vectors they
never had.

A one-statement hybrid query was measured feasible under RLS, with both legs
pruned to one partition. It is recorded as a later latency option. Moving
fusion into SQL changes tie-breaking, which makes it a ranking change governed
by the protocol.

### P7 — `symbols` stays unpartitioned; `chunks.symbol_id` stays one nullable FK · PROPOSED

**Who decides:** the user, when approving 22-02's plan. It closes `REWORK.md`'s
open item, and no answer covered it.

**Why not partition `symbols`:** partitioning it would turn every foreign key
into it (from `chunks`, `symbol_edges` and D4's `memory_anchors`) into a composite
key. That is the same break measured for `retrievals`. `symbols` carries no
vector index, so D2's index-size argument does not apply to it.

**Why one nullable FK is enough:** chunking is symbol-aligned where it matters.
- function and class chunks map one-to-one;
- a `class_summary` maps to its class;
- `file_summary` and fixed-size chunks map to the file's `module` symbol (P8),
  or to `NULL`;
- a large function split into several chunks is many-to-one, which a chunk-side
  FK already supports.

The limitation, stated rather than hidden: a chunk spanning several symbols
points at the module. **The alternative** is a `chunk_symbols` join table with
its own RLS and key, ~3–5 h more.

### P8 — Symbol identity rules · PROPOSED

**Who decides:** the user, when approving 22.1-01's plan. `DECISIONS.md` now
records the measured facts behind each rule; the rules themselves are that
plan's to lock.

- **Identities are minted only at definition sites, never for an alias.** A Go
  `type A = B`, a TypeScript `export { X as Y }` and a Python `Y = X` become
  `imports` edge candidates, not symbols. Under this rule a re-export cannot
  create a second identity, so **D1 emission does not wait for the D3 resolver**
  (D1 correction). Resolving a *name* to its leaf still needs the import graph,
  so D1's third verification criterion moves to 22.1-04.
- **`symbol_path` is the full ancestor chain**, never the four-level display
  breadcrumb (`metadata_builder.py:80-82`).
- **`kind`** maps from the parse: `function`, `method`, `class`, `type`,
  `const`, `module`.
- **`ordinal`** covers the four measured collision shapes. `@typing.overload`
  joins D1's three (D1 correction).
- **The span includes decorators and leading doc comments**, so `span_digest`
  sees a route decorator or a doc comment change. Measured: both fall outside the
  span today (D1/D4 correction). *Deferred to D4:* whether a change to a doc
  comment alone should mark an anchored memory `stale`. Write the span rule so
  either answer is possible.
- **Go non-struct types and package-level constants become symbols, not chunks.**
  Identity is then complete without changing ranking.
- **Each file gets one `module` symbol**, the `from_symbol_id` for top-level code.

### P9 — D3: omit `edge_kind` from the tier-2 reconciliation

**LOCKED 2026-09-17, correction to D3.** D3 left this choice to Phase 22 and
named omission the safer default. SCIP has no notion of a call (verified in
`scip.proto`), so the other option, one shared vocabulary, would cost the `calls`
edge. 22.1-04's plan confirms this rather than choosing it.

- Tier 1 keeps its own vocabulary and emits `calls` and `imports` first.
- Tier 2 retires every unresolved tier-1 edge for
  `(organization_id, from_symbol_id, to_symbol_name)` in the transaction that
  inserts its own edge.
- Every graph query joins `symbols` and excludes archived rows, which D1 requires.
- Traversal uses the `CYCLE` clause.

### P10 — Fetch through GitHub's archive API with a scoped read-only token, caps and a secret filter

**LOCKED 2026-09-17: U4 (token), U5 (archive), U6 (caps), U7 (filters).**

**Token (U4).**
- The backend keeps the App private key and mints an installation token scoped
  to **the one repository**, with `contents: read`. It lasts one hour.
- It mints only for a worker that presents a job id and its `lease_owner` for a
  job that is `running` under that lease. That is the same fence every terminal
  write uses.
- The route listens only internally and is never mounted on the public router.
- *Residual risk, stated:* `ingestion_jobs` has no RLS, so a compromised worker
  could ask for tokens for any **currently running** job. Those tokens are still
  scoped, read-only and short-lived.

**Fetch (U5).**
- `GET /repos/{owner}/{repo}/tarball/{ref}`, with the token in one request header.
- Extract with `tarfile`'s `data` filter, into a per-job temporary directory.
- Never follow symlinks.
- Remove the directory in `finally`, and sweep stale ones at worker start.
- Never log the redirect URL (RESEARCH Q8).
- Measure on the largest benchmark repository first: GitHub documents no size
  limit.

**Caps (U6).** Archive ≤ 500 MB, ≤ 20,000 indexable files, ≤ 1 MB per file,
≤ 100,000 chunks, all checked while streaming.
- A hard cap ends the job `dead` with a plain reason.
- An oversized single file is skipped and counted in `progress`.

**Filters (U7).** An archive holds only tracked files, so the roadmap's "respect
`.gitignore`" is replaced by two filters:
- a secret-looking deny-list (`.env*`, `*.pem`, `*.key`, `id_rsa*` and similar),
  never sent to OpenAI and never stored;
- vendored, generated and binary files, skipped.

### P11 — Incremental by content-addressed file manifest, not by commit diff · PROPOSED

**Who decides:** the user, when approving 22.1-02's plan. No answer covered it.
**The alternative** is GitHub's compare API, which returns at most 300 changed
files (verified) and needs the previous commit to still exist.

Keep a manifest per repository: `(file_path, content_sha256, indexer_version)`.
Each job, inside `complete()`'s transaction:
- hashes the current tree and re-parses only added and changed files;
- deletes the chunks of changed and removed files and inserts the new ones;
- upserts and un-archives the symbols still present, and archives the ones that
  vanished.

`full_ingest` is the same algorithm with the manifest ignored. This survives
force-pushes and missed webhooks. `indexer_version` lets a chunker fix roll out
file by file after launch. Embeddings for unchanged chunk text are reused, keyed
on a hash of the **embedded text plus the model** (RESEARCH Q3, Q9).

### P12 — Progress by polling the job row; ISS-034 in the same plan

**LOCKED 2026-09-17, U8.**

The row already carries `last_stage`, `progress` and a computed `stalled`, and a
UI polls it every few seconds. 22.1-03 documents the `progress` schema and
settles ISS-034 with a deliberately written, mutation-checked isolation test.
SSE, Redis pub/sub and any streaming endpoint are deferred.

The roadmap's v2 breadcrumb imagined a future graph worker "subscribing" to chunk
events. What D3 persists in tables serves that better than an ephemeral stream
would.

### P13 — The seeded-migration CI gate lands first (ISS-031)

**LOCKED 2026-09-17, U1.** The approved 22-01 carries it.

The gate lands in the plan that swaps the image, before the storage migration.
The compose database is itself a seeded database at migration 10, so moving it to
16 is exactly the scenario ISS-031 describes (RESEARCH Q13).

### P14 — `pgvector/pgvector:pg16` everywhere, and the harness's reuse container renamed

**LOCKED 2026-09-17, U1 + D2.** U1's approved 22-01 is this work; D2 chose pgvector.

The image goes into compose, the Go harness, the Python conftest and
`backend-ci.yml`'s service, pinned by digest in CI.

The Go harness reuses its container **by name without checking the image**, so
the name must change along with the image, or every developer machine fails on
the first `CREATE EXTENSION`. Measured (RESEARCH Q2).

Document `--shm-size` for large index builds. *Execution details for 22-01:* the
exact container name and the digest.

### P15 — Qdrant leaves in the storage plans, not after

**LOCKED 2026-09-17, U1 + D2.** D2 decided Qdrant goes; U1's approved 22-02 and
22-03 decide when.

What goes:
- `qdrant_writer.py`
- the Qdrant path of `vector_retriever.py`
- the compose service
- `QDRANT_URL` in `api/main.py`
- the harness's Qdrant-based clear and state checks
- `qdrant-client`
- `pkg/vectordb` and its `go.mod` dependency (K2: dead code in any case). This
  one is deleted in 22-01, since the plans were written.

Keeping both stores for any stretch is the consistency hazard D2 exists to remove.

### P16 — Initial operating numbers · PROPOSED

**Who decides:** 22-05's plan sets them, the reviewer checks them, and 22.1-05
replaces them with measurements.

- **`max_job_duration`: 2 hours.** That is about three times the extrapolated
  end-to-end time for a 50,000-chunk repository, under twice that of U6's
  100,000-chunk cap, and sixty times the largest benchmark.
- **Heartbeat `statement_timeout`: 15 seconds**, a quarter of the beat interval,
  with a test that blocks a beat on the job row's lock.
- **Two worker processes** (four connections) until 22.1-05 measures.

### P17 — Drop `retrievals.chunk_id`'s foreign key

**LOCKED 2026-09-17, U9.**

22-02 drops `retrievals_chunk_id_fkey` before rebuilding `chunks`. The column
stays, so a logged result keeps the chunk id it was shown. `feedback`'s own
foreign key to `retrievals` is unchanged. The shape of the link (to a symbol, or
to a chunk with a tenant) is decided when feedback ships.

**One consequence 22-02 must handle.** Today, deleting a repository cascades
through `chunks` → `retrievals` → `feedback`, and `DELETE /api/repositories/{id}`
reports `feedback_deleted` by counting through that chain before it deletes
(`repositories.go:336-341`). Without the key, the cascade stops at `chunks`. The
response would then report feedback it no longer deletes. 22-02 must either
delete those `retrievals` rows explicitly in the same transaction, or change what
the response claims. Both tables are empty today, so this is about the contract,
not about data.

---

## The split · LOCKED 2026-09-17, U1

The dependency analysis (RESEARCH Q1) shows only storage has to come before the
pipeline. The handler writes chunks and vectors in the completion transaction,
which pgvector allows and Qdrant does not. Symbol emission, incremental ingest,
progress and the graph resolver are not on the path to a first real repository.

So the first phase ends with one real repository indexed end to end, and the
second builds the substrate onto a pipeline already proven.

**Revised 2026-09-17, when the plans were written.** Four boundaries moved:
- **`symbol_edges` moved from 22-02 to 22.1-04.** Nothing in Phase 22 writes it,
  22-02 is already the largest migration, and its reconciliation rule is better
  tested beside its writer.
- **Deleting `pkg/vectordb` moved from 22-02 to 22-01.** It is Go-only dead code
  (K2).
- **`PostgresWriter` writing vectors moved from 22-03 to 22-02.** The new table
  requires them, so the migration and every writer must land in one PR.
- **22-05 gained a fourth handler ending, `Rejected`.** U6's "ends `dead`" needs
  it.

The Phase 22 estimate is now ~60–87 h.

**This list is the authority for plan scope.** `ROADMAP.md` carries one line per
plan and points here. `STATE.md` points here and keeps no copy.

### Phase 22 — pgvector storage and the first real repository (~60–87 h)

| Plan | Scope | Est. |
|---|---|---|
| **22-01** | **pgvector everywhere, and the seeded-migration gate** (P13, P14). Migration `000016_enable_pgvector`, guarded so a non-superuser owner never calls `CREATE EXTENSION`. The image swap in compose, both harnesses (reuse container renamed) and CI, pinned by digest. ISS-031's gate: a Go test that seeds a fresh database at **migration 10** (the compose database's measured version), runs `up` as a `NOSUPERUSER NOBYPASSRLS` owner, and asserts each later migration's effect. `--shm-size` documented. **Deletes `pkg/vectordb`** (moved here from 22-02). | 6–9 h |
| **22-02** | **The partitioned `chunks` table, and every writer of it** (P1, P2, P3, P4, P7, P17). Migration `000017`:<br>(1) drop `retrievals_chunk_id_fkey`;<br>(2) create `symbols` (D1, with `archived_at`, RLS, the trigger and the tenant guarantee);<br>(3) drop `chunks` and recreate it partitioned by `HASH (organization_id)` `MODULUS 64`, with `organization_id`, the tenant guarantee (P3), `embedding vector(1536)`, `embedding_model` (P4) and a nullable `symbol_id` (P7); RLS, FORCE and the policy on the parent **and all 64 partitions**; `trg_assert_tenant`; the indexes.<br>Tests: the partition-RLS guard; the measured cross-tenant leak written as a test; `Subplans Removed`; a misfiled row rejected; cascades; the drift query.<br>**Every Go and Python writer of `chunks` moves in the same PR**, or CI goes red on `main`. That includes `PostgresWriter` writing each chunk with its vector (moved here from 22-03). The repository delete stays honest about feedback (P17). | 16–22 h |
| **22-03** | **Retrieval on pgvector; Qdrant retired** (P4, P5, P6, P15). The vector leg becomes SQL under RLS, with `relaxed_order` and an exact re-sort, and never compares across models. The keyword leg drops the latest-run filter. Qdrant is removed from the pipeline, code, compose, dependencies and the harness, and the harness gains a compose guard. The vector-leg isolation test. **The equivalence gate:** the same ada-002 vectors are read through Qdrant and through pgvector, under a rule fixed in the plan. | 12–18 h |
| **22-04** | **Fetching a repository safely** (P10). The backend's internal listener and token route (one repository, `contents: read`, one hour, live lease only, byte-identical 404). The worker's archive fetcher at an exact SHA, with U6's caps (500 MB applied to both the download and the expansion, as the bomb guard), U7's deny-list, and the vendored, generated and binary filters. Hostile-archive tests with their premises asserted. Redaction tested against captured logs. The per-job directory lifecycle. | 14–20 h |
| **22-05** | **The `full_ingest` handler, and the worker switched on.** Stages `fetch → parse → embed → store`. The run is resolved and attached. `write_results` replaces the repository's chunks inside `complete()`'s transaction. **A new fourth ending, `Rejected`**, so a cap ends the job `dead` in one attempt (U6). `REGISTRY` gets both keys. P16's numbers, provisional. Compose's `workers` service, never with the App key. An end-to-end test through the real worker, with fakes at the network edges. **The live proof: `AlecAsdourian/ES-SC-API-Navigator`**, indexed from the development App into a scratch database and searched through the RAG API's `/search`. | 12–18 h |

**Phase 22 ends with a real GitHub repository indexed end to end**: connect →
queue → worker → pgvector → search, under tenant isolation on both legs.

### Phase 22.1 — Symbols, incremental updates, progress and the code graph (~60–88 h)

| Plan | Scope | Est. |
|---|---|---|
| **22.1-01** | **D1 symbol identity from the chunker** (P8). Full-chain `symbol_path`, `kind`, `ordinal`, a span that includes decorators and doc comments, `span_digest`, `module` symbols, Go non-struct types and constants as symbols, and the alias rule. Upsert-and-unarchive. Chunks linked to symbols. D1's first two verification criteria. Python and Go only; TypeScript waits for the quality track's grammar decision. | 14–20 h |
| **22.1-02** | **Incremental ingestion** (P11). The file manifest (a small migration), per-file delete-and-insert, symbol archival, embedding reuse, and `incremental` made distinct from `full_ingest`. Tests for a force-push, a missed push, and a file deleted then restored (D1's resurrection path). **Closes ISS-027.** | 12–16 h |
| **22.1-03** | **The progress contract and ISS-034** (P12). A documented `progress` schema. The repository's current or last job made reachable from the repository API; ISS-034 offers two shapes and the plan chooses. A deliberately written, mutation-checked isolation test, because `ingestion_jobs` has no RLS and the CI gate ignores `GET`s. | 6–10 h |
| **22.1-04** | **D3 tier 1** (P9). **Creates `symbol_edges`** (D3's DDL; moved here from 22-02), testing D3's upgrade and no-downgrade SQL rule first. Call-site and import candidates from the parser. The resolver (imports plus scope matching) writes `symbol_edges` with `to_symbol_name` always set. The reconciliation rule. A `CYCLE`-safe traversal helper, tested on a cyclic fixture. Archived symbols excluded. D1's re-export criterion. | 20–30 h |
| **22.1-05** | **D2's recall test and the operating numbers.**<br>A multi-tenant, multi-repository recall test, seeded with the benchmark corpora's **real** embeddings copied into synthetic tenants. It needs: at least one tenant large enough that the planner uses HNSW (asserted in the test); repositories filtered inside a partition; a partition shared by several tenants; an exact baseline in the same scope; and assertions on recall **and** on short results.<br>Per-stage ingest timings over the three corpora and one large public repository, both full and incremental. The pool size, `max_job_duration` and the OpenAI throughput ceiling are then set from those timings. | 8–12 h |

### The rejected alternative, kept for its reasoning: foundation first (U1 option B)

Option B put 22-01, 22-02, 22-03 **and 22.1-01** in Phase 22, so every stored
shape would be decided before any pipeline code. Phase 22.1 would then hold fetch,
the handler, incremental, progress, tier 1 and the recall test.

| | A — vertical (chosen) | B — foundation first |
|---|---|---|
| First real repository indexed | end of Phase 22 | second plan of Phase 22.1, **~14–20 h later** |
| Rework | `write_results` touched again in 22.1-01 and 22.1-02: **~2–4 h** | none |
| Real rows without symbol ids | yes, briefly; re-ingested in 22.1 for cents | never |
| Where risk surfaces | fetch, the worker's first real run, and pgvector at scale surface first: the things never exercised end to end | identity work (already well measured, RESEARCH Q6) comes before the untested parts |

### What can run in parallel

With one worker in the fleet, parallelism means **freedom of order**, not work
happening at the same time. Three tracks do not depend on each other and can be
taken in whatever order review throughput allows:
- **storage:** 22-02, then 22-03;
- **fetch:** 22-04;
- **identity:** 22.1-01, which is offline parser work and can start as soon as
  22-01 lands.

They converge at 22-05 and 22.1-02.

### The retrieval-quality track · LOCKED 2026-09-17, U10

It runs **after 22-03** (so the storage equivalence check exists) and **before
Phase 23**. It is not a phase of its own.

Each item is decided using `boost-defaults-protocol.md`'s method: fresh blind
questions, and a rule committed before they exist. They run in this order, so
each is measured on the chunks and vectors it will ship with.

| Decision | Needs first | Est. |
|---|---|---|
| Chunker: ISS-026 (class chunks without method bodies) | 22-03's equivalence check | 8–12 h |
| Chunker: TypeScript grammar, **after adding a TS/JS benchmark corpus** (the benchmark has none) | a TS corpus with blind questions | 10–16 h |
| Embedding model (U3): `text-embedding-3-small` against ada-002, **under a pass rule the user commits before the questions are written** | the chunker decisions | 6–10 h |
| Ranking: ISS-024, ISS-025, ISS-028, ISS-029 | the model decision | 12–20 h |

It can run alongside 22-04, 22-05 and Phase 22.1. Phase 23 waits for both.

---

## Boundaries

**In scope:**
- everything in the two plan tables;
- the storage migration and its tests;
- retiring Qdrant;
- fetch, ingest, incremental and progress;
- D1's identity, D3's tier 1 and D2's recall test;
- the operating numbers.

**Not in scope:**

- **Retrieval-quality changes.** The chunker, the model and ranking are decided
  in the track above, not inside these plans. The storage migration's
  equivalence check is not a quality decision and is in scope.
- **SCIP tier 2.** Customer CI upload, later (K3).
- **D4's `memories` and `memory_anchors` tables.** Later. Only `span_digest` is
  needed now, and it is in 22.1-01.
- **ISS-023** (retrying a `dead` repository through the API). Stays with Phase 23.
- **ISS-021** (the semantic cache). Unchanged. When it is repaired, its key must
  include the embedding model (P4).
- **Single-statement hybrid fusion.** Measured feasible, deferred (P6).
- **SSE.** Deferred (P12, U8).
- **The `feedback` link's final shape.** Decided when feedback ships (P17, U9).

---

## Corrections to the roadmap sketch

1. **"Research: Unlikely."** D1–D5 all land in this phase, and three of them
   needed correcting.
2. **22-01, "shallow clone … respect `.gitignore`."** Replaced by an archive fetch
   (P10). `.gitignore` governs untracked files, so the rule protected nothing.
   Tracked vendored, generated, binary and secret-looking files are what need
   filtering.
3. **22-02, "Postgres + Qdrant writers."** One store and one transaction (P15).
4. **22-03, "diff previous commit vs new HEAD … delete removed files."** A content
   manifest (P11). Removed files' chunks are deleted and their **symbols archived**.
5. **22-04, "Redis pub/sub … SSE."** Polling (P12).
6. **Phase 23-03, "live indexing progress via SSE."** Polling the job row. 23-03
   now also waits on 22.1-03, which carries ISS-034.
7. **Phase 24.** Drop "Qdrant persistence and snapshot strategy" and 24-04's
   Qdrant snapshots. Add: pgvector availability on the chosen host,
   `CREATE EXTENSION` privilege for the migration role, container `--shm-size`,
   and keeping the token route internal-only (U4).
8. **Phase 25-03.** Drop "Qdrant down" from the runbook.

All eight were applied to `ROADMAP.md` on 2026-09-17.

---

## Open questions for execution

These are technical, for the plans to settle. None needs the user.

- **The archive redirect URL.** For a private repository, the redirect link
  carries a short-lived credential of its own [not verified]. 22-04 confirms its
  shape and makes sure nothing logs it.
- **`CREATE EXTENSION IF NOT EXISTS` when the migration role is not a superuser.**
  Confirm on the Phase 24 host whether the privilege check is skipped when the
  extension already exists.
- **The breadcrumb GIN index.** The query's `COALESCE(breadcrumb, '')` does not
  match the index expression. 22-02 makes them agree and proves it with `EXPLAIN`.
- **OpenAI throughput.** The key's tokens-per-minute limit may cap the pool
  before Postgres does. 22.1-05 measures it.
- **Doc-comment-only changes and staleness.** This is a D4 question, recorded
  under P8.
