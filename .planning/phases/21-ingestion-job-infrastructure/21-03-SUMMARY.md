---
phase: 21-ingestion-job-infrastructure
plan: 03
subsystem: backend
tags: [postgres, queue, producers, concurrency, tenancy, locking]

requires:
  - phase: 21-02
    provides: ingestion_jobs, its partial unique index, the composite FK and the tenant trigger; enqueueUpsertSQL / supersedeLiveSQL as named constants with passing tests on PostgreSQL 16
  - phase: 21-01
    provides: repositories.organization_id, and isolation.AssertNoRepositoryTenantDrift
  - phase: 20-03
    provides: POST /api/repositories, its org-wide adopt lookup, and the InstallationRepositoryLister seam that makes the persist path testable
provides:
  - "pkg/jobs.Enqueue and pkg/jobs.SupersedeLive — the only supported way for backend code to put work on the queue, both taking the caller's transaction"
  - "enqueueConflictClause / enqueueReturning / enqueueUpsertSQL / enqueueSetSQL / liveSetPredicate / supersedeLiveSQL / supersedeLiveSetSQL / projectPendingSQL in producer.go, referenced by schema_test.go rather than copied"
  - "POST /api/repositories classifies new / relink / unchanged in Go and acts through pkg/jobs; `FOR UPDATE OF r` on the adopt lookup"
  - "isolation.RetryOnLockContention, IsDeadlock, IsLockTimeout, LockWaitTimeout — the ISS-032 fix, reusable"
  - "handlers.ExistingRepositoryLookupSQL (export_test.go) so a test can run the production lookup"
affects: [21-04 (the webhook producers call the same two functions), 21-05 (sync_state transitions after `pending`), 21-07 (reads the rows this creates)]

tech-stack:
  added: []
  patterns:
    - "A producer takes the CALLER'S transaction, never a pool: the caller owns atomicity and tenant scope, and there is no unscoped path to fall back to"
    - "De-duplicate a set before an ON CONFLICT DO UPDATE: the statement may not touch the same row twice and raises 21000, and nothing in SQL does it for you"
    - "`FOR UPDATE OF <alias>` on a lookup that JOINs: a bare FOR UPDATE locks every table in the FROM clause, which turns a per-row lock into a per-organization one"
    - "Read a projection column AFTER the write that projects it, not from the write's RETURNING, or the response reports the value before the transaction finished with it"
    - "A test that takes AccessExclusiveLock on shared tables sets `lock_timeout` BELOW `deadlock_timeout` and retries: giving up sooner is what keeps it out of a cycle, where waiting longer is what makes it the victim"

key-files:
  created:
    - services/backend/pkg/jobs/producer.go
    - services/backend/pkg/jobs/producer_test.go
    - services/backend/pkg/api/handlers/export_test.go
    - services/backend/pkg/testing/isolation/deadlock.go
    - services/backend/pkg/testing/isolation/deadlock_test.go
  modified:
    - services/backend/pkg/jobs/doc.go
    - services/backend/pkg/jobs/schema_test.go
    - services/backend/pkg/api/handlers/repositories.go
    - services/backend/pkg/api/handlers/repositories_connect_test.go
    - services/backend/pkg/testing/isolation/repositories_organization_id_test.go
    - docs/api-repositories.md
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "`schema_test.go` became an INTERNAL test package (`package jobs`) so it can reference the production constants. The alternative — exporting them, or leaving copies in the test file — either widens the package's API for no caller or reintroduces the drift the constants exist to prevent."
  - "`enqueueReturning` is split out of `enqueueConflictClause` so the set form can report `repository_id` while `enqueueUpsertSQL` stays byte-identical to what 21-02 shipped. Splitting the RETURNING list is what avoids copying the ON CONFLICT clause, which is the part that is easy to get wrong."
  - "Enqueue returns one result per DISTINCT repository, not per input element, and the first request for a repository wins. A bulk payload that names a repository twice is asking for one job."
  - "The projection is written only for repositories that got a NEW job. A repository whose live job was merely flagged keeps its `sync_state`, because that job is running and the state belongs to its worker."
  - "SupersedeLive keeps the plan's signature and is NOT tenant-scoped; the obligation on the caller is stated in the doc comment and PINNED by a test that measures the cross-tenant supersede rather than leaving it as folklore. Adding an `EXISTS (SELECT 1 FROM repositories ...)` scope was considered and rejected: it would depart from the statement 21-02 proved, and would turn a caller's bug into a silent no-op."
  - "A nil transaction is reported ahead of an empty batch: a caller that got here without a transaction is broken whether or not this particular batch was empty."
  - "The installation id is canonicalised with `uuid.Parse(...).String()` before the relink comparison, even though `validate:\"uuid\"` already guarantees the canonical form. The correctness of a text comparison should not rest on a struct tag ten lines away — `uuid_rfc4122` accepts uppercase, and swapping the tag for it would otherwise be a silent infinite re-queue."
  - "`withQueue`'s blanket delete was REMOVED rather than scoped or serialised. Neither of those works: the delete existed so `claimSQL` could be tested against an empty queue, and a concurrently enqueued job with a tied `run_after` breaks that test whether or not the delete holds locks. Ordering replaced it."

