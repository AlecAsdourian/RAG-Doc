---
phase: 21-ingestion-job-infrastructure
plan: 04
subsystem: backend
tags: [postgres, queue, producers, webhooks, migrations, tenancy, concurrency]

requires:
  - phase: 21-03
    provides: pkg/jobs.Enqueue and pkg/jobs.SupersedeLive(ctx, tx, organizationID, repositoryIDs), the supersede-before-enqueue rule, and the note that WasExisting means "joined the live job", not "a rerun is pending"
  - phase: 21-02
    provides: ingestion_jobs, idx_ingestion_jobs_one_live_per_repo and the ON CONFLICT arbiter the backfill re-uses
  - phase: 21-01
    provides: repositories.organization_id, and the one-organization-at-a-time backfill pattern
  - phase: 20-05
    provides: the webhook receiver, its delivery claim, resolveInstallation / inTenant, and the UNVERIFIED_* fixtures (ISS-019)
provides:
  - "push, installation_repositories added/removed and installation deleted all produce work through pkg/jobs; no webhook writes sync_state to ask for work"
  - "migration 000015_backfill_ingestion_jobs — idempotent, tenant-asserting, skips unsyncable repositories"
  - "handlers.countEnqueued — the queued/joined split, named so no caller reaches for the word rerun"
  - "TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository — the L8 barrier test, 5 rounds"
  - "TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs — the tenant boundary on a table with no RLS"
  - "pkg/jobs/backfill_migration_test.go, plus isolation-container scratch databases as a way to test a migration at a version the harness has already passed"
affects: [21-05 (consumes the jobs these handlers create, and owns every sync_state after `pending`), 21-07 (ISS-016 close-out)]

tech-stack:
  added: []
  patterns:
    - "To test a migration the shared harness has already applied, create a scratch DATABASE inside the same container, migrate it to N-1, seed it, and apply N — golang-migrate never re-applies a recorded version"
    - "A no-op `down` makes a backfill re-runnable: `Steps(-1)` then `Steps(1)` is the only way golang-migrate will run an up migration twice, and it is also the shape a production re-run takes"
    - "A migration whose backfill has no validation scan asserts its own tenant scope, because losing that scope is LOUD as a superuser and SILENT as the RLS-subject role we deploy as"
    - "Drive a stand-down off the ids a supersede RETURNED, not off a list of projected states: the projection can disagree with the queue, and the ids cannot"
    - "Name a producer result `joined`, never `rerun`: the flag the upsert sets is cleared again for an unstarted job, so the two words describe different things"

key-files:
  created:
    - services/backend/migrations/000015_backfill_ingestion_jobs.up.sql
    - services/backend/migrations/000015_backfill_ingestion_jobs.down.sql
    - services/backend/pkg/jobs/backfill_migration_test.go
  modified:
    - services/backend/pkg/api/handlers/github_webhook_events.go
    - services/backend/pkg/api/handlers/github_webhook.go
    - services/backend/pkg/api/handlers/github_webhook_isolation_test.go
    - docs/api-github-webhooks.md
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "A `push` that joins a live job answers `joined the live job`, NOT the plan's `rerun flagged`. `WasExisting` does not mean a rerun is pending — for an unclaimed job the producer clears the flag again in the same transaction — so `rerun flagged` would be wrong for the commonest shape of this case. Reading the row back to tell the two apart was rejected: it buys a distinction that changes nothing an operator does, since either way the push is covered by the job that is already live."
  - "`installation_repositories.added` enqueues EVERY matched repository and supersedes only the CHANGED ones. Re-offering a link we already hold does not invalidate the credentials the running ingest is using; withholding the enqueue would make an `added` for an unchanged installation do nothing at all."
  - "`markUninstalled`'s stand-down is driven by the ids `SupersedeLive` returned, kept in disjunction with the old `('pending','syncing')` filter. The states catch rows stranded before this phase; the ids catch the retrying case, which projects as `failed` and which the state filter walks past."
  - "Migration 000015 asserts `app.current_tenant` per organization. Added after measuring that a lost `set_config` raises 42501 as a superuser and does NOTHING AT ALL as the RLS-subject owner we deploy as — the backfill has no `SET NOT NULL` to prove itself with, unlike 000013."
  - "The backfill deliberately leaves an unsyncable repository (`installation_id IS NULL`, or an uninstalled installation) `pending` with no job. A job for one would clone nothing and dead-letter; standing it down to `never_synced` is a handler's work, not a migration's."
  - "The backfill's `down` is a no-op. Deleting jobs on rollback would discard work, and a backfilled job is deliberately indistinguishable from an enqueued one — which is what makes the backfill correct in the first place."
  - "The backfill test builds a scratch DATABASE in the harness container rather than a second container. What it needs is a different migration version, which costs a CREATE DATABASE."

