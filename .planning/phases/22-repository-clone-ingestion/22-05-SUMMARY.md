---
phase: 22-repository-clone-ingestion
plan: 05
subsystem: workers + backend + compose
tags: [ingestion, worker, endings, lease, tokens, revocation, pgvector, compose, live-proof, mutation-testing]

requires:
  - phase: 21-05
    provides: "the transitions (claim, complete, fail, defer, abandon, sweep), their fences, and sanitize_error"
  - phase: 21-06
    provides: "the Worker: loop, heartbeat, give-up rules, Unfinished, the fail-closed entrypoint and its refusal test"
  - phase: 22-02
    provides: "partitioned chunks with a vector and a model per row; PostgresWriter; P17 (retrievals no longer reference chunks)"
  - phase: 22-03
    provides: "both retrieval legs on pgvector as the app role; QueryEngine(postgres_conn, openai_api_key); app_dsn"
  - phase: 22-04
    provides: "the internal token route (marked responses, one 404, two 409s), request_token, fetch_repository under U6/U7, the hand-off in section 15"
provides:
  - "workers.ingest.handler: the full_ingest handler (fetch -> parse -> embed -> store), IngestDeps, deps_from_env, configure; registered for both job types"
  - "the runtime's endings: Rejected (reject, dead in one attempt), InstallationSuspended and InstallationUninstalled defined in the runtime and settled mid-run as at claim time, write_results failures settled instead of stranded"
  - "transitions.reject and its fenced REJECT_SQL"
  - "workers.fetch.revoke_token (DELETE /installation/token, never raises), called when every fetch ends"
  - "workers.fetch.archive.expected_top_levels_for: both measured archive directory names, public (sha7) and private (full SHA)"
  - "PostgresWriter.insert_chunks_on / delete_repository_chunks_on / complete_ingestion_run_on and the shared statement constants; content_hash"
  - "P16, provisional: max_job_duration 2 h, heartbeat statement_timeout 15 s merged into the DSN's options, two worker processes; each with an override"
  - "workers/__main__: build_worker, operating_numbers, the ingest-configuration refusal, the startup sweep"
  - "compose: workers behind the ingest profile with no App key; the backend's internal listener on backend:8081, exposed, never published"
  - "the backend's mint log line carries the scope GitHub reported (reported_repository_ids, reported_permissions)"
  - "22-05-live-proof.md: AlecAsdourian/ES-SC-API-Navigator indexed end to end as rag_doc_app in a scratch database"
affects: [22.1-01 (write_results is the place symbols join the store), 22.1-02 (incremental is the full ingest until then; per-file currency), 22.1-03 (the progress keys and the cumulative rule are the contract's starting point), 22.1-05 (P16's numbers and the first data point), 23 (the syncing-hour gap and the job row as the truth), 24 (secrets in compose, grants, log access, the internal listener)]

tech-stack:
  added: []
  patterns:
    - "A handler that finds the job is not its own raises LeaseLost; it never returns, because the completion fence does not check expiry"
    - "Every progress report carries the whole cumulative dict, because the column is replaced whole"
    - "Connection options are merged into the DSN's own (parse_dsn + make_dsn), never passed as a replacing keyword"
    - "A credential the worker holds is revoked the moment the step that needs it ends, on every path, and the revocation never fails the job"
    - "A mutation tool's proof reads bytes: text mode translates CRLF and can make a proof that cannot fail"
    - "A live proof's first real call is where a check validated against one upstream shape meets another"