issues-created: []
issues-closed:
  - "ISS-032 — reproduced locally first, then fixed with `lock_timeout` below `deadlock_timeout` plus six jittered retries on 40P01/55P03"

duration: ~4h
completed: 2026-09-16
---

# Phase 21 Plan 03: the Go producer

**Connecting a repository now creates a real work item, a relink cancels
the run that was in flight instead of racing it, and the decision to do
either is a value in Go rather than two SQL `CASE` expressions that could
disagree.**

## The producer API

`services/backend/pkg/jobs/producer.go`. Two functions, both taking the
**caller's** transaction — the caller owns atomicity and tenant scope, and
the package holds no pool to fall back to.

```go
type JobType string
const (
    JobTypeFullIngest  JobType = "full_ingest"
    JobTypeIncremental JobType = "incremental"
)

type EnqueueRequest struct { OrganizationID, RepositoryID string; JobType JobType }
type EnqueueResult  struct { RepositoryID, JobID string; WasExisting bool }

func Enqueue(ctx context.Context, tx pgx.Tx, reqs []EnqueueRequest) ([]EnqueueResult, error)
func SupersedeLive(ctx context.Context, tx pgx.Tx, repositoryIDs []string) ([]string, error)
```

`Enqueue`, in order:

1. **Validates the whole batch first** — canonical UUIDs and a known job
   type — so a bad request cannot abort a transaction the caller is still
   using. The canonical-form check is the point, not the parse:
   `uuid.Parse` accepts the URN form, the brace form, the undashed form and
   uppercase, and Postgres accepts some of those and rejects others with
   22P02.
2. **De-duplicates on `RepositoryID`,** first request wins. `ON CONFLICT DO
   UPDATE` may not touch the same row twice in one statement — **21000**,
   measured, see the mutation table.
3. **Runs one statement,** `enqueueSetSQL`: 21-02's upsert over
   `unnest($1::uuid[], $2::uuid[], $3::text[])`, sharing the
   `enqueueConflictClause` constant with the single-row form. One
   repository and two hundred take the same code path.
4. **Writes the projection** — `sync_state = 'pending'` — for the
   repositories that got a **new** job only, and checks the row count,
   because under row-level security a write to somebody else's repository
   matches nothing and reports success.

Results come back in the order of the de-duplicated input.

### The statements moved, verbatim

`enqueueUpsertSQL`, `enqueueConflictClause` and `supersedeLiveSQL` now live
in `producer.go` with their comments. `schema_test.go` became an **internal
test package** (`package jobs`) so 21-02's tests reference those constants
rather than keeping copies — which mutation 2 below demonstrates: removing
the predicate from the production conflict clause fails **fourteen** tests,
nine of them 21-02's.

Two constants are new, and both are shared rather than copied:

