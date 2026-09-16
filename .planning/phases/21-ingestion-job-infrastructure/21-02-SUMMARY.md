---
phase: 21-ingestion-job-infrastructure
plan: 02
subsystem: database
tags: [postgres, queue, migrations, foreign-keys, triggers, tenancy, skip-locked]

requires:
  - phase: 21-01
    provides: repositories.organization_id and repositories_id_org_key, which the composite foreign key references; isolation.WithSuperuserConn and AssertNoRepositoryTenantDrift, callable from a non-test package
  - phase: 17-01
    provides: the testcontainers harness (SetupTestDB, WithTwoOrgs, TenantScope)
  - phase: 0002
    provides: ingestion_runs UNIQUE (repository_id, commit_sha), which is why a retry resolves its run instead of inserting one
provides:
  - "Migration 000014 — ingestion_jobs, idx_ingestion_jobs_claimable, idx_ingestion_jobs_one_live_per_repo (the ISS-016 guard), ingestion_jobs_repo_tenant_fk (the guarantee), trg_ingestion_jobs_tenant (the message), and no RLS by documented decision"
  - "pkg/jobs — the package 21-03 fills, with schema_test.go holding the shared SQL as named constants: enqueueUpsertSQL, supersedeLiveSQL, completeSQL, claimSQL, sweepSQL, failSQL, clearRerunSQL, resolveRunSQL"
  - "16 tests (44 with subtests) pinning W1-W6, L4, both ISS-013 tenant-scope shapes, the claim, the sweeper and the lease fence on PostgreSQL 16"
affects: [21-03 (lifts the enqueue and supersede statements), 21-05 (lifts claim/complete/fail/sweep/clear/resolve), 21-07 (must filter by organization_id explicitly)]

tech-stack:
  added: []
  patterns:
    - "A partial unique index is the queue's concurrency guard: `UNIQUE (repository_id) WHERE state IN ('queued','running')` makes two live jobs unrepresentable, and its predicate must be REPEATED in every upsert's ON CONFLICT, because arbiter inference will not select a partial index otherwise"
    - "A denormalised tenant on a table with no RLS is guarded by a composite foreign key to the parent's (id, organization_id); the trigger beside it supplies the message, not the guarantee"
    - "A tenancy trigger that reads a table WITH row-level security is not an existence oracle for free: the read returns nothing outside the caller's tenant, so the disclosing branch is unreachable. Reading a table WITHOUT it (000013's projects read) needs an explicit gate"
    - "Test an unfiltered queue statement verbatim by deleting foreign rows inside a transaction that is rolled back, rather than adding a filter the statement does not have in production"
    - "Mutation-test a migration against a FRESH container, and PROVE the mutation reached the database (pg_get_functiondef, pg_indexes, pg_constraint) before believing a pass or a failure"

key-files:
  created:
    - services/backend/migrations/000014_ingestion_jobs.up.sql
    - services/backend/migrations/000014_ingestion_jobs.down.sql
    - services/backend/pkg/jobs/doc.go
    - services/backend/pkg/jobs/schema_test.go
  modified:
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "The tenant trigger reads `repositories.organization_id` directly rather than joining `projects`, which 21-CONTEXT L5 wrote because the column did not exist. The plan permits either; the direct read is also what makes the trigger a non-oracle, since `repositories` carries FORCE ROW LEVEL SECURITY and `projects` does not."
  - "Both of the trigger's refusals use SQLSTATE 42501, matching 000013's guard and 000010's assert_installation_matches_repository_tenant. Neither the plan nor the context specified a code."
  - "The mismatch message KEEPS `(owner %)`, unlike 000013's. Under row-level security that branch is only reachable by a caller whose own tenant owns the repository, or by a role that bypasses RLS and can already read the table. Pinned by TestIngestionJobs_TheTenantTriggerIsNotAnExistenceOracle."
  - "`supersedeLiveSQL`, `completeSQL` and `clearRerunNaiveSQL` were added to the plan's list of named constants: L4, W5 and W4 cannot be written without them, and 21-03/21-05 will lift the first two."
  - "No update trigger on `updated_at`. Every statement writes it explicitly, as the context's statements already do; a trigger would be a second writer of a column those statements already set."

issues-created: []

duration: ~2h
completed: 2026-09-16
---

# Phase 21 Plan 02: `ingestion_jobs`

**The queue exists, with the ISS-016 guard in the schema rather than in
application logic — and every SQL statement 21-03 through 21-06 will use has
now run on the PostgreSQL version we deploy, including the three that fail in
ways the context recorded wrongly.**

