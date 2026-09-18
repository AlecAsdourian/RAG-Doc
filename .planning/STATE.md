# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08; product-vision reframe recorded in memory at `product_vision.md`, 2026-09-03)

**Core value:** Persistent, shared, code-aware memory substrate for parallel AI coding agents. v1.0 ships as a smart docs platform (hybrid RAG over connected code repos) — the foundation the substrate is built on.
**Current focus:** Milestone v1.0 MVP — first shippable version. Next up: **plan Phase 22** (pgvector Storage & the First Real Repository). It is researched and scoped, and its decisions are locked in `.planning/phases/22-repository-clone-ingestion/22-CONTEXT.md`.

## Current Position

Milestone: v1.0 MVP (10 phases: 17-25, plus 22.1 inserted 2026-09-17)
Phase: **22 RESEARCHED AND SCOPED, 2026-09-17 (PR #45). Plans are not yet written.** The user approved splitting it into **Phase 22** (pgvector storage, and one real repository indexed end to end) and **Phase 22.1** (symbols, incremental updates, progress and the code graph), with a retrieval-quality track between 22-03 and Phase 23. The user answered U1–U10 and took every recommendation. **`22-CONTEXT.md` is the authority** for the split, the plans, the answers and every decision; this file keeps no copy of any of them. **Next: plan Phase 22.**
Phase 21: COMPLETE — Ingestion Job Infrastructure, all seven plans executed (21-01 merged as `fbb9793` / PR #37; 21-02 as `58ca2e8` / PR #38; 21-03 as `3068a42` / PR #39; 21-04 as `0c63917` / PR #40; 21-05 as `a92df5f` / PR #41; 21-06 as `de6b6e9` / PR #42; 21-07 as `b1aac04` / PR #43, which returned **APPROVE WITH NITS** and had its four minors and two nits applied before merge). Phase 22, the phase that makes the queue do something, came next and is now researched (see the Phase line above). **What turns it on is the list in `docs/api-ingestion-jobs.md` under "The Phase 22 hand-off", which is the authority** — this file deliberately keeps no copy of it, because PR #43's review found three files carrying three different versions and that is the failure mode `21-CONTEXT.md` opens by naming. Phase 20 COMPLETE — all five plans merged.
Plan: Phases 17, 19, 20 and 21 closed. Phase 21 was researched 2026-09-10 (`21-RESEARCH.md`, `21-CONTEXT.md`); eight decisions locked (L1-L8); planned 2026-09-14; **ISS-016 is now closed on evidence in 21-07**, and ISS-023 (retrying a `dead` repository through the API) stays open by decision O1 — the split is what let ISS-016 close cleanly. Phase 22 was researched 2026-09-17 (`22-RESEARCH.md`, `22-CONTEXT.md`). Its decisions were locked the same day, and it is not yet planned.
Status: Phase 17 closed 2026-09-06. Phase 18 deprioritized. Phase 19 closed 2026-09-08. The GitHub App is registered and its contract verified against the live API (20-02). Repositories now have a tenant-scoped CRUD API (20-03). `repositories.organization_id` is stored and guaranteed by a composite foreign key (21-01). The queue table exists (21-02): `ingestion_jobs`, with the ISS-016 guard as a partial unique index, a composite foreign key onto `repositories (id, organization_id)`, a tenant trigger for the message, and no row-level security by documented decision. The queue has its first producer (21-03): `pkg/jobs.Enqueue` and `pkg/jobs.SupersedeLive`, and `POST /api/repositories` now creates a real work item instead of stamping `sync_state`, which ends the connect path's half of ISS-016. **Every producer is now on the queue (21-04):** `push`, `installation_repositories` `added`/`removed` and `installation` `deleted` all go through `pkg/jobs`, no webhook writes `sync_state` to ask for work, and migration 000015 gave a job to every repository the old path had stranded `pending`. **The queue now has a consumer side (21-05):** `workers.jobs` holds claim, mark_started, complete, fail, defer, abandon, sweep and run resolution, with 21-02's statements ported verbatim into Python and re-run from there. **And there is a worker process now, which refuses to run (21-06):** `workers.jobs.runtime.Worker` claims, checks the repository's CURRENT installation, marks started, heartbeats the lease on its own connection, runs a handler, completes or fails, and sweeps on the heartbeat's schedule — but `python -m workers` finds `workers.jobs.handlers.REGISTRY` empty, says so and exits 2 **before it reads any configuration**, so nothing claims a real job before Phase 22 registers the ingestion handlers and adds `DATABASE_URL` to compose. **And the queue has a reader (21-07), which completes the phase:** `GET /api/admin/jobs/{id}` returns one job to any member of its organization, filtered by an explicit `organization_id` because the table has no row-level security and the CI isolation gate does not scan GETs — so its isolation test is written deliberately and mutation-checked. It never returns `lease_owner` or `payload`, answers one byte-identical 404 to every kind of miss, and reports liveness as `lease_expires_at` plus a `stalled` flag computed from the job row, because `sync_state = 'syncing'` is not evidence of a live worker. **ISS-016 is closed on evidence**; ISS-023 stays open by decision O1; **ISS-034 is newly filed** — nothing hands a UI a job id, so the endpoint is unreachable from a repository until something adds either a `job_id` on the repository response or a list-by-repository endpoint. That is now scheduled in 22.1-03.
Last activity: 2026-09-17. Phase 22 was researched against the real schema and scoped (PR #45). The user answered U1–U10 the same day. The results are recorded in three places:
- the split and every decision in `22-CONTEXT.md`;
- the evidence in `22-RESEARCH.md`;
- eight measured contradictions with D1–D5, as dated corrections in `.planning/v2-substrate/DECISIONS.md`.

Two findings deserve a line here:
- **pgvector, measured:** no current image has it. On `pgvector/pgvector:pg16` (pgvector 0.8.6), all fifteen migrations apply through both test harnesses with identical schemas, and both suites pass. One trap: the Go harness reuses its container by name without checking the image, so the reuse name must change along with the image.
- **Partition RLS leak, measured:** row-level security on a partitioned parent does not reach its partitions. The app role read and overwrote another tenant's row by addressing its partition directly. Every partition gets its own RLS (locked, P2).

Previously, 2026-09-16 — 21-07 executed, and **Phase 21 is complete**: the queue has a reader. `GET /api/admin/jobs/{id}` returns one job to **any member of its organization** — no role gate, by the user's decision, because it shows their own repository's indexing status and Phase 23's progress UI reads it directly. **It needed care two mechanisms would normally have supplied and neither does here.** `ingestion_jobs` has no row-level security (21-CONTEXT L5), so `WHERE id = $1 AND organization_id = $2` is the ENTIRE tenant boundary — nothing in the database catches a mistake. And the CI isolation scanner matches POST/PUT/PATCH/DELETE only, so a GET passes the gate with no test at all; run against this branch it reports `{"missing": [], "skipped": [], "covered": []}`, which is the measured form of "it asked for nothing". So the isolation test is written deliberately and **mutation-checked**: neutering the organization filter to `AND $2::text IS NOT NULL` fails exactly one test, `Scenario2`, the cross-tenant case. **Eleven mutations, eleven killed**, and one of them is a finding about the harness rather than the code: deleting the filter outright *also* breaks the statement's arity, so every scenario fails with a 500 from pgx and the run proves nothing about isolation — the neutered form is the faithful one, and both are recorded. **One 404 for every miss**, and the test asserts the three bodies are BYTE-IDENTICAL rather than merely all 404: "no such job", "another organization's job" and "that is not a UUID" are indistinguishable, or the endpoint answers "does this job id exist?" for every tenant. `lease_owner` and `payload` are never returned, pinned by asserting the response's exact key set rather than the absence of two strings. **21-06's ruling is in the response shape, not only in the docs:** `sync_state = 'syncing'` is NOT evidence of a live worker — a crashed worker, a dead connection and a shutdown deferral all leave it behind with nobody working — so the endpoint returns `lease_expires_at` and a `stalled` flag computed in SQL from the JOB ROW, with the same predicate `claimSQL` and the sweeper use, `lease_expires_at IS NULL` half included (without it a null-lease strand reports itself healthy, and that row blocks every future job for its repository). **ISS-016 IS CLOSED, on evidence re-run before any of this was written:** nine racing and guard tests at `main` (`de6b6e9`) against PostgreSQL 16, all passing — the partial unique index, supersede-before-enqueue including its silent-loss case, the relink scenario this issue was filed about, concurrent relinks, concurrent first connects, sixteen concurrent enqueues over five warm rounds, and the bulk `added`-racing-a-relink case that once queued one repository of three while reporting success. **ISS-023 stays open** by decision O1. `GET /api/admin/jobs/{id}` is added to ISS-012's affected surfaces, with the note that it is the one route where a stale claim has no RLS behind it. 21-05's deferred question is also settled: `claimSQL` and `sweepSQL` now have a **gate**, not a docstring — `TestClaimAndSweepNeverReachARequestHandler` tokenizes every Go file under `pkg/api` with `go/scanner` and fails on the identifiers or the pasted SQL, reading code and not comments (the first cut scanned raw bytes and failed on the handler's own comment explaining the rule). `docs/api-ingestion-jobs.md` is new and is where Phase 22 and Phase 23 should start. **One gap named rather than closed, and now numbered:** nothing returns a repository's job id, so this endpoint is usable by anything that already holds one and not by a UI starting from a repository — **ISS-034**, owner 23-03, carrying the two candidate shapes and the reason choosing between them is its own work. **PR #43 returned APPROVE WITH NITS, no critical and no important findings**, and the four minors plus two nits are applied. The reviewer re-ran M1, M1a, M3, M4, M5 and M10 rather than trusting the table, could not construct a cross-tenant read by any route it tried, confirmed the three misses are byte-identical with no timing oracle, re-ran all nine ISS-016 tests and confirmed each exercises what its citation claims, and ruled deviation 2 an IMPROVEMENT — following the plan's own bullet literally would have shipped the bug M4 catches. **Its sharpest finding is a documentation bug with a UI consequence:** `stalled` was described as "exactly the reclaimable predicate", and it is not — it is the `running`-with-a-dead-lease branch both statements share, without the `attempts` condition that partitions them, so a job at `attempts = max_attempts` with an expired lease reads `stalled: true` while the claim refuses it and the next sweep writes `dead`. The flag was right and the sentence was wrong; it is corrected in the doc, in the struct comment and in ROADMAP's 23-03 entry, each saying to compare `attempts` with `max_attempts`. Also applied: the step-count reconciliation (the doc is the authority and ROADMAP and `__main__.py` now point at it instead of keeping counts of four and three), 21-05's two deliberately-declined redaction arms added to the roll-up with the trade rather than only the upside — the bare-40-hex arm shares its shape with a git commit SHA **and with the App client secret**, unreachable today because no worker holds that value — M5's exact mutation spelling recorded because its kill count depends on it, and `last_stage` documented as advisory rather than an enum (migration 000014 has no `CHECK`, and adding one would be a migration this plan does not ship). **Nit 5 was recorded rather than acted on:** the claim/sweep gate fired on a verbatim paste and passed on the same SQL reformatted, so it is strong against the only way this SQL realistically reaches `pkg/api` and weak against deliberate evasion — which its docstring already said.
Previously, 2026-09-16 — 21-06 executed: the queue has a consumer PROCESS, and it fails closed. `python -m workers` checks the handler registry FIRST and exits 2 with a message about Phase 22 — the order matters, because the compose `workers` service has no `DATABASE_URL`, so a configuration read would crash first and hide the real reason (mutation M9 swaps the two checks and is killed). Behind that refusal the runtime is complete and tested: the loop sweeps, claims, checks the installation, marks started, heartbeats and completes. **The claim-time installation check is ISS-033's ending, built:** `installation_id IS NULL` or `uninstalled_at` set → `abandon` (`superseded`, `never_synced`, never `failed`); `suspended_at` set → `defer` an hour with the attempt handed back, so a week of suspension cannot dead-letter a healthy repository; otherwise `mark_started` and run — and `mark_started` runs AFTER the check, so a doomed job never shows as `syncing`. **The heartbeat thread gets its own connection**, because a shared psycopg2 connection shares its transaction and a heartbeat commit would commit the handler's half-written work; its fenced `UPDATE` carries both halves, and zero rows is the only way a superseded worker learns it has been replaced. **`FOR UPDATE SKIP LOCKED` is pinned from Python at last** — 21-05 left that gap deliberately and named this barrier test as what would close it: eight threads, own connections, one claim, five warm rounds, and the mutation that deletes the clause fails it while all 45 transition tests still pass. **28 mutations, 28 killed and one deliberate survivor** — fourteen from the first cut, nine for the guards the first review added and five for the second's, including the review's own defect (`Unfinished` falling through to `complete`) as mutation M18. Four of the original fourteen found real gaps first: the supersede test had to count the write callback's CALLS rather than its rows (the row is rolled back either way), nothing could observe the heartbeat's separate connection, nothing could observe `syncing`, and nothing read a `LogRecord`. 30 new tests, 5 consecutive clean runs plus one under concurrent load, no compose or Dockerfile change, and nothing under `services/backend` touched. **PR #42's review returned CHANGES REQUESTED with three important findings, all reproduced against a real PostgreSQL 16 and all fixed.** (1) A shutdown could write `completed` over unfinished work: a Phase-22-shaped handler that stopped after `clone` produced `state=completed last_stage=parse sync_state=synced`, a row contradicting itself and a UI saying the repository was ingested. The first cut had folded shutdown into `should_abort()` and left “raise if you have not finished” to a docstring; the plan defines that flag as lease-only, so there was never the contradiction the deviation claimed to resolve. A handler now has THREE endings and the TYPE is the contract — `return` completes, `raise Unfinished` DEFERS with the attempt handed back (so an operator's restarts cannot walk a healthy repository to `dead`), anything else fails — and `is_shutting_down()` is separate from `should_abort()`. (2) `Worker.run` never reconnected: killing its backend produced 44 identical errors in 15s, the next job was never claimed, and the process stayed alive so no restart policy fired. It now checks health each iteration, reconnects with a bounded backoff, and raises `DatabaseUnavailable` → exit 1 after ten failures. (3) The heartbeat never gave up: its connection could die and the lease expire under a running handler with `should_abort()` still False. It now reopens in place (one drop is a blip) and gives up once a full lease has passed with no beat landing. `max_job_duration` was added for the hung-handler case, defaulting to None because the number is a multiple of an ingest nobody has measured. Two of the PR's claims were re-measured IN ITS FAVOUR — a row carrying both `suspended_at` and `uninstalled_at` behaves correctly, and the heartbeat genuinely holds a long lease (110 samples, minimum headroom 1.426s) — and one was withdrawn: the shared-connection failure is LOUD, not sometimes silent. **A second review returned APPROVE WITH NITS and found five more, all fixed:** `Unfinished` could re-claim WITHOUT BOUND outside a shutdown — measured at 193 re-claims in 6 seconds with `attempts` pinned at 0, past BOTH of the phase's backstops (`CLAIM_SQL`'s `attempts < max_attempts` and `_SWEEP_SQL`'s `attempts >= max_attempts`) and raising nothing — so the free pass is now tied to `stop.is_set()`, which is the condition that makes it self-limiting; the FIRST connect bypassed the reconnect policy, which is the one most likely to fail because the compose `workers` service has no `depends_on`; no `connect_timeout` meant a DROPPING network path blocked inside `psycopg2.connect` and the heartbeat's give-up rules never ran, so every connect now has one and both rules are evaluated on every beat rather than only after an exception; `require_tenant` still carried the misleading “does not nest” message that `_unscoped` had been fixed for, and every terminal write goes through it; and `max_job_duration` was silent in both directions, so the startup line now carries it and an unset bound logs a WARNING. **Two rulings recorded rather than acted on:** `sync_state = 'syncing'` after a part-way deferral stays (projecting `pending` would flicker the UI within a second, and changing `defer` would break the suspended path) — with the consequence carried into 21-07's plan, that **`syncing` is not evidence of a live worker**, the evidence is `lease_expires_at` on the job row; and `max_job_duration = None` stays, now that it is announced. Worker-pool sizing stays an open input: one job at a time per process, two connections per busy worker, and Phase 22 measures the rest.
Previously, 2026-09-16 — 21-05 executed: the Python consumer's state transitions exist as plain, tested functions over psycopg2, and every SQL statement 21-02 proved on PostgreSQL 16 has now run from both languages that use it. **Every terminal write is fenced on BOTH halves of the lease** — `id + lease_owner + state = 'running'` — and `complete` raises `LeaseLost` from inside its tenant-scoped transaction, so a worker that has lost its lease rolls back the results it had already written (proven by a probe row that disappears). `complete` clears the rerun flag, completes, projects `synced`, and only THEN enqueues the follow-up: the reverse order raises nothing and loses the rerun, and the mutation that reorders it fails on a missing row rather than an error. **Backoff is settled:** `min(60s x 4^(n-1), 60 minutes)` multiplied by a factor in [0.5, 1.0) — a multiplier, not tenacity's `wait_random`, which adds and would push the tail above the cap; 81 minutes worst case across five attempts. **`lease_owner` is settled too:** a UUID4 generated at worker start, because a container scheduler reuses hostname and pid. `defer` gives the claim's attempt back, so a suspended installation can never dead-letter; `abandon` writes `superseded` and `never_synced`, never `failed`, which is the ending ISS-033 is filed on. `last_error` is redacted (`ghs_`, `ghp_`, `github_pat_`, `sk-`) and capped at 2,000 characters before it reaches a column 21-07 hands back. **25 mutations, 24 killed**; three survived first and added four tests — the claim's `attempts < max_attempts` and `lease_expires_at IS NULL` were ported correctly and tested only in Go, and the rerun clear's `state = 'running'` turned out to be unobservable through `complete` in Python, because the two statements share a transaction and the `LeaseLost` rolls a bad clear back. `PostgresWriter` and `IngestionPipeline` are unchanged; Phase 22 owns wiring the pipeline to runs and making chunk writes idempotent per run.
Previously, 2026-09-16 — 21-04 executed: the four webhook paths that request or cancel work now do it through `pkg/jobs`. `push` creates an `incremental` job and, when one is already live, JOINS it — replacing the `sync_state <> 'syncing'` guard, which "fixed" ISS-016 by dropping the push. Bulk `installation_repositories.added` runs one lookup with `FOR UPDATE OF r`, decides in Go which installations actually changed, supersedes only those, and then enqueues EVERY matched row in one `jobs.Enqueue` — the L8 case that once queued 1 of 3 while reporting success, now raced against a concurrent relink through a barrier over five rounds. `removed` supersedes the live jobs it stands down. `installation.deleted` supersedes every repository under the installation and drives its stand-down off the ids `SupersedeLive` RETURNED, so a repository whose job was retrying (projected `failed`) cannot be left looking like it is still retrying. Migration 000015 backfills a job for every repository left `pending` by the old path, stands the unsyncable ones (`installation_id IS NULL`, uninstalled installation) down to `never_synced` rather than leaving them looking queued, runs one organization at a time, and is idempotent by the producer's own `ON CONFLICT` arbiter — and it satisfies ISS-031 by structure: the file ends with the `DO` block, so no DML follows the tenant it sets. **PR #40's review (APPROVE WITH NITS) corrected the stand-down half:** the first cut left those rows `pending` on the argument that a handler would fix them, and both writers of `never_synced` key on `installation_id = $1`, which never matches NULL — so the row would have rendered as "queued, syncing soon" forever. ISS-033 was filed from the same review for the producer-side `uninstalled_at` race. The stale comment claiming a failed delivery "is not automatically retried, by design" is corrected: `claimDelivery` re-claims `'failed'`, so every handler runs twice and every producer call is re-entrant.
Previously, 2026-09-16 — 21-03 executed: `pkg/jobs` now holds the two producer functions the rest of the phase builds on — `Enqueue` (L7's upsert in set form, over `unnest`, de-duplicated because `ON CONFLICT DO UPDATE` raises 21000 on a repeated row, with the `sync_state` projection written in the same transaction only for repositories that got a NEW job) and `SupersedeLive` (L4 step 1, reporting which repositories actually had a live job). 21-02's `enqueueUpsertSQL`, its conflict clause and `supersedeLiveSQL` moved into `producer.go` verbatim, and `schema_test.go` became an internal test package so it references the production constants rather than copies. `POST /api/repositories` was restructured: the decision to queue work is now a value in Go (new / relink / unchanged) instead of two SQL `CASE` expressions, the org-wide adopt lookup takes `FOR UPDATE OF r` so a connect does not lock the organization's project row and serialise every other connect, and `sync_state` is read after the enqueue so the response carries what was committed. Sixteen concurrent enqueues through a barrier resolve to one live job over five rounds; two concurrent relinks through the real router both return 201 with one live job. **ISS-032 closed**, and it was reproduced first: adding `./pkg/jobs/...` to the package-parallelism set made the drift self-test deadlock, a three-attempt retry was measured failing, and the fix is `lock_timeout` below `deadlock_timeout` plus six jittered retries on 40P01/55P03.
Previously, 2026-09-16 — 21-02 executed: migration 000014 creates `ingestion_jobs` with `UNIQUE (repository_id) WHERE state IN ('queued','running')` — ISS-016's fix, in the schema — plus `ingestion_jobs_repo_tenant_fk` and `trg_ingestion_jobs_tenant`; `pkg/jobs/schema_test.go` holds every shared statement (enqueue upsert, supersede, complete, claim, sweeper, fail, conditional rerun clear, run resolution) as a named constant with a passing test on PostgreSQL 16, and pins the corrected wrong-order failure as **silent loss, not 23505**. PR #38's review then found a real defect — `clearRerunSQL` fenced on the lease but not on `state`, so a superseded worker could consume a rerun flag it could no longer act on, because `supersedeLiveSQL` deliberately leaves the lease attached; fixed, and `FOR UPDATE SKIP LOCKED`, `updated_at = NOW()` and the `state` half of the fence are now tested rather than merely present. 2026-09-14: 21-01 merged (PR #37) — migration 000013 stores `repositories.organization_id`, backfilled one tenant at a time and proven on seeded data under row-level security; `isolation.AssertNoRepositoryTenantDrift` is D5's reusable drift check. Earlier the same day: benchmark (PR #31), batched Qdrant uploads (PR #30), neutral boost defaults (PR #32), ISS-030 closed (PR #34), Phase 21 broken into seven plans

**Retrieval quality, 2026-09-13.** `services/workers/scripts/rag_quality_harness.py`
measures retrieval on this repository's own code. It asks 25 tuning questions and
15 held-out questions, and scores each by the rank of the file holding the answer.

- **Indexing (PR #27):** 85 of 186 Go functions, every method, had never been
  indexed, and no Go doc comment had reached an embedding. Held-out MRR went
  0.444 → 0.554; across all 40 questions, 34 → 35 answers in the top 5.
- **Breadcrumbs (PR #28):** the `chunks.breadcrumb` column was empty for every
  chunk, so results and citations carried no qualified names. Fixed; ranking
  unchanged.
- **Keyword search is effectively off (ISS-029).** AND semantics returns nothing
  for 35 of the 40 questions, so hybrid search runs almost entirely on vectors.
- **Ranking tuning is shelved.** OR keyword search, rank normalisation and fusion
  weights were measured under a pass rule fixed in advance. One configuration
  passed, then failed once the breadcrumb fix landed; none passes now. The
  branch is pushed but unmerged.
- **Real-code benchmark (PR #31):** miniflux (Go) and mealie (Python), pinned
  open-source applications, with 45 blind questions each, split into tuning,
  holdout and confirm sets. Scoring is at file and symbol level. At baseline, the
  right file reached the top 5 for 36 of the 60 original questions, and the
  answering function for 20.
- **Indexing larger repositories (PR #30):** every vector went to Qdrant in one
  request, so anything over roughly 1,000–1,500 chunks failed to index. Uploads
  are now batched.
- **Boost defaults (PR #32):** neutral, except the vendor/generated-path penalty.
  This was decided under `scripts/rag_benchmarks/boost-defaults-protocol.md`, with
  the rule committed before the confirm questions were written. On those
  questions, symbol-level MRR went 0.111 → 0.319 (miniflux) and 0.196 → 0.342
  (mealie), and no file metric fell.
- **The next ranking or chunking change:** measure it on the benchmark corpora. Use
  the same protocol: explore on tuning, and decide under a rule fixed before the
  deciding questions exist. ISS-025, ISS-026 and ISS-028 remain the known root
  causes.

**v2 substrate work, 2026-09-10.** `.planning/v2-substrate/` holds `DESIGN.md`
(the RAG redesign and 21 fleet proposals), `RESEARCH.md` (R-A…R-G), `DECISIONS.md`
(D1–D5) and `REWORK.md` — **read REWORK.md first; it records what review
overturned.** D1–D5 gate Phase 22 because that phase writes the first real rows.

**Two claims that were in this file as settled fact are withdrawn:**

- *"SCIP indexers must execute untrusted build commands, so a sandbox moves
  forward."* **Wrong** — that describes only *precise* indexing, which is opt-in
  and run by the repository owner in **their** CI. We never execute a customer's
  build. The sandbox is back in the fleet layer and Phase 24's
  nested-virtualization constraint is withdrawn.
- *"The pruning question was measured rather than assumed."* **Measured against a
  schema this repo does not have** — `chunks` has no `organization_id` and its
  RLS policy is a two-hop `EXISTS` join, not the scalar equality the experiment
  used. Under D2+D5 that becomes the target schema, so the result describes what
  we are building, not what exists. D2 no longer rests on it; it rests on
  index-size runway.

**New in D5:** every denormalised `organization_id` is trigger-maintained,
following `sync_github_installation_tenant`. Both `chunks` (partition key) and
`ingestion_jobs` (pre-tenant claim) need one, and both can drift.

**Phase 21 is decided, planned and now SHIPPED** (all seven plans; this paragraph is kept for the decisions it records). `21-CONTEXT.md` locks **eight**
decisions (L1–L8); L7 (a push against a live job sets `needs_rerun`) and L8
(concurrent enqueues resolve through one per-row upsert) were added in the third
rework. The headline reversal stands: the ROADMAP's tentative Redis Streams
pick is rejected in favour of a Postgres `ingestion_jobs` table claimed with
`FOR UPDATE SKIP LOCKED`, because the queue and the chunk writes then commit in
one transaction.

**Reworked after review.** Five correctness bugs were fixed in the schema
(supersede-before-enqueue, a poison-job attempt guard plus sweeper, a null-lease
strand, the `failed` state collapsed into `queued`-with-backoff, and the tenancy
column guarded by a composite foreign key plus a `BEFORE INSERT` trigger on
`ingestion_jobs` — NOT the mirror-trigger-on-`repositories` shape L5 explicitly
rejects). The pgmq rejection was re-justified: its
primary stated reason — "it is an extension" — is false, since pgmq ships a
pure-SQL install path. **L1 does not depend on D2's contested half**; chunks
have been in Postgres since migration 000003, so #25 targets `main` rather than
stacking on #24.

**Planned 2026-09-14 as seven plans.**
- **Schema first:** 21-01 adds the tenant column on `repositories`; 21-02 adds the job table and tests every shared SQL statement on PostgreSQL 16.
- **Producers:** 21-03 wires Connect; 21-04 wires the webhooks.
- **Consumer:** 21-05 writes the state transitions; 21-06 adds the worker runtime.
- **Close-out:** 21-07 adds the status endpoint and closes ISS-016.

Three open questions from the context were settled in the plans:
- **Worker identity:** `lease_owner` is a UUID generated when the worker starts.
- **Backoff:** 60 s × 4^(attempts−1), capped at 60 minutes, with jitter.
- **`sync_state`:** a projection of job state, with the table in 21-03 and 21-05.

The user chose that the job-status endpoint is readable by any member of the job's organization. **`python -m workers` refuses to start until Phase 22 registers real handlers,** so queued jobs are not dead-lettered in the meantime.

Progress: v1.0 MVP ████░░░░░░ 4/10 phases complete (17, 19, 20, 21); Phase 22 researched, not planned (Phase 18 Observability deferred — see project_phase18_deprioritized memory)

**Note on this section's history:** 19-01 and 19-02 both shipped without updating STATE.md, so this file sat two plans stale. Brought current in 19-03 — and then went stale again through 20-01/20-02/20-03, which is why the note is worth keeping. Update this file in the same commit as the summary, not afterwards.

## Performance Metrics

**Velocity:**
- Total plans completed: — (v1.0 tracking starts here)
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| — | — | — | — |

**Recent Trend:**
- Last 5 plans: —
- Trend: —

## Accumulated Context

### Decisions (v1.0 MVP milestone)

- **Milestone version:** `v1.0 MVP` (nothing has formally shipped; v0.9 = pre-milestone initial build)
- **Sign-ups:** Public (open registration) with abuse protections in Phase 24
- **Repository integration:** GitHub App (not OAuth App) — per-installation permissions, higher rate limits, built-in webhook signing
- **Phase ordering:** Isolation → Observability → Auth → Repo → Job Infra → Ingestion → Frontend → Deploy → Launch. Cross-cutting concerns (isolation, observability) established BEFORE more endpoints ship so every new endpoint inherits them
- **Cost controls:** Per-org rate limits + LLM cost caps included in v1.0 Phase 24 (mandatory; not optional)
- **Billing:** Explicitly deferred to post-v1.0 (need traction data first)
- **v2 door-keeping:** Every design decision noted for whether it closes GraphRAG/agent/MCP doors — chunking output events (Phase 22-04), job progress payload shape, etc.
- **Reviewer session:** Live since Phase 17-01; bootstrap prompt at `.planning/fleet/reviewer-session-prompt.md`. Every code PR since has gone through it.

### Decisions (retained from v0.9)

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions still affecting current work:

- **Tailwind v4 syntax:** `@theme` directive + CSS variable syntax
- **SSE streaming:** AsyncGenerator pattern for chat responses
- **Path aliases:** `@/` import alias for frontend
- **Vector DB technology:** Qdrant (self-hosted, cosine similarity, 1536-dim) — **superseded by pgvector** (`DECISIONS.md` D2); Phase 22 implements it and retires Qdrant in 22-03
- **LLM model choice:** GPT-4o Mini (cost efficiency)
- **Semantic cache threshold:** 0.95 cosine similarity, 1h TTL
- **Database primary keys:** UUIDs over SERIAL
- **Timestamps:** TIMESTAMPTZ
- **RLS:** Enabled + FORCED in migration 000008 (verification pending Phase 17)
- **Migration tool:** golang-migrate
- **Deduplication:** SHA256 content_hash

### Deferred Issues

- **ISS-001:** Shared type definitions for cross-phase data contracts — surface again during Phase 18 (structured logging conventions) or Phase 20 (repos API shape)
- **ISS-002:** Cross-phase verification pattern in planning workflow — template updated; apply to all v1.0 plans
- **ISS-004:** Org selection mechanism — **✅ closed 2026-09-08** across 19-03 (JWT-carried claim, header gone) and 19-04 (list + select endpoints). Frontend picker is Phase 23 work against `docs/auth-frontend-contract.md`.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today (nothing removes memberships), but **whatever ships membership removal must rewrite the claim** — short token TTLs do not bound this. See ISSUES.md.
- **ISS-005:** Supabase Native OAuth webhook handler — **scheduled: Phase 19-01**
- **ISS-006:** Test database connectivity — **✅ closed 2026-09-05** in Phase 17-01 via testcontainers-go harness (`pkg/testing/isolation`); see ISSUES.md
- **ISS-007:** JWT-carried tenant claim — **✅ closed 2026-09-08** in Phase 19-03. Tenant identity now comes only from the Supabase-signed `app_metadata.organization_id` claim; the `X-Organization-ID` path is deleted, including from CORS. Closed without the per-request membership re-check the original filing called for — reasoning in ISSUES.md and 19-03-SUMMARY.md.
- **ISS-008:** Request-scoped tenant transaction for DB-hitting endpoints — **✅ closed 2026-09-08** in Phase 20-01 (`db.TenantScoper`). Handlers touching an RLS table take the scoper and not a pool, so an unscoped query is inexpressible rather than merely discouraged.
- **ISS-013:** Unscoped access to an RLS table behaves differently depending on connection history — **filed 2026-09-08** during 20-01. Not live (the scoper makes it unreachable), but it is a heisenbug generator and the fix is now known to be a one-line `AfterConnect` sentinel rather than a six-table migration. It bit a test during 20-03. Both shapes are now pinned for the new queue table too (21-02, `TestIngestionJobs_EnqueueingNeedsTenantScope`), because an unscoped enqueue reaches them through the tenant trigger's read of `repositories`.
- **ISS-014:** `pkg/db` imports `pkg/auth`, inverting the layering — **filed 2026-09-08** during 20-01.
- **ISS-015:** The isolation scanner's coverage match is method-blind — **filed 2026-09-08** during 20-03's review. The nested-block, middleware-wrapped and multi-segment holes it originally also claimed are closed and pinned.
- **ISS-016:** `sync_state` has no lease, so a relink can re-queue a run already in flight — **filed 2026-09-09** during 20-03's review. **Must be settled before Phase 21 builds the queue**, not after.
- **ISS-017:** Three residual soft edges in the isolation scanner — **filed 2026-09-09**, all LOW and none reachable today.
- **ISS-009:** `pkg/vectordb` build — **✅ closed 2026-09-08.** Qdrant client bumped v1.7.0 → v1.19.2; the pin had always predated the API the package was written against. Fixing it surfaced a panicking unit test that had never been able to run.
- **ISS-010:** Isolation harness parallel race — **✅ closed 2026-09-08.** Role setup now holds a `pg_advisory_xact_lock`; verified over 5 cold-container parallel runs.
- **ISS-011:** OAuth callback routes — **✅ closed 2026-09-08** by unmounting them (not repairing). Repairing the 500 alone would have converted a loud failure into a silently claim-less account. Handlers kept as reference; deleting them is a planner/user call.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today; **whatever ships membership removal must rewrite the claim.** See ISSUES.md.
- **ISS-033:** The webhook producers do not check `uninstalled_at`, so a push racing an uninstall queues a job under a dead installation — **filed 2026-09-16** by PR #40's review. LOW-MEDIUM and **conditional on 21-06**: if the worker abandons such a job at claim time (superseded, `never_synced`, no attempt consumed) the cost is one wasted round trip; if it instead lets the job fail its way to `dead`, the repository ends at `failed` and the priority rises. See ISSUES.md.
- **ISS-027:** Re-indexing a repository leaves every earlier run's vectors searchable — **filed 2026-09-13.** Latent until something re-indexes; **HIGH before Phase 22 ships** (scheduled: 22-03 removes the second store; 22.1-02 closes it with per-file currency), and "filter to the latest run" is the wrong fix for incremental indexing. See ISSUES.md.
- **ISS-024, ISS-025, ISS-026, ISS-028, ISS-029:** retrieval-quality findings, **filed 2026-09-13** with measurements. They are boosts that never fire, stopword identifiers, duplicate oversized class chunks, breadcrumbs matching only whole names, and keyword search returning nothing. **Fix these root causes before any further ranking tuning.**
- **ISS-030:** search returns partial or empty results as a success when a retriever fails — **✅ closed 2026-09-14** by PR #34. A failed retriever now fails the request: 503 on `/search` and `/chat`, an error frame on `/chat/stream`, with no exception text in any response. A query containing control characters is rejected at the boundary (400 in Go, 422 in Python). See ISSUES.md.
- **Frontend inline-style pollution** — ongoing rule, cleaned per component touched
- **Mocked repos/orgs/graph in frontend** — **replaced in Phase 23**

### Blockers/Concerns

- ~~**User action needed:** GitHub App registration~~ — **done 2026-09-08.** App id 4880866 (`rag-doc-dev`); private key lives outside the repository.
- **User action needed:** Deployment target choice in Phase 24-01 (recommend I bring back options + trade-offs at that point)
- **User action needed:** Observability stack choice in Phase 18 (self-hosted vs SaaS — cost implications)

## Session Continuity

Last session: 2026-09-17
Stopped at: Phase 22 researched, scoped and locked on PR #45: `22-RESEARCH.md` and `22-CONTEXT.md`, the corrections to `DECISIONS.md`, and `ROADMAP.md` split into Phase 22 and Phase 22.1. Phase 21 is complete and merged (PR #37 to PR #44).
Resume file: None

Next command suggested: plan Phase 22 (`/gsd:plan-phase 22`), starting with 22-01. **Read `22-CONTEXT.md` first.** It holds the plan scopes and the user's answers. It marks each decision LOCKED or PROPOSED, and five are still PROPOSED (P3, P7, P8, P11, P16), each settled when its plan is approved. What turns the worker on is still the list in `docs/api-ingestion-jobs.md#the-phase-22-hand-off`, and it lands in 22-05.

**Settle ISS-016 as part of planning Phase 21, not after.** `sync_state` on
`repositories` is a status column being used as a queue: no lease, no owner,
no attempt counter, so two workers can believe they own the same repository.
Phase 20 added a second writer to it (the webhook), which makes the race
easier to hit. The queue's shape is Phase 21's decision and this is the input
to it.

**What Phase 21 inherits, concretely:** a work item is a `repositories` row
with `sync_state = 'pending'`. There is no queue table — 20-05 deliberately
did not invent one. `docs/api-github-webhooks.md` has the query and three
traps: rows with `installation_id IS NULL` are unsyncable rather than failed
and must not be retried; a suspended or uninstalled installation cannot mint
a token, so check `suspended_at` / `uninstalled_at` before attempting a sync.

**The GitHub App is fully configured** (registration 2026-09-08; user
authorization and client credentials 2026-09-09). Its contract was verified
against the live API before code was written against it, which corrected
three specs. `docs/github-app-setup.md` is the runbook if it ever has to be
redone — note it now has a REQUIRED user-authorization step, without which
the install callback fails closed.

**Two env-loading traps, hit for real on 2026-09-09** and documented in
`docs/local-development.md`: a UTF-8 BOM in `.env` makes the documented
`set -a && . ./.env && set +a` fail on line 1 and export nothing; and an
unquoted Windows path loses its backslashes when sourced, so the backend
panics naming a path that is not the one in the file. Also,
docker-compose's `backend` service passes none of the GitHub or Supabase
variables through and would panic on the missing `SUPABASE_WEBHOOK_SECRET`
— run the backend directly.

**Why ISS-008 comes first, verified rather than assumed:** `repositories` is RLS-scoped (000008) and carries the 000009 trigger. The only Go handler touching the database today is `user_orgs.go`, which reads `users`/`organizations`/`organization_memberships` — none of which have RLS. So no Go handler has ever read an RLS-scoped table, and `GET /api/repositories` is the first. Without the request-scoped tenant transaction it returns zero rows with no error.

**Environment note (new in 19-03):** local dev now has two separate Postgres instances — Supabase's (auth only) and docker-compose's on port 5434 (all application tables). They are NOT the same database, which is why 19-03 could not use a Supabase Auth Hook. Runbook: `docs/local-development.md`. The Go backend does not read `.env`; only docker-compose does.

**CI now builds and tests the Go code** (`.github/workflows/backend-ci.yml`, added 2026-09-08). `go build ./...`, `go vet ./...`, and `go test -p 1 ./...` all gate every PR. Before this, nothing in CI had ever invoked a compiler — the isolation check runs a Python script over the diff — which is how `pkg/vectordb` stayed uncompilable from Phase 3 to Phase 19.

The workflow supplies Postgres and Redis service containers for `pkg/auth`'s pre-17-01 helpers. Migrating those onto the testcontainers harness would let both be dropped; tracked in `19-02-SUMMARY.md`.

The full gate is: `go mod download`/`verify`/`tidy -diff`, migrations applied, `go build ./...`, `go vet ./...`, `go test -p 1 ./...`, the harness packages again at default parallelism, and `go test -race` on the concurrency-sensitive packages — `./pkg/api/...`, `./pkg/db/...`, `./pkg/jobs/...` and `./pkg/testing/...` (21-02 added `pkg/jobs` before it had a concurrent test, so 21-03's barrier race test and 21-06's worker pool gate from the day they land). **All of them gate** — the `-race` step shipped as `continue-on-error` because it could not be executed on the authoring machine (no cgo), and was promoted once it ran green.

**On ISS-010's regression guard:** the real one is `TestEnsureAppRoleIsConcurrencySafe` in `pkg/testing/isolation`, which releases 16 concurrent callers through a barrier and detected the missing advisory lock 8 times out of 8. The CI step that runs the harness packages at default parallelism is defense in depth only — measured at roughly one detection in eight, so a green result there proves little on its own. Do not replace the test with the step.

**That step now has a known flake — ISS-032, filed 2026-09-16.** 21-01's `TestRepositoriesOrganizationID_DriftCheckDetectsDrift` takes `ACCESS EXCLUSIVE` on `repositories` **and** `projects` (measured from `pg_locks`: dropping a foreign key locks the referenced table too), while other packages' fixtures write both in the other order. One deadlock in fifteen runs; the identical commit passed on re-run, and 16 local runs produced none. A red result there is worth re-running once before treating it as a real failure.

**Fleet handoff notes for the worker session:**
- Read the phase `-CONTEXT.md` first for vision context
- Plans lock design decisions inline — don't re-litigate them without cause
- Every commit follows `feedback_commit_convention.md`: one sentence, conventional prefix, no attribution trailers
- Every PR references its plan file in the description
- All work on a feature branch → PR → reviewer session comments → merge after approval
- When a plan's stated approach turns out to be impossible against the real system, revise the PLAN file with a REVISION NOTICE recording what was actually verified, then execute the revised version — do not silently improvise (pattern established in 19-03)

**Fleet handoff notes for the reviewer session:**
- Bootstrap prompt: `.planning/fleet/reviewer-session-prompt.md`
- Isolation-test-coverage hard rule is live as of 17-05

### Roadmap Evolution

- 2026-09-03 — Milestone v1.0 MVP created: 9 phases (17-25); roadmap restructured with milestone groupings; v0.9 disposition table records disposition of original phases 1-16 (some shipped, some superseded, some deferred to v2)
- 2026-09-03 — Product vision reframed: from "smart documentation platform" to "persistent shared memory substrate for parallel AI coding agents"; v1.0 remains RAG-over-code as the foundation, v2 adds GraphRAG + agents + MCP
- 2026-09-03 — Phase 17 planned: 3 sub-plans in ROADMAP expanded to 5 executable plans (17-01 harness, 17-02 endpoint verification, 17-03 DB trigger migration, 17-04 Python harness + workers audit, 17-05 CI gate + docs); design decisions locked inline (testcontainers, PL/pgSQL BEFORE trigger, regex-on-diff scanner)
- 2026-09-17 — Phase 22 split into Phase 22 (pgvector storage and the first real repository) and Phase 22.1 (symbols, incremental updates, progress and the code graph), by the user's answers U1 and U2; a retrieval-quality track is scheduled between 22-03 and Phase 23 (U10). See `22-CONTEXT.md`.