| Constant | Why |
|---|---|
| `enqueueReturning` | split out of the conflict clause so the set form can report `repository_id`. `enqueueUpsertSQL` expands byte-identically to 21-02's. |
| `liveSetPredicate` | `state IN ('queued','running')`, shared by both supersede forms so they cannot disagree about what "live" means. |

### ⚠ `SupersedeLive` is not tenant-scoped, and that is now measured

Its statement touches neither `organization_id` nor `repository_id`, so
`trg_ingestion_jobs_tenant` never fires, and `ingestion_jobs` has no
row-level security. **A repository id from another organization is
superseded just as readily as one of the caller's.**

`TestSupersedeLive_IsNotScopedByTheDatabase` runs exactly that from inside
org A's transaction and asserts it succeeds. The obligation the doc comment
states — pass only ids the same transaction has already read out of
`repositories`, which *is* scoped — is the only thing standing between a
caller and another tenant's queue. Connect satisfies it through its
`FOR UPDATE OF r` lookup.

This follows `doc.go`'s existing treatment of `claimSQL` and `sweepSQL`:
state the cross-tenant property and pin it, rather than quietly adding a
filter to a statement 21-02 proved.

## The Connect restructure

### Before and after

| | Before | After |
|---|---|---|
| **Where the decision lives** | two SQL `CASE` expressions — one in the adopt `UPDATE`, one in the `INSERT`'s `DO UPDATE` — that could disagree | one `connectDecision` value in Go, logged, assertable |
| **How it is reported** | not at all; `RETURNING sync_state` says only what the column holds, so "already pending" and "just re-queued" look identical | `decision`, `flagged_existing_job` and `superseded_live_job` in one `slog` line after the commit |
| **The existing-row lookup** | `SELECT … JOIN projects … LIMIT 1`, no lock | the same, **`FOR UPDATE OF r`** |
| **Queueing work** | `sync_state = 'pending'` written by the row write | `jobs.Enqueue`, in the same transaction |
| **A relink over a running job** | stamped `pending` over `syncing`; two writers, last one wins (**ISS-016**) | `jobs.SupersedeLive` then `jobs.Enqueue`, in that order, one transaction |
| **`sync_state` in the response** | from the row write's `RETURNING` | re-read after the enqueue, so it is what the transaction committed |

### The decision table

| Classification | When | What it does |
|---|---|---|
| `new` | the org-wide lookup finds no row | `Enqueue(full_ingest)` |
| `relink` | `installation_id` changed, **or** `github_repo_id IS NULL` | `SupersedeLive`, then `Enqueue(full_ingest)` |
| `unchanged` | anything else | nothing |

`unchanged` is the case that matters most and the easiest to lose: a
re-connect is a metadata refresh, not a retry. Mutation 11 (always enqueue)
is what holds it.

### `FOR UPDATE OF r`, not `FOR UPDATE`

The lookup joins `projects`, and a bare `FOR UPDATE` locks **every table in
the FROM clause** — including the organization's default project row, which
every repository in the organization hangs off. Nothing would fail;
connects would simply queue up one at a time behind whichever one is in
flight, and the only symptom would be latency under load.

`TestRepositoriesConnect_ConcurrentConnectsOfDifferentRepositoriesDoNotBlock`
holds one repository's row using **`handlers.ExistingRepositoryLookupSQL`
itself** (exported through `export_test.go`) and then connects a different
repository through the real router with a deadline. Mutated to a bare
`FOR UPDATE`, it fails in 17s with the message that names the cause.
Running the production statement rather than a copy is what gives the test
that property: a hand-written `FOR UPDATE OF r` in the test would have kept
passing after someone widened the real one.

### What did not change

tx1 (the installation lookup) and the GitHub round-trip stay outside the
write transaction. Holding a transaction open across a network call is what
the existing structure deliberately avoids, and this plan did not touch it.

## Race tests, and their rounds