## The final migration

`000014_ingestion_jobs`, transcribed from 21-CONTEXT L2 and L5, in this order:

1. **The table** — five states (`queued`, `running`, `completed`, `dead`,
   `superseded`; no `failed`, decision O2), two job types, the lease pair,
   `attempts`/`max_attempts` (default 5)/`run_after`, `last_stage`,
   `progress`, `needs_rerun`, `last_error`, `payload` (**never credentials
   or an installation id**), and both timestamps.
2. **`idx_ingestion_jobs_claimable`** on `(run_after) WHERE state IN
   ('queued','running')` — without it the claim degrades to a full scan as
   terminal rows accumulate.
3. **`idx_ingestion_jobs_one_live_per_repo`,** `UNIQUE (repository_id) WHERE
   state IN ('queued','running')`. **This is the ISS-016 fix.** It is also
   the arbiter the enqueue upsert infers against, which is why that statement
   has to repeat the predicate.
4. **`ingestion_jobs_repo_tenant_fk`** — `FOREIGN KEY (repository_id,
   organization_id) REFERENCES repositories (id, organization_id) ON DELETE
   CASCADE`, onto 21-01's `repositories_id_org_key`. **The guarantee.**
5. **`ingestion_jobs_fix_tenant()` and `trg_ingestion_jobs_tenant`**, `BEFORE
   INSERT OR UPDATE OF organization_id, repository_id`. **The message.**
6. **`COMMENT ON COLUMN`** for `organization_id`, `payload` and
   `needs_rerun`, and a **`COMMENT ON TABLE`** carrying the four things a
   reader must know: no RLS and why, `organization_id` is an authorization
   input that 21-07 must filter by explicitly, writes must be tenant-scoped,
   the ordering rule, and the pruning `DELETE` Phase 24 will schedule.

The down migration drops the trigger, the function and the table. Queue rows
are work not yet done; the record of what happened lives in `ingestion_runs`.