issues-created: []
review: pending
issues-closed: []

duration: ~5h
completed: 2026-09-16
---

# Phase 21 Plan 04: the webhook producers

**Every webhook that asks for work or cancels it now does so through
`pkg/jobs`. No handler writes `sync_state` to queue anything, the bulk
`installation_repositories.added` case is raced against a relink in a
test, and migration 000015 gives a job to every repository the old path
left stranded.**

## Before and after, per event

| Event | Before | After |
|---|---|---|
| `push`, default branch | `UPDATE repositories SET sync_state='pending' … AND sync_state <> 'syncing'`; outcome `queued` / `ignored: repository not connected` | `jobs.Enqueue(incremental)`; outcome `queued`, **`joined the live job`**, or `ignored: repository not connected`. No `sync_state` write, no `syncing` guard. |
| `push`, other branch | ignored | unchanged |
| `installation_repositories` `added` | one unconditional `UPDATE … SET installation_id, sync_state='pending'` over known rows | `SELECT … FOR UPDATE OF r` → compare installations in Go → re-point every matched row → `jobs.SupersedeLive(changed)` → **one** `jobs.Enqueue(full_ingest)` over **every** matched row. Outcome carries five counts. |
| `installation_repositories` `removed` | `installation_id = NULL, sync_state='never_synced'` | the same `UPDATE … RETURNING id`, then `jobs.SupersedeLive` over those ids (L4). |
| `installation` `deleted` | `uninstalled_at`, then `sync_state='never_synced'` **only** for `('pending','syncing')` | `uninstalled_at`; read every repository under the installation; `jobs.SupersedeLive` over all of them; stand down `WHERE installation_id = $1 AND (sync_state IN ('pending','syncing') OR id = ANY(<superseded ids>))`. The `'failed'` comment is kept and now true. |
| `installation` `suspend` | sets `suspended_at` | unchanged, deliberately. The worker defers a suspended installation's job at claim time without consuming an attempt (21-05/21-06); cancelling here would burn the work for a condition that heals. |

Unknown repositories are still never created: the payload's repository
shape is reduced and connecting is a deliberate act.

## The `push` outcome string, decided rather than inherited

The plan offered `"rerun flagged"` for the `WasExisting` case. **That
would be wrong**, and 21-03 is why: a live job at `queued` / `attempts = 0`
has not read the repository yet, so `Enqueue` clears the flag it just set,
in the same transaction. For the commonest shape of this case — a push
arriving while a job waits in the queue — no rerun is pending at all.

The two real states are "the live job has not started, and will clone at
whatever HEAD is current when it does" and "the live job is running, and
`needs_rerun` makes 21-05 re-queue it once on completion". **Both mean the
same thing to whoever reads the delivery log: the push is covered by the
job that is already live.** So the outcome says exactly that —
**`joined the live job`** — and the handler does not read the row back to
split a distinction nobody acts on. The Go helper is called
`countEnqueued`, returning `queued` and `joined`; the word *rerun* appears
in this file only in the comment explaining why it is absent.

## The bulk case, and the test that is the only thing holding it

`installation_repositories.added` for three repositories, racing a relink
of one of them through `POST /api/repositories`, released together, **five
rounds**, asserting that every repository ends with **exactly one live
job**.

This is 21-CONTEXT L8's case. The design it replaced caught 23505 from a
plain `INSERT` and returned success — right for two reconnects of one
repository, wrong the moment a statement touches a set, and it left **two
of three never queued while reporting a win**.

**Mutation 18 is the evidence that this test earns its place.** Truncating
the handler's enqueue to `reqs[:1]` — queue the first repository, report
success for all three, which is the original bug exactly — **fails one
test in the entire suite, and it is this one.** Every other `added` test
carries a single repository and cannot see the difference.