| Test | Shape | Rounds |
|---|---|---|
| `TestEnqueue_ConcurrentEnqueuesResolveToOneLiveJob` | 16 goroutines, each with its **own transaction opened before the barrier**, released together onto the same repository. Every call must succeed, exactly one `WasExisting=false`, exactly one live job, `sync_state = pending`. | **5 rounds per run**, all warm but the first; run 3× under `-race` → **15 rounds**, no failures, no data races |
| `TestRepositoriesConnect_ConcurrentRelinksLeaveOneLiveJob` | two relinks of the same repository through the **real router**, released together, over a live `running` job seeded per round. Both must be 201; one live job; `sync_state = pending`. | **5 rounds per run**, run 3× under `-race` → **15 rounds** |
| `TestRepositoriesConnect_ConcurrentConnectsOfDifferentRepositoriesDoNotBlock` | one transaction holds a repository's row with the production lookup; a connect for a different repository in the same organization must finish within 15s | run 3× under `-race` |

**Its own pool for the barrier test.** `pgxpool` defaults `MaxConns` to
`max(4, NumCPU)` and CI's runner has two cores, so on the machine that
matters 16 racers would have queued for connections instead of racing. The
test copies the harness pool's config with `MaxConns = 18`.

**One honest limit.** The two-caller relink race does not exercise the
upsert's conflict branch: `FOR UPDATE OF r` serialises the two into
`relink` + `unchanged`, so the second caller enqueues nothing. That is the
correct behaviour and it is what L8 asks for ("both succeed, one job"), but
the *flagging* path at the HTTP level is therefore untested. The producer's
16-way barrier is what covers it — and mutation 1 confirms the coverage, by
failing there and **not** in the handler suite.

## Mutation results

Every mutation was applied to the committed file, run, and reverted from a
backup; the tree was verified clean afterwards. Where a mutation produced a
database error, the SQLSTATE was read rather than assumed.

### `pkg/jobs`

| # | Mutation | Result |
|---|---|---|
| 1 | `enqueueSetSQL` drops the `ON CONFLICT` clause (a plain INSERT) | **Killed: 4.** `ASecondEnqueueFlagsTheLiveJob`, `BulkRacingALiveJob`, `TheWrongOrderLosesTheJobSilently`, `ConcurrentEnqueuesResolveToOneLiveJob` — `23505` on `idx_ingestion_jobs_one_live_per_repo`. The handler suite stayed green, which is the honest limit noted above. |
| 2 | Remove the predicate from `enqueueConflictClause` | **Killed: 14 top-level** — 5 of this plan's and **9 of 21-02's**, all `42P10`. This is also the proof that `schema_test.go` now references the production constant instead of a copy. |
| 3 | Remove the de-duplication in `Enqueue` | **Killed: 1.** `DuplicateRepositoryIDsAreDeDuplicated`, with `21000 ON CONFLICT DO UPDATE command cannot affect row a second time` — the error the de-duplication exists for, measured rather than cited. |
| 4 | Never write the projection | **Killed: 4 in `pkg/jobs` + 3 top-level and 1 subtest in the handlers** |
| 5 | Project EVERY repository, not only the fresh ones | **Killed: 2.** `ASecondEnqueueFlagsTheLiveJob` and `BulkRacingALiveJob` — the two that assert a running job keeps its `syncing`. The handler suite stayed green: no handler test reaches a flagged enqueue. |
| 6 | `canonicalUUID` returns after the parse, dropping the canonical-form check | **Killed: 2 top-level** (4 subtests): the URN, brace, uppercase and undashed cases, plus `SupersedeLive_RejectsANonCanonicalID` |

### `pkg/api/handlers`