**Jobs → repositories → projects is now enforced by foreign keys end to
end,** as 21-01's summary predicted: `ingestion_jobs (repository_id,
organization_id) → repositories (id, organization_id) → projects (id,
organization_id)`.

### Two departures from L5's transcription, both deliberate

**1. The trigger reads `repositories.organization_id` directly** instead of
joining `projects`. L5 wrote the join because the column did not exist yet;
the plan permits either. It is one table read rather than two, and it is also
what makes the second departure safe.

**2. The mismatch message keeps `(owner %)`, where 000013's could not.** PR
#37's review measured 000013's equivalent branch answering "does this project
exist and who owns it?" from inside another tenant, because it reads
`projects`, which has no row-level security. This one reads `repositories`,
which has `FORCE ROW LEVEL SECURITY` — a caller scoped elsewhere sees no row
at all and gets `repository <id> does not exist`, byte-identical to a
repository id that exists nowhere. The mismatch branch is reachable only when
the caller's own tenant owns the repository, or by a role that bypasses RLS
and can already read the whole table.

That is a property of *which table the trigger reads*, not of the code, so it
is pinned by a test (`TheTenantTriggerIsNotAnExistenceOracle`) and by mutation
C below, which shows what the message degrades to without the NOT FOUND
branch.

## Facts the plan rested on, re-measured on PostgreSQL 16.15

The context's statements were executed on PostgreSQL 17. Our harnesses, CI and
compose all run `postgres:16-alpine`.

| Claim, as the context recorded it on 17 | On 16.15 |
|---|---|
| `ON CONFLICT DO UPDATE` with no target raises `42601` | **Same.** `ON CONFLICT DO UPDATE requires inference specification or constraint name` |
| `ON CONFLICT (repository_id)` without the index predicate raises `42P10` | **Same.** `there is no unique or exclusion constraint matching the ON CONFLICT specification` |
| Only the full form parses, and reports `was_existing` via `xmax <> 0` | **Same.** First call `f`, second `t` and the same id |
| A bulk enqueue racing a live job handles every row (W3) | **Same.** 1 flagged, 2 inserted, all 3 repositories with exactly one live job |
| `RETURNING needs_rerun` after clearing yields the NEW value, `false` (W4) | **Same.** The fenced form returns a row; the naive form returns `false` |
| Complete-then-re-enqueue in that order raises no 23505 (W5) | **Same** |
| A retry reuses its `ingestion_runs` row (W6) | **Same.** Same id twice for one commit, a different id for a different commit |
| The composite FK refuses a mismatch with the trigger disabled and RLS bypassed (W1) | **Same.** `23503` naming `ingestion_jobs_repo_tenant_fk` |
| An unscoped write fails two ways depending on connection history (ISS-013) | **Same.** Unset GUC → the trigger's `42501 repository … does not exist`; after a committed `SET LOCAL` → `22P02 invalid input syntax for type uuid: ""`, raised inside the RLS policy |
| `RETURNING OLD.*` "is PostgreSQL 18 and errors on 17" | **Errors on 16 too**, with `42P01 missing FROM-clause entry for table "old"` — the code the context did not name. Nothing in this plan uses it. |

**Nothing behaved differently on 16 than the context recorded on 17.** The one
addition is the SQLSTATE for `RETURNING OLD.*`.

## The corrected L4/W5 failure mode, confirmed

21-CONTEXT L4 and L7 recorded `23505` for the wrong order. The correction
dated 2026-09-14 says that is true only of a *plain* `INSERT`; through L7's
upsert — the only enqueue path — the reverse order raises nothing. **Measured,
all three cases:**

| Case | Test | Result on 16 |
|---|---|---|
| Right order: complete, then upsert | `CompletionThenReEnqueue/the_right_order_leaves_one_new_live_job_and_no_error` | No error; one new `queued` job; the old one `completed` |
| Wrong order through the upsert | `CompletionThenReEnqueue/the_wrong_order_through_the_upsert_loses_the_rerun_silently` | **No error, and NO live job afterwards** |
| Wrong order through a plain `INSERT` | `CompletionThenReEnqueue/the_wrong_order_through_a_plain_INSERT_raises_23505` | `23505` on `idx_ingestion_jobs_one_live_per_repo` |
| Right order: supersede, then upsert | `SupersedeBeforeEnqueue/the_right_order_supersedes_the_old_job_and_queues_a_new_one` | Old job `superseded`, one new `queued` job |
| Wrong order through the upsert | `SupersedeBeforeEnqueue/the_wrong_order_through_the_upsert_leaves_no_live_job_and_no_error` | **No error, and NO live job afterwards** |
| Wrong order through a plain `INSERT` | `SupersedeBeforeEnqueue/the_wrong_order_through_a_plain_INSERT_raises_23505` | `23505` |

**One detail the correction did not record, measured here:** the `needs_rerun`
flag the upsert set *survives on the terminal row*. The repository is left
with no live job, and the row that remembers a rerun was wanted is
`completed` or `superseded`, where nothing will ever look at it. So the loss
is not only silent, it is also durable-looking — a reader inspecting the table
afterwards sees a flag that reads as "a rerun is pending". Asserted by the
test.

## Mutation results

Each mutation ran the whole `pkg/jobs` suite. Migration mutations ran against
a **freshly created** harness container (`docker rm -f
rag-doc-isolation-tests` first — golang-migrate never re-applies a version
already in `schema_migrations`, so a reused container silently tests the old
schema), and each was **proven to have reached the database** before the
result was believed.

| # | Mutation | Where | Predicted | Result |
|---|---|---|---|---|
| 1 | Remove the predicate from the upsert's `ON CONFLICT` | `enqueueConflictClause` | a test fails | **Killed: 5 top-level.** `EnqueueUpsertParsesAndReports`, `BulkEnqueueRacingALiveJob`, `CompletionThenReEnqueue`, `SupersedeBeforeEnqueue`, `EnqueueingNeedsTenantScope` — all with `42P10` |
| 2 | Drop `AND attempts < max_attempts` from the claim | `claimSQL` | a test fails | **Killed: 1.** `Claim/a_job_at_max_attempts_is_not_claimed` |
| 3 | Drop the `state = 'queued'` branch from the sweeper | `sweepSQL` | a test fails | **Killed: 1.** `Sweeper/a_queued_job_at_max_attempts_is_dead-lettered` |
| 4 | Drop `lease_expires_at IS NULL` from the claim | `claimSQL` | a test fails | **Killed: 1.** `Claim/a_running_job_with_a_null_lease_is_reclaimed` |
| 5 | Make the unique index non-partial | **000014** (fresh container) | a test fails | **Killed: 5.** `OneLiveJobPerRepository/once_the_first_is_completed`, `EnqueueUpsertParsesAndReports/ON_CONFLICT_(repository_id)_without_the_index_predicate` (which now *succeeds*, because a non-partial index is a valid arbiter), `CompletionThenReEnqueue`, `SupersedeBeforeEnqueue`, `SchemaShape/the_indexes`. Proven: `pg_indexes` showed the index with no `WHERE` |
| A *(added)* | Drop `ingestion_jobs_repo_tenant_fk` | **000014** (fresh container) | — | **Killed: 2.** W1 — the mismatched row **inserted cleanly** ("An error is expected but got nil"), which is exactly what W1 exists to prove cannot happen — and `SchemaShape/the_composite_foreign_key`. Proven: `pg_constraint` count 0 |
| B *(added)* | Remove the trigger's mismatch `RAISE` | **000014** (fresh container) | — | **Killed: 1.** `TenantTriggerNamesTheMismatch` only; the row is still refused, now with `23503` from the composite key. **The guarantee holds; only the message is lost** — which is what the migration's comment claims. Proven: `pg_get_functiondef` no longer contains `does not match` |
| C *(added)* | Remove the trigger's NOT FOUND `RAISE` | **000014** (fresh container) | — | **Killed: 2.** `TheTenantTriggerIsNotAnExistenceOracle` and `EnqueueingNeedsTenantScope/a_fresh_connection`. The message degrades to `organization_id <the caller's own org> does not match repository <id> (owner <NULL>)`, so the non-oracle property depends on both branches, not one. Proven: `pg_get_functiondef` |