| Test | Shape | Rounds |
|---|---|---|
| `TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository` | bulk `added` (3 repositories, one with a `running` job) vs. a relink of one of them through the real router, released together, on a shared pool with `MaxConns = 12` | **5 per run**; run 3× under `-race` → **15 rounds**, no failures, no data races |
| `TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs` | two organizations holding the same `github_repo_id`, both with a live job; only orgA's installations deliver | run 3× under `-race` |

## Redelivery: the comment was wrong, and it mattered

`github_webhook.go` said a failure "is not automatically retried, by
design". **`claimDelivery` re-claims a `'failed'` delivery immediately**,
so GitHub redelivering runs the whole handler a second time. The code and
the comment had disagreed since the failed disjunct was added; the docs and
`AFailedDeliveryIsReclaimableImmediately` both describe the code.

Corrected here because this plan is what made it matter: every handler now
writes to a queue, so re-entrancy is a property the producers have to hold.
`RedeliveryOfAFailedDeliveryCreatesNoSecondLiveJob` drives a `push`, marks
the delivery `'failed'`, and redelivers with the same
`X-GitHub-Delivery`: the second run joins the live job, and the repository
ends with **one** job, not two.

## Migration 000015, and what measuring it changed

**Up:** one `DO` block, one organization at a time, inserting a
`full_ingest` job for every repository at `sync_state IN
('pending','syncing')` whose installation exists and is not uninstalled,
with `ON CONFLICT (repository_id) WHERE state IN ('queued','running') DO
NOTHING`; then normalising `syncing` → `pending` over the same eligibility
predicate.

**`DO NOTHING`, not the producer's `DO UPDATE SET needs_rerun`.** A
backfill is not new work — it is the same work, already queued — so
flagging would buy a second full ingest of a repository being ingested
right now.

**Down:** `SELECT 1;`. Deleting jobs on rollback would discard work, and a
backfilled job is deliberately identical to an enqueued one.

**ISS-031, satisfied by structure.** The file ENDS with the `DO` block;
the only thing after it is a comment. Nothing can be poisoned by the
tenant the loop leaves behind, and the block sets its own tenant rather
than inheriting whatever 000013 left on the session.

### The finding that added a guard the plan did not ask for

The plan's sketch has no proof that the backfill ran. 000013 could rely on
`ALTER COLUMN … SET NOT NULL`, whose validation scan ignores row-level
security. This migration has no such statement, so **what happens when the
`set_config` is lost was measured in both shapes:**

| Shape | A backfill that lost its tenant scope |
|---|---|
| **Superuser** (the test harness) | **42501** — `tenant isolation violated: app.current_tenant must be set for UPDATE on repositories`, from `trg_assert_tenant`, because RLS is bypassed and a row reaches the `syncing` UPDATE |
| **RLS-subject owner** (what we deploy) | **Nothing.** `psql` exit 0, `DO`, **zero rows backfilled**, migration recorded as applied. Every read is filtered to zero rows before a row trigger can fire. |

The second is the shape that matters, and nothing else in the file would
catch it — the same asymmetry 21-01 measured for 000013, now with no
`SET NOT NULL` to fall back on. So the loop asserts its own scope:

```sql
IF current_setting('app.current_tenant', true) IS DISTINCT FROM org.id::text THEN
  RAISE EXCEPTION 'backfill is not scoped to organization %: app.current_tenant is %' …
```

With it, the same mutation fails **loudly in both shapes**.

### Backfill counts on the seeded test

`pkg/jobs/backfill_migration_test.go` creates a scratch **database** inside
the harness container (not a second container — what it needs is a
different migration version, which costs a `CREATE DATABASE`), migrates it
to **000014**, seeds it across two organizations, then applies **000015**.

| Fixture | Before | Live jobs after | `sync_state` after |
|---|---|---|---|
| `a-pending` | `pending`, live installation | **1 (new)** | `pending` |
| `a-no-installation` | `pending`, `installation_id IS NULL` | **0** | `pending` |
| `a-uninstalled` | `pending`, installation uninstalled | **0** | `pending` |
| `a-already-queued` | `pending`, already has a `queued` job | **1 — the same job id**, `needs_rerun = false` | `pending` |
| `a-syncing` | `syncing`, live installation | **1 (new)** | **`pending`** |
| `a-synced` | `synced` | **0** | `synced` |
| `b-pending` | `pending`, live installation | **1 (new)** | `pending` |
| `b-never-synced` | `never_synced` | **0** | `never_synced` |