| # | Mutation | Result |
|---|---|---|
| 7 | `FOR UPDATE OF r` → `FOR UPDATE` | **Killed: 1.** `ConcurrentConnectsOfDifferentRepositoriesDoNotBlock`, after 17s, reporting that the connect blocked on another repository's connect |
| 8 | Drop `existingGitHubRepoID == nil` from the relink arm | **Killed: 1 subtest.** `AdoptingARowThatNeverHadAGitHubIDQueuesIt`. `AdoptsALegacyRow…` correctly survives — that row's `installation_id` is NULL, so it is still a relink. |
| 9 | Swap the supersede and the enqueue | **Killed: 3 top-level + 1 subtest**, and **nothing raised an error** — every failure is an assertion about a missing live job. The silent failure mode, caught. |
| 10 | Never supersede | **Killed: 1 top-level + 1 subtest** |
| 11 | Enqueue on `unchanged` too | **Killed: 1 top-level + 1 subtest.** `ReconnectingToTheSameInstallationCreatesNoJob` catches it on `needs_rerun` — the assertion that the in-flight job is not even *flagged* is what earns its place here. |
| 12 | Read `sync_state` before the supersede/enqueue instead of after | **Killed: 2 top-level + 1 subtest**, including 20-03's own `PersistsWhatGitHubReported` |

### 21-02's statements, re-checked after the `withQueue` change

The claim and sweeper tests lost their empty queue (below), so the
mutations that depended on it were re-run:

| # | Mutation | Result |
|---|---|---|
| 13 | Drop `attempts < max_attempts` from `claimSQL` | **Killed: 1 subtest.** `Claim/a_job_at_max_attempts_is_not_claimed` |
| 14 | Drop the `state = 'queued'` branch from `sweepSQL` | **Killed: 2 subtests.** `Sweeper/a_queued_job_at_max_attempts_is_dead-lettered` and `EveryStatementAdvancesUpdatedAt/sweepSQL` |
| 15 | Delete `FOR UPDATE SKIP LOCKED` from `claimSQL` | **Killed: 1.** `ClaimSkipsRowsLockedByAnotherWorker`, `57014` after the 3s statement timeout |

### One deliberate survivor