The 22P02 half of the tenant-scope test survives mutation C, correctly: the
RLS policy raises before any branch of the function runs.

## ISS-031 and ISS-013

**000014 is DDL only** — `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE ADD
CONSTRAINT`, `CREATE FUNCTION`, `CREATE TRIGGER`, `COMMENT` — so ISS-031's
hazard (000013 leaves the migrating session holding the last organization's
id, then `''`) cannot touch it. **Measured rather than assumed:** a scratch
`postgres:16-alpine` taken to version 12, seeded with three organizations and
four repositories inserted under their own tenants, then migrated `up`
through **000013 and 000014 in one golang-migrate run** — clean, version 14,
not dirty, all four repositories backfilled. A `down 1` / `up` on that same
seeded database, with a job row present, is also clean.

**ISS-013's two shapes are now pinned for this table** by
`TestIngestionJobs_EnqueueingNeedsTenantScope`, so a later reader who hits
`22P02` from an enqueue looks at the caller's tenant scope rather than
"fixing" the trigger. The test that produces `''` hijacks its pooled
connection and closes it, so the poisoned backend never returns to the pool.

## Deviations from the plan

1. **Three named constants beyond the plan's list** — `supersedeLiveSQL`,
   `completeSQL` and `clearRerunNaiveSQL`. L4, W5 and W4's negative half
   cannot be written without the first two, and 21-03/21-05 will lift them.
   `enqueueConflictClause` is split out of `enqueueUpsertSQL` so the bulk
   form (W3) is structurally the same statement rather than a copy.
2. **Tests beyond the plan's list:** the existence-oracle test, `failSQL`'s
   lease fence and direct dead-letter on the final attempt, two extra claim
   cases (backoff not elapsed; live lease), one extra sweeper case (below max
   attempts), and the schema-shape subtests (no RLS, the table comment's three
   load-bearing sentences, both `CHECK`s, and that the tenant trigger is the
   only user trigger).
3. **Three extra mutations** (A, B, C above), following 21-01's practice: a
   guarantee nobody has seen fail proves nothing.
4. **`-race` ran in a container, not natively.** This machine has no C
   toolchain, which is the same condition `backend-ci.yml`'s comment records
   for the `-race` step. It was run as `golang:1.25` on Linux with the
   worktree and the Docker socket mounted and
   `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`, against the same
   harness Postgres: **ok, no races.** Note that CI's `-race` step covers
   `./pkg/api/... ./pkg/db/... ./pkg/testing/...` and **not** `./pkg/jobs/...`;
   worth adding when 21-03 puts a barrier race test in this package.
5. **`withQueue`.** `claimSQL` and `sweepSQL` deliberately have no repository
   filter — production has one queue — so in a container shared across
   packages a stray row makes "nothing was claimed" untestable. The claim and
   sweeper tests therefore run inside an **unscoped transaction** (as a worker
   does) that first deletes jobs for other repositories and is then **rolled
   back**. Nothing is removed; the statements stay verbatim.