**8 repositories seeded, 4 eligible, 4 live jobs after one application and
4 after two.** Idempotency is exercised by `Steps(-1)` then `Steps(1)` —
the only way golang-migrate will run an up migration twice, and the shape a
production re-run takes. The map of repository → live job id is compared
for equality across the two applications, so a replaced job would fail even
if the count matched.

Three properties are also asserted over the whole table each time: no job
carries an organization that disagrees with its repository's; every
backfilled job is `full_ingest`, `attempts = 0`, `needs_rerun = false`; and
**no syncable repository is left `pending` with no live job.**

### Deployment shape, measured separately

A scratch `postgres:16-alpine` (port 55504, removed afterwards) with a
`rag_doc_owner NOSUPERUSER NOBYPASSRLS` owner, migrations 000001-000014
applied as that role (`repositories` and `github_installations`
`relforcerowsecurity = t`, `ingestion_jobs` `relrowsecurity = f`), seeded
with the same eight fixtures:

- **000015 applied cleanly.** Exactly the four expected jobs, carrying the
  right organization; `a-already-queued` kept its seeded job id;
  `a-syncing` became `pending`.
- **Applied a second time:** still 4 jobs, 0 with `needs_rerun`.
- **The `set_config` mutation:** silent no-op before the guard, `ERROR:
  backfill is not scoped to organization …` (psql exit 3) after it.

One thing this shape demonstrates in passing, which is 21-01's lesson
again: reading `repositories` from an unscoped session returned **0 rows**,
so a `SELECT`-based check of a backfill proves nothing.

## What ISS-019 did NOT gain

**No payload field was widened.** `githubWebhookEnvelope` is byte-identical
to 20-05's — `git diff` on `github_webhook.go` touches only two comments —
and the handlers read exactly the fields they read before: `Ref`,
`Repository.ID`, `Repository.DefaultBranch`, `Installation.ID`,
`RepositoriesAdded[].ID` and `RepositoriesRemoved[].ID`. Every new `push`
and `installation_repositories` test keeps the `UNVERIFIED_*` prefix, and
`docs/api-github-webhooks.md` now carries the warning in the section that
describes the handlers, not only in the one about redelivery.

## Mutation results

Every mutation was applied to the committed file, run, and restored from a
backup; the tree was verified clean afterwards, and every database error
was read rather than assumed.

### `pkg/api/handlers`

| # | Mutation | Result |
|---|---|---|
| 1 | `push` enqueues `full_ingest` instead of `incremental` | **Killed: 1** — `PushOnTheDefaultBranchQueuesTheRepository` |
| 2 | Restore `AND sync_state <> 'syncing'` on the push lookup | **Killed: 1** — `PushAgainstARunningJobJoinsItRatherThanQueueingASecond` |
| 3 | `added` supersedes every matched row, not only the changed ones | **Killed: 1** — `AddedForAnUnchangedInstallationJoinsTheLiveJob` |
| 4 | `added` enqueues only the changed rows | **Killed: 2** — the same test plus `AddedOnlyTouchesTheOwningOrganization`, whose repository's installation does not change |
| 5 | Swap the supersede and the enqueue in `added` | **Killed: 2 top-level + 2 subtests, and nothing raised an error.** Every failure is an assertion about a missing live job; the repository ends with one `superseded` row carrying `needs_rerun = true` and no live job. The silent failure mode `pkg/jobs/doc.go` describes, caught. |
| 6 | Drop `FOR UPDATE OF r` from the `added` lookup | **Survived** 15 rounds — see below |
| 7 | `markUninstalled` drops the `id = ANY(<superseded ids>)` disjunct | **Killed: 1** — `InstallationDeleted_KeepsTheRowAndTheRepositories`, on the retrying repository |
| 8 | `markUninstalled` never supersedes | **Killed: 1** — same test, on the running and retrying repositories |
| 9 | `removed` never supersedes | **Killed: 1** — `RepositoriesRemovedStandsDownWithoutDeleting` |
| 10 | `push` always reports `"queued"` | **Killed: 1** — `PushAgainstARunningJob…` |
| 11 | Neuter `SupersedeLive`'s `AND organization_id = $2` | **Killed 1 in `pkg/jobs`** (`SupersedeLive_CancelsNothingForAnotherTenant`); **survived the entire webhook suite** — see below |
| 18 | `added` enqueues only the FIRST matched repository (the L8 bug, exactly) | **Killed: 1, and only 1 — the barrier test.** `round 1: repository 0 (github id 795001) must end with exactly one live job` |