`Enqueue`'s row-count check on the projection (`updated %d of %d
repositories`) is **unreachable today** and no mutation kills it. Reaching
it needs a transaction whose tenant scope covers the enqueue but not the
projection, and the enqueue is scoped by the same `repositories` read, so
it fails first with 42501. It is kept as cheap insurance against a future
caller that scopes them differently, and is recorded here rather than left
for a reviewer to discover — following 21-01's practice with D5's guard.

## `withQueue`: removed, not narrowed

21-02 left a warning on `withQueue`: it ran
`DELETE FROM ingestion_jobs WHERE repository_id <> ALL($1)` inside a
rolled-back transaction, which was safe only while `pkg/jobs` was the sole
writer of the table. **This plan makes `pkg/api/handlers` a writer.**

Neither of the two suggested fixes works:

- **Scoping the delete** cannot work. The delete existed so that
  `claimSQL` — which deliberately has no repository filter — could be
  tested against a queue holding only this test's rows. Leaving other
  packages' rows in place is exactly what breaks "nothing was claimed".
- **Serialising** would need every writer to cooperate, and one of the
  writers is now production code inside an HTTP handler.

**So the delete is gone, and ordering replaced it.** `withQueue` is now
`asWorker(t, pool, fn)`: an unscoped transaction, rolled back, that deletes
nothing. The claim tests backdate `run_after` by a decade
(`claimTestEpoch`), so the test's own job is the oldest claimable row in the
table; the negative cases assert "*this* job was not claimed" rather than
"nothing was claimed", which is sound precisely because the test's row would
be picked first if it were claimable at all. `requireNoOlderJobs` turns the
one way that argument can fail — a leaked row from a crashed run, backdated
further — into a plain sentence.

`TestIngestionJobs_ClaimSkipsRowsLockedByAnotherWorker` had a second blanket
`DELETE` of its own; that is gone too, and its two rows are backdated
further than `claimTestEpoch`.

**What is left, stated precisely:** `claimSQL` locks the one row it picks,
which in a negative case may be a foreign row for the few statements until
the transaction rolls back; and `sweepSQL` is a blanket `UPDATE`, so it
touches rows at `attempts >= max_attempts`, which no producer creates. **No
test in this package deletes a row it did not create any more.** Mutations
13-15 confirm the claim and sweeper tests still have teeth without the
delete.

## ISS-032: reproduced, then fixed — and the filed fix was not enough

The issue said the deadlock was **not reproducible locally** (16 runs, zero
deadlocks) and recommended, cheapest first, "a bounded retry on `40P01`".

**Both halves of that turned out to be wrong, and measuring beat guessing.**

1. **It reproduces** once `./pkg/jobs/...` is added to the
   package-parallelism set — which this plan's tests make a realistic thing
   to do. `go test ./pkg/api/... ./pkg/auth/... ./pkg/db/... ./pkg/jobs/...
   ./pkg/testing/... -count=3` at default parallelism failed with
   `still deadlocking after 3 attempts: ERROR: deadlock detected (SQLSTATE 40P01)`.
2. **The bounded retry alone is insufficient.** That message *is* the first
   cut of this fix: three attempts, 100ms linear backoff. All three
   deadlocked. Under four packages' worth of sustained traffic against
   `repositories` and `projects`, a short fixed backoff just lands in the
   next burst.

**What shipped, two halves:**

- **`SET LOCAL lock_timeout = 750ms`** on the transaction that does the
  DDL — deliberately *below* PostgreSQL's default `deadlock_timeout` of one
  second. The detector does not run until a transaction has waited that
  long, so a transaction that gives up first is normally not a victim and,
  more usefully, stops being one side of a cycle before a cycle can be
  reported. Waiting *longer* is the instinct, and it is what makes a
  deadlock the likely outcome instead of a timeout.
- **`isolation.RetryOnLockContention`** — six attempts, linear backoff with
  jitter, retrying **both** `40P01` and `55P03`, because the other side's
  detector can still fire first and pick us. `40001` is deliberately not
  retried: a serialization failure is a statement about the data, not about
  who won a lock race.

`deadlock_test.go` exercises the helper with synthetic errors, causes a
**real** PostgreSQL deadlock to prove the classification, and measures that
`LockWaitTimeout` produces a retryable `55P03` — a retry nobody has watched
retry is a `for` loop with a comment on it.

**Evidence after the fix:** the command that reproduced it, at `-count=5`
(25 package runs, default parallelism, `./pkg/jobs/...` included), passed
every time.

**What is NOT established, and is recorded as such in ISS-032:** which half
does the work. A mutation removing only the `lock_timeout`, keeping the six
retries, also passed 5 rounds — the original deadlock was a one-in-N event
and this machine would not reproduce it again on demand. Both halves are
cheap, complementary and argued from the lock model rather than from that
one measurement.

## Deviations from the plan

1. **`schema_test.go` is an internal test package now.** The plan did not
   say how the tests would reference production constants; `package jobs`
   is the only way that does not either widen the package's API or leave
   copies behind.
2. **Two constants beyond what moved** — `enqueueReturning` and
   `liveSetPredicate` — both so that the set forms share the original text
   rather than copying it. `enqueueUpsertSQL` expands byte-identically to
   21-02's.
3. **`enqueueUpsertSQL` lives in `producer.go` but the producer does not
   run it.** It is the canonical L7 statement the set form generalises, and
   W2 points the two failing `ON CONFLICT` spellings at it. Referenced by
   the tests, so it is not dead.
4. **The installation id is canonicalised in Connect,** which the plan did
   not ask for. The relink decision became a Go string comparison against
   `installation_id::text`; the validator already guarantees the canonical
   form (pinned by a new test asserting the four non-canonical spellings
   are 400s), and this is the second layer, because a text comparison's
   correctness should not rest on a struct tag ten lines away.
5. **`withQueue` was removed rather than narrowed** — see above; neither
   option the plan's brief offered actually works.
6. **ISS-032's fix is bigger than the filed recommendation,** because the
   filed recommendation was measured failing.
7. **`-race` ran in a container, not natively.** This machine has no C
   toolchain — the same condition `backend-ci.yml`'s comment records. Run
   as `golang:1.25` on Linux with the worktree and the Docker socket
   mounted and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`.
8. **No new `EnqueueRequest` field for `payload`.** The plan's signature
   has none, and migration 000014's comment is explicit that a payload must
   never carry credentials or an installation id. 21-04 can add one when it
   has something to put in it.