key-files:
  created:
    - services/workers/workers/ingest/__init__.py
    - services/workers/workers/ingest/handler.py
    - services/workers/tests/ingest/__init__.py
    - services/workers/tests/ingest/fakes.py
    - services/workers/tests/ingest/test_handler.py
    - services/workers/tests/isolation/test_ingest_end_to_end.py
    - services/workers/tests/test_compose_environment.py
    - .planning/phases/22-repository-clone-ingestion/22-05-live-proof.md
    - .planning/phases/22-repository-clone-ingestion/22-05-live-proof-scripts/ (seven files, verbatim, at PR #58's review)
  modified:
    - services/workers/workers/jobs/runtime.py
    - services/workers/workers/jobs/transitions.py
    - services/workers/workers/jobs/handlers.py
    - services/workers/workers/jobs/__init__.py
    - services/workers/workers/__main__.py
    - services/workers/workers/fetch/__init__.py
    - services/workers/workers/fetch/archive.py
    - services/workers/workers/fetch/client.py
    - services/workers/workers/storage/postgres_writer.py
    - services/workers/tests/isolation/test_job_worker_runtime.py
    - services/workers/tests/isolation/test_job_transitions.py
    - services/workers/tests/fetch/test_archive_hostile.py
    - services/workers/tests/fetch/test_fetch_client.py
    - services/backend/pkg/github/client.go
    - services/backend/pkg/github/scoped_token_test.go
    - services/backend/pkg/internalapi/repository_token.go
    - services/backend/pkg/internalapi/repository_token_isolation_test.go
    - docker-compose.yml
    - docs/api-ingestion-jobs.md
    - docs/internal-api.md
    - docs/local-development.md
    - .planning/phases/22-repository-clone-ingestion/22-ACCEPTANCE.md (A10's qualification, at the review)
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "Both job types run the full ingest until 22.1-02; the registry imports the ingest module when a job runs, which breaks an import cycle (workers.fetch -> workers.jobs -> handlers -> ingest -> workers.fetch), and __main__ imports and configures it at startup so a broken build fails before a claim"
  - "Stages are reported on entry, after a checkpoint, so last_stage is the stage the job was in when it stopped; store is reported last and a completed job reads store"
  - "The cumulative progress is one running dict; each stage's own keys first appear in the NEXT report (fetch reports {}), and store adds its own before reporting"
  - "A chunker exception on one file is skipped and counted (parse_errors), as IngestionPipeline always did, rather than dead-lettering the repository; but when EVERY indexable file raises, the job fails (ParseFailed, retried) instead of replacing a good index with nothing (PR #58's review, B-M3)"
  - "An archive member whose name holds a control character (Unicode Cc: C0, DEL, C1) is refused as unsafe_path, so no customer file name reaches a log line, the disk or chunks.file_path (PR #58's review, A-L1)"
  - "Compose gives the worker stop_grace_period 2m and restart on-failure:5; the values' reasons sit beside them (PR #58's review, A-L4)"
  - "The heartbeat's options start from PGOPTIONS when the DSN names none, as libpq would; the scope check accepts metadata beside contents only at read (PR #58's review, A-N3 and A-N2)"
  - "Distinct chunk texts are embedded once, in slices of 1,000 with a checkpoint between, and with use_cache=False so a long-lived worker's generator does not accumulate every vector it ever made"
  - "write_results replaces the repository's chunks (DELETE by repository, then INSERT) in complete()'s transaction; retrievals citing a replaced chunk are left dangling, consciously (U9, P17), pinned by a test"
  - "A write_results exception is settled like a handler's (normally fail) after the rollback, rather than escaping and stranding the job running"
  - "The worker refuses to start (exit 2) without INTERNAL_API_URL or OPENAI_API_KEY, after the registry and DSN checks"
  - "The archive's top-level directory may be either measured form, {owner}-{repo}-{sha7} (public) or {owner}-{repo}-{sha} (private); nothing else"
  - "The backend logs the scope GitHub reported on every mint, so the fail-closed check's premise is on record, not only its verdict"

issues-closed: []
issues-updated: [ISS-027]
issues-filed: [ISS-039, ISS-040]
review: "PR #58 — reviewer B (the phase's records, the endings and their tests): CHANGES REQUESTED, small; three mediums (M1 the locked 22.2 order missing from the hand-off, M2 A10's dated qualification, M3 a chunker failing on every file completing the job over an emptied index), five lows, five nits; M1, M7, M10 and M17 re-run and killed. Reviewer A (security, runtime, live proof): approve with nits; one medium (M1 the lease owner in the worker's logs, now ISS-039), seven lows (L1 control characters in file names forging log lines, L2 a refusal from another deployment, now ISS-040), eight nits. Everything applied 2026-09-29, the two correctness fixes with tests and mutants (section 19)."
duration: "about 2 h of wall-clock agent time on 2026-09-29 (branch from 19:02 local; first commit 19:46), against the plan's 15-21 h estimate"
completed: 2026-09-29
---

# Phase 22 Plan 05: the worker switched on, and a real repository indexed end to end

**`python -m workers` now claims jobs and runs a `full_ingest` handler --
fetch, parse, embed, store -- that writes a repository's chunks and vectors
in the same transaction as the job's completion.** Every non-happy path
takes an ending consistent with Phase 21's policy: a U6 cap is `dead` in one
attempt; a suspension or an uninstall discovered mid-run ends exactly as it
would at the claim; a refused token writes nothing; a misrouted internal API
fails loudly. The installation token is revoked the moment the fetch ends.
P16's operating numbers are set, provisionally. Compose declares the worker
behind a profile, without the App key.

**And the approved repository, `AlecAsdourian/ES-SC-API-Navigator`, was
indexed end to end from the development App into a scratch database, every
process running as `rag_doc_app`:** one attempt, 82 chunks with their
ada-002 vectors at the commit GitHub reported, found by `/search` for each
pre-registered question, refused to another tenant, and re-indexed
idempotently (`22-05-live-proof.md`). **The first live fetch found a real
bug** -- GitHub archives a private repository under the full SHA, which
22-04's sha7-only check refused -- and **it confirmed the fail-closed scope
check** against the real API. Both are below.

---

## 1. The handler and the stage vocabulary

`workers.ingest.handler.make_full_ingest_handler(deps) -> Handler`, with
`IngestDeps` holding everything it talks to -- the internal API URL and an
optional transport for it, GitHub's base URL and an optional transport, the
chunker, the embedder, the workdir, `Limits`, `max_chunks` and
`embed_slice` -- so tests fake only the network edges. The registered
`full_ingest` builds its dependencies from the environment on first use
unless `workers/__main__` has configured them at startup, which it does.

| Stage | Reported | What happens |
|---|---|---|
| `fetch` | on entry | `request_token(job id, lease owner)`; `fetch_repository` as a context manager; **`revoke_token` in its `finally`** |
| `parse` | on entry | `SemanticChunker.chunk_file` per file, a checkpoint per file; more than `MAX_CHUNKS` (100,000) is `Rejected` the moment it is crossed |
| `embed` | on entry | one vector per **distinct** chunk text, in slices of 1,000 with a checkpoint before each |
| `store` | **last**, then the handler returns | `write_results`, run by the runtime inside `complete()`'s transaction |

**The vocabulary is `fetch|parse|embed|store`** (it was `clone|...`; there
is no clone since U5). `docs/api-ingestion-jobs.md`, "Stages and progress",
is its authority; 000014's column comment is historical and was left alone.
A stage is reported on entry, after a checkpoint, so `last_stage` is the
stage the job was in when it stopped (a shutdown while embedding leaves
`embed`, asserted). `store` is reported last because `report_progress`
cannot run inside `write_results` (same connection, psycopg2's re-entrancy
guard) and `COMPLETE_SQL` does not touch `last_stage`: **a completed job
reads `last_stage = 'store'`** -- asserted end to end and on the live job.

**The checkpoint** (`_checkpoint`): `should_abort()` -> **`LeaseLost`**,
tested first because nothing may be written when both hold;
`is_shutting_down()` -> **`Unfinished`**.

## 2. The endings, and the contract change

The exception type is the contract. `docs/api-ingestion-jobs.md`, "How a job
ends", is the one table of it (the runtime's and the handler's docstrings
point at it rather than restating it -- a first draft carried a copy in
`runtime.py`, removed in `fe61756` under the checklist's one-authority rule).

| The handler raises | Raised when | Ending (the row) | The handler's test | The row's tests |
|---|---|---|---|---|
| nothing -- returns `write_results` | done | `complete`: `completed`, `synced`, the run and chunks committed together | `test_the_stages_run_in_order_and_every_report_carries_the_cumulative_progress` | `test_a_repository_is_ingested_end_to_end_as_the_app_role`; live job 1 |
| **`Rejected`** (the chunk cap) | more than 100,000 chunks, after parsing, before embedding | **`reject`: `dead` in one attempt**, `failed`, a plain reason | `test_the_chunk_cap_rejects_before_anything_is_embedded` | `test_a_cap_ends_the_job_dead_in_one_attempt[chunks]`; `test_each_handler_exception_takes_its_ending[rejected]`; `test_reject_writes_dead_in_one_attempt_with_a_redacted_reason` |
| **`FetchRejected`** (a `Rejected` since 22-05) | a fetcher cap: 500 MB of download or expansion, 20,000 files | the same | `test_a_fetch_cap_is_a_rejection_too` | `test_a_cap_ends_the_job_dead_in_one_attempt[files]` |
| **`InstallationSuspended`** | the token route's marked `409 installation_suspended` | **`defer` 60 minutes**, attempt handed back, **no projection** (reads `syncing`; below) | `test_the_installation_exceptions_propagate_unchanged[suspended]` | `test_a_mid_run_suspension_defers_an_hour_and_never_dead_letters`; `test_each_handler_exception_takes_its_ending[suspended]` |
| **`InstallationUninstalled`** | the marked `409 installation_uninstalled` | **`abandon`**: `superseded`, `never_synced` | `...propagate_unchanged[uninstalled]` | `test_a_mid_run_uninstall_abandons`; `...takes_its_ending[uninstalled]` |
| **`LeaseLost`** for `TokenRefused` | the route's **marked** 404, the fixed body | **nothing written**; the job stays `running` under its lease until reclaimed | `test_a_refused_token_raises_lease_lost_and_touches_github_not_at_all` | `test_a_refused_token_writes_nothing_and_the_job_stays_running` (the lease asserted live when refused, the heartbeat never ticked); `test_a_lease_lost_raised_by_the_handler_writes_nothing` |
| **`LeaseLost`** for `should_abort()` | at a checkpoint | nothing written | `test_a_lost_lease_between_stages_raises_lease_lost[fetch/parse/embed]`; `test_a_progress_report_that_matches_no_row_raises_lease_lost`; `test_a_fetch_report_that_matches_no_row_stops_before_asking_for_a_token` (the review's L1: no token requested); `test_a_lost_lease_and_a_shutdown_at_once_is_a_lost_lease`; `test_a_lease_lost_after_the_store_report_is_left_to_the_runtime` | the runtime's supersede tests (21-06), `test_a_supersede_mid_run_aborts_the_handler_and_writes_nothing` for the runtime's check before `complete` |
| **`ParseFailed`** (PR #58's review, B-M3) | **every** indexable file raised in the chunker | **`fail`**: attempt consumed, backoff, `last_error` names the count; **the index it would have replaced is untouched** | `test_a_chunker_that_fails_on_every_file_fails_the_job_instead_of_emptying_the_index`; `test_a_tree_with_nothing_indexable_is_not_a_parse_failure` | `test_a_chunker_that_fails_on_every_file_fails_the_job_and_keeps_the_index` (a good first ingest, then the failing one: chunks and ids unchanged) |
| `InternalApiMisrouted`, propagated | any **unmarked** answer (chi's 404 from the public router) | **`fail`**: attempt consumed, backoff, `last_error` names `INTERNAL_API_URL` | `test_a_misrouted_internal_api_fails_plainly_never_as_a_lost_lease` | `test_a_misrouted_internal_api_fails_loudly_never_as_a_lost_lease` |
| `TokenRequestFailed`, `FetchFailed`, anything else | a marked answer outside the contract, a transport failure, a failed download or extraction | `fail` (retried; `dead` at 5) | 22-04's client tests | `...takes_its_ending[other]` |
| `Unfinished` | `is_shutting_down()` at a checkpoint | `defer` 0, attempt handed back (only during a shutdown; otherwise `fail`, 21-06) | `test_a_shutdown_between_stages_raises_unfinished[parse/embed/store]`; `test_a_shutdown_during_embedding_stops_at_the_next_slice`; `test_a_shutdown_during_parsing_stops_at_the_next_file` (the review's L1) | `test_a_shutdown_during_embedding_defers_with_the_attempt_handed_back` |
| -- `write_results` raises inside `complete()` | a failed store | rolled back, then **settled** like a handler's exception (normally `fail`) | -- | `test_a_write_results_that_raises_fails_the_job_instead_of_stranding_it` |

**Re-parenting, as the plan asked:** `FetchRejected` subclasses the
runtime's `Rejected`; `InstallationSuspended` and `InstallationUninstalled`
are defined in `workers.jobs.runtime` and re-exported by
`workers.fetch.client`, one class each (`type(exc) is` the runtime's class
is asserted by `test_the_installation_exceptions_propagate_unchanged`; the
identity across every import order -- nine modules, each imported first in a
fresh interpreter -- was checked by a script, not a committed test). `REJECT_SQL` is fenced on both halves (`id`, `lease_owner`,
`state = 'running'`) and its `reject` joins the reclaimed-worker and
superseded-worker fence tests.

**The mid-run suspension's hour of `syncing`**, known and documented, not
changed: `mark_started` has projected `syncing` and `DEFER_SQL` (frozen
since 21-05) projects nothing. The job row is the truth -- `queued`,
`run_after` about an hour out, a reason in `last_error`, no lease, `stalled`
false -- and the end-to-end test pins every one of those, and the
`syncing`, so a change to either is a decision. `docs/api-ingestion-jobs.md`
adds it to "`syncing` is NOT evidence of a live worker".

**Found while writing it, and fixed:** before 22-05, an exception from
`write_results` escaped `_invoke` (only `LeaseLost` was caught around
`complete`), the loop logged it and moved on, and the job sat `running`
until its lease expired, was reclaimed, raised again, and reached `dead`
through the sweeper with `last_error` NULL. Phase 21's callbacks were one
INSERT; this one replaces a repository's chunks. Mutation M9 reproduces the
old behaviour and is killed.

## 3. The token: requested, used, revoked

`revoke_token(token)` is `DELETE /installation/token` authenticated by the
token itself (no App key, no JWT), `follow_redirects=False`, a 10-second
timeout. **It never raises**: a failed revocation is logged with the
repository, the host and the status or the exception's *class* -- never the
token, never an exception message -- and the token lives out its own hour,
which is no worse than 22-04. The handler calls it in the `finally` of the
fetch, so it runs after a good fetch, a cap, a failed download or an
extraction error, and before any later stage could raise `LeaseLost`; a
refused token has nothing to revoke (asserted: no GitHub request at all).
Tests: `test_the_token_is_revoked_once_the_fetch_ends_with_the_token_itself`
(the `Authorization` is the token; the revocation is the last GitHub request,
after the download), `test_a_failed_revocation_never_fails_the_job_and_logs_no_token[status/transport]`,
and the cap tests assert one revocation each. **Live: HTTP 204** on all
three fetches (run 1's failed one and run 2's two good ones; the three log
lines are quoted in the record since PR #58's review).

**What revocation covers is the worker's own token, and only that** (PR #58's
review, A-M1; section 16 is corrected). A token the route mints for anyone
else presenting a live lease is not the worker's to revoke and lives GitHub's
hour, and so does the worker's own when its process is killed before the
`finally` runs.

## 4. Cumulative progress

`PROGRESS_SQL` replaces the column, so the handler keeps one dict and sends
all of it every time; each stage adds keys and removes none:

| Report | Carries |
|---|---|
| `fetch` | `{}` |
| `parse` | `files_indexable`, `skipped` (reason -> count, never paths) |
| `embed` | + `files_parsed`, `parse_errors`, `chunks` |
| `store` | + `chunks_embedded`, `chunks_stored` |

The unit test asserts every payload is a value-for-value superset of the one
before; the end-to-end test and the live job assert the **completed row**
still holds `skipped` (the fixture's `.env`: `{"secret": 1}`; the live
repository: `{"unsupported": 6}`). Mutation M7 (a `store` report of
`chunks_stored` alone) is killed by both. `parse_errors` is a key the plan
did not list (deviation 3); when **every** file raises, the job fails with
`ParseFailed` instead and never reaches `embed` (section 19). 22.1-03 turns
this into the documented contract.

## 5. `write_results`: the replacement, the run, and the dangling retrievals

Inside `complete()`'s tenant-scoped transaction, in order:
`resolve_ingestion_run` (a retry of the same commit reuses its run) then the
fenced `attach_ingestion_run`; `DELETE FROM chunks WHERE repository_id` --
every chunk of the repository, whichever run wrote it; the insert through
`PostgresWriter.insert_chunks_on(cur, ...)`, which shares
`INSERT_CHUNK_SQL` with `insert_chunks` (now a wrapper that opens a tenant
transaction and calls it); and `complete_ingestion_run_on`, sharing
`COMPLETE_RUN_SQL` with `complete_ingestion_run`. A reader sees the old set
or the new one, never neither and never both.

**Retrievals whose chunk is deleted by the replacement are left dangling, by
design, and consciously.** U9 (option A) chose that "a logged result keeps a
chunk id that may later point at nothing"; deleting them would destroy
user-authored feedback on every re-index, and repointing them is the
link-shape decision U9 deferred to when feedback ships. Nothing writes
`retrievals` today. `test_a_reingest_leaves_a_retrieval_of_a_replaced_chunk_dangling`
writes a query, a retrieval citing a real chunk and its feedback, re-ingests,
and asserts the retrieval and its feedback survive with the chunk id it was
shown, which no longer exists. `DELETE /api/repositories/{id}`'s documented
limit (22-02) is now reachable in practice. ISS-027 carries a dated line.

## 6. P16's numbers, provisional until 22.1-05

| Number | Value | Override | Where | Pinned by |
|---|---|---|---|---|
| `max_job_duration` | **2 hours** | `WORKER_MAX_JOB_DURATION_SECONDS` | `runtime.DEFAULT_MAX_JOB_DURATION`; `__main__.build_worker` always sets it | `test_the_deployed_worker_has_a_bound_and_never_warns_about_one` (the startup line reads `max_job_duration=7200s`, and the unset-bound WARNING does not appear) |
| heartbeat `statement_timeout` | **15 s** (a quarter of the 60 s beat) | `WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS` | `runtime.DEFAULT_HEARTBEAT_STATEMENT_TIMEOUT`, the heartbeat connection only | the identity and blocked-beat tests (section 7) |
| worker processes | **2** (4 connections) | `WORKER_REPLICAS` | compose `deploy.replicas`; `runtime.PROVISIONAL_POOL_SIZE` names it | `test_the_workers_service_has_what_it_needs_and_the_provisional_pool` |
| workdir disk | **about 1 GB per process** | `WORKER_WORKDIR` | `docs/local-development.md` | **derived from the code, not measured**: the archive (up to 500 MB) is kept until extraction ends, and the tree can reach 500 MB plus one file before the counters stop it (22-04's re-check: a dense bomb is stopped *at* the cap) |

An override must be a positive whole number, or the worker refuses to start
(exit 2); the message names the variable and the value it was given (a
number, not a secret).

**22.1-05's first data point, from the live proof** (one repository, one
worker, Windows, Python 3.13.7): 12 archive members, 28,833 bytes
downloaded, 133,120 bytes expanded, 3 indexable files, 82 chunks; claim to
completion **8.06 s** -- fetch 3.5 s (the mint about 1.4 s and the download
1.0 s of it), parse 0.26 s, embed 4.0 s (one OpenAI batch, 30,728 tokens),
store 0.26 s. A second, identical ingest took 7.5 s.

## 7. The heartbeat's `statement_timeout`, merged into the DSN's options

`Worker._connect` builds the heartbeat's DSN with `_merged_options_dsn`:
`parse_dsn`, append `-c statement_timeout=<ms>` to the DSN's own `options`,
`make_dsn`. **Never `options=` as a keyword**, which replaces the DSN's
`options` -- where `app_dsn` sets `-c role=rag_doc_app` -- and would make the
heartbeat's connection the container superuser. The loop's connection is
unchanged.

- `test_the_heartbeat_keeps_its_callers_identity` reads the identity of
  **every connection the worker opens, at the moment it is opened, on the
  thread that opened it** (a wrapped `psycopg2.connect`), under `app_dsn`
  plus an operator option `-c work_mem=7MB`: the heartbeat is `rag_doc_app`
  like the loop, keeps `work_mem = 7MB`, and has `statement_timeout = 15s`;
  the loop's is `0`. **M5** (a replacing keyword) makes the heartbeat the
  superuser and is killed; **M4** (no option) is killed here too.
- `test_a_heartbeat_that_blocks_on_a_row_lock_times_out_and_gives_up`: a
  second connection holds `SELECT ... FOR UPDATE` on the job row; the beat
  raises `QueryCanceled` (57014, "canceling statement due to statement
  timeout", read from the log record), counts as a failure, and the clock
  rule sets `should_abort()` one lease after the last beat that landed.
  **M4** fails it on its own deadline (the handler's patience runs out with
  `should_abort()` still False).

## 8. The entrypoint

- **`test_the_entrypoint_refuses_to_start_without_handlers` changed on
  purpose** (fact-check a5): the same assertions and both parametrisations,
  now running the real `__main__` with the registry **cleared** first
  (`python -c "import workers.jobs.handlers as h; h.REGISTRY.clear(); import runpy; runpy.run_module('workers', run_name='__main__')"`).
  `NO_HANDLERS_MESSAGE` keeps "Phase 22" (it now says Phase 22 registers the
  two handlers, so an empty map means the build was altered).
- **`test_the_entrypoint_refuses_without_a_dsn_once_handlers_exist`**: the
  plain `python -m workers`, the shipped registry, no `DATABASE_URL` -> exit
  2 with `NO_DSN_MESSAGE`, never the handler message.
- **New:** `test_the_entrypoint_refuses_without_the_ingest_configuration[INTERNAL_API_URL/OPENAI_API_KEY]`
  -> exit 2 before any database connection (deviation 4).
- `__main__` calls `sweep_stale_workdirs(workdir, max_job_duration + lease)`
  once at startup. `test_an_unreachable_database_is_retried_and_then_gives_up_with_exit_1`
  now gives the entrypoint the ingest configuration and a private workdir,
  since it gets that far before the database.

## 9. Compose

`workers`: `profiles: ["ingest"]`; `DATABASE_URL` (compose's `coderag`, as
compose's other services use it -- deviation 5); `OPENAI_API_KEY=${OPENAI_API_KEY}`;
`INTERNAL_API_URL=http://backend:8081`; `depends_on: postgres: service_healthy`;
`deploy: replicas: ${WORKER_REPLICAS:-2}`; since PR #58's review (A-L4),
`stop_grace_period: 2m` and `restart: on-failure:5`, each with its reason
beside it (section 19). **`backend`:
`INTERNAL_ADDR=backend:8081`** -- the service name, as 22-04's guard asks, not
the plan's `0.0.0.0:8081`, which the backend refuses without the override
(deviation 6) -- and `expose: ["8081"]`, never `ports:`; a comment says
compose does not start the backend today and that secrets are Phase 24's.

`tests/test_compose_environment.py` runs `docker compose ... config --format json`
(never `up`) and extracts **variable names** inside a helper that never
returns the parsed configuration, because it interpolates the OpenAI key:
`workers` is absent without the profile; its names include no
`GITHUB_APP_*` or `GITHUB_WEBHOOK_SECRET`; it has `DATABASE_URL`,
`OPENAI_API_KEY`, `INTERNAL_API_URL`, the profile, the dependency and two
replicas; 8081 is exposed and not published; `INTERNAL_ADDR` is
`backend:8081` and the worker's URL points at it. A failed `config` reports
its exit code, not its output. It skips, with the reason, where there is no
Docker CLI (the `python:3.11-slim` image). `docs/local-development.md` warns
people about `docker compose config` the same way. **Compose was never
started** by any test or by the live proof.

## 10. The backend: the scope GitHub reported, on record

`github.ScopedToken` gained `ReportedRepositoryIDs` and `ReportedPermissions`
(both `json:"-"`), filled from the mint reply after the fail-closed checks
accept it, and the internal route's `repository token minted` line now
carries `reported_repository_ids` and `reported_permissions`
(`contents:read,metadata:read`). Not in the plan (deviation 1): without it
the live proof could have shown only that the check *passed*, not what
GitHub *reported*. `TestRepositoryToken_ScopesToOneRepositoryReadOnly` and
Scenario6 assert both; M16 (the attribute not logged) is killed.

## 11. The end-to-end test

`tests/isolation/test_ingest_end_to_end.py`, ten tests (eleven since PR
#58's review added the all-files parse failure) through the real
`Worker` on `app_dsn` (`rag_doc_app` asserted, `rolsuper` and
`rolbypassrls` false), two organizations with installations, jobs enqueued
with `ENQUEUE_UPSERT_SQL`. Fakes only at the network edges: the token route
and GitHub are `MockTransport`s under the real `request_token`,
`fetch_repository` and `revoke_token`; the embedding API is a deterministic
bag-of-words vector behind a **real** `EmbeddingGenerator` (model
`test-fixed`). The fixture archive holds three Python files and a `.env`,
under the private (full-SHA) directory name.

It asserts: `completed` in one attempt with `last_stage = 'store'`;
`synced`; the run completed with the fixture's SHA and count and attached to
the job; every chunk with a 1536-d vector, the model, the run; no `.env`
path or content; the completed row's `progress` holding `skipped
{"secret": 1}` with `chunks_stored`; the token presented with the job's own
lease and revoked by itself; no drift (chunk tenant or run against its
repository's, read with RLS bypassed); a `QueryEngine` on `app_dsn` finding
the expected file first with the fixture's commit as provenance, and
nothing as B with A's repository id. Then idempotency, the dangling
retrieval, and each ending (section 2), with `caplog` over every test
showing no `ghs_` and no `Authorization`. **Five consecutive runs: 10 passed
each**; after PR #58's review, with the all-files parse failure added, **11
passed each**.

## 12. The live proof

The record, with every command and its redacted output, is
`22-05-live-proof.md`; the three questions were committed (`f2bf121`)
before any search.

**Run 1 found a bug in 22-04's fetcher.** The first fetch was refused:
`archive top-level directory is not the expected
'AlecAsdourian-ES-SC-API-Navigator-f798806'`. Measured through `gh api`
(first member names only): GitHub names a **private** repository's archive
`{owner}-{repo}-{full sha}` -- by SHA and by branch -- and a public one's
`{sha7}`, authenticated or not (mealie and `octocat/Hello-World`). 22-04's
check had been validated against public mealie only, so it refused every
private repository, the product's normal case. Fixed in `76fa5c0`:
`expected_top_levels_for` returns exactly the two measured forms (anything
else, an unobserved abbreviation length included, is still refused before
anything is written), and the message names the directory the archive held
when it is plain text. M17 (back to sha7 only) is killed by 29 tests. The
worker was stopped after the first failed attempt; run 1's scratch database
was removed and run 2 started from a clean one.

**Run 2, the proof** (each item's command and output is in the record):

| # | Item | Result |
|---|---|---|
| 1 | the job | `completed`, `attempts = 1`, `last_stage = 'store'`, `last_error` NULL; progress `{"chunks": 82, "chunks_embedded": 82, "chunks_stored": 82, "files_indexable": 3, "files_parsed": 3, "parse_errors": 0, "skipped": {"unsupported": 6}}` -- the counts predicted from the tree before the run; claim to completion 8.06 s |
| 2 | the projection | `synced`, `last_synced_at` set |
| 3 | the commit | the run's `commit_sha` = `f798806452c0743312780e0cc3e97301286696bd` = `gh api .../commits/main --jq .sha`, read at the time |
| 4 | the chunks | 82, every one with a 1536-d vector and `text-embedding-ada-002`; every path tracked at the SHA; no deny-listed name |
| 5 | search as A | Q1's expected file at rank **1** (the very `find_filter_match` the question was written from), Q2's at **1**, Q3's (`README.md`) at **2**; every result's chunk at commit `f798806...`; the keyword leg empty for all three (ISS-029) |
| 6 | isolation | `/search` as B with A's repository id: HTTP 200, **0 results**, all three questions, from the RAG API connected as `rag_doc_app` |
| 7 | idempotency | enqueued again: `completed` in one attempt, the same run, 82 chunks, 0 duplicates, 0 of the first ingest's ids surviving |
| 8 | secrets | 0 `ghs_`, `-----BEGIN`, `sk-`, `Authorization:` or `Bearer` in the three logs; `last_error` NULL; the private download link carries **one query parameter, named `token`** (22-04's public link had none) |
| 9 | teardown | the three processes stopped, the scratch container removed, the workdir empty, `docker ps -a` identical to before run 1, the compose volumes unchanged |

**The token's scope as GitHub reported it**, on both mints:
`reported_repository_ids=1103353668`, `reported_permissions=contents:read,metadata:read`.
**22-04's fail-closed scope check is confirmed against the real API**, and
was not changed. **The revocation answered HTTP 204** every time. Every
database session was `rag_doc_app` (`pg_stat_activity`); the worker never
had the App key (the backend's `.env` was sourced into the backend's shell
only). Cost: about 61,600 ada-002 tokens, about $0.006.

## 13. Mutations

Each on a **copy** of the committed tree (`git archive HEAD`), by a tool with
a unique name that refuses anything but exactly one match, proves the
mutation landed by reading the file's **bytes** (mutated text present once,
original absent; for an additive mutant, the inserted text present once and
absent before), runs the target tests, restores the file from the pristine
copy and proves it identical. Predicates were neutered, not deleted.

**The first pass proved nothing, and is discarded.** It printed `mutated
present=0 original absent=True` for every mutation: the proof read the file
in text mode, which translates CRLF (the copy is checked out with CRLF), so
it could see neither string, and "original absent" was trivially true. Every
mutant "failed" that pass, but a proof that cannot fail is not a proof. The
tool was fixed to read bytes (and to decode subprocess output as UTF-8,
since the Go run's output crashed a cp1252 reader), and everything was
re-run; the table is the re-run. M15 is additive and was re-run with its own
proof.

| # | Mutation | Result |
|---|---|---|
| M0 | the committed code, the copy | 135 passed (the five target files); Go `pkg/internalapi`, `pkg/github` ok |
| M1 | `REJECT_SQL`'s owner fence -> `AND %s::text IS NOT NULL` | **killed** -- `test_a_reclaimed_workers_terminal_writes_are_all_refused[reject]` (the superseded twin passes: the state half still holds, as designed) |
| M2 | `Rejected` caught as a plain failure | **killed** -- both cap tests, `...takes_its_ending[rejected]` |
| M3 | `TokenRefused` mapped back to `return None` | **killed** -- `test_a_refused_token_writes_nothing_and_the_job_stays_running` (the job reads `completed`), the handler's refused-token test |
| M4 | the heartbeat's `statement_timeout` removed | **killed** -- the blocked-beat test on its own deadline, the identity test (`0`, not `15s`) |
| M5 | `options=` as a replacing keyword | **killed** -- the identity test (the heartbeat is the superuser) |
| M6 | the revocation neutered | **killed** -- 6 tests (the revocation, both caps in each file, the end-to-end happy path) |
| M7 | the `store` report not cumulative | **killed** -- the superset test, the end-to-end completed row |
| M8 | a mid-run suspension mapped to `Rejected` (the withdrawn mapping) | **killed** -- the end-to-end suspension test, `...[suspended]` |
| M9 | `write_results`' exception not settled | **killed** -- `test_a_write_results_that_raises_fails_the_job_instead_of_stranding_it` (the job stranded, reclaimed, `last_error` NULL) |
| M10 | the replacement's `DELETE` neutered (`AND false`) | **killed** -- `test_a_second_ingest_replaces_the_chunks_idempotently` |
| M11 | the chunk cap neutered | **killed** -- both chunk-cap tests |
| M12 | a mid-run uninstall not abandoned | **killed** -- the end-to-end uninstall, `...[uninstalled]` |
| M13 | `should_abort()` ignored at checkpoints | **killed** -- the three lost-lease tests, both-at-once |
| M14 | the `ingest` profile commented out | **killed** -- `workers` resolves without the profile |
| M15 | `GITHUB_APP_PRIVATE_KEY_PATH` added to the worker's environment (additive) | **killed** -- `test_the_workers_service_never_carries_the_app_key` |
| M16 | the reported permissions not logged (Go) | **killed** -- Scenario6 |
| M17 | the private (full-SHA) directory refused again, as 22-04 did | **killed** -- 29 tests, including the two new fetcher tests and the handler's whole happy path |

**Extended at PR #58's review**, the same tool and byte proof, on a fresh
`git archive` copy of `46aa287` (the review's code fixes plus `main`
merged): **M0 on that copy, 214 passed and 3 skipped** over the six target
files (the handler, the end-to-end file, both runtime files, the compose
test and the hostile-archive file), Go `pkg/internalapi` and `pkg/github`
ok. Every mutation below printed `mutated present=1 original absent=True`
before it ran and `restored: identical to the committed copy = True` after.
Fourteen mutations, **fourteen killed**:

| # | Mutation | Result |
|---|---|---|
| M18 | the all-files parse guard neutered (`if files and parsed == 0 and False`) -- B-M3 | **killed** -- the handler's all-files test and the end-to-end one (the good index would have been emptied); the one-file and nothing-indexable tests still pass under it, as they must |
| M19 | the control-character refusal neutered -- A-L1 | **killed** by 10 -- the eight parametrised member names (LF, CR, TAB, SOH, ESC, DEL, NEL, CSI), the directory name, and the handler's forged-log test; the non-ASCII test passes under it, as it must |
| M20 | the parse warning logs `str(exc)` -- B-L2 (the review's X2) | **killed** -- `test_a_chunker_failure_on_one_file_is_counted_not_fatal`, reading the `LogRecord` |
| M21 | the write-step warning logs `str(exc)` -- B-L2 (X4) | **killed** -- `test_a_write_results_that_raises_fails_the_job_instead_of_stranding_it`, reading the `LogRecord` |
| M22 | `_enter`'s no-row guard neutered, the report still made -- B-L1 (X1) | **killed** -- `test_a_fetch_report_that_matches_no_row_stops_before_asking_for_a_token` (without the guard the stop comes from the next checkpoint, after the token request); the older no-row test still passes, which is why it could not pin the guard |
| M23 | `_parse`'s per-file checkpoint -> `pass` -- B-L1 (X5) | **killed** -- `test_a_shutdown_during_parsing_stops_at_the_next_file` (the chunker kept being called after the shutdown arrived, where the test allows one call) |
| M24 | `metadata` allowed at any level (Go) -- A-N2 | **killed** -- `TestRepositoryToken_RefusesAScopeWiderThanAsked/metadata_above_read` |
| M25 | `PGOPTIONS` not merged -- A-N3 | **killed** -- `test_the_heartbeat_keeps_an_identity_set_through_pgoptions` (the heartbeat connected as the DSN's own role) and the unit test of `_merged_options_dsn` |
| M26 | `stop_grace_period` back to `10s` -- A-L4 | **killed** -- the compose stop-and-restart test |
| M27 | `restart: on-failure`, unbounded -- A-L4 | **killed** -- the same test |
| M28 | `_embed`'s missing-vector guard neutered -- B-N2 | **killed** -- `test_an_embedder_that_returns_no_vector_fails_plainly` |
| M29 | a zero or negative heartbeat timeout accepted -- B-N2 | **killed** -- both cases of `test_a_heartbeat_timeout_that_is_not_positive_is_refused` |
| M30 | the startup sweep removed -- B-N2 | **killed** -- `test_the_entrypoint_sweeps_stale_job_directories_once_before_it_runs` |
| M31 | `REJECT_SQL`'s state half -> `(state = 'running' OR true)` -- the review's X3 | **killed** -- `test_a_superseded_workers_writes_all_raise_lease_lost[reject]` (the owner half is M1's) |

The review's four survivors (X1, X2, X4, X5) are M22, M20, M21 and M23:
each now fails a test written for it, so **no survivor is recorded**.

## 14. Verification

| Check | Result |
|---|---|
| `pytest tests/ workers/` in CI's shape (fresh venv from `requirements.txt`, Python 3.13.7, `REDIS_URL` on a scratch Redis db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no reachable `.env`), on `97ad3e6` | **578 passed, 3 skipped** (`main`: 515 passed, 3 skipped; the three skips are 22-04's tiny-filesystem case and the two on-disk symlink cases Windows cannot run) |
| the same in **`python:3.11-slim`** (Python 3.11.16, the production image, testcontainers through the Docker socket), on `97ad3e6` | **576 passed, 5 skipped** -- the four compose tests skip there by design (no Docker CLI) and the tiny-filesystem case; the two symlink cases run |
| `tests/isolation/test_ingest_end_to_end.py`, five consecutive runs | 10 passed, five times -- on `fe61756`, and again on `97ad3e6` after the fixtures moved to the private (full-SHA) directory name |
| import order (a script, not a test) | each of nine modules importable first in a fresh interpreter, with the classes identical across `workers.fetch` and `workers.jobs.runtime` |
| Go: `go build ./...`, `go vet` on the changed packages, gofmt on LF-normalised copies (no curly quotes) | clean |
| Go: `go test ./pkg/github/... ./pkg/internalapi/...` (the isolation scenarios ran, Scenario6 included) | ok |
| Go: `-race` in `golang:1.25` (go1.25.14 linux/amd64) with the Docker socket, `./pkg/internalapi/... ./pkg/github/...` | ok, **0 data races** |
| `scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD` | **PASS** (no route added) |
| compose | `docker compose config` resolved (with and without the profile); **never started** |
| port 5434, compose's Postgres, the compose volumes | never touched; `docker ps -a` identical before and after the live proof |
| commits | one sentence, a conventional prefix, no trailers (`git log --format=%B`) |
| **At PR #58's review**, on `46aa287` (the fixes plus `main` merged): `pytest tests/ workers/` in CI's shape, as above (a new scratch Redis, db 15) | **602 passed, 3 skipped** -- the 24 new tests are the difference from 578; the same three skips |
| the same in **`python:3.11-slim`** (3.11.16), on a `git archive` copy of `46aa287` | **599 passed, 6 skipped** -- the five compose tests (no Docker CLI, by design) and the tiny-filesystem case |
| `tests/isolation/test_ingest_end_to_end.py`, five consecutive runs | **11 passed, five times** |
| Go: `go build ./...`, `go vet` and gofmt (LF-normalised) on the touched packages; `go test ./pkg/github/... ./pkg/internalapi/...` on the worktree (the harness's container named for this session, ISS-037's override) | clean; ok, the seven `TestRepositoryTokenIsolation` scenarios run |
| Go: `-race` in `golang:1.25` (go1.25.14 linux/amd64), `./pkg/internalapi/... ./pkg/github/...`, on a `git archive` copy of `46aa287` | ok, **0 data races** |
| `scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD` | **PASS** |

## 15. Acceptance criteria

| | Criterion | Status | Evidence |
|---|---|---|---|
| A1 | pgvector everywhere | met (22-01) | the live proof ran on the pinned digest |
| A2 | migrations proven in the deployment shape | met (22-01) | no migration in 22-05 |
| A3 | tenant isolation on `chunks` by schema | met as corrected (2026-09-29, in `22-ACCEPTANCE.md`) (22-02) | the end-to-end drift check, on the first ingest and on the replacement since PR #58's review; the live proof's writes as `rag_doc_app` |
| A4 | every writer honest about tenancy and vectors | advanced by 22-02; **the new writer** (`insert_chunks_on`, the same statement and refusals) supplies the tenant, the vector and the model on every row | the end-to-end chunk assertions; live item 4 |
| A5 | partition pruning | met (22-02, 22-03) | -- |
| A6 | retrieval on pgvector, rankings unchanged | met as qualified (22-03) | -- |
| A7 | Qdrant retired | met (22-03) | -- |
| **A8** | fetching is safe | **met: 22-05 completes it** -- the **100,000-chunk cap** is enforced after parsing and before embedding, `Rejected` -> `dead` in one attempt (the handler and end-to-end cap tests; M11); the token is **revoked** when the fetch ends (M6; live 204); the scope check is **confirmed against the real API**, and since the review accepts `metadata` only at `read` (M24); the archive check now accepts the private directory name (M17); and since the review a member name holding a control character is refused (M19) | sections 2, 3, 12, 13, 19 |
| **A9** | the worker processes real jobs safely | **met** -- `REGISTRY` has both keys; the endings table and its tests (section 2), with the review's `ParseFailed` so a chunker failing on every file never empties an index (M18); cumulative progress (M7); the heartbeat's timeout merged (M4, M5), `PGOPTIONS` included since the review (M25); compose behind a profile without the App key (M14, M15), stopping gracefully (M26, M27) | sections 2, 4, 7, 9, 13, 19 |
| **A10** | one real repository indexed and searchable end to end | **met as qualified (2026-09-29, in `22-ACCEPTANCE.md`, at PR #58's review)** -- `ES-SC-API-Navigator`, connect-shaped seed -> queue -> worker -> pgvector -> the RAG API's `/search`, scratch database, every process as `rag_doc_app`, the expected file returned for every question (ranks 1, 1, 2), refused to B, compose untouched. The real connect path is Phases 20-21's tests; the backend's search relay is `search_isolation_test.go`'s | `22-05-live-proof.md` and its committed scripts; section 12 |
| **A11** | the phase leaves honest records | **met for 22-05, on this evidence**: measured and inferred are marked (section 16); every "does NOT pin" from 22-01 to 22-05 is carried to the hand-off (section 18, completed at PR #58's review with the notes the review found missing); ISS-027 is current, and ISS-039 and ISS-040 are filed; the guarantees this plan found false (22-04's archive check, and at the review the lease-owner exposure understated in section 16 and `repository_token.go`) are corrected in code, docs and here; the broken first mutation pass is recorded rather than used | sections 12, 13, 16, 18, 19 |

**Every criterion A1-A11 is met with evidence recorded in a SUMMARY**
(A1-A7 in 22-01 to 22-03's, A8-A11 here; A3 as corrected, A6 and A10 as
qualified, each with its dated note in `22-ACCEPTANCE.md`), so Phase 22
closes on evidence when this PR merges.

## 16. Measured, inferred, and not pinned (22-05)

- **Measured:** everything in the live proof; the archive naming rule (five
  probes); the P16 data point; every test and mutation above.
- **Inferred, not measured:** the ~1 GB disk figure (derived from the
  fetcher's code); that GitHub names every private repository's archive with
  the full SHA (one private repository measured, two public ones).
- **Does NOT pin:**
  - **memory and write size at the 100,000-chunk cap.** Vectors are held as
    Python lists of 1,536 float objects -- about 49 KB each by arithmetic
    (8-byte pointers plus 24-byte floats) -- so a repository at the cap
    would need about 5 GB of worker memory for vectors alone, and
    `write_results` sends each vector as text (about 30 KB) in one
    transaction. **And the vectors are not all of it** (PR #58's review,
    B-L4): the whole fetched tree is held in memory as well --
    `FetchedFile.content` for every indexable file, up to the 500 MB
    expansion cap -- through parse and embed. **Inferred, not measured**;
    22.1-05's timings will show it. An OOM kill there is a crash: the job is
    re-fetched and re-embedded from scratch up to five times, then `dead`
    with `last_error` NULL;
  - **a job that deterministically overruns `max_job_duration`** (the
    review's B-L4) is cut loose (`LeaseLost`, nothing written), reclaimed,
    re-fetched and re-embedded -- nothing is reused until 22.1-02 -- up to
    five times, then dead-lettered by the sweeper with `last_error` NULL
    (`_SWEEP_SQL` writes no reason). That is the price of a wrong
    provisional number, and 22.1-05's to measure away;
  - **a store longer than the lease blocks its own heartbeat** (the review's
    A-L3, measured by the review with the runtime suite's timings and a 4 s
    store): `attach_ingestion_run` updates the job row inside `complete()`'s
    transaction, so the heartbeat's `UPDATE` waits on the worker's own lock,
    times out (`57014`) and, once a lease has passed, logs a false
    "assuming it is lost" ERROR. The job still completes -- the row lock
    keeps claimers out -- but if that store then rolled back, the job would
    be claimable at once (inferred). At the cap it is the normal case,
    inferred: 82 chunks stored in 0.26 s live scales to about 317 s for
    100,000, past the 300 s lease;
  - `chunks_stored` is reported before the store runs, so it is true of a
    `completed` row only; a failed store leaves the count and a `last_error`;
  - a token the worker never received (a malformed 200) or a revocation that
    fails lives out GitHub's hour; so does one held by a worker killed
    mid-fetch (SIGKILL, an OOM kill, a compose stop past its grace period);
  - **the worker's own logs carry its worker id, which is the lease owner**,
    on every transition line (21-06's format) and on the runtime's startup,
    progress and heartbeat lines. **Corrected at PR #58's review (A-M1):**
    this bullet first said an attacker would have to read the logs "while a
    job runs" and that the token was "revoked at fetch end". Both
    understated it. The id is generated once per process and serves every
    job that process claims, the heartbeat keeps each lease live for the
    whole job, and the route mints a fresh token on every call, so **no race
    is needed**: whoever can read a live worker's logs and reach the
    internal listener can mint a token for any repository that worker is
    ingesting, across tenants. **Nothing revokes that token**: the worker's
    revocation covers only the token its own fetch used, so each one so
    minted lives GitHub's full hour. Still one repository and read-only. The
    live-proof record masks the id. **Filed as ISS-039, a Phase 24 gate**;
    no exposure under compose today (reading the logs needs the Docker
    daemon, which can already read `ingestion_jobs.lease_owner`);
  - a file whose chunking raises is skipped and counted (`parse_errors`),
    so it is missing from search with only that count to say so; **when
    every file raises, the job fails instead** (`ParseFailed`, section 19);
  - `incremental` is the full ingest until 22.1-02;
  - compose's `workers` connects as compose's superuser `coderag`, like the
    other compose services, so RLS does not bind it there; the unprivileged
    role and its grants are Phase 24's. Since PR #58's review (A-L7) the
    operator docs say so where operators read (`docs/local-development.md`,
    `docs/api-ingestion-jobs.md`'s "What a deployed worker needs"): a
    deployment's `DATABASE_URL` must log in as a `NOSUPERUSER NOBYPASSRLS`
    role;
  - the startup sweep's bound (`max_job_duration + lease`) assumes the
    workdir is per host or per container;
  - an archive directory name of another abbreviation length has never been
    observed and would be refused, loudly (fail closed, by choice);
  - the API's `/search` response carries no provenance (the `QueryEngine`
    does); the live proof read the commit by chunk id;
  - **a marked refusal from another deployment's token route** reads as a
    lost lease (PR #58's review, A-L2): every job is left `running`,
    reclaimed and refused until the sweeper dead-letters it with
    `last_error` NULL. **Filed as ISS-040, a Phase 24 gate** (reasoned, not
    measured);
  - a repository over a cap is rejected per **job**, so it is re-fetched --
    up to 500 MB -- on every push, only to be rejected again (the review's
    A-N7; `test_a_rejected_job_is_terminal_and_the_repository_can_be_queued_again`
    pins the re-queue) -> 24-05's cost controls.

## 17. Deviations from the plan

1. **The backend logs the scope GitHub reported** (`47adbb8`): the plan's
   Task 3 wanted the token's scope "as GitHub reported it", which only the
   backend sees; nothing else reads it.
2. **The archive directory check was fixed** (`76fa5c0`), after the live
   proof's first fetch found 22-04's check refusing a private repository.
   Run 1 is recorded; run 2 is the proof. The plan's Task 3 is "no code
   changes"; this was a bug the proof exists to find, and the fix went
   through tests and a mutant before run 2.
3. **`parse_errors`** is a progress key the plan did not list: a file whose
   chunking raises is counted rather than failing the job.
4. **A third entrypoint refusal**: no `INTERNAL_API_URL` or
   `OPENAI_API_KEY` is exit 2 at startup, after the registry and DSN checks;
   otherwise every job would fail five times and dead-letter.
5. **Compose's `DATABASE_URL` is `coderag`'s**, as compose's other services
   connect; compose has no `rag_doc_app`.
6. **`INTERNAL_ADDR=backend:8081`, not the plan's `0.0.0.0:8081`**, which
   22-04's guard refuses (PR #52's review, L3; `22-CONTEXT.md`'s 22-05 row).
7. **`write_results` failures are settled** (M9), found while writing the
   store; not in the plan.
8. **Embedding in slices with checkpoints, and parsing with a checkpoint per
   file**, beyond "between stages", so a shutdown or a lost lease can land
   inside the long stages.
9. **ISS-037 was not 22-05's first task**, as `22-CONTEXT.md`'s row says: the
   coordinator gave it to a parallel agent. The Go runs here used the shared
   reuse container, which was at 17, like this branch.
10. **The live proof ran the backend binary built from the branch** rather
    than `go run .`, because stopping `go run` on Windows leaves its child
    running; and the backend's `.env` was sourced with its CRLF endings
    stripped. The worker and the RAG API ran on the host's Python 3.13.7.
11. **The runtime's endings table was first written into `runtime.py`'s
    docstring as well as the doc**, and replaced by a pointer (`fe61756`)
    under the one-authority rule.

**Added at PR #58's review** (section 19 has each one's test and mutant):

12. **`ParseFailed`**: when every indexable file raises in the chunker, the
    job fails (retried) instead of storing nothing over a good index -- a
    new exception the plan's endings did not have (B-M3, A-L5).
13. **22-04's fetcher refuses a member name holding a control character**
    (`unsafe_path`), so no customer file name reaches a log line (A-L1).
14. **Compose's `workers` stops in two minutes and restarts at most five
    times** (`stop_grace_period: 2m`, `restart: on-failure:5`) (A-L4).
15. **22-04's scope check accepts `metadata` only at `read`** (A-N2).
16. **The heartbeat's options start from `PGOPTIONS`** when the DSN names
    none, as libpq itself would (A-N3).

The PR body's list was ten long while this one was eleven (the review's
B-N4); both now carry these sixteen.

---

## 18. Phase-level hand-off: Phase 22 -> Phases 22.1 and 22.2

Phase 22 ends with the goal met: a real GitHub repository indexed end to end
-- connect-shaped seed, queue, worker, pgvector, search -- under tenant
isolation on both legs. What Phase 22.1, the retrieval-quality track (now
Phase 22.2) and Phases 23-24 inherit, **including every "does NOT pin" item
from 22-01 to 22-05**, by plan of origin.

**The order after 22-05 is the user's, locked 2026-09-29, and
`22.2-CONTEXT.md` QD11 is its authority** (merged with PR #57): on the
chunker, **22.2-02** (the chunker's bug fixes) first, then **22.1-01**
(symbol identity, including TypeScript symbols, and not changing the
display breadcrumb), then **22.2-04** (the chunk-shape candidate). 22.2-01
and 22.2-03 touch neither the chunker nor this plan's files and can run
alongside. This section records the order and does not restate QD11's
reasons.

**From 22-01 (pgvector everywhere, the seeded-migration gate)**
- The gate cannot see DML that inherits a tenant beyond 000013's and
  000015's outcome assertions, nor a validating trigger's reads; its FORCE
  check encodes the owner-migrates premise (a non-owner migrating role would
  need `relrowsecurity` alone) -- Phase 24's grant model.
- `CREATE EXTENSION vector` needs a superuser: production's operator creates
  it once before migrating (Phase 24).
- **Phase 24's host must offer pgvector 0.8 or later** (iterative scans,
  which the repository filter depends on) **and a container `--shm-size`
  large enough for HNSW index builds** (carried at PR #58's review, B-L4;
  also in ROADMAP's Phase 24 research topics).
- An old compose volume initialised by the Alpine image needs a `REINDEX`
  (musl -> glibc collation) before use; documented, nothing does it for you.

**From 22-02 (partitioned `chunks`, every writer)**
- The composite tenant keys hold only while foreign-key triggers are enabled:
  replica mode or `DISABLE TRIGGER` accept a misfiled row; loading that way
  is forbidden and **the drift query is the guard**, in CI.
- **ISS-036:** the single-column `ingestion_run_id` and `symbol_id` keys carry
  no tenancy -> **22.1-01**'s composite keys, under ISS-031's rule.
- A mutation of **one** partition's policy is caught by the guard on every
  run, and by the leak test only when tenant B hashes to that partition.
- **`TRUNCATE` must never be granted** on `chunks`, its partitions or
  `symbols` (Phase 24's grants).
- The repository delete reaches only retrievals whose chunk still exists;
  since 22-05's re-ingest replaces chunks, that limit is now reachable (U9's
  link-shape decision, when feedback ships).

**From 22-03 (retrieval on pgvector)**
- HNSW **eligibility** is proven, not the planner's **choice** at scale; the
  equivalence gate judged exact search on both sides at <= 2,816 chunks per
  repository, so **HNSW-served rankings have no equivalence evidence** ->
  **22.1-05**'s recall test.
- The breadcrumb GIN index matches the expression, but the planner picking
  it in the full keyword statement is unproven.
- **The keyword leg is empty for most natural-language questions**
  (ISS-029): 120 of 130 benchmark questions, and all three live questions
  here -> the retrieval-quality track, with fresh questions and a rule
  committed first (its tolerance ~2e-6, fixed before its next rule).
- ISS-021: the semantic cache's constructor is still broken; its key must
  carry the model (P4).
- **For 22.1-02:** the benchmark harness's `--ingest` refuses an indexed
  corpus without `--clear` until per-file currency lands (22-03's note in
  ISS-027, carried here at PR #58's review, B-L4). The worker's full ingest
  replaces a repository's chunks; the harness's own path does not.

**From 22-04 (fetching safely)**
- The marker's value across a protocol change: the Go and Python constants
  point at each other and nothing checks one against the other.
- CPU on an archive of millions of tiny non-indexable members: nothing bounds
  the member count separately (the expansion cap and `max_job_duration` do).
- A repository with more than 20,000 name-indexable files that the content
  filters would bring under the cap is rejected (the conservative reading).
- The download cap counts decoded bytes (stricter than the wire, never
  looser).
- U7's known limit: a key pasted into `README.md`, `config.py` and the like is
  indexed and sent to OpenAI -> a question for the retrieval-quality track.
- `GET /repositories/{id}` is an undocumented alias (it answered live); the
  fallback, `GET /repos/{full_name}`, is noted beside it.
- **A caller that uses `extract_archive` directly owns its own cleanup**
  (22-04 §15's dense-bomb note, second half, carried at PR #58's review,
  B-L4): `fetch_repository` removes the job directory on every path, and
  `extract_archive` alone does not. 22.1-02 is the likeliest such caller,
  for a resumed stage.
- **Settled by 22-05's live proof:** GitHub's mint reply shape (the scope
  check is confirmed); the private download link's credential (a `token`
  parameter); the life of the worker's own token after its lease (revoked
  when its fetch ends -- **only its own**: a token minted for anyone else
  with the lease owner lives its hour, ISS-039). **Corrected by it:** the
  archive directory check (section 12). **Corrected at PR #58's review:**
  a member name with a control character is now refused (A-L1), and
  `metadata` is accepted only at `read` (A-N2).

**From 22-05 (this plan)** -- section 16's list, and:
- **22.1-01** (after 22.2-02, in QD11's order): `write_results` is where
  symbols join the store (upsert and unarchive in the same transaction);
  the chunk replacement is per repository today. A chunker change that
  makes every file raise now fails the job (`ParseFailed`) rather than
  emptying the index -- the guard 22.2-02 and 22.1-01, both chunker
  changes, most need.
- **22.1-02:** `incremental` is the full ingest; per-file currency, the
  manifest and embedding reuse replace the whole-repository `DELETE`; the
  dangling-retrieval pin must be kept or deliberately changed.
- **22.1-03:** the progress keys and the cumulative rule above are the
  contract's starting point; ISS-034 still hides the job id from a UI; the
  mid-run suspension's hour of `syncing` is what a UI must read around.
- **22.1-05:** P16's three numbers are provisional; the first data point is
  section 6's; measure memory and the store at the chunk cap, and the
  OpenAI throughput ceiling. Also, from PR #58's review (section 16 has
  each): a job overrunning `max_job_duration` is re-fetched and re-embedded
  up to five times and ends `dead` with `last_error` NULL; the whole fetched
  tree is held in memory through parse and embed, not only the vectors; and
  **a store longer than the lease blocks its own heartbeat** (A-L3:
  `attach_ingestion_run` locks the job row inside `complete()`, so the beat
  waits, times out and logs a false "assuming it is lost" ERROR; about
  317 s against a 300 s lease at the cap, inferred). The review's fixes:
  attach last, just before the rerun clear, with an early fence check that
  takes no lock; or have the heartbeat stand down quietly once `complete`
  begins. Either way, measure the store at scale.
- **Phase 24:** secrets in compose (the backend does not start there
  today); the unprivileged role and grants for compose's services; the
  internal listener stays unpublished (a bearer secret or mTLS if the
  worker and the backend ever sit on different hosts); the workdir's disk
  (about 1 GB per process) and, if a worker ever shares a host with other
  users, its ownership (the review's A-N5: the default is a predictable
  path under the shared temporary directory, so refuse a workdir that is a
  symlink, not the process's own, or group- or world-writable); and **two
  gates filed by PR #58's review:
  ISS-039** (the lease owner in the worker's logs: hash it before any
  deployment ships worker logs off the host) **and ISS-040** (a refusal
  from another deployment's token route reads as a lost lease: check the
  worker's own database before believing it). A killed worker cannot
  revoke its token (bounded by GitHub's hour; compose's two-minute grace
  period makes it rare, `docs/internal-api.md`).
- **24-05:** a repository over a cap is rejected per job, so it is
  re-fetched (up to 500 MB) on every push only to be rejected again
  (A-N7). Whether the cost controls remember a rejection per repository,
  and for how long, is 24-05's decision; nothing remembers it today.

**Next: merge 22-05; then, on the chunker, 22.2-02, 22.1-01 and 22.2-04 in
the order `22.2-CONTEXT.md` QD11 locks** (above). None of their plans is
written yet.

---

## 19. PR #58's review, applied 2026-09-29

Two reviewers. **Reviewer B** (the records, the endings and their tests):
CHANGES REQUESTED, small -- three mediums, five lows, five nits; re-ran M1,
M7, M10 and M17 (all killed, as recorded) and found four survivors of its
own (X1, X2, X4, X5). **Reviewer A** (security, the runtime, the live
proof): approve with nits -- one medium, seven lows, eight nits; re-ran M3
and M6 (killed) and probed revocation on every failure path, file-name log
injection, the lease owner in a real run's logs, a store longer than the
lease, and `PGOPTIONS`. The coordinator's list decided what was fixed and
what was filed. Commits: `c9dba88` (the fetcher), `81fcc32` (the handler),
`165b6a6` (the runtime), `f34b3cc` (the end-to-end tests), `9a5f020` (Go),
`6924b06` (compose), `905d7ca` (the docs), then `main` merged (`46aa287`,
bringing PR #57's `22.2-CONTEXT.md`), `2fc58ad` (a comment in the fetcher,
nothing else: `tests/fetch` and `tests/ingest` re-run on it, 209 passed and
3 skipped), then the records.

### Correctness: both fixed, tested and mutation-checked

- **A chunker failing on every file emptied the index (B-M3 = A-L5).** The
  reviewer's probe: a chunker that always raises reached `store` with no
  chunks, and `write_results`, which *replaces* the repository's chunks,
  would have deleted a good index, inserted nothing and projected `synced`.
  **Fix:** `_parse` raises **`ParseFailed`** when there were indexable
  files and none parsed -- an ordinary failure (`fail`: the attempt
  consumed, a backoff, retried, `dead` at 5), `last_error` reading
  `ParseFailed: every one of the N indexable files failed to parse (N
  raised in the chunker); refusing to replace the repository's index with
  nothing`. Not `Rejected`: nothing is over a cap, and a fixed chunker will
  parse the repository. **Tests:** the handler's
  `test_a_chunker_that_fails_on_every_file_fails_the_job_instead_of_emptying_the_index`
  (the type is `ParseFailed` and not `Rejected`, `LeaseLost` or
  `Unfinished`; the count in the message; every file tried; nothing sent to
  the embedder; stages `fetch, parse`; the token revoked; the workdir
  clean); the end-to-end
  `test_a_chunker_that_fails_on_every_file_fails_the_job_and_keeps_the_index`
  (a good first ingest, then the failing one: `queued`, `attempts = 1`,
  `ParseFailed` and the count in `last_error`, `run_after` pushed out,
  `last_stage = 'parse'`, **every chunk, its id and the run exactly as the
  first ingest left them**, `sync_state = 'failed'`). The partial case stays
  counted (`test_a_chunker_failure_on_one_file_is_counted_not_fatal`), and
  a tree with nothing indexable still completes with an empty index
  (`test_a_tree_with_nothing_indexable_is_not_a_parse_failure`): nothing
  raised there. **M18** killed.
- **File names with control characters reached the log raw (A-L1).** The
  reviewer extracted, indexed and logged a member named
  `app/x\n2026-09-29 20:17:10,248 INFO workers.jobs.transitions job FORGED:
  complete.py` in `python:3.11-slim`. **Fix:** `_unsafe` refuses any name
  holding a Unicode `Cc` character -- C0, DEL and C1 -- counted
  `unsafe_path`, which keeps such a name off the disk, out of every log line
  and out of `chunks.file_path`. A tab is refused too, as a control
  character: a repository with such a name loses that one file from the
  index, counted, and says so in `skipped`. **Tests:** eight parametrised
  hostile archives (LF, CR, TAB, SOH,
  ESC, DEL, NEL, CSI), a control character in a directory name, a
  non-ASCII name still extracted (the refusal is `Cc`, not "unusual"), and
  the handler-level test with the reviewer's own forged line: never
  chunked, `unsafe_path` counted, `"FORGED" not in caplog.text`. **M19**
  killed by 10.

### Records

- **B-M1, the locked order.** Section 18, `STATE.md` and `ROADMAP.md` now
  name 22.2-02, then 22.1-01, then 22.2-04, each pointing at
  `22.2-CONTEXT.md` QD11 (on `main` since PR #57) rather than restating it.
  The three lines that named 22.1-01 as next are corrected.
- **B-M2, A10.** `22-ACCEPTANCE.md` carries a dated qualification: a
  connect-shaped seed, the enqueue by `ENQUEUE_UPSERT_SQL`, the real connect
  path Phases 20-21's tests, `/search` the RAG API's, the backend's relay
  `search_isolation_test.go`'s. Section 15's A10 row reads "met as
  qualified".
- **A-M1, the lease owner in the worker's logs: ISS-039**, a Phase 24 gate,
  with the reviewer's text, measurement and fix plan (a 12-hex hash of the
  lease owner through one helper at about 30 sites; the end-to-end secret
  check extended; a mutation). **No logging code changed here**, as the
  coordinator asked. The record is corrected now: section 16 and the PR
  body had said an attacker's token is "revoked at fetch end" and that the
  logs must be read "while a job runs"; the id is per process, the heartbeat
  keeps the lease live, the route mints on every call, and a token minted
  for anyone else lives GitHub's hour. `repository_token.go`'s package
  comment (the review's A-N6) and `docs/internal-api.md` say the same.
- **A-L6, the live proof's scripts:** committed verbatim in
  `22-05-live-proof-scripts/` (checked: `.gitignore` does not exclude them,
  and they hold no secret), described in the record, with the three HTTP
  204 lines quoted.
- **B-L3:** the handler test's docstring points at
  `test_job_worker_runtime.py::test_a_supersede_mid_run_aborts_the_handler_and_writes_nothing`.
- **B-L5:** section 15's "section 17" is section 18;
  `docs/api-ingestion-jobs.md` no longer calls the runtime's pointer a
  table.

### Tests

- **B-L2, the two new warning lines.** Both tests now read the
  `LogRecord`: the chunker's warning and the runtime's write-step warning
  each carry the exception's text with `[REDACTED]` where the token was, and
  never the token. **M20, M21** killed.
- **B-L1, the two guards that survived their removal.** Both are now
  observable, so neither is recorded as a survivor:
  `test_a_fetch_report_that_matches_no_row_stops_before_asking_for_a_token`
  (without the guard the next checkpoint still stops the job, but only after
  a token was requested for a job that is not ours) and
  `test_a_shutdown_during_parsing_stops_at_the_next_file` (a chunker that
  raises the shutdown flag on its first call is called once). **M22, M23**
  killed.

### Operations

- **A-L7:** "a deployment's `DATABASE_URL` logs in as a `NOSUPERUSER
  NOBYPASSRLS` role; compose's `coderag` is a superuser, so tenant isolation
  is not exercised under compose" is in `docs/local-development.md`'s
  worker section, `docs/api-ingestion-jobs.md`'s "What a deployed worker
  needs" and the compose comment.
- **A-L4, compose's stop and restart.** `stop_grace_period: 2m`: SIGTERM
  lets the worker reach its next checkpoint and hand the job back, and two
  minutes covers the stretches measured (the live job took 8 s whole; one
  embedding slice is ten API calls); a fetch near the 500 MB cap on a slow
  link, or a store near the chunk cap, can outlast it, and then the open
  transaction rolls back and the job is reclaimed. `restart: on-failure:5`:
  exit 1 (the database stayed unreachable) is what a restart is for, but
  Docker cannot tell it from exit 2, a refusal no restart fixes, so the
  retries are bounded -- six runs of about three minutes of reconnecting
  each ride out a twenty-minute outage, and a refusal stops after six
  lines. The reasons are beside the values; `test_compose_environment.py`
  pins both (**M26, M27** killed); `docs/local-development.md` and
  `docs/internal-api.md` say a killed worker cannot revoke its token, which
  GitHub ends within the hour.
- **A-L2, a refusal from another deployment's token route: filed as
  ISS-040, a Phase 24 gate, not fixed here.** Chosen over fixing now
  because the fix changes what the c2 end-to-end test means (a refused
  token with a live lease would stop being "write nothing"), needs a new
  question to the worker's own database that duplicates the Go route's
  predicate in Python -- including the lease's expiry, which the progress
  fence alone does not check -- and the review ruled filing it fine, since
  it bites only where staging and production can reach each other. The
  review's startup probe (A-N8) is in the same issue.

### The hand-off's gaps (B-L4, and reviewer A's for 22.1-05 and 24-05)

All carried, in section 18 or section 16: 22-04's note that a caller using
`extract_archive` directly owns its own cleanup; 22-03's `--ingest` /
`--clear` note for 22.1-02; pgvector 0.8 or later and `--shm-size` for
Phase 24; a job overrunning `max_job_duration` re-embedded up to five times
and `dead` with `last_error` NULL; the whole fetched tree held in memory;
A-L3's store that blocks its own heartbeat, for 22.1-05; A-N7's re-fetch on
every push of a repository over a cap, for 24-05.

### The nits

**Applied:**
- **B-N1:** one authority per list -- the handler's docstring points at the
  doc for the progress keys, and `workers/fetch/__init__.py` for the
  endings.
- **B-N2:** the three unpinned guards are pinned: `_embed`'s missing
  vector (M28), `Worker`'s refusal of a zero or negative heartbeat timeout
  (M29; a zero `timedelta` is falsy, so it would have switched the timeout
  off without a word), and `__main__`'s startup sweep, run in-process with
  its bound asserted (M30).
- **B-N3:** the endings' remaining side effects -- the cap tests'
  `last_stage` (`parse` for the chunk cap, `fetch` for the file cap) and no
  run; the shutdown test's `sync_state` still `syncing`; the uninstall
  test's chunks and runs empty; and the drift check after the re-ingest,
  not only the first ingest.
- **B-N4:** section 15's A3 row reads "met as corrected"; `STATE.md`'s Phase
  line is current; the deviation count now matches the PR body's
  (section 17, sixteen); `REJECT_SQL`'s state half joined the table (M31).
- **B-N5:** the lease-owner finding has a number, ISS-039 (with A-M1).
- **A-N1:** the printable top-level check is a full match, with a test for
  the trailing newline `$` let through.
- **A-N2:** `metadata` is accepted beside `contents` only at `read`, with a
  `metadata:write` case (M24).
- **A-N3:** the heartbeat's options start from `PGOPTIONS` when the DSN
  names none, pinned on real connections where `PGOPTIONS` sets the role
  (without it the heartbeat connected as the DSN's own role) and by a unit
  test of libpq's rule (M25).
- **A-N6:** `repository_token.go`'s package comment corrected (with A-M1).

**Not applied, with the reason:**
- **A-N4** (`embedding_generator.py` and `openai_client.py` log raw OpenAI
  exception text): it predates 22-05, lives in the embeddings package this
  PR does not otherwise touch, and no leak is known (OpenAI masks keys). It
  is folded into ISS-039, so the worker's log hygiene is fixed in one pass.
- **A-N5** (refuse a workdir that is a symlink, foreign-owned or
  group-writable): the worker runs in its own container, where the
  temporary directory is private; the check matters on a shared host, which
  is a deployment decision -- carried to Phase 24 in section 18.
- **A-N7** (a repository over a cap re-fetched on every push): a cost
  question for 24-05, carried in section 18; no code change asked.
- **A-N8** (a startup probe of the token route): part of ISS-040's fix,
  since the probe that catches a misrouted URL is the natural place to
  catch the wrong deployment too.