### Migration 000015

Each run builds a fresh scratch database, so a mutated migration is really
applied — golang-migrate's "never re-apply a recorded version" does not
mask anything here.

| # | Mutation | Result |
|---|---|---|
| 12 | Drop `set_config` from the loop (harness shape) | **Killed** — `42501 tenant isolation violated: app.current_tenant must be set for UPDATE on repositories` |
| 12b | The same, **deployment shape, before the guard** | **SURVIVED, silently** — exit 0, `DO`, zero rows backfilled. This is what added the tenant assertion. |
| 12c | The same, **with the guard**, both shapes | **Killed in both** — `backfill is not scoped to organization <id>: app.current_tenant is <unset>` |
| 13 | Drop `gi.uninstalled_at IS NULL` | **Killed** — 5 live jobs where 4 were expected; `a-uninstalled` got one |
| 14 | `DO NOTHING` → `DO UPDATE SET needs_rerun = TRUE` | **Killed** — "a backfilled job is an ordinary unstarted full ingest" |
| 15 | Remove the `ON CONFLICT` clause | **Killed** — `23505 duplicate key value violates unique constraint "idx_ingestion_jobs_one_live_per_repo"` |
| 16 | Drop the `syncing` → `pending` UPDATE | **Killed** — `a-syncing: sync_state`, expected `pending` |
| 17 | Drop `AND r.installation_id IS NOT NULL` | **Survived** — redundant with the inner join; now documented as such |

### Two deliberate survivors, both recorded rather than papered over

**Mutation 6 — `FOR UPDATE OF r`.** Dropping it survives 15 rounds of the
barrier test. That is the honest result and the lock is kept anyway: what
actually guarantees "one live job" is the partial unique index plus the row
locks the queue's own upsert takes, and the repository lock is defence in
depth over the read-then-write of `installation_id`. It is `FOR UPDATE OF
r` rather than a bare `FOR UPDATE` for 21-03's reason — a bare one also
locks the joined `projects` row, which every repository in the organization
hangs off.

**Mutation 11 — `SupersedeLive`'s tenant filter.** It survives the whole
webhook suite, and that is not a gap in the tests: the handlers resolve
repositories inside an RLS-scoped transaction, so the statement is never
handed a foreign id, exactly as PR #39's reviewer traced for Connect.
`TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs` therefore proves the
**resolution** is careful — which is what this plan controls — while
`pkg/jobs`' own test proves the **predicate** is there for the day it is
not. Saying which test holds which half is the point of recording this.

## Verification

| Check | Command | Result |
|---|---|---|
| Backend, whole module | `DATABASE_TEST_URL=<scratch CI-shaped PG16> REDIS_URL=redis://localhost:63792/14 go test ./... -count=1 -p 1 -v` | **189 top-level pass (492 with subtests), 3 skip, 1 fail** — the known `TestSignatureComparisonIsConstantTime`, see below |
| Race detector, CI's step | `go test -race ./pkg/jobs/... ./pkg/api/... -count=1 -p 1` in `golang:1.25` with the Docker socket mounted and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal` | `pkg/jobs` ok; `pkg/api/handlers` fails only on the CRLF test. **No data races.** |
| Barrier tests, repeated | `-race -count=3` on both new top-level tests, same container | **15 rounds** of the bulk race and 3 of the tenant test; all pass, no races |
| Migration, harness | `go test ./pkg/jobs/ -run Backfill` | ok — 8 seeded, 4 eligible, 4 after one application and 4 after two |
| Migration, deployment shape | scratch PG16, `rag_doc_owner NOSUPERUSER NOBYPASSRLS` owner, `psql` | same four jobs, idempotent, guard fires on the mutation |
| Migration, harness container | `docker rm -f rag-doc-isolation-tests`, then the suite | rebuilt from the committed migrations; `schema_migrations = 15, dirty = f` |
| The plan's grep gate | `grep -rnE "sync_state *= *'pending'" services/backend/pkg --include=*.go \| grep -v _test.go` | one hit: `pkg/jobs/producer.go:188`, the projection. The other three are comments. |
| Nothing else writes the queue | `grep -rn "ingestion_jobs" services/backend/pkg --include=*.go` outside `pkg/jobs` and tests | comments only |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | `{"missing": [], "skipped": [], "covered": []}` — no route line changed |
| Build and vet | `go build ./... && go vet ./...` | clean |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |
| Python | — | **No file under `services/workers` changed** (`git diff --stat RAG-Doc/main -- services/workers` is empty), so the suite was not run. CI's Workers job covers it. |