## Verification

| Check | Command | Result |
|---|---|---|
| Backend, whole module | `DATABASE_TEST_URL=<scratch CI-shaped PG16> REDIS_URL=redis://localhost:63791/14 go test ./... -count=1 -p 1 -v` | **181 top-level pass (478 with subtests), 3 skip, 1 fail.** The failure is `TestSignatureComparisonIsConstantTime`, the known Windows-CRLF-checkout failure; `git diff RAG-Doc/main` shows this PR touches neither that test nor the file it reads. The skips are `pkg/auth`'s pre-existing "superseded by pkg/testing/isolation (Phase 17-01)". |
| New producer package | `go test ./pkg/jobs/... -count=1` | ok — 21-02's 20 top-level tests plus this plan's 11 |
| Race detector, `pkg/jobs` | `go test -race ./pkg/jobs/... -count=1` in `golang:1.25` | ok, no races |
| CI's `-race` step verbatim | `go test -race ./pkg/api/... ./pkg/db/... ./pkg/jobs/... ./pkg/testing/... -count=1 -p 1`, same container | `pkg/db`, `pkg/jobs`, `pkg/testing/...` ok, no races. `pkg/api/handlers` fails only on the CRLF test above — the container mounts this Windows worktree; CI checks out LF. |
| Barrier tests, repeated | `-race -count=3` on the producer barrier and on both handler concurrency tests | 15 rounds each of the two 5-round tests; all pass, no races |
| Package parallelism, with `pkg/jobs` added | `go test ./pkg/api/... ./pkg/auth/... ./pkg/db/... ./pkg/jobs/... ./pkg/testing/... -count=5` at default parallelism | 25 package runs, only the CRLF failure. This is the command that reproduced ISS-032 before the fix. |
| Workers, CI environment | `REDIS_URL=redis://localhost:63791/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, no `DATABASE_URL`, no `.env`, fresh venv from `requirements.txt` | **181 passed**, 0 skipped, 0 failed (19 pre-existing `utcnow` deprecation warnings). No Python file changed in this PR. |
| The plan's grep gate | `grep -nE "sync_state *= *'pending'" services/backend/pkg/api/handlers/repositories.go` | nothing |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | `{"missing": [], "skipped": [], "covered": []}` — no route line changed, as the plan predicted |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

**No container on port 5434 (docker-compose Postgres) or Qdrant was started
or touched.** Scratch containers: `rag2103-ci` (55503) and `rag2103-redis`
(63791), removed afterwards, plus the shared `rag-doc-isolation-tests`
harness container, which was left on the real schema — no migration
changed, so it never needed rebuilding.

**One environment note, not a defect.** Docker Desktop on this machine
twice refused to create a testcontainers provider mid-run
(`rootless Docker is not supported on Windows, failed to create Docker
provider`), failing a whole package at `SetupTestDB` in ~0.13s with every
test at 0.00s. It is not related to anything here; it is recorded because
the signature looks alarming and would otherwise be chased.

## What 21-04 inherits

- **`Enqueue` takes a slice already,** so `installation_repositories.added`
  for N repositories is one call and one statement. W3's scenario runs
  through the production set form, not only through 21-02's generated
  `VALUES` list.
- **`SupersedeLive` takes a slice too,** and reports which repositories
  actually had a live job — the honest answer to "what did this
  interrupt?", which is what a webhook handler wants to log.
- **The ordering rule and its silence,** in both doc comments and pinned by
  `TestSupersedeLive_TheWrongOrderLosesTheJobSilently`.
- **`SupersedeLive`'s tenant obligation.** A webhook resolves repositories
  by GitHub id; whatever it passes must have come from a scoped read of
  `repositories` in the same transaction.
- **`asWorker` and `claimTestEpoch`,** so a new test in `pkg/jobs` does not
  reintroduce a blanket delete.
- **`isolation.RetryOnLockContention`,** for any future test that takes
  strong locks on shared tables.

---
*Phase: 21-ingestion-job-infrastructure — 3 of 7 plans*
*Completed: 2026-09-16*