6. **`ingestion_jobs` is deliberately NOT added to `protectedTables`** in
   `db_assertion_test.go`, per the plan: it has no RLS and no
   `trg_assert_tenant`, by design. `container_test.go`'s table-count check is
   a `>=`, so it needed no change either. No fixture changed:
   `cleanupOrg`'s `DELETE FROM repositories` cascades to this table.

## Verification

| Check | Command | Result |
|---|---|---|
| Backend, whole module | `DATABASE_TEST_URL=<scratch CI-shaped PG16> REDIS_URL=redis://localhost:63790/14 go test ./... -count=1 -p 1 -v`, on a **freshly created** harness container | **152 top-level pass (421 with subtests), 3 skip, 1 fail.** The failure is `TestSignatureComparisonIsConstantTime` ("could not find the end of verifySignature"), the known Windows-CRLF-checkout failure; `git diff RAG-Doc/main` shows this PR touches neither that test nor the file it reads. The skips are `pkg/auth`'s pre-existing "superseded by pkg/testing/isolation (Phase 17-01)". |
| New package | `go test ./pkg/jobs/... -count=1 -v` | **16 top-level, 44 with subtests, all pass** |
| Race detector | `go test -race ./pkg/jobs/... -count=1` inside `golang:1.25` (see deviation 4) | ok, no races |
| Workers, CI environment | `REDIS_URL=redis://localhost:63790/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, no `DATABASE_URL`, no reachable `.env`, fresh venv from `requirements.txt` | **181 passed**, 0 skipped, 0 failed (19 pre-existing `utcnow` deprecation warnings) |
| 000014 through the Python applier | psycopg2, every `*.up.sql` sorted, **one `execute` per whole file** — the way `tests/isolation/conftest.py` does it, which differs from `psql -f` | 14 files applied; table, trigger and composite key all present |
| Up, down, up | golang-migrate **v4.19.1** (the version CI pins) on scratch `postgres:16-alpine`: full `up` → dump → `down 1` → dump → `up` → dump | **Clean.** The version-13 dump after `down 1` is identical to a database migrated straight to 13, and the two version-14 dumps are identical — excluding the one line `pg_dump` regenerates every run (`\restrict <random token>`) |
| Seeded, 13 and 14 in one run | above, under ISS-031 | clean, `schema_migrations = 14, dirty = false` |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | nothing missing (no endpoint changed) |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

**No container on port 5434 (docker-compose Postgres) or Qdrant was started
or touched.** Scratch containers used: `rag2102-scratch` (55501),
`rag2102-ci` (55502), `rag2102-redis` (63790), plus the harness's own
`rag-doc-isolation-tests`, which was removed and rebuilt between mutation
runs and once more before the final verification.

## What 21-03 through 21-07 inherit

- **The statements**, as named constants in `pkg/jobs/schema_test.go`, each
  with a passing test on PostgreSQL 16. Lift them verbatim.
- **The ordering rule**, in the table comment and in `doc.go`: supersede or
  complete before enqueueing, in one transaction. Getting it wrong is silent.
- **The lease fence** on every terminal write, and a test showing it matching
  zero rows for a worker that no longer owns the job.
- **21-07's obligation**, stated in the table comment: this table has no RLS,
  so `GET /api/admin/jobs/{id}` must filter by `organization_id` explicitly,
  and the CI isolation gate will not catch a mistake because it scans
  mutation endpoints.
- **`pkg/jobs` is not in CI's `-race` step.** Add it when 21-03's barrier race
  test lands.

## Follow-ups

- **Add `./pkg/jobs/...` to `backend-ci.yml`'s race step** when 21-03 puts a
  concurrent test in this package. Not done here: at 21-02 the package has no
  concurrent test, so the step would cost runtime and gate nothing.
- **21-CONTEXT L5's open item — drift on re-parent — is now closed at this
  level too,** by 21-01's composite key plus this one. A repository cannot
  change organization, so a job's `organization_id` cannot go stale. L5 asks
  Phase 22 to revisit rather than inherit silently; the answer is that the
  schema already forbids the move.
- **The backoff ceiling** that 21-CONTEXT's open questions asked 21-02 to
  pick belongs to **21-05** under the 2026-09-14 plan mapping. `failSQL`
  takes the interval as a parameter, so nothing here constrains the choice.

## Next Phase Readiness

21-03 can import `pkg/jobs` and build `Enqueue` and `SupersedeLive` on
statements that have run.

---
*Phase: 21-ingestion-job-infrastructure — 2 of 7 plans*
*Completed: 2026-09-16*