### Two failures that are this Windows checkout, not this change

Both were proved rather than assumed:

- **`TestSignatureComparisonIsConstantTime`** reads `github_webhook.go` and
  fails with "could not find the end of verifySignature". Checking out
  **`RAG-Doc/main`'s own copy** of that file through git (CRLF) fails
  identically; writing the same bytes with LF (`git show … >`) passes. The
  function it inspects is untouched by this PR.
- **`go mod tidy -diff`** reports every line of `go.sum` as changed.
  `go.mod` and `go.sum` are `CRLF` in the working tree and **LF in the
  committed blob** (`git show HEAD:… | file -`), and neither file is
  modified by this PR.

### Containers

Scratch, created and removed: `rag2104-deploy` (55504, deployment-shape
migration check), `rag2104-ci` (55505, `pkg/auth`'s `DATABASE_TEST_URL`),
`rag2104-redis` (63792). The shared `rag-doc-isolation-tests` harness was
removed once so it would rebuild from the committed migrations, and is left
holding the real schema at version 15. **The docker-compose Postgres (port
5434) and Qdrant were never started or touched.**

## Deviations from the plan

1. **The `push` outcome is `joined the live job`, not `rerun flagged`** —
   the plan flagged this decision as one to make deliberately. Reasoning
   above.
2. **Migration 000015 asserts its own tenant scope per organization,**
   which the plan's sketch does not. Added after measuring that the
   alternative is a silent no-op in the deployment shape.
3. **The backfill's `syncing` → `pending` UPDATE carries the full
   eligibility predicate,** not just `sync_state = 'syncing' AND
   installation_id IS NOT NULL`. Moving a row to `pending` without giving
   it a job is the exact stranding this migration exists to end.
4. **The migration skips unsyncable repositories and leaves them
   `pending`.** The plan's success criterion says "no repository is
   stranded `pending` without a job"; the same plan says repositories with
   `installation_id IS NULL` get no job. The criterion is read as "no
   SYNCABLE repository", stated in the migration and asserted in the test,
   and standing the rest down to `never_synced` was left to the handlers
   rather than added as a third piece of DML.
5. **`recordAddedRepositories` updates `installation_id` for every matched
   row but supersedes only the changed ones,** which the plan's step 3 says
   and its step 4 implies; spelled out here because "matched" and "changed"
   diverge for the first time in this handler.
6. **A new test helper file was not created.** The new tests live in
   `github_webhook_isolation_test.go` beside the fixtures they reuse, and
   the queue helpers (`liveJobsFor`, `seedRunningJob`, `clearJobsFor`,
   `countJobs`) come from 21-03's `repositories_connect_test.go` in the same
   test package.
7. **`-race` ran in a container, not natively** — no C toolchain on this
   machine, the same condition 21-03 recorded.
8. **The barrier test races the webhook against `POST /api/repositories`,**
   for which it needed a router carrying both producers
   (`webhookAndConnectServer`); `webhookServer`'s stub lister reports no
   repositories, which is right for every other test in the file.

## What 21-05 inherits

- **Jobs of both types now exist.** `incremental` comes only from `push`;
  everything else is `full_ingest`. Nothing yet distinguishes them
  downstream — Phase 22 does.
- **`sync_state` after `pending` is entirely 21-05's.** The producer writes
  `pending` for a repository that got a *new* job and nothing else; the
  handlers write `never_synced` on stand-down. Every other transition is
  the worker's.
- **`needs_rerun = true` on a `running` job is now reachable from a
  webhook**, which is the case L7 is about and which 21-05's completion
  path has to drain — completion first, then the re-enqueue, in that order.
- **A suspended installation must be deferred at claim time without
  consuming an attempt.** Nothing in this plan cancels a job on `suspend`,
  and `docs/api-github-webhooks.md` now promises that behaviour.
