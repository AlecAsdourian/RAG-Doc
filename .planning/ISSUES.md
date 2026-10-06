# Project Issues Log

Enhancements discovered during execution. Not critical - address in future phases.

## Open Enhancements

### ISS-041: Two of 22.2-01's test files fail on Python 3.11, the version the worker images use

- **Discovered:** 2026-10-06, by the 22.1-03 worker's verification of PR #64 in `python:3.11-slim`. Measured there: 701 passed, **1 failed, 7 errors**, none in PR #64's files. CI runs 3.12 (`workers-ci.yml:72`, `isolation-check.yml:28`), so it passes there.
- **Type:** Testing / Operations
- **Priority:** Low, but fix it with the next 22.2 PR that touches these files. Nothing in production runs these scripts today; the risk is that CI and the shipped image disagree.
- **What:**
  - The 7 errors are in `services/workers/tests/test_rag_quality_harness.py`, whose tests shell out to `git`, which `python:3.11-slim` does not have.
  - The 1 failure is `services/workers/tests/test_tripwire.py:270`. It compares a summary recorded under Python 3.12 with a recomputation. Python 3.12 changed `sum()` of floats to use compensated summation, so 3.11 differs in the last digits.
- **Why it matters:** `services/workers/Dockerfile` and `Dockerfile.api` are `FROM python:3.11-slim`, while CI tests on 3.12. A test that only passes on the CI version hides version-specific behaviour, and the decision tools (`tripwire.py`, `decide.py`) must give the same verdict wherever they run.
- **Fix:** either align the versions (move the images to 3.12, or add 3.11 to CI's matrix), or make the tests version-independent:
  - skip the `git`-dependent tests with a clear reason when `git` is missing;
  - compare floats in the tripwire test with a tolerance, or compute the aggregate with `math.fsum` on both sides.

  If the aggregate itself feeds a decision, `math.fsum` is the better fix, because it gives the same verdict on either version.
- **Related:** `22.2-01-SUMMARY.md`; `22.1-03-SUMMARY.md` §7.
- **Owner:** the 22.2 track. 22.2-07 (`decide.py`) is the natural place, because it shares the aggregate.

### ISS-040: A marked refusal from another deployment's token route reads as a lost lease, so every job dies silently

- **Discovered:** 2026-09-29, by PR #58's review (22-05), reviewer A's L2. Reasoned from the code and from a signature one test already shows; not measured against two real deployments.
- **Type:** Correctness / Operations
- **Priority:** **A Phase 24 gate.** Close it before a deployment runs a staging and a production stack whose workers could reach each other's internal listener. Not reachable under compose today: there is one backend and one database.
- **The path:** the handler turns `TokenRefused` (the route's **marked** 404) into `LeaseLost` (`workers/ingest/handler.py`), and `LeaseLost` writes nothing — correct when the lease really is gone (fact-check c2), because a write would clobber the job's new owner.
- **The case:** `INTERNAL_API_URL` reaches a **real** token route that reads **another** database: a staging backend serving a production worker, or a backend whose `DATABASE_URL` differs from the worker's. That route finds no such lease and answers the marked 404 to every job. Each job is then left `running` while its lease is live, reclaimed after it lapses, refused again, and after five attempts dead-lettered by the sweeper with `last_error` NULL (`_SWEEP_SQL` writes no reason). It is fact-check c3's failure (a misrouted URL looking like a lost lease), reached **through** the marker instead of around it. `test_a_refused_token_writes_nothing_and_the_job_stays_running` already shows the signature: the lease live in the worker's database when the route refused it, and `last_error` None.
- **Fix (reviewer A's):**
  - On `TokenRefused`, ask the worker's **own** database whether the lease is still ours: `ctx.report_progress("fetch", …)` is already fenced on owner and state.
  - If the report lands, the lease is live here and the route disagrees: raise a plain failure that names the mismatch, e.g. "the token route refused a lease this worker's database says is ours; `INTERNAL_API_URL` and `DATABASE_URL` may not be the same deployment". c2 is preserved: this ends in `fail`, never `complete`.
  - If it does not land, raise `LeaseLost` as today.
  - One subtlety, inferred from the SQL and not measured: `PROGRESS_SQL`'s fence does not check the lease's **expiry**, and the route does. A lease that has expired but not yet been reclaimed would therefore "land" and fail with the mismatch message, where today it writes nothing. An attempt is spent either way, but the message would mislead, so the check should compare `lease_expires_at > NOW()` as well, and its test should pin that case.
  - **And a startup probe (the review's N8):** `deps_from_env` cannot tell the internal listener from the public API, or one deployment's from another's. `workers/__main__` could call the route with a random job id and a random lease owner at startup and require the marked 404, turning a misrouted URL into the exit-2 refusal the entrypoint exists for; a probe that also presented a known lease would catch the wrong-deployment case before the first job.
- **Why not fixed in 22-05:** it changes the c2 end-to-end test's meaning (a refused token with a live lease stops being "write nothing") and needs a new runtime question that duplicates the Go route's predicate in Python. The review ruled filing it for Phase 24 fine, because staging beside production is where it bites.
- **Related:** fact-check c2 and c3 in `22-05-PLAN.md`; ISS-039 (the same route's other Phase 24 gate); `docs/api-ingestion-jobs.md`, "How a job ends".
- **Owner:** Phase 24, beside the internal-listener requirement in ROADMAP's research topics.

### ISS-039: The worker logs its lease owner, the token route's only credential, on every transition line

- **Discovered:** 2026-09-29, by PR #58's review (22-05), reviewer A's M1, from the worker's own finding (`22-05-SUMMARY.md` §16). Measured by the review in one real ingest through the real `Worker`: **12 of 24 log records carry the lease owner, 7 of them on the same line as the job id**, and it is the exact value the token route received.
- **Type:** Security
- **Priority:** **A Phase 24 gate.** Close it before any deployment ships worker logs off the host, or lets anything but the worker reach the internal listener.
- **What:** `worker=<lease_owner>` is written by `transitions._log` on every transition, by the `LeaseLost` messages' `worker={worker_id}`, and by `runtime.py`'s startup, progress-report and heartbeat lines — counted on this branch: 22 format strings in `runtime.py`, 3 in `transitions.py` and six `LeaseLost` messages. The lease owner is the only credential `POST /internal/jobs/{id}/repository-token` checks, and 22-04 treats it as a secret everywhere else (`workers/fetch/client.py`'s messages, the route's Scenario6, `test_handler.py`'s "never logged" assertion).
- **Impact:** whoever can read a live worker's logs **and** reach the internal listener can mint a one-repository, read-only token for any repository that worker is ingesting, across tenants. **No race is needed:** the id is generated once per process and reused for every job it claims, the heartbeat keeps each lease live for the whole job, and the route mints a fresh token on every call (no cache, no per-lease limit). Each token so minted lives GitHub's full hour, because the worker's revocation covers only the token its own fetch used.
- **Today:** no exposure beyond Docker-socket holders under compose, who can already read `ingestion_jobs.lease_owner` (the table has no row-level security; `repository_token.go` states that residual risk). Not in `GET /api/admin/jobs/{id}`, not in `last_error`, not in the backend's logs, not in `pg_stat_activity`.
- **Fix (reviewer A's):**
  - Log `worker=<first 12 hex of sha256(lease_owner)>` through **one helper** used at every site (about 30; no test asserts the raw id).
  - Add `worker.worker_id not in caplog.text` to `assert_no_secret_logged` in `test_ingest_end_to_end.py`, with a mutation it kills (log the raw id at one site).
  - ~~Correct `repository_token.go`'s package comment (N6).~~ **Done in PR #58:** it said the lease owner was generated "when it claimed the job" and that the worker revokes "on `LeaseLost`"; it now says once per process, and that revocation covers only the worker's own token. `docs/internal-api.md` says the same.
  - Ids logged before the fix die with their processes, so nothing already logged needs scrubbing.
- **Related, same review (N4), not a leak today:** `embedding_generator.py` and `openai_client.py` log raw OpenAI exception text (`str(e)`). It predates 22-05 but is now on the production path; OpenAI masks keys, so no leak is known, but the checklist's "every `str(e)`" rule says to route it through `sanitize_error`. Do it in the same pass, since both are "what the worker's logs may carry".
- **Owner:** Phase 24, beside the internal-listener requirement in ROADMAP's research topics.

### ISS-038: The noise penalty treats every `migrations/` directory as generated code, so application code under one is pushed out of the results

- **Discovered:** 2026-09-29, by the retrieval-quality track's research (`22.2-RESEARCH.md` R5), from the committed 22-03 records. Measured, not inferred.
- **Type:** Retrieval quality
- **Priority:** MEDIUM. It silently removes correct answers. It is a ranking default, so it is decided under the protocol, not patched.
- **What happens:**
  - `MetadataBooster.NOISE_PATTERNS` includes `(^|.*/)migrations/.*` (`metadata_booster.py:21`), multiplied by `noise_penalty` 0.3 (`:52`). That penalty is the one multiplier the neutral defaults (PR #32) kept.
  - mealie's `mealie/services/migrations/` is application code: the importers from other recipe apps. The benchmark spec keeps it deliberately.
  - In the 22-03 pgvector records, 113 entries across mealie's 45 boosted lists carry the 0.3. miniflux has none.
- **One case, traced (ml-05, a tuning question):**
  - The answer, `PaprikaMigrator._migrate`, is the vector leg's #1 (0.7847). The answer file holds the leg's #1, #2, #5 and #6, and six of the top eight carry the penalty.
  - The penalty moves it to #26, and the final top five contains nothing from the file. That is a miss at both file and symbol level.
- **The ingest filter disagrees.** 22-04's `VENDORED_DIRS` (`workers/fetch/filters.py:59`) does not treat `migrations/` as vendored. So the pipeline pays to embed code that ranking then discounts.
- **Fix direction:**
  - Narrow the pattern to schema-migration layouts: alembic `versions/`, Django's numbered `migrations/0001_*.py`, numbered SQL files.
  - Or drop it, and keep schema migrations out at ingest.
  - Either way, decide it with the keyword-leg decision, because it changes ranking.
- **Owner (2026-09-29, the user's answer QU8):** 22.2-06. It is part of the keyword-leg bundle (with ISS-029 and ISS-028), decided under the protocol on its own fresh set.
- **Related:** ISS-024 and ISS-025 (the other boosts), and `22.2-CONTEXT.md` QD9.

### ISS-036: `chunks.ingestion_run_id` and `chunks.symbol_id` carry no tenancy, so a tenant can cite another tenant's run or symbol on its own row

- **Discovered:** 2026-09-29, by the reviewer session on PR #49 (22-02), measured in the deployment shape (a `NOSUPERUSER NOBYPASSRLS` owner, FORCE RLS everywhere) as `rag_doc_app`.
- **Type:** Correctness / Tenancy
- **Priority:** LOW-MEDIUM. **Not a boundary breach:** tenant A can only mis-file its own row, and tenant B cannot read it. But B deleting its run or symbol then reaches A's partition (the cascade and the `SET NULL` run with row-level security bypassed), and nothing refuses the write. P3's "a misfiled row is unrepresentable" holds on the **repository** axis only.
- **What is wrong:** 000017's `chunks` carries `chunks_repo_tenant_fk` `(repository_id, organization_id) → repositories (id, organization_id)`, which is the tenant guarantee, and two single-column keys, `ingestion_run_id → ingestion_runs(id)` and `symbol_id → symbols(id)`. Per-row foreign-key checks run with `SECURITY_NOFORCE_RLS`, so they see every row whatever the tenant: as A, a chunk of A's repository citing **B's `ingestion_run_id`** was accepted (`INSERT 0 1`), and one citing **B's `symbol_id`** was accepted too. Neither composite key notices, because both are keyed on the repository.
- **What guards it today (22-02, PR #49's review applied):** `isolation.CheckChunkTenantDrift` has two more arms, `chunks:<id>:run` (the chunk's run belongs to another repository, or is gone) and `chunks:<id>:symbol` (the same for its symbol), run at the end of every test that writes the tables; `TestChunksPartition_TheSingleColumnKeysCarryNoTenancy` pins the acceptance, the cross-partition delete and the check's report; 000017's header and `docs/isolation.md` state the limit.
- **The fix, scheduled for 22.1-01** (the plan that gives `symbols` a writer): composite keys on `chunks`, `(ingestion_run_id, repository_id) → ingestion_runs (id, repository_id)` and `(symbol_id, repository_id) → symbols (id, repository_id)`, each needing `UNIQUE (id, repository_id)` on the referenced table (a UNIQUE validates by heap scan and is not subject to row-level security). **Under ISS-031's rule** the keys go on tables that will hold rows by then, and every table the validation reads forces row-level security: `chunks` (whose validation reads every partition through its own policy), `ingestion_runs` and `symbols`. So either lift FORCE for that one statement on every one of them, the parent, all 64 partitions and the referenced table, before any tenant is set and put it back in the same file, or find the rows-free moment. 22.1-01's plan decides, and measures it through the seeded gate.
- **Related:** ISS-031 (the rule the fix has to follow), P3 in `22-CONTEXT.md`.

### ISS-035: The `make seed*` scripts have failed since migration 000009

- **Discovered:** 2026-09-17, by the fact-check of the Phase 22 plans, measured at schema version 15.
- **Type:** Developer tooling
- **Priority:** LOW. Nothing runs them, which is why nobody noticed. But they are documented Make targets (`services/backend/Makefile`'s `seed`, `seed-lineage` and `seed-complete`), so they mislead.
- **What is wrong:**
  - `scripts/seed.sql`, `seed-lineage.sql` and `seed-complete.sql` all fail with `42501` from `trg_assert_tenant`. They write tenant-scoped tables without setting `app.current_tenant`.
  - `seed.sql` is the other two's prerequisite.
  - `seed-complete.sql` inserts no chunks: it writes `queries`, `retrievals` and `feedback`.
  - Since 22-02's migration 000017 (PR #49), `seed-lineage.sql`'s chunk inserts also lack `organization_id`, `embedding` and `embedding_model`, all `NOT NULL` now, so they would fail on those even with a tenant set. The columns and the fixed test vector to copy are in `pkg/testing/isolation/fixtures.go` (`TestChunkInsertSQL`).
- **The fix, when someone needs seed data:** set each organization's tenant with `set_config('app.current_tenant', …, true)` inside a transaction per organization, as 000015 does, and add the new chunk columns. Otherwise delete the three scripts and their Make targets.
- **A working example exists since 22-01:** `services/backend/pkg/testing/isolation/testdata/seed_at_000010.sql` seeds four organizations at schema version 10 in exactly that shape, as the unprivileged `rag_doc_app`. It is written for version 10, so it is a pattern to copy, not a drop-in replacement.
- **Why not in Phase 22:** 22-02 is already the largest plan, and this is unrelated tooling that no plan depends on.

### ISS-033: The webhook producers do not check `uninstalled_at`, so a push racing an uninstall queues a job under a dead installation

- **Discovered:** 2026-09-16, by the reviewer session on PR #40 (21-04), with a concrete race and file:line evidence.
- **Type:** Correctness / Ingestion
- **Priority:** LOW-MEDIUM — wasteful rather than wrong, *provided 21-06 lands as planned*. See "Why it is not urgent".
- **What is wrong:** `handlePush`'s lookup (`services/backend/pkg/api/handlers/github_webhook_events.go:617-621`) and `recordAddedRepositories`'s lookup resolve repositories by `installation_id` alone:
  ```sql
  SELECT id::text FROM repositories
  WHERE installation_id = $1 AND github_repo_id = $2
  ```
  Neither joins `github_installations` or tests `uninstalled_at`. Migration `000015` does (`AND gi.uninstalled_at IS NULL`, pinned by mutation 13), and `docs/api-github-webhooks.md` asserts the rule for the whole system — so the invariant is currently enforced in the backfill and in the handlers that *stand down*, but not in the two that *produce*.
- **The race, which needs no operator action.** A `push` and an `installation.deleted` dispatched close together. They are independent requests and **neither takes a lock the other waits on** — `markUninstalled`'s repository read has no `FOR UPDATE`, and neither does the push lookup:
  1. `push` reads the repository; it is still linked.
  2. `installation.deleted` supersedes the live jobs (none yet) and stands the repository down to `never_synced`.
  3. `push` commits: `Enqueue` inserts an `incremental` job **and** projects `sync_state = 'pending'`, undoing the stand-down.
  4. The repository shows `pending` under a dead installation, with a live job no token can serve.

  A manual redelivery from the App's Advanced tab reaches the same place, and `claimDelivery` re-claims `'failed'` deliveries specifically so that path works.
- **⚠ WHY IT IS NOT URGENT, and why the reviewer's stated ending does not survive the phase.** The review described the job burning five attempts and dead-lettering to `failed` — the retry-looking terminal state `markUninstalled`'s own comment forbids. **That is not where 21-06 leaves it.** The worker checks the repository's installation when it CLAIMS the job, which is the same reason `payload` deliberately carries no installation id (21-CONTEXT L2, L8), and a job whose installation is uninstalled is **abandoned**: superseded, repository back to `never_synced`, no attempt consumed and no dead-letter. So the cost is one claim's round trip and a `sync_state` that is briefly wrong, not a wrong terminal state — as long as 21-06 implements that check. If it does not, this issue's priority rises with it.
- **THE CLAIM-TIME CHECK NOW EXISTS (21-06, 2026-09-16), so the ending above is built rather than planned.** `workers.jobs.runtime.Worker` reads the repository's CURRENT installation immediately after claiming, under `require_tenant`, and branches: `installation_id IS NULL` or `uninstalled_at` set -> `abandon` (job `superseded`, lease cleared, repository back to `never_synced`, never `failed`); `suspended_at` set -> `defer` with the attempt handed back; otherwise `mark_started` and run. `mark_started` runs AFTER the check, so such a job never tells the UI it is `syncing`. Pinned by `test_a_dead_installation_abandons_the_job[uninstalled]` and `[no-installation]`, and by mutation M12 (an uninstalled installation is run instead of abandoned), which is killed. **The priority stays LOW-MEDIUM:** the cost of the race is one claim's round trip and a `sync_state` that is briefly and wrongly `pending`, not a wrong terminal state.
- **The producer-side fix below is still open,** and it is what stops that brief wrong `sync_state` being shown at all.
- **The fix, when someone is next in the file:** join the installation in both lookups, exactly as the migration does, and answer `ignored: installation uninstalled` rather than `queued`.
  ```sql
  SELECT r.id::text FROM repositories r
  JOIN github_installations gi ON gi.id = r.installation_id
  WHERE r.installation_id = $1 AND r.github_repo_id = $2
    AND gi.uninstalled_at IS NULL
  ```
  **It is defence in depth, not the fix for the race.** Two statements in two transactions with no shared lock can always interleave the other way — the uninstall can land *between* the producer's read and its commit — so the check narrows the window without closing it. Closing it would mean locking the installation row in both handlers, which is a bigger change than the symptom justifies while the claim-time check exists.
- **Related:** the identical argument already applies to `suspended_at`, which is deferred at claim time by design rather than checked by a producer (21-05/21-06).

### ISS-029: Keyword search returns nothing for most natural-language questions, so hybrid search is effectively vector-only

- **Discovered:** 2026-09-13, while measuring why the breadcrumb fix (PR #28) left every ranking unchanged. Measured on the quality harness.
- **Type:** Retrieval quality
- **Priority:** HIGH for retrieval quality. Nothing is broken, but half of hybrid search contributes almost nothing.
- **What happens:** `FTSRetriever.search` builds its query with `plainto_tsquery`, which joins every word of the question with AND. A chunk matches only if it contains all of them.
- **Measured on the 40 harness questions** (452-chunk index, `main`'s code):
  - keyword search returns **no results for 35 of 40** questions; median 0, maximum 2
  - only **6 of 200** top-5 results were found by both retrievers
  - so ranking is effectively vector search plus boosts, and every keyword-side feature (breadcrumb matching, rank normalisation, fusion weights) has almost nothing to act on
- **OR semantics was tried, and it isn't the fix on its own.** `feat/ranking-tuning` (pushed, unmerged, shelved 2026-09-13) switches keyword search to OR and adds rank normalisation and per-retriever fusion weights.
  - With OR, keyword search returns its 50-result limit for every question, and 198 of 200 top-5 results are found by both retrievers.
  - Ten configurations were measured against `main`'s ranking, under a decision rule fixed before any result was seen: adopt only if MRR rises on both the tuning and held-out sets and combined top-5 recall doesn't fall.
  - Before the breadcrumb fix, one configuration passed: OR with keyword weight 0.5. It scored tuning MRR 0.737, held-out MRR 0.656, 35/40 in top 5 and 24 at #1, against `main`'s 0.643, 0.554, 35/40 and 19.
  - The breadcrumb fix changed nothing under `main`'s ranking, yet it moved 15 of that configuration's 40 ranks. It then failed the rule (0.697, 0.536, 33/40, 21 at #1), and no configuration passed. Its earlier pass was fragile.
- **The held-out set is no longer blind.** Every configuration was checked against it, on two indexes. Any future ranking decision needs a new question set, written and verified before its results are seen.
- **Fix direction:** fix what OR exposes before tuning weights again, then re-measure OR against AND on a fresh blind question set:
  - ISS-026: oversized duplicate class chunks, which keyword length bias rewards
  - ISS-025: identifiers extracted from stopwords, which let the breadcrumb boost fire on words like "the"
  - ISS-024: content boosts that never fire
  - ISS-028: breadcrumbs that match only whole qualified names
- **Owner (2026-09-29, the user's answer QU8):** 22.2-06, the keyword-leg bundle with ISS-028 and ISS-038. It runs after the chunk-shape decision (ISS-026), on its own fresh set. The 22-03 records show the leg empty for all 90 benchmark questions (`22.2-RESEARCH.md` R5).

### ISS-028: Keyword search on breadcrumbs matches only whole qualified names

- **Discovered:** 2026-09-13, while fixing the empty `chunks.breadcrumb` column. Measured with `ts_debug`, not inferred.
- **Type:** Retrieval quality
- **Priority:** MEDIUM. Decide with the ranking work, by measurement.
- **What happens:** Postgres's `english` text-search parser reads a dotted name as a single token:
  - `RepositoriesHandler.Connect` → `host` → `{repositorieshandler.connect}`
  - `QueryEngine._enrich_results_with_metadata` → `file` → `{queryengine._enrich_results_with_metadata}`
  - `types.go` → `host` → `{types.go}`

  So the breadcrumb branch of keyword search (`FTSRetriever.search`, backed by migration 000006's GIN index) matches only a query containing the whole qualified name. `to_tsvector('english','RepositoriesHandler.Connect') @@ plainto_tsquery('english','connect')` is false.
- **Why it matters:** migration 000006 says the index "enables searches like auth.middleware.validateToken", and that exact form does work. But a question that names a symbol only in part, such as "the Connect handler", never matches. For natural-language questions, the words in a breadcrumb add nothing to keyword search.
- **Fix direction:** index a word-split form alongside the display value. Split on `.` and `_` and at lower-to-upper case boundaries, so `RepositoriesHandler.Connect` becomes `repositories handler connect`. Change the query expression and the GIN index expression together, so the index is still used. This changes ranking, so measure it against the harness's tuning and held-out sets rather than shipping it as a fix.
- **Owner (2026-09-29, the user's answer QU8):** 22.2-06, in the keyword-leg bundle.
  - It is a new indexed expression: a migration under ISS-031's rule and the seeded gate.
  - The stored breadcrumb is not rewritten, because the benchmark's symbol scoring reads it.
- **Not the empty-column bug.** That one, fixed in the same change that filed this, left the column NULL for every chunk. This one is about how a populated column is tokenized.

### ISS-027: Re-indexing a repository leaves every earlier run's vectors searchable

- **Discovered:** 2026-09-13, while preparing to re-ingest after the Go method fix. Measured, not inferred.
- **Type:** Correctness / Ingestion
- **Priority:** HIGH before Phase 22 ships; latent today, because nothing re-indexes yet.
- **The mechanism, each link checked:**
  1. Qdrant point ids are chunk ids (`storage/qdrant_writer.py:78-79`), and chunk ids are fresh for every ingestion run. A re-ingest therefore adds a complete new set of points instead of replacing the old ones.
  2. Nothing deletes anything. `storage/` and `pipeline/` contain no delete of points, chunks or runs.
  3. The vector retriever ignores `run_id` — `retrieval/vector_retriever.py:81-83` is a placeholder comment saying so — and the Qdrant payload carries no run id to filter on.
  4. Old `ingestion_runs` and `chunks` rows stay in Postgres, so enrichment re-reads stale chunks under RLS rather than dropping them.
- **Net effect:** every re-index adds a searchable copy of the repository while earlier copies remain. Search would keep returning code that no longer exists, including deleted functions and superseded versions.
- **The two retrievers already disagree about which run is current.** Keyword search filters to `ingestion_run_id = latest completed run` (`FTSRetriever._get_latest_run_id`); vector search sees every run. So a chunk can be invisible to one retriever and live in the other.
- **Measured state when found:** one run, 368 Qdrant points against 368 chunks, so nothing was mixed yet. Confirmed by clearing the harness repository's points and runs before re-ingesting for the Go method fix.
- **⚠ "Latest run" is the wrong fix for incremental indexing.** Phase 22's incremental re-index re-embeds only changed files, so unchanged files legitimately keep chunks from earlier runs. Filtering to the latest run would hide most of the repository. Currency has to be per file (or per symbol, per D1), not per run.
- **Fix direction, decided with Phase 22:** when a file is re-indexed, delete that file's superseded points and chunks in the same transaction that writes the replacements. Record the run and file in the Qdrant payload so the stores can be reconciled. D2's move to pgvector would put vectors under the same transaction and the same filter, which removes the cross-store half of this problem.
- **What that fix creates, for 22-05's `write_results` to handle consciously (PR #49's review, 2026-09-29):** deleting superseded chunks is exactly the event that leaves `retrievals` rows whose `chunk_id` points at nothing. Since 000017 (22-02, P17) that column has no foreign key, so nothing cascades and nothing refuses, and `DELETE /api/repositories/{id}` reaches only retrievals whose chunk **still exists** (the limit is written in `repositories.go`'s `Delete` and `docs/api-repositories.md`). `write_results` must decide what happens to a replaced chunk's retrievals, delete them, repoint them, or leave them and say so, rather than leave it to accident. The link's final shape is U9's, decided when feedback ships; today nothing writes `retrievals` or `feedback`.
- **2026-09-29, 22-03: the cross-store half is gone, and the two retrievers agree again.** Vectors live in `chunks.embedding`, deleted with the chunk under the same transaction and row-level security as the text; Qdrant, its point ids and its payload are retired. The keyword leg no longer filters to the latest **completed** run: both legs search the repository's chunks, and `QueryEngine.query` refuses a `run_id` rather than apply it to one leg. **What remains is per-file currency on re-index (22.1-02):** nothing yet deletes a superseded file's chunks, so a second ingest of the same repository would still leave two runs' chunks searchable, which is why the harness's `--ingest` still refuses an indexed corpus without `--clear`.
- **2026-09-29, 22-05: the worker's full ingest replaces a repository's chunks; the retrievals are left dangling, consciously.** `write_results` deletes every chunk of the repository and inserts the new set inside `complete()`'s transaction (`DELETE_REPOSITORY_CHUNKS_SQL`, `workers/storage/postgres_writer.py`), so a retry, a rerun or a second push through the worker leaves exactly one copy (`test_a_second_ingest_replaces_the_chunks_idempotently`; mutation M10, the DELETE neutered, is killed; the live proof re-indexed the approved repository and read the same 82 chunks back, no duplicate, none of the first ingest's ids surviving). A `retrievals` row citing a replaced chunk is **left as it is**, its `chunk_id` pointing at nothing and its `feedback` intact: the ending the user chose in U9 ("a logged result keeps a chunk id that may later point at nothing"). Deleting it would destroy feedback on every re-index, and repointing it is the link-shape decision U9 deferred to when feedback ships. `test_a_reingest_leaves_a_retrieval_of_a_replaced_chunk_dangling` pins it, and `DELETE /api/repositories/{id}`'s documented limit (it reaches only retrievals whose chunk still exists) is now reachable in practice. The benchmark harness's own `--ingest` path is unchanged (it still refuses an indexed corpus without `--clear`). **What remains open is 22.1-02's per-file currency for incremental ingest.**


### ISS-026: Whole classes and large functions are stored as single oversized chunks

- **Discovered:** 2026-09-13, while diagnosing keyword-search length bias.
- **Type:** Retrieval quality / Chunking
- **Priority:** MEDIUM — affects both keyword and vector ranking.
- **Measured on this repository's index:** median chunk 360 characters, p90 2,476, max **14,566**. 48 chunks exceed 2,000 characters and 16 exceed 5,000. The largest are entire Python classes stored as one `class` chunk — `AnswerGenerator` 12,826, `QueryEngine` 12,792, `SemanticChunker` 11,369, `TreeSitterParser` 9,664 — plus `router.go`'s router constructor at 14,566.
- **Why it matters:** unnormalised `ts_rank_cd` rewards a long chunk for containing many term hits, so the 14,566-character chunk ranked first for unrelated questions. And a 12K-character embedding averages a whole class into one vector, which blurs what any single method does.
- **Answered 2026-09-13 — the class chunks are duplicates, not just oversized.** For all 13 class chunks over 5,000 characters, the file also contains `function` chunks inside the class's line range covering nearly the same text:

  | class | class chunk | method chunks inside it | chars in those methods |
  |---|---|---|---|
  | `AnswerGenerator` | 12,826 | 8 | 12,141 |
  | `QueryEngine` | 12,792 | 6 | 12,382 |
  | `SemanticChunker` | 11,369 | 8 | 11,756 |
  | `TreeSitterParser` | 9,664 | 7 | 9,569 |
  | `MetadataBuilder` | 8,546 | 12 | 8,396 |

  So the same code is indexed twice: once precisely, method by method, and once as a single blurred block — and the blurred copy is the one length bias rewards.
- **Fix direction:** stop emitting a full class body when its methods are already chunked. Keep a short class chunk carrying the signature, docstring and method list. Re-splitting the body would only add a third copy. Re-measure on the quality harness before and after, and expect keyword-search length bias to fall sharply, since most of the >5,000-character chunks disappear.
- **2026-09-29, a precondition, measured by the quality track's chunk census (`22.2-RESEARCH.md` R6):** a function chunk starts at `def` (`semantic_chunker.py:154-158`), so its decorators are outside it.
  - mealie has 844 decorated definitions (773 functions, 71 classes). 466 of them have their decorators only inside an enclosing class chunk, which is this issue's duplicate. 105 have them in no chunk at all.
  - Of the 270 route decorators (`@router.get("/…")`), 245 reach the index only through a class chunk.
  - Removing the class body before decorators move into their method's own chunk would drop them from the index entirely. Phase 22.2 orders the two accordingly (`22.2-CONTEXT.md` QD6).
  - Also measured: 147 of mealie's 679 class chunks are at least 80% covered by their own method chunks, and 31 of its 32 chunks over 5,000 characters are class chunks. In Go, 0 of miniflux's 329 are.
- **Owner (2026-09-29, the user's answers QU4 and QU5):** Phase 22.2's chunk-shape decision, one bundled candidate.
  - 22.2-04 builds the candidate and the user commits its rule.
  - 22.2-05 decides it on the shared fresh set.
  - Both come after 22.2-02 has moved decorators into their chunks.

### ISS-025: Whether the breadcrumb boost fires depends on which retriever found the chunk

- **Discovered:** 2026-09-13, same investigation. Measured.
- **Type:** Correctness / Retrieval quality
- **Priority:** MEDIUM
- **Status, 2026-09-13:** defect 1 is fixed (PR #28). Defects 2 and 3 remain open.
- **Three compounding defects, as found:**
  1. **The stores disagreed. Fixed in PR #28, and wider than first measured.** As found, for `file_summary` chunks Qdrant stored `breadcrumb` as the filename (`'jwt.go'`, `'errors.go'`), while Postgres stored `NULL` for all 50 of them. In fact the chunk insert never wrote the `breadcrumb` column, so it was NULL for every chunk. Since PR #28 the column matches `metadata` on 452 of 452 harness chunks, and so matches Qdrant's payload.
  2. **Fusion keeps the first system's metadata.** `RRFFusion.fuse` stores metadata from a chunk's first occurrence, and `QueryEngine` passes `{"fts": ..., "vector": ...}` in that order. While the column was NULL, a chunk found by both retrievers inherited Postgres's `NULL` breadcrumb, and a chunk found only by vector search inherited Qdrant's filename. Now that the stores agree, the breadcrumb comes out the same either way, but the rule itself is unchanged.
  3. **The "identifiers" matched against it are mostly stopwords.** `QueryParser`'s snake_case pattern `[a-z_][a-z0-9_]{2,}` matches every lowercase word of three or more letters, so *"where is the GitHub App JWT signed"* yields `['GitHub', 'JWT', 'where', 'the', 'signed']`, and `_matches_identifiers` is a case-insensitive substring test.
- **Net effect, from the trace before PR #28:** `auth/jwt.go`'s summary, found only by vector search, received `breadcrumb_match_boost` because `'jwt.go'` contains "jwt". Every chunk found by both retrievers had `breadcrumb=None` and could not. A 1.3x boost was decided by retrieval order and filename substrings.
- **Measured after PR #28:**
  - **`main`'s AND keyword search:** no harness ranking changed, because keyword search rarely returns anything (ISS-029).
  - **OR keyword search:** the boost now also reaches chunks found by both retrievers. For OR with keyword weight 0.5, 15 of 40 ranks moved.
  - **Defect 3 now reaches more chunks:** its stopword matching can fire on more chunks than before.
- **Fix direction:** restrict identifier extraction to genuinely code-shaped tokens: ones containing `_` or internal capitals, or quoted. Make fusion merge metadata rather than keep the first occurrence.
- **Owner (2026-09-29, the user's answer QU8):** 22.2-06.
  - Defect 3 is fixed there as a neutral cleanup. At today's neutral boosts it changes no ranking, and an equivalence run proves it at the 2e-6 tolerance.
  - Defect 2 is moot since 22-03: both legs read the same row's metadata (`22.2-RESEARCH.md` R1).

### ISS-024: The content-based ranking boosts have never fired in production

- **Discovered:** 2026-09-13, while diagnosing retrieval ranking on the quality harness. Measured, not inferred.
- **Type:** Correctness / Retrieval quality
- **Priority:** MEDIUM — nothing breaks, but two documented ranking features have no effect.
- **What is wrong:** `MetadataBooster._calculate_multiplier` applies `identifier_match_boost` and `quoted_match_boost` by reading `chunk["content"]`. No chunk carries that key when the booster runs. The Qdrant payload stores `chunk_id, repository_id, file_path, language, chunk_type, breadcrumb` — no content (`storage/qdrant_writer.py:81-88`). `FTSRetriever` returns a 200-character `content_preview`, not `content`. Full content is only attached by `QueryEngine._enrich_results_with_metadata`, which runs **after** boosting.
- **Measured:** an instrumented query traced the booster at call time — **0 of 70** chunks carried `content`. A search for `"login error"` in quotes receives no quoted-term boost at all.
- **Why the unit tests did not catch it:** `test_metadata_booster.py` builds chunks that include `content`, so it tests a path the pipeline never takes. Same shape as ISS-021, where the semantic cache passed its tests and never ran.
- **Options, not yet decided:** boost the top-N *after* enrichment; have both retrievers return content; or delete the two boosts. Re-measure on the harness before choosing, since the identifier boost as written also matches stopwords (see ISS-025) and may be net harmful once it can fire.
- **Decided 2026-09-29 (the user's answer QU8, `22.2-CONTEXT.md` QD9): delete the two boosts,** in 22.2-06.
  - That means `quoted_match_boost` and `identifier_match_boost`, with their environment variables and configuration keys.
  - They are 1.0 since the boost protocol's verdict (PR #32), so the deletion changes no ranking, and an equivalence run proves it.
  - Bringing either back would be a ranking change, decided under the protocol.


### ISS-021: The semantic cache has never run, so a documented cost control has been absent since Phase 12

- **Discovered:** 2026-09-10, by the reviewer session on PR #26 while checking the severity of ISS-020. Independently verified.
- **Type:** Correctness / Cost
- **Priority:** MEDIUM — nothing is broken by its absence, but a documented cost control has been silently absent since Phase 12.
- **What is wrong:** `services/workers/api/main.py:64-68` constructs `SemanticCache(redis_url=..., qdrant_url=..., openai_api_key=...)`. The actual signature (`semantic_cache.py`) is `(redis_url, embedding_generator, similarity_threshold=0.95, ttl=3600)`. Two unexpected keyword arguments, one missing required argument — a guaranteed `TypeError`.
- **Why nobody noticed:** the call is wrapped in `try/except Exception` and the failure is reported as `logger.warning(f"Failed to initialize SemanticCache: {e}")`. `semantic_cache` stays `None`, and `AnswerGenerator` treats `None` as "caching disabled". The system degrades silently to exactly the behaviour it would have if the feature had never been written.
- **Signature drift, not a typo:** `main.py` is Phase 05; `SemanticCache` landed in Phase 12 with a different constructor. No test covers the wiring.
- **Impact:** Phase 12's research put semantic caching at roughly a 40% hit rate and treated it as a primary defence for PROJECT.md's stated cost constraint. That saving has been **0% realized since Phase 12**. Any cost projection that assumed it is wrong.
- **The ordering gate is now fully discharged, as of 2026-09-10.** It had two parts and both are done: ISS-020 org-scoped the key, and ISS-022 put the guard in CI. Repairing the constructor is therefore safe to do now — the cache will come up tenant-scoped, with 8 isolation tests gating every PR against regression.
- **When it is repaired, expect it to be the first time this code has ever executed.** It has been dead since Phase 12, so treat a green test suite as necessary rather than sufficient; the read path in particular has never run against a populated cache outside the new tests.
- **Class problem worth a separate look:** a bare `except Exception` + `logger.warning` around service initialization turns any wiring bug into silent feature loss. Worth auditing the other optional-dependency initializations in `main.py` on the same pass.
- **Re-confirmed 2026-09-14 by the PR #34 review, and still open.**
  - The reviewer found the same wiring bug independently. `api/main.py:64-68` still passes `qdrant_url` and `openai_api_key` to a constructor that takes `embedding_generator`, and `main.py:70-71` swallows the `TypeError`.
  - Reproduced offline on `fix/search-fails-loudly`: constructing `SemanticCache` with `main.py`'s arguments raises `TypeError: SemanticCache.__init__() got an unexpected keyword argument 'qdrant_url'`.
  - The call dates from `c3e146d` (Phase 05-02).
  - No second issue was filed, because this entry already records the bug.
- **Repairing it turns on a path that PR #34 hardened first.**
  - `AnswerGenerator` embeds the query for the cache lookup before retrieval. With the cache running, an OpenAI outage would have made `/chat` a 500 instead of the 503 that ISS-030 defines.
  - PR #34 makes a failed lookup or write fall through with a warning.
  - Covered only with a mocked cache, in `workers/generation/test_answer_generator.py` and `tests/api/test_routes_retrieval_failure.py`. Nothing has exercised it against a running cache.
- **Two latent effects to fix in the same change** (from the PR #34 re-review, 2026-09-14). Neither can happen while the cache is off.
  - **The key fragment would be logged twice per failed request.** `workers/embeddings/embedding_generator.py:176-179` logs the raw exception (`{e}`) when an embedding call fails. According to the review, the cache lookup's embedding call reaches that line, and retrieval then logs the same failure again. PR #34 brought retrieval failures down to one log line; switching the cache on would undo that. Drop `{e}` from that log line.
  - **An OpenAI outage would run the embedding retries twice before the 503:** once for the cache lookup, then again for retrieval.
  - **Why no test caught either:** the route test replaces the embedding generator with a mock. Test the repaired wiring with the real `EmbeddingGenerator` and a failing client.
- **2026-09-29, 22-03: the argument list changed and the bug did not.** Qdrant was retired, so `api/main.py` no longer passes `qdrant_url` to `SemanticCache(...)`; the call still passes `openai_api_key` in place of the required `embedding_generator`, still raises `TypeError`, and is still swallowed by the `except` below it. Left that way deliberately, with a comment at the call: the repair is this issue's own change, made in order (its key must carry the embedding model too, 22-CONTEXT P4).

### ISS-001: Implement shared type definitions for cross-phase data contracts

- **Discovered:** Phase 12 Task 3 (2026-01-12)
- **Type:** Refactoring / Code Quality
- **Description:** Create shared TypedDict definitions for data passed between phases to prevent integration bugs. Currently, phases make assumptions about data structure from upstream phases (e.g., AnswerGenerator assumed QueryEngine returns 'content' field). This led to a bug where QueryEngine returned 'content_preview' but not 'content', causing LLM to receive insufficient context. Shared type definitions would:
  - Make contracts explicit and type-checkable
  - Prevent field name mismatches
  - Enable IDE autocomplete for cross-phase data
  - Document expected data structures in code
- **Impact:** Medium (prevents integration bugs, improves maintainability)
- **Effort:** Medium (create `workers/types/` module with contracts for retrieval results, chunk data, query results)
- **Suggested phase:** Before Phase 13 (Web UI) to establish contracts for API responses
- **Example:**
  ```python
  # workers/types/retrieval.py
  class ChunkResult(TypedDict):
      chunk_id: str
      file_path: str
      content: str  # ← Explicit requirement
      content_preview: str
      # ... all fields documented
  ```

### ISS-002: Add cross-phase verification pattern to planning workflow

- **Discovered:** Phase 12 Task 3 (2026-01-12)
- **Type:** Process Improvement
- **Description:** When a phase depends on output from a previous phase, add an explicit verification task to the plan that checks the upstream component's actual output before implementation begins. This would have caught the QueryEngine/AnswerGenerator contract mismatch earlier. Pattern: Before implementing integration, run upstream component and verify its output schema matches assumptions.
- **Impact:** Medium (prevents integration issues, improves plan quality)
- **Effort:** Low (documentation/template update to remind planners to add verification tasks)
- **Suggested phase:** Update planning templates after Phase 12 completion
- **Example task in PLAN.md:**
  ```markdown
  <task type="auto">
    <name>Verify QueryEngine output contract</name>
    <action>
      Before implementing AnswerGenerator, verify QueryEngine returns:
      - Full 'content' field (not just preview)
      - All required metadata fields

      Run test query and inspect output schema.
    </action>
  </task>
  ```


### ISS-012: A revoked membership does not revoke the organization claim

- **Discovered:** Phase 19-04 (2026-09-08)
- **Type:** Security / Authorization
- **Priority:** HIGH before any membership-removal feature ships; **not exploitable today** (nothing removes memberships)
- **Description:** The organization claim is written at two moments — provisioning, and `POST /api/user/select-organization` — and at no other time. Supabase re-reads the same `raw_app_meta_data` column at every token mint, so refreshing a token *preserves* the claim rather than recomputing it. Removing a user from an organization therefore does not end their access to it: their token still names that org, `TenantMiddleware` still honors it, and every refresh renews it indefinitely.
- **Why it is not a live bug:** no code path removes a membership. `git grep 'DELETE FROM organization_memberships'` finds only test cleanup.
- **Why it is filed anyway:** the natural mental model — "stateless claims expire, so exposure is bounded by token lifetime" — is **wrong here**, and it is the model a future author will bring. Short token TTLs do not mitigate this at all. This was written into 19-03's summary as fact before a reviewer caught it.
- **Affected surfaces — every tenant-scoped route, and these by name.** The list is kept because "every tenant route" is easy to agree with and hard to act on:
  - `GET|POST /api/repositories`, `GET|DELETE /api/repositories/{id}` (20-03)
  - `GET /api/github/install`, `GET /api/github/installations`, `GET /api/github/installations/{id}/repositories` (20-04)
  - `POST /api/search` and `POST /api/chat/stream`
  - **`GET /api/admin/jobs/{id}` (21-07)** — added when it shipped. Worth its own line for one reason: `ingestion_jobs` has **no row-level security**, so where the others would also have to get past a policy, here the organization claim is the *entire* boundary. A removed member holding a stale claim reads their former organization's job status — including `last_error`, `last_stage` and `progress` — with nothing else in the way. The handler is correct; the claim is what is stale.
- **What closes it:** whatever ships membership removal must also rewrite the affected user's claim (and, if they were removed from their *active* org, decide what to put there — most likely another membership, or nothing plus a 403 that routes them to the org picker). `auth.AdminClient` is the mechanism; `cmd/backfill-org-claims` is the precedent.
- **Related:** ISS-004 (closed), and the "Not covered here" section of `docs/auth-frontend-contract.md`, which carries this warning forward to Phase 20+.

### ISS-005: Supabase Native OAuth webhook handler

- **Discovered:** Phase 4 Plan 3 (Architecture Decision)
- **Type:** Feature Implementation / Integration
- **Priority:** MEDIUM (architectural decision needs implementation)
- **Description:** Decided to use Supabase Native OAuth (Supabase handles OAuth flow) instead of custom OAuth implementation. This requires implementing webhook handler to receive `user.created` events from Supabase and provision users in our database. Current OAuth handlers serve as reference implementation only.
- **Impact:** Medium (can't use Supabase OAuth until webhook handler exists, currently relying on direct DB user creation)
- **Effort:** Medium (webhook endpoint, signature verification, user provisioning reuse)
- **Suggested phase:** Phase 5 or dedicated "Phase 04-05: Supabase Integration"
- **Blocked by:** Requires actual Supabase project setup with credentials
- **Current code:** `services/backend/pkg/auth/provisioning.go` (ProvisionOAuthUser, CreateOrganizationForUser - reusable)
- **Implementation:**
  - Create `POST /webhooks/supabase` endpoint
  - Verify webhook signature (HMAC with Supabase webhook secret)
  - Handle `user.created` event: call ProvisionOAuthUser, CreateOrganizationForUser
  - Configure webhook URL in Supabase dashboard
  - Update frontend to use Supabase JS client for OAuth


### ISS-014: `pkg/db` imports `pkg/auth`, which inverts the layering

- **Discovered:** Phase 20-01 review (2026-09-08)
- **Type:** Architecture / Maintainability
- **Priority:** LOW — no cycle today, and nothing is blocked
- **Description:** `db.tenantFromContext` reads the caller's organization via `auth.OrgIDFromContext`, so `pkg/db` depends on `pkg/auth`. `pkg/auth` currently imports no internal package, so there is no cycle.
- **Why it may bite:** `pkg/auth` already does its own database work with a raw pool (`provisioning.go`, `webhook.go`). The first time any of `users` / `organizations` / `organization_memberships` gains RLS, or any auth flow needs a tenant-scoped write, `pkg/auth` will want `db.TenantScoper` — and the import direction makes that a refactor rather than a line.
- **Fix:** move the context key and its accessors to a leaf package (`pkg/tenantctx`) that both can import. Mechanical: the key already has accessors as of 20-01, so the change is an import rewrite across five call sites.
- **Not done in 20-01** because the cycle does not exist, the benefit is speculative, and the refactor would have widened a plan that already grew a security fix.

### ISS-023: A `failed` repository cannot be retried through the public API

- **Discovered:** split out of ISS-016 on 2026-09-10 (decision O1).
- **Type:** Usability / API surface
- **Priority:** LOW-MEDIUM — there is a workaround (wait for the automatic retry), and no data is at risk.
- **Description:** re-connecting a repository deliberately does not reset its sync state, so a repository whose ingestion has exhausted its attempts has no user-facing route back into the queue. Documented in `docs/api-repositories.md`.
- **Why it is separate from ISS-016:** ISS-016 is a correctness bug about two writers racing for one repository, and Phase 21 closes it by making the work item a real queue entry. This is a missing *feature* on the API surface. Bundling them meant Phase 21 could only ever half-close the issue, which is exactly the contradiction review found across three files.
- **What Phase 21 gives it:** the state machine makes the retry *possible* — `dead` is a terminal state a repository can be lifted out of by re-queueing with `attempts` reset. Exposing that is not Phase 21's deliverable.
- **STILL OPEN as of 2026-09-16, after Phase 21 shipped**, by decision O1 and deliberately. ISS-016 closed in 21-07; this did not, and the split is the reason it could. What changed is that the pieces now exist: a `dead` job is outside the live set, so `pkg/jobs.Enqueue`'s upsert would insert a fresh job rather than flag the old one, and `GET /api/admin/jobs/{id}` can already say *why* it died (`last_error`, `attempts`, `state`). What is missing is a user-facing route that does it — and a decision about whether it re-queues the existing job with `attempts` reset or enqueues a new one, which the docs should state either way.
- **The workaround is real but not obvious:** a push to the repository, or a reconnect that changes the installation, enqueues fresh work. A plain reconnect does **not** — it is a metadata refresh, which `docs/api-repositories.md` says.
- **Owner:** whichever phase works the repository API surface (22 or 23).

### ISS-019: `push` and `installation_repositories` payload shapes are unverified

- **Discovered:** Phase 20-05 (2026-09-09)
- **Type:** Testing / Contract
- **Priority:** MEDIUM — Phase 21 acts on what these handlers write, so a wrong shape here becomes a wrong sync there
- **Description:** 20-05's `installation` handler is tested against real deliveries captured from the live App. `push` and `installation_repositories` are tested against payloads written from GitHub's documentation, because no such delivery has ever reached a capture server. Their tests are named `UNVERIFIED_*` so nobody mistakes them for evidence about the shape.
- **Why this is not pedantry:** capturing the `installation` payloads in 20-02 corrected three specs — `size` was in kilobytes not bytes (wrong by ~1000×), the callback could not be JWT-authenticated, and the repository shape was reduced. Documentation-derived fixtures are how a suite ends up agreeing with itself and disagreeing with the sender.
- **How to capture:** start a tunnel, point the App's webhook URL at it, run a capture server, then (a) push to a connected repository and (b) add and remove a repository from the installation in GitHub's settings. The existing fixtures in `services/backend/pkg/api/handlers/testdata/github/` show the envelope format.
- **One thing to fix while doing it:** the existing captures stored the body **parsed**, not as raw bytes, so their real signatures cannot be replayed. Capture the raw body too, and a signature test can then run against a genuine GitHub signature rather than a self-signed one.
- **And capture a REDELIVERY of the same event.** The whole idempotency design rests on `X-GitHub-Delivery` being stable when GitHub redelivers, and `github-app-setup.md` tells readers redelivery is safe on that basis. The two fixtures we have are different events with different ids, so nothing in the repo actually evidences stability — it is documented as verified and is not. The Redeliver button makes this a one-minute check once a tunnel is up.
- **Unchanged by 21-04, deliberately.** That plan rewrote what `push` and `installation_repositories` DO — they now go through `pkg/jobs` — without reading one additional payload field. `githubWebhookEnvelope` is byte-identical to 20-05's (PR #40's review diffed it: every changed line in `github_webhook.go` is a comment), and `docs/api-github-webhooks.md` now carries this warning in the section that describes the handlers rather than only in the one about redelivery. So the exposure is the same size it was: the handler logic is tested, the field names are still not evidence.
- **The `UNVERIFIED_` convention, stated precisely.** 21-04 first claimed "every new test keeps the prefix", which PR #40's review showed was too strong. The convention is: a **per-event subtest** driving an unverified payload carries the prefix. Five tests drive one without it — `AddedOnlyTouchesTheOwningOrganization` and `CrossTenant_AWebhookCannotTouchAnotherOrgsRepositories` from 20-05, and 21-04's `RedeliveryOfAFailedDeliveryCreatesNoSecondLiveJob`, `TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository` and `TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs`. All five are about a property spanning events (tenancy, redelivery, the queue), and each now carries the caveat in its own comment. **Renaming them was considered and rejected:** the prefix earns its place by marking the cases a reader would otherwise take as evidence about the payload SHAPE, and spreading it across every test that happens to send a `push` body would drain it of meaning. The file header states the rule and names all five, so the exception is written down rather than inferred.

### ISS-018: A Redis outage at startup disables GitHub installs until the process restarts

- **Discovered:** Phase 20-04 review (2026-09-09)
- **Type:** Operability
- **Priority:** MEDIUM — silent, and it lands during exactly the event most likely to coincide with it (a deploy)
- **Description:** `NewRouterWithValidatorAndAdmin` dials Redis once, at construction, to build the install flow's state store. If that dial fails, `installStates` stays nil for the life of the process: `GET /api/github/install` returns 503 and the callback refuses to link, forever, with one startup WARN and nothing afterwards. A Redis blip during a deploy therefore disables GitHub installations with no ongoing signal.
- **Why the current behaviour is still right as far as it goes:** refusing is correct — a flow that cannot store its state token cannot be completed safely, and starting one anyway leaves the user with a live unlinked installation, which is the state 20-04's user-authorization check exists to protect. The problem is that it never heals and barely announces itself.
- **What to do:** dial lazily on first use so a recovered Redis heals itself, and log at ERROR (not silently) on each refusal so the condition is visible in monitoring. The store is also never `Close()`d — the old ISS-011 probe explicitly closed its client; this one holds a pooled connection for the process lifetime.
- **Related:** `docs/local-development.md` documents the restart requirement.

### ISS-017: Three residual soft edges in the isolation scanner

- **Discovered:** Phase 20-03 fourth review pass (2026-09-09), after the approval
- **Type:** Testing / CI
- **Priority:** LOW — none is reachable in this codebase today; all three are cheap when someone is next in the file
- **Why filed rather than fixed:** the scanner took three rounds to close the free pass and each round's fix produced the next finding. These are contrived or unreachable, and the marginal value of a fourth change to a file that now has 34 tests is lower than the risk of introducing a fifth.

1. **A prefix can still leak out of a string literal.** Route paths are read from the comments-blanked view, which keeps literals, so a line that opens a real brace *and* mentions `.Route("…")` inside a string pushes that path:
   ```go
   for _, s := range []string{`.Route("/evil"`} {
       r.Post("/wipe", h.Wipe)
   ```
   reports `POST /evil/wipe`. The important direction — a commented-out registration — is closed and pinned. Fix: require the match offset to fall outside every literal span.

2. **Two `_scan_go` behaviours are correct but unpinned.** Dropping rune tracking entirely, and dropping backslash-escape handling inside literals, both survive the suite. The shipped code handles `'{'`, `'"'`, `"say \"hi\""` and `'\''` correctly — verified by hand, not by test. Without rune tracking, `if c == '"' {` opens a runaway string that blanks the rest of the file.

3. **A leading `/` is now required, which drops a Go 1.22 ServeMux host pattern.** `mux.HandleFunc("POST example.com/api/wipe", h.Wipe)` is invisible. No impact while this repo is chi-only, and the leading slash is what stopped `cache.Delete("session-key")` reading as a route — but the module docstring advertises `HandleFunc("METHOD path")` without the caveat.

- **Also noted:** the adoption query's `p.organization_id` predicate cannot be mutation-tested, because no test can simulate "RLS regressed". It is documented as the second layer rather than the scope, which is the honest framing.

### ISS-015: The isolation scanner's coverage match is method-blind

- **Discovered:** Phase 20-03 review (2026-09-08), while fixing the nested-`chi.Route` blind spot
- **Type:** Testing / CI
- **Priority:** LOW — the ratchet works; this is the last soft edge in it
- **Description:** `scripts/ci/check-isolation-tests.py` decides coverage by looking for the endpoint's path in an isolation-test file. It cannot see which HTTP method the test exercises, so an existing test that only does `GET /api/things` marks a newly added `POST /api/things` as covered.
- **Why it was not fixed with the rest of 20-03's scanner work:** every cheap way to add method-awareness reads a Go test for method tokens (`http.MethodPost`, `"POST"`, helper wrappers) and guesses. A gate that fails a PR because the author spelled the method differently gets disabled, and a disabled gate is worse than a loose one. Worth doing properly — resolve the handler symbol per route and check the test drives that handler — or not at all.
- **Correction to the first version of this entry.** It claimed "every mutation route in a nested `chi.Route` block is now detected with its full path". False when written, and found by review: `r.With(mw).Post(...)` and `r.Method("POST", …)` were both invisible, and a new route nested under an already-tested prefix was marked covered for nothing. Both fixed and pinned; this entry is narrowed to what actually remains.
- **What IS pinned now, each by its own test in `scripts/ci/test_check_isolation.py`:** nested `chi.Route` blocks; middleware-wrapped registrations; `Method`/`MethodFunc`; every static segment of a parameterised path having to appear, not just the leading one; a path resolving to `/` never matching; braces inside strings, raw strings and block comments; and a skip marker on a group opener not reaching the routes inside it.

### ISS-013: Unscoped access to an RLS table behaves differently depending on connection history

- **Discovered:** Phase 20-01 (2026-09-08), while writing the tests for `TenantScoper`
- **Type:** Correctness / Operability
- **Priority:** MEDIUM — no live code path hits it, but it is a heisenbug generator
- **Description:** The RLS policies in migration 000008 compare against `current_setting('app.current_tenant', true)::uuid`. The `missing_ok` flag makes an *unset* GUC return `NULL`, which filters every row and returns an empty result with no error. But a **committed `SET LOCAL` leaves the GUC as an empty string** on that backend permanently (`RESET` and `SET TO DEFAULT` do not clear it — established 17-02), and `''::uuid` raises **SQLSTATE 22P02**.
- **So the same unscoped query is silently empty OR a 500**, depending on which pooled connection it gets and what that connection did earlier. Verified by direct probe; both halves are pinned by `TestUnscopedAccess_BehaviourDependsOnConnectionHistory` in `pkg/db/tenant_isolation_test.go`.
- **Why it is not live today:** `db.TenantScoper` makes unscoped access unreachable from a correctly-constructed handler, and the only handler holding a raw pool (`user_orgs.go`) touches no RLS table.
- **Why it is filed anyway:** it fails in the direction that trains people badly. A fresh test process gets `NULL` and sees a clean empty result; production, once connections have been reused, gets intermittent 500s with a message about invalid uuid syntax that points nowhere near the actual cause.
- **Independently reproduced** on PG 16.11 during 20-01 review, including the case I had not tested. **Nothing clears it short of reconnecting:**

  | after a committed `SET LOCAL` | `current_setting('app.current_tenant', true)` |
  |---|---|
  | (as-is) | `''` |
  | `RESET app.current_tenant` | `''` |
  | `SET app.current_tenant TO DEFAULT` | `''` |
  | `RESET ALL` | `''` |
  | **`DISCARD ALL`** | **`''`** |

  And pgxpool never runs `DISCARD ALL` — it only destroys closed, busy, in-transaction, or expired connections, and this repo registers no `AfterRelease` hook. Corroborating evidence from another angle: `SetupTestDB` does `SET ROLE rag_doc_app` in `AfterConnect`; if pgxpool reset connections on release, that would revert to the superuser (`rolbypassrls=t`) and **every isolation test in the repo would silently pass**. It does not.

- **THE FIX IS CHEAPER THAN FIRST WRITTEN — no migration required.** The original entry said this needs a policy change across all six tables. It does not. A one-line `pgxpool.Config.AfterConnect` sentinel gives the recommended always-loud behaviour on every connection, measured:

  ```sql
  SET app.current_tenant = '';                      -- once, in AfterConnect
  SELECT count(*) FROM repositories;                -- ERROR 22P02, deterministically
  BEGIN; SET LOCAL app.current_tenant = '1111...';
    SELECT count(*) FROM repositories;              -- works
  COMMIT;                                            -- reverts to '', same after ROLLBACK
  ```

  Reversible, no schema change, and it makes the two "kinds of missing" into one.

- **Other options:** `NULLIF(current_setting('app.current_tenant', true), '')::uuid` in the policies makes it deterministically *silent*; dropping the `missing_ok` flag makes it deterministically *loud*. Both are migrations across six tables and neither is necessary given the above.
- **Recommendation:** the `AfterConnect` sentinel, giving deterministically **loud**. With `TenantScoper` in place an unscoped query is by definition a bug, and a bug that always throws is cheaper than one that sometimes returns `[]`. Still an operational-risk judgement — a 500 is worse than an empty list for a user who trips it — so it belongs to whoever owns that call, but it is now a pool-constructor line rather than a schema change.

## Closed Enhancements

### ISS-034: Nothing hands a UI a job id, so the job-status endpoint is unreachable from a repository ✅

- **✅ CLOSED 2026-10-06 by PR #64 (22.1-03, branch `feat/22.1-03-job-status`), on shape 1, as the user accepted it that day.** Evidence in `22.1-03-SUMMARY.md`:
  - **`current_job`** (the same object `GET /api/admin/jobs/{id}` returns, or `null`, the key always present) on `GET /api/repositories`, `GET /api/repositories/{id}` and the `201` of `POST /api/repositories`, which now returns the job the connect enqueued (or the live job it joined, or the latest). "Current" is the live job, else the newest by `(created_at, id)`, else `null`, defined once in `currentJobJoinSQL` as two index-served lookups (the live one first), with `jobColumnsSQL` shared with the job endpoint. Contract: `docs/api-repositories.md`.
  - **`status`** on the job object, computed in SQL against the database clock from the job row and its installation, never `sync_state`: `queued`, `scheduled`, `retrying`, `deferred_suspended`, `running`, `stalled`, `dead_pending`, `completed`, `dead`, `superseded`. Its one authority is `docs/api-ingestion-jobs.md`'s status table.
  - **Migration 000018**, `idx_ingestion_jobs_repository_recent`; `EXPLAIN (ANALYZE, BUFFERS)` over 9,950 jobs on 100 repositories (19,900 in the table), as `rag_doc_app` under a tenant, measured each lookup on its index under a `Limit` with no `Sort`.
  - **The isolation test, written on purpose:** `repositories_current_job_isolation_test.go`. The organization filters are masked by `repositories`' row-level security, so Scenario4 plants another organization's job on the caller's repository (beside a terminal job of the caller's) under replica mode in a superuser transaction that is never committed, and runs the exported production statements in it as the app role. **The plan's thirteen mutations, M1a to M10, all killed, and M11 and M12 added by PR #64's review (the row's cap; suspended over retrying), killed**, each proven present and its original absent by bytes before it ran and restored byte-identical after; plus the progress contract's five (P1, P1b, P2, P3, P4).
- **Not chosen: shape 2**, `GET /api/repositories/{id}/jobs`. It would answer history as well as status, but the repository list would make one call per repository per poll, and each client would derive "current" itself. It stays additive later (~3–4 h, the same isolation pattern); `docs/api-repositories.md` lists it under "Not in this API yet".
- *The entry as filed, kept for its reasoning (present tense and line numbers as of 2026-09-17):*
- **Discovered:** 2026-09-17, by the reviewer session on PR #43 (21-07), which ruled that deferring it is correct and that it needs a **number** rather than living only in doc prose.
- **Type:** API surface / Frontend blocker
- **Priority:** LOW today, **BLOCKING for 23-03.** Nothing claims a job before Phase 22, so the endpoint is inert; the moment Phase 23 wants a progress UI it is the first wall.
- **What was missing (as filed):** `GET /api/admin/jobs/{id}` (21-07) reads one job by id, and **no API response anywhere returns a job id.** `POST /api/repositories` logs `job_id` server-side (`repositories.go` (then line 819)) and its response body carries only the repository; `GET /api/repositories` and `GET /api/repositories/{id}` return the `Repository` object, which has no job field. So the endpoint is usable by anything that already holds an id — a log line, a support query — and not by a UI starting from a repository, which is every UI.
- **Where the prose already says this, and why that was not enough:** `docs/api-ingestion-jobs.md` (under the response shape), `docs/api-repositories.md`'s "Not in this API yet", and `21-07-SUMMARY.md`. Every other deferred item this phase produced got a number (ISS-023, ISS-027, ISS-033); this one lived only in prose, which is the one place `consider-issues` will never look.
- **Two candidate shapes, and choosing between them is the work:**
  1. **A `job_id` (or a small `current_job` object) on the repository response.** Cheapest for the obvious UI, and it forces a definition of *current*: the live job, or the last terminal one when there is none? A repository with a `dead` job and no live one is exactly the case a user needs to see.
  2. **`GET /api/repositories/{id}/jobs`**, a short list. Answers history as well as status, which `RepoSettingsPage` ("real sync history", 23-03) wants anyway, and keeps the repository response stable.
- **Why it was NOT settled in 21-07:** it is an API-contract decision with its own documentation, tests and isolation surface — and `ingestion_jobs` has no row-level security, so *any* new reader of it needs a deliberately written isolation test for the same reason 21-07's did, and the CI gate will not ask for one if it is a `GET`. 21-07's five deviations were kept small on purpose; this would have been a sixth and the largest.
- **One thing whichever shape wins must carry:** `sync_state = 'syncing'` is not evidence of a live worker, and `stalled` is not evidence of a retry. See `docs/api-ingestion-jobs.md`.
- **Owner:** Phase 23 (23-03), or Phase 22 if the repository API is open for another reason first.
- **Owner, as of 2026-09-17: 22.1-03**, together with the progress contract (`22-CONTEXT.md` P12, U8). 23-03 consumes it.
- **Planned 2026-10-06 in `22.1-03-PLAN.md`: shape 1.** A `current_job` (the same object `GET /api/admin/jobs/{id}` returns, or `null`) on `GET /api/repositories`, `GET /api/repositories/{id}` and the connect `201`, so the repository list polls one endpoint rather than one per repository. "Current" is the live job, else the newest by `(created_at, id)`. Shape 2 (a history list) is recorded as not chosen and additive later. The isolation test plants a drifted job row so the organization filter's mutation is observable. Closed by PR #64 on that evidence.

### ISS-037: The Go isolation harness's single reuse-by-name container collides across parallel worktrees at different migration versions ✅

**Found:** 2026-09-29, during 22-04 (PR #52), while 22-02 ran in a sibling worktree. Filed at PR #52's review (L8).
**Owner:** 22-05, as its first task.
**Severity:** medium — a fleet-workflow hazard; no product impact.

- **✅ CLOSED 2026-09-29, by its own fix PR, #56 (branch `fix/iss-037-harness-container-name`), ahead of 22-05 rather than inside it.** The fix is the one adopted below, measured:
  - **The name:** `rag-doc-isolation-tests-pgv16-` and the first 12 hex digits of the SHA-256 of the checkout's root. The root is found five directories up from `container.go`'s compiled path, the way the migrations directory already was (which now derives from the same root), with symlinks and Windows letter case resolved; no git. `ISOLATION_CONTAINER_NAME` overrides it and must be lowercase `[a-z0-9_.-]`, at least two characters, starting with a letter or digit, or setup fails naming the variable. The constant is now `containerNamePrefix`. Each container carries the label `rag-doc.isolation.checkout` with the checkout that created it.
  - **Real runs:** `go test ./pkg/testing/isolation/... -count=1` created `rag-doc-isolation-tests-pgv16-971ef0687c03` (id `003e79087fa0`, labelled with this worktree, `schema_migrations` 17, clean); a second run reused it (no create, same id and start time); after `docker stop`, a third run started the same id. A valid override created a container by that name; `ISOLATION_CONTAINER_NAME="Bad Name"` failed setup with the message and created nothing.
  - **Unit tests,** `container_name_test.go`, no Docker: different checkouts give different names; one checkout gives one name, pinned for GitHub Actions' workspace path; the override wins; a name Docker could not use is refused; every derived name is a valid Docker name with the documented listing prefix; the root holds `services/backend/go.mod`.
  - **Four mutations, four killed:** the bare constant (the "different checkouts" test and the pin fail), the override ignored, the override's check neutered, and the root one directory too high, which would hash `.claude/worktrees` and give every agent worktree one container again.
  - **Suites, local:** `go test ./... -count=1 -p 1` on a scratch Postgres passes every package except `pkg/api/handlers`, whose only failure is the known CRLF-only `TestSignatureComparisonIsConstantTime`; that package passes with just that test skipped. Also locally, not in CI: the packages of CI's parallelism step plus `pkg/jobs` and `pkg/internalapi` (seven packages, that test skipped), run at default parallelism with no container to start from, passed and left exactly one container for the checkout. CI's own steps are unchanged, and all of them passed on #56.
  - **Nothing else named the container** in code, CI or scripts. `docs/local-development.md` gives the derived form, the override, and how to list (by the label, `docker ps -a --filter label=rag-doc.isolation.checkout`, which also finds override names) and remove them. The Python conftest was never affected: it starts an unnamed container per pytest session and stops it at teardown.
  - **#56's review, APPROVE WITH NITS, all five applied.** A root that is not absolute is refused before any container exists: under `go test -trimpath` every checkout derived the name ending `587fa80cee2f`, and with the override set the harness still failed on migrations, which are found from the same path. `TestResolveContainerName_RefusesTheRootTrimpathGives` pins the refusal, and mutation M5 (the guard neutered) is killed. Both `-trimpath` runs, with and without the override, now fail with the message and create nothing. The docs now cover the label listing, Windows junctions (not resolved under go 1.25's `winsymlink` default: one checkout reached two ways gets two containers, never two checkouts one), and a branch switch inside one checkout, which still gives ISS-037's own error (measured with 000017 removed: `no migration found for version 17`).
  - **Left by hand:** the old shared `rag-doc-isolation-tests-pgv16` stays on developer machines, and a branch that has not merged `main` since the fix still uses it.

**What happens.** `pkg/testing/isolation/container.go` names ONE container (`containerName`, :34) and reuses it by name (`WithReuseByName`, :120), so every worktree on a host shares it. When 22-02's run migrated the shared container to 000017 while 22-04's tree was still at 000016, golang-migrate refused every 22-04 run with `no migration found for version 17: read down for version 17 .: file does not exist` — the harness applies its own tree's migrations to a database already past them. 22-04 worked around it with a local, uncommitted rename of the constant until `main` (with 000017) was merged. The next pair of parallel plans with different newest migrations hits it again.

**Fix, recommended by the review and adopted:** derive the container name from the worktree by default — `containerName + "-" + shortHash(worktreeRoot)` — with `ISOLATION_CONTAINER_NAME` as an override. **Not an env-only override**: that recreates the collision the first time someone forgets to set it. Cost: one Postgres container per worktree; the harness removes nothing, so stale ones are `docker rm -f`'d by hand as today. `docs/local-development.md`'s "The Postgres image" section names the container and needs the derived form.

### ISS-031: A migration that sets a tenant leaves the migrating session unable to read RLS tables, and CI cannot catch it ✅

- **Discovered:** 2026-09-16, by the reviewer session on PR #37 (21-01). Independently reproduced there.
- **Type:** Correctness / Operability

- **✅ CLOSED 2026-09-29, at 22-02's execution.** Three things are true, each checked rather than cited:
  - **the fix is on `main`:** 22-01 merged as `4a3b16c` (PR #48). 000014 declares its key inside `CREATE TABLE` and 000013 adds its key before its loop, with the schema identical to what shipped before;
  - **the gate and its tenant audit run in CI:** `TestMigrationsApplyToASeededDatabase` is in `go test ./...` (`backend-ci.yml`'s whole-module step), seeded at 10, migrated in one session as `rag_doc_owner`, auditing every `ALTER TABLE`, and asserting FORCE intact and no key `NOT VALID` after the up;
  - **000017 is the first migration written under the rule, and the gate proved it:** every foreign key in 22-02's `000017_partitioned_chunks` (`chunks_repo_tenant_fk`, `symbols_repo_tenant_fk`, the single-column keys, `symbol_id`) is declared inside its `CREATE TABLE`, the 64 partitions inherit them by `PARTITION OF`, and the file runs in the gate's poisoned session with the audit empty. The mutation that moves `chunks_repo_tenant_fk` into an `ALTER TABLE` fails the gate at 17, dirty, with `22P02` (`22-02-SUMMARY.md`, M4). The migration's header carries the rule for the next author.

  **What stays open is not this issue:** the GUC behaviour itself (`''` after a committed `set_config`) is ISS-013's, and the rule for keys over existing data is written in 000014 §4, the gate's header and below, where an author reads it. The entry is kept whole for that reason.
- **Priority:** ~~MEDIUM — latent. 21-02's migration is DDL only, so nothing is broken today; the phase adds five more migrations.~~ **HIGH, and LIVE on `main` in the deployment shape. Corrected 2026-09-17.** The original "nothing is broken today" was wrong. It must be fixed before the first deploy, and 22-01 fixes it.
- **✅ FIXED FOR 13 → 14 IN 22-01 (2026-09-17, PR #48, in review), AND THE CLASS IS NOW GUARDED IN CI.** Evidence in `22-01-SUMMARY.md`.
  - **The fix:** 000014 declares `ingestion_jobs_repo_tenant_fk` inside `CREATE TABLE`. With comments stripped, the SQL diff against `main` is that move and nothing else.
  - **The gate:** `pkg/testing/isolation/migration_seeded_test.go`, `TestMigrationsApplyToASeededDatabase`. It runs in `go test ./...`, so in CI.
    - It seeds four organizations at version 10, as `rag_doc_app` under each one's tenant.
    - It uninstalls one installation at 12.
    - It then runs **one** `up` to the newest version, as `rag_doc_owner` (`NOSUPERUSER NOBYPASSRLS`), in one session.
    - It asserts 000013's, 000015's and 000016's outcomes as the superuser, and that every seeded row survived.
    - It also asserts its own premises: who owns the database and every table, and that FORCE is on. A gate that silently ran as the superuser passes the unfixed 000014 (mutation M8b).
  - **Failed before the fix,** on the commit that added the gate (`4a33b08`): `schema_migrations at 14 (dirty=true): SQLSTATE 22P02: migration failed: invalid input syntax for type uuid: ""`. The server log's `CONTEXT` is the foreign key's validation query, `SELECT fk."repository_id", fk."organization_id" FROM ONLY "public"."ingestion_jobs" fk LEFT OUTER JOIN ONLY "public"."repositories" pk …`.
  - **Passes after the fix** (`5677cc1`), three runs, each on its own scratch database, none left behind. The one-session `up` takes about 85 ms.
  - **The schema is identical:** 550 catalog lines match, `convalidated` included. The comparison tool is shown to catch a `NOT VALID` key.
  - **000015 leaves `''` too, measured.** A probe `000017` adding a composite tenant key by `ALTER TABLE` failed the gate at 17, dirty, `22P02`; the same key declared inside `CREATE TABLE` passed (M9, M9b). ~~So the rule below is enforced for every later migration, not only asserted.~~ **Corrected by PR #48's review (I1): that half of the gate is loud only because 13 and 15 run in its one session.** The same probe in a fresh session, the shape of every deploy after the first, reached 17 clean with the key marked valid (the review's measurement). What the gate enforces, and how, is itemised below.
  - ~~**The one hole, recorded:** a sentinel tenant set before an `ALTER TABLE … ADD CONSTRAINT` passes the gate vacuously (M3). Only review can catch that, and the rule forbids it.~~ **Closed by the tenant audit (2026-09-29):** M3 is now killed (`14: ingestion_jobs_repo_tenant_fk on public.ingestion_jobs validated under a tenant (tenant=…dead)`).
- **THE SECOND INSTANCE, IN COMMITTED 000013, FOUND BY PR #48'S REVIEW (I1) AND FIXED 2026-09-29.**
  - **Mechanism:** a migration file is one transaction, so the tenant 000013's loop sets with `set_config(…, true)` is still in force when the same file later runs `ADD CONSTRAINT repositories_project_org_fkey`. The key's validation read `repositories` through its policy under the **last** organization's tenant. In the gate that is delta, which owns no repositories, so it read nothing and passed.
  - **Measured (the review's MX13, re-run 2026-09-29):** 000013's backfill mutated to file alpha's four repositories under bravo's organization. With the key **after** the loop, as `rag_doc_owner` the one-session `up` reached **16, clean**, with the key marked valid over four rows that violate it; only the gate's superuser drift check and the 000015 assertions caught it. As the superuser the same mutation failed at 13 with `23503`.
  - **Harm today: none.** The backfill copies the value from the row the key points at, and the drift check covers 000013. The harm was in the three places that said the gate enforces the rule: 000014's section 4, the gate's header and this entry. All three now say what it guards.
  - **The fix:** `repositories_project_org_fkey` is added **before** the loop. Every `organization_id` is NULL there, so the validation has nothing to check (MATCH SIMPLE skips NULLs), and every value the loop writes is checked per row, which bypasses row-level security, against `projects`, which has no policy at all. **Measured:** MX13 now fails at **13, dirty, `23503`** in the deployment shape and as the superuser alike. The schema is identical to `main`'s (550 catalog lines, `convalidated` included), and up, down, up is clean. This is a second edit to an applied migration on the same nothing-is-deployed grounds; 000013's section 3 carries the justification, and what still runs after its loop (`SET NOT NULL`, the UNIQUE, the triggers) and why each is safe there.
  - **The tenant audit, now in the gate (`installTenantAudit`).** Two event triggers watch every `ALTER TABLE` from the first migration on and record each foreign key the command left validated that was not before, when either **(a)** the session's `app.current_tenant` was non-empty, or **(b)** the key's table has forced row-level security and held at least one row the key applies to (every key column non-NULL). No version is exempt: an exemption is where the next instance hides. **Measured:** it flags committed-before-the-fix 000013 (`13: repositories_project_org_fkey … tenant=…000d, checkable_rows=8`), the sentinel (M3, by (a)), and a key added over eight violating rows in a **fresh** session (by (b): `tenant=NULL, force_rls=t, checkable_rows=8`, where the up reached 18 clean). Disabling (a) lets M3 survive again; disabling (b) lets the fresh-session case through; so both clauses are load-bearing. It does not flag a key inside `CREATE TABLE`, a key added while its column is NULL (000013's shape now), or a key added with FORCE lifted for the statement (FORCE is off at that moment; measured: the same eight rows then fail the key with 23503). It deliberately ignores `SET NOT NULL`, CHECK and UNIQUE, which scan the heap and are not subject to row-level security (measured: a violating CHECK fails with 23514 and a violating UNIQUE with 23505 on the poisoned session). **Two limits of (b), from the second review:** it would also flag a FORCE table whose policy shows the owner every row (`USING (true)`), where the validation was in fact complete; none exists, every policy here is tenant-scoped. And it encodes the premise that the **owner** migrates, which the gate pins: FORCE is what subjects an owner to its policies, while a non-owner, non-BYPASSRLS migrating role is subject to them on any table with RLS enabled, FORCE or not. **For Phase 24's grant model:** if the migration role is not the owner, (b) should test `relrowsecurity` alone. **Two assertions beside the audit (2026-09-29, the second review's minors 2 and 3):** after the up, every table with RLS still forces it and every table that forced it before still does (a lift that never came back would otherwise survive every test; measured: a probe of just `ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY` passed the gate before it), and no foreign key is left `NOT VALID` (added without a validation and never validated: the audit, watching validations, sees nothing; measured: such a key passed the gate before it).
  - **What the gate guards, precisely:** (1) the `''` leftover, through the one-session run, which is why 13 and 15 must stay inside it; (2) any foreign key validated by `ALTER TABLE` under a tenant or through forced RLS, in any version, independent of the session. **What it cannot see:** DML that inherits a tenant, beyond what the 000013 and 000015 outcome assertions cover, and a validating trigger's reads.
  - **What remains open is the GUC behaviour itself, not this failure.** 000013 and 000015 still leave the migrating session at `''` (ISS-013's mechanism), and after the first production deploy an applied migration can no longer be edited. Closing this entry is the planner's call at merge.
- **Measured 2026-09-17, by the fact-check of the Phase 22 plans, then reproduced by the planner on a scratch `pgvector/pgvector:pg16`:**
  - **Setup:** a database owned by a `NOSUPERUSER NOBYPASSRLS` role, migrated to 10, seeded with two tenants' rows, then **one** `migrate up`.
  - **What happens:** 000013's loop leaves `app.current_tenant = ''`. `000014:158-161` then runs `ALTER TABLE ingestion_jobs ADD CONSTRAINT ingestion_jobs_repo_tenant_fk`, whose validation reads `repositories` through its policy under FORCE RLS as the owner and raises `22P02 invalid input syntax for type uuid: ""`.
  - **Result:** `schema_migrations` is left at **14, dirty**.
  - **A fresh session from 13 onward passes.**
  - **A superuser owner passes the same upgrade (measured),** because superusers bypass RLS even under FORCE. So the compose database, whose migrations run as its superuser, and CI's empty databases do not see it. **Production will**, since it will not migrate as a superuser.
  - The comment at `000014:150-155`, "foreign-key checks run with row-level security bypassed", is true of per-row checks and **not** of `ADD CONSTRAINT`'s validation.
- **The fix, scheduled in 22-01 and measured to work:** declare `ingestion_jobs_repo_tenant_fk` inside 000014's `CREATE TABLE`.
  - A new table has no rows to validate, so no validation query runs.
  - The same seeded deployment-shape upgrade then reaches 15, clean, with both tenants backfilled.
  - The resulting schema is **identical** to the original path's: a 295-line catalog dump matches exactly, including `convalidated`.
  - **Editing an applied migration is acceptable only because nothing is deployed.** Databases that already recorded 14 keep their identical constraint.
- **The rule for the class, as written in 000014's section 4 and the gate's header (revised 2026-09-29 with an answer for keys on existing tables):**
  - a key on a **new** table goes inside `CREATE TABLE`: no rows, no validation query;
  - a key on an **existing** table, for a column the migration **adds**: add the key before any tenant is set in the file, while the column is still NULL, then fill the column under each organization's own tenant. The validation has nothing to check and every written value is checked per row with RLS bypassed. 000013 has this shape now; never after a tenant-setting loop;
  - a key over data **already there**, where the key's table **or the table it references** forces RLS: the validation is one query joining the two, and as the owner each side is read through its own policy, whatever the tenant. NULL hides the key's table's rows, so violating rows go unchecked (measured: eight violating rows, key marked valid), and hides the referenced table's rows, so **correct** rows fail (measured by PR #48's second review and pinned by `TestForeignKeyValidationReadsTheReferencedTable`: eight correct pairs keyed to `repositories`, `23503` at 18 in a fresh session, `22P02` in the gate's one session); an organization sees one organization's rows on each side; `''` fails with `22P02`. The referenced side is this issue's original mechanism (`ingestion_jobs` had no policy; `repositories` did). There is no tenant that makes it correct. Lift FORCE, for that one statement, on **every table the validation reads that has it**, the key's table and the referenced table, as 000012 does, before any tenant is set (measured: the same eight pairs pass with FORCE lifted on `repositories`, FORCE restored, audit silent); the ACCESS EXCLUSIVE lock closes the window until the transaction ends. Put FORCE back in the same file: the gate now fails if any RLS table is left without it after the up, and if any foreign key is left `NOT VALID`;
  - never rely on the tenant an earlier loop left behind, and never set a sentinel tenant to make a validation pass.

  22-01's seeded gate migrates 10→12 separately (the seed needs version 12's `uninstalled_at`), then runs **every migration from 12 on in one session**, which is where the poison starts. **Both 000013 and 000015 leave the setting at `''`** (measured by the fact-check), so a later `ALTER TABLE … ADD CONSTRAINT` in the same run fails with `22P02`; 22-02's key-move mutation is killed exactly that way. The tenant audit above is what catches the same key in a fresh session.
- **Rejected alternatives:**
  - lifting FORCE around the constraint, which briefly disables a guard;
  - `NULLIF`-tolerant policies, which are ISS-013's territory, broad, and silent;
  - one session per migration, which every runner would have to honour.
- **What is wrong:** `000013`'s backfill sets `app.current_tenant` per organization with `set_config(..., true)`. That is the right call — it satisfies `trg_assert_tenant` and `FORCE ROW LEVEL SECURITY` without lifting either, and 000012's lift-FORCE pattern was measured raising 42501 here. But it leaves the migrating session with the **last organization's id** for the rest of that file, and `''` after it commits. This is ISS-013's hazard, now reachable from the migration path.
- **The failure it sets up:** a later migration, applied **in the same run**, that does any DML against a table with row-level security. It sees one organization's rows and reports success, or raises `22P02 invalid input syntax for type uuid: ""` and leaves `schema_migrations` dirty. **It passes on CI's empty database and fails on a database with rows,** which is the direction that trains people badly.
- **Why a comment is not enough:** the guard is four lines of comment in `000013`. Nothing fails if the next author doesn't read them, and nothing in CI applies migrations to a database that has rows.
- **Two candidate fixes, both measured in the review:**
  1. **A CI check that applies every migration to a seeded database.** Catches this whole class, not just this instance — including the 000012-pattern bug that CI's empty database would also have passed. The larger change, and the one with value beyond this phase.
  2. **Make the backfill GUC-free:** lift `FORCE` and `DISABLE TRIGGER trg_assert_tenant` for one statement under the `ACCESS EXCLUSIVE` lock the migration already holds. The reviewer ran this: it backfilled every row and left `app.current_tenant` NULL. It trades the trap for a briefly disabled guard, which is what 21-01 deliberately avoided.
- **Recommendation:** fix 1, before a later plan in this phase adds a migration with DML. Fix 2 only if fix 1 proves expensive.
- **Scheduled 2026-09-17 in 22-01** (fix 1): a Go test seeds a fresh database at migration 10, the compose database's measured version, and runs `up` as a `NOSUPERUSER NOBYPASSRLS` owner, before 22-02's migration exists (`22-CONTEXT.md` P13).
- **The migration this was filed in anticipation of has now shipped, and it complies by structure rather than by comment.** 21-04's `000015_backfill_ingestion_jobs` is the phase's first migration with DML. It sets `app.current_tenant` per organization in a `DO` block, exactly as `000013` does, and **the file ends with that block** — the only statements after it are comments, so there is no DML left to be poisoned by the tenant the loop leaves behind. It also sets the tenant itself rather than inheriting whatever `000013` left on the session, which is the other half of this issue's advice for a later migration in the same run. **That does not close this issue:** the guard is still that the author read the comment, and CI still applies migrations only to an empty database.
- **Related:** ISS-013 (the same GUC behaviour, from the pooled-connection side).

### ISS-016: `sync_state` has no lease, so a relink can re-queue a run already in flight ✅

- **Discovered:** Phase 20-03 second review (2026-09-09)
- **Type:** Correctness / Ingestion
- **Priority:** MEDIUM — must be settled **before Phase 21 builds the queue**, not after
- **Description:** `POST /api/repositories` sets `sync_state = 'pending'` when a repository's `installation_id` changes. If the row was `syncing` at that moment, it is re-queued while the original run is still going, and whichever finishes last writes the final state. `idx_repositories_sync_state` is a partial index on `sync_state <> 'synced'`, so the Phase 21 worker will pick the re-queued row straight up.
- **Why it was not simply avoided:** refusing to re-queue a `syncing` row is worse. The in-flight run holds an installation token for an App that was just uninstalled, so it will fail regardless — and leaving the row `syncing` strands it until that failure lands, with nothing to retry it.
- **The actual gap:** `sync_state` is a status column being used as a queue, with no lease, owner or attempt counter. Two writers can believe they own the same repository. 20-05's webhook writes go through the same upsert, so it inherits this.
- **~~What Phase 21 should do: give the queue a lease (`sync_lease_owner`, `sync_lease_expires_at`)~~** — superseded. Adding a lease to `sync_state` was the wrong fix; the column stops being a queue entirely. See the resolution below.
- **Also carried:** a `failed` repository cannot be retried through this API at all — re-connecting deliberately does not reset the state. Documented in `docs/api-repositories.md`. **Split out as ISS-023** — Phase 21 does *not* own retry.
- **NARROWED and SETTLED 2026-09-10; not yet shipped.** This issue is now **the racing-relink half only**. The second half — a `failed` repository cannot be retried through the public API — is split out as **ISS-023**, because an issue that half-closes never closes cleanly: the first draft of Phase 21 had three files giving three different answers about whether this closed when Phase 21 ships.
- **The resolution, locked in `.planning/phases/21-ingestion-job-infrastructure/21-CONTEXT.md` (L2, L4):** not a lease on `sync_state`. The root cause is that a *status column* was used as a *queue*, so Phase 21 introduces `ingestion_jobs` as the work item, demotes `sync_state` to a projection the job writes and the UI reads, and makes a relink **supersede** an in-flight job rather than race it. A partial unique index on `(repository_id) WHERE state IN ('queued','running')` makes two live jobs unrepresentable — the guard is in the schema, not only in the code path that remembers it.
- **⚠ Ordering matters and review caught it wrong the first time:** supersede **then** enqueue, both in one transaction. The reverse order raises 23505 against the non-deferrable partial unique index in exactly this issue's own scenario. **Corrected 2026-09-14:** that is true of a plain `INSERT`. Through L7's upsert, the only enqueue path, the reverse order raises nothing and silently leaves no live job. The order is still mandatory, and 21-02 pins both behaviours.
- **Progress, 2026-09-16 (21-03):** the CONNECT path is done. `POST /api/repositories` no longer writes `sync_state` as a way of asking for work; it classifies the call (new / relink / unchanged) in Go and goes through `pkg/jobs`, superseding any live job before enqueueing its replacement in the same transaction. `TestRepositoriesConnect_RelinkSupersedesARunningJob` is this issue's own scenario, and `TestRepositoriesConnect_ConcurrentRelinksLeaveOneLiveJob` races two of them.
- **Progress, 2026-09-16 (21-04): EVERY PRODUCER IS NOW ON THE QUEUE.** `push`, `installation_repositories` `added` and `removed`, and `installation` `deleted` all go through `pkg/jobs`; no webhook writes `sync_state` to ask for work, and `grep -rnE "sync_state *= *'pending'"` over non-test Go finds only `pkg/jobs/producer.go`'s projection. Three specifics worth recording, because each was a way this issue could have survived the phase:
  - **The `push` guard that "fixed" ISS-016 by losing work is gone.** `sync_state <> 'syncing'` dropped a push against a repository mid-sync. A push now JOINS the live job (L7): unclaimed, it will clone at the current HEAD; running, `needs_rerun` makes 21-05 re-queue it once.
  - **The bulk `installation_repositories.added` case is raced in a test.** Three known repositories, one already being ingested, against a concurrent relink of one of them, through a barrier, five rounds — every repository ends with exactly one live job.
  - **Migration 000015 backfills the rows the old path stranded.** A repository left `pending` before this plan had no job at all; it now has one, unless it is unsyncable (`installation_id IS NULL`, or an uninstalled installation), in which case it deliberately gets none.
- **Still open:** the consumer half. 21-05 and 21-06 write the state transitions and the worker runtime; 21-07 closes this issue with the admin endpoint.
- **CLOSED 2026-09-16 in 21-07, when Phase 21 shipped.** The racing half is
  gone, and the three things that make it gone were re-run on `main` before
  this line was written rather than cited from a summary:

  | Guard | Where | Evidence re-run on `main` |
  |---|---|---|
  | two live jobs for one repository are **unrepresentable** | `idx_ingestion_jobs_one_live_per_repo`, `UNIQUE (repository_id) WHERE state IN ('queued','running')` (migration `000014`) | `TestIngestionJobs_OneLiveJobPerRepository` — PASS |
  | **supersede before enqueue**, in one transaction | `pkg/jobs.SupersedeLive` then `pkg/jobs.Enqueue` | `TestIngestionJobs_SupersedeBeforeEnqueue` (all three cases, including the silent-loss one) — PASS |
  | this issue's own scenario, raced | `POST /api/repositories` | `TestRepositoriesConnect_RelinkSupersedesARunningJob` — PASS; `TestRepositoriesConnect_ConcurrentRelinksLeaveOneLiveJob` — PASS |
  | two first connects racing | `POST /api/repositories` | `TestRepositoriesConnect_ConcurrentFirstConnectsDoNotDoubleIngest` — PASS |
  | sixteen concurrent enqueues, five warm rounds | `pkg/jobs` | `TestEnqueue_ConcurrentEnqueuesResolveToOneLiveJob` — PASS |
  | the **bulk** case that once queued 1 of 3 while reporting success | `installation_repositories.added` racing a relink | `TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository` — PASS; `TestEnqueue_BulkRacingALiveJobHandlesEveryRow` — PASS; `TestIngestionJobs_BulkEnqueueRacingALiveJobHandlesEveryRow` — PASS |

  Nine tests, run together at `RAG-Doc/main` (`de6b6e9`) against PostgreSQL 16
  before any 21-07 code existed, all passing. `sync_state` is a projection
  now — written by the job, read by the UI — and no producer writes it to ask
  for work: `grep -rnE "sync_state *= *'pending'"` over non-test Go finds only
  `pkg/jobs/producer.go`'s projection.
- **What does NOT close with it.** Retrying a `dead` repository through the
  public API is **ISS-023**, split out by decision O1 and still open. The
  state machine makes the retry possible; exposing it was never Phase 21's
  deliverable, and pretending otherwise is what made this issue unable to
  close cleanly in its first draft.

### ISS-032: The drift self-test takes ACCESS EXCLUSIVE locks on shared tables, and deadlocks under CI's package-parallelism step ✅

- **Discovered:** 2026-09-16, on PR #38's CI (run 35118173356). First occurrence in fifteen Backend CI runs; the re-run of the identical commit passed.
- **Type:** Test reliability
- **Resolved:** 2026-09-16 in 21-03, which is the plan that made it likely enough to matter: it puts job producers in `pkg/api/handlers`, so more concurrent transactions touch `repositories` and `projects` at once.
- **The mechanism, as filed, re-confirmed:** `TestRepositoriesOrganizationID_DriftCheckDetectsDrift` manufactures drift by dropping a foreign key, and `ALTER TABLE repositories DROP CONSTRAINT repositories_project_org_fkey` takes `AccessExclusiveLock` on **`repositories` AND `projects`** — the referenced table, whose RI triggers it must remove. Other packages hold `AccessShare` on `projects` while taking `RowExclusive` on `repositories` (every connect does: it reads the organization's default project, then writes the repository), and fixture cleanup takes them the other way round. A cycle with no author at fault.
- **⚠ REPRODUCED LOCALLY, which the original entry said was not possible.** The filing recorded 16 local runs with zero deadlocks. Adding `./pkg/jobs/...` to the package-parallelism set — which 21-03's tests make a realistic thing to do — reproduced it on this machine: `go test ./pkg/api/... ./pkg/auth/... ./pkg/db/... ./pkg/jobs/... ./pkg/testing/... -count=3` at default parallelism failed with `still deadlocking after 3 attempts: ERROR: deadlock detected (SQLSTATE 40P01)`.
- **So fix direction 1 as filed — "a bounded retry on 40P01" — was tried first and measured INSUFFICIENT.** Three attempts on a 100ms linear backoff exhausted all three. Under four packages' worth of sustained traffic, a short fixed backoff just lands in the next burst.
- **What shipped, two halves:**
  1. **`SET LOCAL lock_timeout = 750ms`** on the transaction that does the DDL — deliberately *below* PostgreSQL's default `deadlock_timeout` of one second. The detector does not run until a transaction has waited that long, so a transaction that gives up first is normally not a deadlock victim and, more usefully, stops being one side of a cycle before a cycle can be reported. Waiting *longer* is the instinct and is what makes a deadlock the likely outcome instead of a timeout.
  2. **`isolation.RetryOnLockContention`** — six attempts, linear backoff with jitter, retrying **both** `40P01` and `55P03`. The other side's detector can still fire first and pick us, so both codes are retryable; `40001` deliberately is not.
- **Where it lives:** `services/backend/pkg/testing/isolation/deadlock.go`, with self-tests in `deadlock_test.go` — including one that causes a real PostgreSQL deadlock and one that measures that `LockWaitTimeout` produces a retryable `55P03`. A retry nobody has watched retry is a `for` loop with a comment on it.
- **Evidence after the fix:** the command that reproduced it, at `-count=5` (25 package runs, default parallelism, `./pkg/jobs/...` included), passed every time; the drift test never failed.
- **What is NOT established:** which half does the work. A mutation removing only the `lock_timeout`, leaving the six retries, also passed 5 rounds — the original deadlock was a one-in-N event and this machine could not reproduce it again on demand. Both halves are cheap, complementary and argued from the lock model rather than from that one measurement.
- **Related:** ISS-010 (the same step, the same shared container).

### ISS-030: Search returns partial or empty results as a success when one retriever fails ✅

- **Discovered:** 2026-09-14, while re-verifying a benchmark measurement. Reproduced on purpose.
- **Type:** Correctness / Reliability
- **Resolved:** 2026-09-14. The decision was to fail loudly rather than return a result flagged as degraded.
  - **Retrieval:** `QueryEngine.query` raises `RetrievalError` (`workers/retrieval/errors.py`, exported from `workers.retrieval`) when keyword or vector search fails. It names the failed retriever(s), chains the original exception and logs at error level. No partial result is returned, and the `fts_error` / `vector_error` metadata fields are gone.
  - **Routes:** `/search` and `/chat` map it to 503 with a fixed, retryable detail, such as "Search is temporarily unavailable (vector search failed); please retry". `/chat/stream` sends the same message as an SSE error frame. Any other exception now gets a fixed 500 detail or error frame instead of `str(e)`, and the full error is logged server-side.
  - **Chat:** `AnswerGenerator` lets the error propagate. An outage can no longer come back as "I don't have enough information", and a failure never reaches the cache write-back.
  - **Go backend:** no change needed. `search.go` already maps any RAG client error to a generic 503, and `chat.go` relays error frames as they arrive.
- **Guarded by:**
  - `workers/retrieval/test_query_engine.py` (7 tests): a failure in either retriever raises `RetrievalError` naming it, and both succeeding returns results.
  - `workers/generation/test_answer_generator.py` (7 tests): the error propagates and is not cached, and a failed semantic cache lookup or write never decides the response.
  - `tests/api/test_routes_retrieval_failure.py` (23 tests): 503 or an error frame on each route with no error text, and a generic 500.
    - `test_iss030_a_failing_retriever_cannot_produce_a_silent_200` runs the real `QueryEngine` and `AnswerGenerator` behind the routes.
    - Further tests check that the cause's text is logged exactly once per request, and that a failed cache lookup still yields the 503.
  - `tests/api/test_routes_query_validation.py` (36 tests): control characters are a 422 on each route, before the engine is called. Tab, newline and carriage return are accepted.
  - `pkg/api/handlers/rag_errors_test.go` and `query_text_test.go` (5 Go tests): the backend's 503 mapping, its error-frame relay, and the 400 for control characters.
  - **Mutation-verified**, each on a copy of `services/workers`. The final run used the 77 tests in `tests/api` and the two unit-test files:
    - restoring partial results fails 16 tests
    - mapping `RetrievalError` to 500 fails 11
    - putting `str(e)` back into the 503 detail fails 9, and into the stream's error frame, 5
    - catching it inside `AnswerGenerator` fails 14
    - removing the query validator fails 27; rejecting only NUL fails 15; also rejecting tab, newline and carriage return fails 9
    - a route logging the cause again fails 1 to 2 tests; repeating it in the engine's log message fails 3
    - removing the cache lookup guard fails 7, and the write guard, 1
    - in Go, removing the check from both handlers fails both rejection tests, in all 16 subtests
- **Measured against the local stack with a rejected OpenAI key:**
  - **Before:** `QueryEngine.query` raised nothing and returned 0 results. `/search` answered 200 with no results; `/chat` and `/chat/stream` answered "I don't have enough information".
  - **After:** `QueryEngine.query` raises `RetrievalError` naming vector search, with the 401 as its cause. `/search` and `/chat` answer 503, and `/chat/stream` sends a single error frame. None of them carries error text.
  - **The harness's self holdout scores are unchanged** with the real key: 12/15 in top 5, 7 at #1, MRR 0.622, and the same rank for every question.
- **A leak this entry had missed.** On the old code, `/search`'s 200 response carried `metadata.vector_error`, and with it the OpenAI error text: "Incorrect API key provided", `sk-` and the key's last four characters. The Go backend passes `metadata` through to its own clients. Removing the field closed it. Measured with a deliberately invalid key, so no real key was exposed.
- **Follow-ups from the PR #34 review, fixed in the same PR:**
  - **A NUL in the query became a permanent 503.**
    - psycopg2 cannot bind U+0000, so keyword search raised `ValueError`, and `QueryEngine` reported it as an outage: "please retry" forever, with an ERROR traceback on every request. On the old code the same query returned vector-only results.
    - Now `SearchRequest` and `ChatRequest` reject U+0000, and every other C0 control character except tab, newline and carriage return, with a 422.
    - The Go backend applies the same rule on `/api/search` and `/api/chat/stream` with a 400. Without it, Go would turn the Python 422 into a 503.
  - **The cause's text was logged about three times per request.** `QueryEngine` now logs each retriever failure once, with its traceback. The routes log a one-line outcome naming the retriever, with no exception text.
  - **A failed semantic cache lookup would have made `/chat` a 500.**
    - The lookup embeds the query through OpenAI before retrieval, so an OpenAI outage escaped as an internal error.
    - A failed lookup or write now logs a warning without exception text and continues.
    - This was latent, because the cache never runs (ISS-021).
  - **Not changed:**
    - `QueryEngine` still wraps any retriever exception as `RetrievalError`, including input-shaped ones such as `ValueError`. Boundary validation makes the known case unreachable through the API.
    - The 503 detail still names the failed retriever. That was intended, and the reviewer did not object.
    - `api/routes.py` still imports `RetrievalError` at module load.
- **Priority when open:** HIGH before anything user-facing depends on search.
- **What happened:** `QueryEngine.query` caught either retriever's exception and fused whatever the other found. The failure survived only as `metadata.fts_error` or `metadata.vector_error`, which nothing read: not the routes, not `AnswerGenerator`, not the Go `RAGClient`.
  - With a rejected OpenAI key it returned 0 results for a question whose answer normally ranks #4. Keyword search returns nothing for most natural-language questions (ISS-029), so a vector failure usually meant no results at all.
  - In a heavily loaded run, 6 of 130 benchmark queries came back empty this way, and each ranks #1-#4 when re-run.
- **Not done:**
  - No retry of the embedding call.
  - The cause of the loaded-run failures was never captured. Qdrant logged no failed searches, so a failed embedding call is likely, but unconfirmed.


### ISS-022: No CI job runs the Python worker tests ✅

- **Discovered:** 2026-09-10, while adding the ISS-020 regression guard and looking for the job that would run it.
- **Type:** Testing / CI
- **Resolved:** 2026-09-10 by `.github/workflows/workers-ci.yml`. Runs `pytest tests/` on every PR with a Redis service container; testcontainers provisions its own Postgres. **Verified by a real run, not by inspection: 24 passed in 12.25s** (run 34540233783). Every test was already passing locally — none were broken, they had simply never been executed.
- **Priority when open:** MEDIUM-HIGH — it silently voided a whole directory of security tests.
- **What is missing:** `.github/workflows/` contains only `backend-ci.yml` (Go: build, vet, test, race) and `isolation-check.yml` (runs `scripts/ci/check-isolation-tests.py`, a diff scanner that is itself Python but executes no test suite). **Nothing runs `pytest`.**
- **Consequence:** everything in `services/workers/tests/isolation/` — the Python half of the tenant-isolation guarantee — has never been executed by CI. The Go isolation tests gate every PR; their Python counterparts gate nothing.
- **It is worse than untested, because it looks tested.** The isolation CI gate accepts a Python isolation test as coverage for a Python mutation endpoint. So a test that never runs can satisfy the ratchet that exists to force real coverage.
- **Second-order:** the `services/workers/venv` did not have `testcontainers` installed even though `requirements.txt` declares `testcontainers[postgres]>=4.0.0`, and `tests/isolation/conftest.py` imports it at module scope. So the directory could not be collected locally either — no import error had ever been surfaced by anything.
- **Same class as a failure already recorded in STATE.md:** `pkg/vectordb` stayed uncompilable from Phase 3 to Phase 19 because nothing in CI invoked a compiler. This is that, for Python.
- **What was done:** added `workers-ci.yml` running `pytest` with a Redis service container and Docker available for testcontainers. The ISS-020 guard (`tests/isolation/test_semantic_cache_isolation.py`) needs only Redis and `REDIS_URL`, so it can gate immediately; the Postgres-backed tests need Docker-in-CI and may need work before they pass.
- **The ISS-020 guard now enforces.** It was documentation until this landed.
- **The guard is now written to fail rather than skip** when `REDIS_URL` is set and Redis is unreachable, so once a CI job exists it cannot report green while guarding nothing. With `REDIS_URL` unset it still skips, which is the courtesy for a developer machine with no Redis.


### ISS-020: The semantic cache key is not tenant-scoped ✅

- **Discovered:** 2026-09-10, by the reviewer session on PR #24. **Substantially corrected 2026-09-10** after the reviewer session on PR #26 checked the original entry — see the corrections note at the end, which matters more than the finding.
- **Type:** Security / Tenant isolation
- **Resolved:** 2026-09-10. Key is now `cache:query:{hash}:{organization_id}:{repository_id}`; all repo-scoped scan patterns carry the organization; `clear_cache`/`get_cache_stats` refuse a falsy organization and require `all_tenants=True` for a global flush; the stored `organization_id` is re-checked on read. Guarded by `services/workers/tests/isolation/test_semantic_cache_isolation.py` (8 tests, mutation-verified). That guard runs in CI as of 2026-09-10 (ISS-022).
- **Priority when open:** MEDIUM (latent). **Not currently exploitable, because the cache never runs** — see ISS-021. It becomes live the moment that is repaired.
- **The chain, each link verified:**
  1. `semantic_cache.py:149` writes keys as `cache:query:{query_hash}:{repository_id}`; `:70` reads by scanning `cache:query:*:{repository_id}`. **No organization component in either.**
  2. `answer_generator.py:104-130` consults the cache at the top of `generate()`. A hit returns at `:130` having touched only Redis — so the tenant-scoped re-read in the retrieval path never runs.
  3. `pkg/api/handlers/chat.go:32` takes `repository_id` from the request body with a UUID **format** check and no ownership query.
- **Reachable only via `POST /api/chat/stream`.** `/api/search` does **not** reach the cache — it calls `query_engine.query()` directly. (The original entry cited `search.go`; that was wrong. The same missing-ownership-check weakness exists there, but it is not part of this chain.)
- **Matching is similarity, not equality.** `get_cached_response` scans every entry for the repository and returns the best above a 0.95 cosine threshold; the `query` argument is not used for matching. A *near*-identical question is enough — the original entry's "matching query hash" precondition was too narrow.
- **What partially contains the retrieval path (and not this one):** `_enrich_results_with_metadata` (`query_engine.py:286-348`, called unconditionally at `:202`) re-reads chunk ids under `require_tenant` and drops rows RLS withholds. Genuinely partial — the metadata counts at `:216-223` bypass that filter, and the Qdrant leg at `:264-281` is org-unscoped. The cache sits in front of all of it.
- **Exploit preconditions, stated honestly:** an authenticated tenant, plus the victim's `repository_id` (a v4 UUID, not disclosed cross-tenant), plus the victim having asked a near-identical question about that repository inside the 1-hour TTL, plus the cache being operational. An attacker cannot self-prime the cache: the no-chunks path returns before the write-back.
- **Why file it anyway:** the only control is the unguessability of an identifier. That is the same reasoning rejected in 20-04 — `GetInstallation` proved an installation was *real*, not that the caller *controlled* it (`20-04-SUMMARY.md:25`). We decided that once.
- **Fix as shipped:** `organization_id` was already in scope at `answer_generator.py:76`. Add it to the key, and to **all three** scan patterns (`semantic_cache.py:70,179,244`) — miss those and invalidation silently stops matching.
- **Regression guard — placement matters.** `chat_isolation_test.go:126` already has a cross-tenant scenario and passes, because it stubs the Python side; adding to it would be vacuous. The guard belongs in `services/workers/tests/isolation/`.
- **Why the CI gate will not catch it:** not because it ignores POSTs — `check-isolation-tests.py` does match `Post`. It is diff-scoped to newly-added route registrations, treats coverage as a textual path mention, and models *routes*, not *data paths*. A leak inside a cache layer behind an existing route is outside what it can see. (The original entry's reasoning here was wrong; the conclusion was right.)
- **⚠ D2/R6 does not fix this.** That decision puts RLS over the retrieval path via pgvector. The cache is Redis and sits in front of Postgres.
- **Related, separate disposition:** `pkg/vectordb`'s `DeleteByChunkID` (`vectordb/client.go:411-437`) filters on `chunk_id` only, with no tenant predicate. Verified latent — the package has zero importers. Note that D2 removes the Qdrant path entirely, so **deleting `pkg/vectordb`** may be the correct resolution rather than adding a predicate.
- **Corrections to the original entry, recorded deliberately.** It was filed as HIGH and "live today". Both were wrong: the cache does not run, and the precondition chain is narrower in one way (needs an operational cache) and broader in another (near-match, not exact). It also cited the wrong endpoint and the wrong reason the CI gate misses it. The errors came from repeating a report without independent verification — the same habit that produced blocking reviews on PR #24 and PR #25. Kept visible here rather than quietly rewritten.


### ISS-008: Request-scoped tenant transaction for DB-hitting endpoints ✅

- **Discovered:** Phase 17-02 (2026-09-06)
- **Closed:** 2026-09-08 (Phase 20-01)
- **Type:** Architecture / Correctness
- **Original problem:** `TenantMiddleware` once tried to `SET LOCAL app.current_tenant` on a pool-acquired connection and released it before the handler ran — broken twice over, since `SET LOCAL` outside a transaction is a no-op and pgx rejects a parameterized `SET`. It was removed in 17-02, leaving no way for a Go handler to query an RLS-scoped table.
- **Verified blocking, not theoretical:** the only Go handler touching the database before this phase was `user_orgs.go`, which reads `users` / `organizations` / `organization_memberships` — none RLS-scoped. **No Go handler had ever read an RLS-scoped table.** `GET /api/repositories` (20-03) is the first.
- **Resolution: option 2, hardened.** `db.TenantScoper` opens a transaction, sets `app.current_tenant` from the verified claim, and runs the handler's callback inside it. Option 1 (middleware-opens-transaction) was rejected: it would hold a pooled connection and an open transaction for the life of every authenticated request, including `/api/chat/stream`, and it couples commit to HTTP status. Full comparison in `20-01-DESIGN.md`.
- **The hardening is the part that matters.** Option 2's weakness is that a handler can forget. So tenant-scoped handlers are constructed with a `*db.TenantScoper` and **not** a `*pgxpool.Pool` — there is no unscoped path through the type, and giving a handler a pool becomes a visible act in `router.go` rather than an omission inside a handler. Same principle as 19-03 deleting the `X-Organization-ID` header instead of deprecating it.
- **Files:** `services/backend/pkg/db/tenant.go` (new), `pkg/db/tenant_isolation_test.go` (new), `pkg/auth/middleware.go` (the reserved `db *pgxpool.Pool` parameter is gone, not ignored), `pkg/api/router.go`, `docs/isolation.md`.
- **Surfaced ISS-013** — the failure mode for bypassing this turned out to be nondeterministic rather than merely silent, which is why the type refuses to hand out a pool rather than merely documenting that you shouldn't use one.
- **Mutation-verified:** removing the `SET LOCAL` turns four tests red.

### ISS-009: `pkg/vectordb` does not compile against its pinned Qdrant client ✅

- **Discovered:** Phase 19-03 (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work)
- **Resolution:** bumped `github.com/qdrant/go-client` v1.7.0 → v1.19.2 and fixed three points of API drift — `CreateFieldIndex` now returns `(*UpdateResult, error)`, and `NewIDString` became `NewIDUUID`. The package was written against the high-level client API introduced in v1.9, so the pin had *always* been wrong. Never a regression; it simply never compiled.
- **What fixing it exposed:** with the package building, its own unit tests ran for the first time and **panicked**. `TestUpsertVectorsValidation` builds a zero-value `Client{}`, and its "matching lengths" case — the one meant to prove validation *accepts* good input — necessarily proceeds past validation into the wire call, dereferencing a nil connection. `TestSearchSimilarValidation` had the identical latent bug and had never run at all, because the first panic killed the test binary.
- **Also fixed:** validation split into `validateUpsertInput` / `validateQueryVector` so the rules are testable without a live Qdrant, plus an `ErrNotConnected` guard so a clientless call returns a legible error rather than panicking several frames inside the SDK.
- **Root cause of the invisibility:** no CI job had ever built the Go code. Closed alongside the new `.github/workflows/backend-ci.yml`.

### ISS-010: Isolation harness setup races when test packages run in parallel ✅

- **Discovered:** Phase 19-03 (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work)
- **Resolution:** `ensureAppRole` now runs its whole statement sequence in one transaction holding `pg_advisory_xact_lock`. All five statements are covered, not just the GRANTs — the `DO $$ … CREATE ROLE` block is check-then-act and races the same way, it just failed less visibly (SQLSTATE 42710 instead of XX000).
- **Why transaction-scoped, not session-scoped:** `CREATE ROLE` and `GRANT` are both transactional in Postgres, so the sequence commits or rolls back as a unit, and the server releases an xact lock however the process dies. A session-level `pg_advisory_lock` leaks if a test binary panics between acquire and release.
- **Precedent:** the same mechanism golang-migrate already uses around migrations — which is exactly why migrations survived the concurrency that broke role setup.
- **Verified:** 5 consecutive **cold-container** parallel runs, zero failures. Cold is the case that matters: the race reproduced on roughly 40% of cold starts and effectively never on a warm container, so its failure profile was "green locally, red in CI".

### ISS-011: OAuth callback routes are live and broken, and bypass org-context push ✅

- **Discovered:** Phase 19-03 review (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work) — **unmounted, not repaired**
- **Resolution:** `/auth/{github,gitlab}/{login,callback}` are no longer mounted. They were served whenever Redis happened to be reachable, and every completed callback returned 500 — GitHub's numeric user id fails the `uuid.Parse` provisioning has done since 19-02.
- **Why unmount rather than fix:** repairing the 500 alone would have been worse. These handlers are a second provisioning path that never calls `pushOrgContext`, so a user created through them would have no organization claim and be refused by every tenant-scoped route — turning a loud 500 into a quiet broken account.
- **Handlers kept, not deleted.** Deleting is a planner/user call, and they stay useful as reference if direct OAuth is ever wanted alongside Supabase-native. Reviving them needs a non-UUID identity column in provisioning plus routing through the same post-provision org-context push the webhook uses.
- **The StateStore probe is retained** — it reports a real configuration gap, and Phase 20's GitHub App flow will want it.

### ISS-004: Organization selection mechanism for multi-org users ✅

- **Discovered:** Phase 4 (Authentication System)
- **Closed:** 2026-09-08 (Phase 19-04; security half closed in 19-03)
- **Type:** User Experience / Authorization
- **Original problem:** Users belonging to multiple organizations had no way to choose which org context they operate in beyond the `X-Organization-ID` header. Needed: (1) an endpoint listing the user's organizations, (2) a frontend picker, (3) the selection stored in a JWT claim, (4) middleware reading the claim instead of the header.
- **Done in 19-03:** items (3) and (4). `app_metadata.organization_id` is written onto the Supabase user and read back off the verified JWT by `TenantMiddleware`; the header path deleted, not deprecated.
- **Done in 19-04:** item (1) plus the switching mechanism — `GET /api/user/organizations` and `POST /api/user/select-organization`, both user-scoped and deliberately outside `TenantMiddleware` so a user with no organization claim can still reach them. Item (2), the frontend picker, is Phase 23 work implementing `docs/auth-frontend-contract.md`; the backend contract it needs is complete and documented, so this issue is closed rather than left open on UI.
- **Note on the original implementation sketch:** it called for a Supabase Auth Hook to inject the claim at token-mint time. Impossible in this architecture — the hook is a Postgres function inside Supabase's instance and `organization_memberships` lives in a different one. The backend writes `raw_app_meta_data` instead.
- **Verified against the live project (2026-09-08):** `refreshSession()` genuinely re-reads `raw_app_meta_data`, so 202 → refresh → new claim works and a full sign-out/sign-in is not required.
- **Security note carried forward:** switching writes BOTH `organization_id` and `organization_role`, because Supabase merges `app_metadata` and omitting the role leaves the previous one in place. See 19-04's summary; the escalation this prevents is pinned by a test.
- **Successor issue:** ISS-012 — a *revoked* membership still does not revoke the claim. Out of scope here (nothing removes memberships yet).

### ISS-007: JWT-carried tenant claim (supersedes header trust) ✅

- **Discovered:** Phase 17-02 (2026-09-06)
- **Closed:** 2026-09-08 (Phase 19-03)
- **Type:** Security / Authorization
- **Priority:** HIGH before v1 public rollout
- **Original problem:** `TenantMiddleware` sourced the caller's tenant from the `X-Organization-ID` request header. Any authenticated user could set it to any org id and the middleware forwarded it downstream unchecked — a valid login for one organization could read another organization's data. 17-02 pinned the behavior in `search_isolation_test.go` scenario 5 rather than leaving it undetected.
- **Resolution:** Tenant identity now comes exclusively from `app_metadata.organization_id`, a Supabase-signed claim on the access token. The header path is **deleted**, not deprecated — including from the CORS `Access-Control-Allow-Headers` list, so a client cannot even send it. A token with no organization claim gets 403 rather than defaulting into anyone's org.
- **Files:**
  - `services/backend/pkg/auth/supabase_admin.go` (new) — writes `app_metadata` onto the Supabase user via the admin API
  - `services/backend/pkg/auth/jwt.go` — `ExtractOrganizationID` / `ExtractOrganizationRole` read the nested claim and require a UUID
  - `services/backend/pkg/auth/middleware.go` — header read replaced by claim read
  - `services/backend/pkg/auth/webhook.go` — pushes org context after provisioning
  - `services/backend/cmd/backfill-org-claims` (new) — the repair path for a failed push
  - `services/backend/pkg/api/router.go` — wires the admin client, drops the header from CORS
- **Deviation from the original plan, deliberate:** no Supabase Auth Hook, and no per-request membership re-check. See the 19-03 summary for the reasoning on both; the short version is that the hook cannot reach the app's database (separate Postgres instance), and re-querying membership on every request would add a DB round-trip to the hot path to defend against an attacker who would already need Supabase's signing key.
- **Correction to an earlier version of this entry.** It described the org-context push as converging "on every webhook delivery". That is false and was caught in review: Supabase database webhooks fire once and never retry (recorded in `04-06-SUMMARY.md`), and the trigger behind ours is `AFTER INSERT ON auth.users`, so each user gets exactly one delivery for all time. A failed push therefore leaves that user permanently claim-less. The code is replay-safe, but nothing replays it — `cmd/backfill-org-claims` is the actual repair, and it is also the migration step for users provisioned before the claim existed.
- **Second correction.** The residual risk of skipping the per-request membership check was described as "the token lifetime window". Also false: `raw_app_meta_data` is written once and never recomputed, so Supabase re-reads the same stale value at every mint and a *refreshed* token carries the *same* organization. Removing a user from an org does not expire their claim — short token TTLs do not mitigate this at all. Nothing removes memberships today, but **19-04 must rewrite the claim on every membership change**; that, not token expiry, is what bounds the exposure.
- **Verified:** the claim shape was confirmed against the live Supabase project by round-trip (admin write → sign in → decode token), not assumed. `testjwt.Sign` now emits the same nested shape, with a drift-guard test asserting the flat shape is *not* emitted — the pre-19-03 harness signed flat claims, so every isolation test passed against tokens production could never receive.
- **Tests:** 11 isolation scenarios across `search_isolation_test.go` (7) and `chat_isolation_test.go` (4), plus unit coverage for claim extraction. Scenario 7 is the regression guard for this very issue: with the header path re-added to the middleware, it is the only test in the suite that fails. Both it and scenario 5 were confirmed non-vacuous by mutation — breaking the middleware turns them red.

### ISS-006: Test database connectivity configuration ✅

- **Discovered:** Phase 4 Plan 4 (Integration Tests)
- **Closed:** 2026-09-05 (Phase 17-01 — Go isolation harness)
- **Type:** Infrastructure / Testing
- **Priority:** LOW (superseded by a better approach)
- **Original problem:** Integration tests could not connect to the docker-compose Postgres from the Windows host. Structurally the tests were correct but the connectivity story was flaky.
- **Resolution:** Replaced the docker-compose dependency entirely with `testcontainers-go`. Every test now spins up (or reuses) an ephemeral Postgres via the Docker API — no host port binding, no `pg_hba.conf` tuning, no host-vs-container URL split. Migrations are applied programmatically via `golang-migrate`. Container reuse across `go test` invocations keeps the second-and-onward run under ~1.5s.
- **Files created:**
  - `services/backend/pkg/testing/isolation/container.go` — `SetupTestDB` and container reuse
  - `services/backend/pkg/testing/isolation/migrator.go` — programmatic migration runner
- **Verified:** Works from Windows host without docker-compose running; two consecutive `go test` invocations complete in <5s.

### ISS-003: OAuth state validation (CSRF protection) ✅

- **Discovered:** Phase 4 (Authentication System)
- **Closed:** 2026-01-13 (Phase 4 - Security Fix)
- **Type:** Security / Authentication
- **Priority:** HIGH (security gap)
- **Description:** OAuth handlers were generating CSRF state tokens but not validating them on callback, creating CSRF vulnerability.
- **Resolution:** Implemented StateStore using Redis with 5-minute TTL. State tokens are:
  - Generated on login and stored in Redis (key: `oauth:state:{token}`)
  - Validated on callback (checks existence in Redis)
  - Single-use (deleted after validation)
  - Auto-expire after 5 minutes (TTL)
- **Files Created:**
  - `services/backend/pkg/auth/state_store.go` - Redis-based state storage
  - `services/backend/pkg/auth/state_store_test.go` - 5 tests verifying CSRF protection
- **Files Modified:**
  - `services/backend/pkg/auth/handlers.go` - Updated all OAuth handlers to use StateStore
  - `services/backend/.env.example` - Added REDIS_URL configuration
- **Tests Added:**
  - TestStateStore_StoreAndValidate: Basic functionality
  - TestStateStore_SingleUse: Tokens work once only
  - TestStateStore_InvalidToken: Unknown tokens rejected
  - TestStateStore_ExpiredToken: Tokens expire after TTL
  - TestStateStore_CSRFProtection: CSRF attack prevention verified
- **Impact:** CSRF vulnerability closed, OAuth flow now secure
