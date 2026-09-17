---
phase: 21-ingestion-job-infrastructure
plan: 07
subsystem: backend
tags: [api, tenancy, queue, isolation, mutation-testing, docs, phase-close-out]

requires:
  - phase: 21-02
    provides: migration 000014 — `ingestion_jobs`, the five states, the lease columns, the partial unique index, the composite FK and the tenant trigger
  - phase: 21-03
    provides: "`pkg/jobs.Enqueue` and `pkg/jobs.SupersedeLive`, and the connect path's racing tests"
  - phase: 21-04
    provides: every webhook producer on the queue, and the bulk-add race test
  - phase: 21-05
    provides: "the state transitions, the `sync_state` projection table, the backoff, and the redaction that makes `last_error` safe to hand back over HTTP"
  - phase: 21-06
    provides: "the worker runtime, the claim-time installation check, and the ruling that `sync_state = 'syncing'` is not evidence of a live worker"
  - phase: 20-01
    provides: "`db.TenantScoper` and the rule that a tenant handler holds one instead of a pool"
  - phase: 17-05
    provides: the CI isolation ratchet — which does not cover this route, and that is the point
provides:
  - "GET /api/admin/jobs/{id} — one job, readable by ANY member of its organization, with `stalled` computed from the job row"
  - "handlers.JobsHandler / handlers.IngestionJob / handlers.NewJobsHandler"
  - "pkg/api/handlers/jobs_isolation_test.go — 11 cases, written deliberately because the CI gate asks for none"
  - "pkg/jobs.TestClaimAndSweepNeverReachARequestHandler — 21-05's deferred nit, built as a gate"
  - "docs/api-ingestion-jobs.md — the queue as built, and where Phases 22 and 23 should start"
affects: [22 (registers the handlers and turns the queue on), 23 (reads this endpoint for progress, and needs a way to find a job id)]

tech-stack:
  added: []
  patterns:
    - "When neither the database nor CI will catch a mistake, write the test on purpose and mutate it — a guard nobody has seen fail proves nothing"
    - "Neuter a predicate, do not delete it: deleting `AND organization_id = $2` also breaks the statement's arity, so every test fails on a 500 and the run proves nothing about isolation"
    - "Assert the response's exact KEY SET, not the absence of two names: `NotContains(body, \"lease_owner\")` passes against the same value under another name"
    - "Compare BODIES, not statuses, when a refusal must not be an oracle — three ways to miss must be byte-identical"
    - "Read the tenant BEFORE parsing the id, so the guard is reachable with no chi route context and the order is itself testable"
    - "Compute a liveness predicate in SQL against the DATABASE clock, with the same text the claim uses, rather than re-deriving it in Go"
    - "A source-text gate must read CODE, not prose: the first cut failed on the handler's own comment explaining the rule it enforces"
    - "A text gate asserts its own premises — that the directory exists, that the walk walked, and that the scanner can see literals and identifiers but not comments"

key-files:
  created:
    - services/backend/pkg/api/handlers/jobs.go
    - services/backend/pkg/api/handlers/jobs_isolation_test.go
    - services/backend/pkg/jobs/handler_guard_test.go
    - docs/api-ingestion-jobs.md
  modified:
    - services/backend/pkg/api/router.go
    - docs/api-repositories.md
    - docs/api-github-webhooks.md
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "NO ROLE GATE, and the handler never reads `auth.OrgRoleKey`. The user decided on 2026-09-14: any member of the job's organization may read it, because it shows their own repository's indexing status and Phase 23's progress UI reads it directly. The isolation test proves it with a MEMBER token rather than an owner's, because every role-gated route in this codebase admits an owner and an owner token would prove nothing."
  - "`lease_expires_at` and a computed `stalled` are RETURNED; `lease_owner` is not. The plan's SELECT list omits the expiry, but the bullet 21-06's review added to this plan names `lease_expires_at` as evidence, and an endpoint that carries the rule is stronger than one that documents it. The OWNER stays out: a worker id is infrastructure identity, and it is the value every terminal write is fenced on."
  - "`stalled` is evaluated in SQL, against the database clock, with `claimSQL`'s exact predicate INCLUDING the `lease_expires_at IS NULL` half. `NULL < NOW()` is NULL rather than true, so the shorter form reports a null-lease row healthy — and that row is invisible to a naive liveness check while still holding the partial unique index against every future job for its repository."
  - "ONE 404, asserted as BYTE-IDENTICAL bodies rather than three matching status codes. Not-found, another organization's job and a malformed id are indistinguishable, or the endpoint answers `does this job id exist?` for every tenant in the system."
  - "The organization is read BEFORE the id is parsed. That ordering makes a tenant-less request 403 whatever the id looks like, and it is what lets the handler's own guard be tested by calling it directly with no chi route context — a different statement from `TenantMiddleware refuses`, and mutation M3 kills only the direct test."
  - "The read runs inside `InTenantTx` although `ingestion_jobs` has no RLS. Not because the table needs it, but because `every tenant handler opens a tenant transaction` is a rule a reader can check at a glance; an exception would have to be re-justified by everyone who meets it."
  - "21-05's deferred nit is BUILT, not declined. `claimSQL`/`sweepSQL` in a request handler now fails a test rather than contradicting a docstring. It reads code and not comments, it asserts its own premises, and mutation M10 — the claim pasted into `jobs.go` — kills it. The honest framing is in the file: a text scan over one directory, the cheap half of a rule whose expensive half is the reasoning in `doc.go`."

issues-created: []
issues-closed: [ISS-016]
review: "pending"
duration: ~4h
completed: 2026-09-16
---

# Phase 21 Plan 07: the queue has a reader, and the phase closes

**`GET /api/admin/jobs/{id}` returns one ingestion job to any member of its
organization.** No role gate — the user decided that on 2026-09-14, because
it shows the caller's own repository's indexing status and Phase 23's
progress UI reads it directly.

**It is the one endpoint in this codebase where two safety nets that
normally overlap are both absent**, and the whole shape of this plan follows
from that:

- **The database will not catch a mistake.** `ingestion_jobs` has no
  row-level security, by decision (21-CONTEXT L5) — a worker claims a job
  *before* it knows the tenant, so scoping the claim by the answer would be
  circular. `WHERE id = $1 AND organization_id = $2` is the entire tenant
  boundary. Everywhere else in `pkg/api/handlers`, a cross-tenant read
  returns zero rows because a policy refused it and the handler's filtering
  is the second layer. Here it is the first and the last.
- **CI will not ask for a test.** `check-isolation-tests.py` matches
  POST/PUT/PATCH/DELETE only, and only route lines added in the diff. Run
  against this branch it reports
  `{"missing": [], "skipped": [], "covered": []}` — the measured form of "it
  asked for nothing". A `GET` over this table ships green with no isolation
  test at all.

So the isolation test is written on purpose rather than by ratchet, and it is
**mutation-checked**.

## The contract

```
GET /api/admin/jobs/{id}
Authorization: Bearer <supabase access token with an organization_id claim>
```

```jsonc
{
  "id": "…", "repository_id": "…",
  "job_type": "full_ingest",          // or "incremental"
  "state": "running",                 // queued|running|completed|dead|superseded
  "attempts": 1, "max_attempts": 5,
  "run_after": "…",                   // the backoff target
  "lease_expires_at": "…",            // null unless leased
  "stalled": false,                   // computed from the JOB ROW — see below
  "last_stage": "parse",              // clone|parse|embed|store, or null
  "progress": { "files_parsed": 12 }, // redacted by the worker, or null
  "needs_rerun": false,
  "last_error": "…",                  // redacted by the worker, or null
  "ingestion_run_id": null,
  "created_at": "…", "updated_at": "…"
}
```

| Status | When |
|---|---|
| 200 | the job exists and belongs to the caller's organization |
| 401 | no bearer token, or not a valid one |
| 403 | valid token, no `organization_id` claim |
| **404** | no such job, **or** another organization's job, **or** a non-canonical UUID — **byte-identical in all three cases** |
| 500 | a server-side fault |

**`lease_owner` and `payload` are never returned.** The owner is
infrastructure identity and the value every terminal write is fenced on; the
payload is *input to the worker*, not status for a reader, and putting it in
the contract would suggest it is a safe place to store something.
`last_error`, `last_stage` and `progress` **are** returned, and they are safe
because 21-05 and 21-06 redacted them before they reached the column — this
endpoint is the reason that redaction exists.

**Route placement, which the plan called out and which mutation M6
measures.** The tenant group already mounts `/api`
(`r.With(middleware.Timeout(60*time.Second)).Route("/api", …)`), so the
registration is `r.Get("/admin/jobs/{id}", …)` **inside** that block. A
leading `/api` there serves `/api/api/…`, reachable by nothing and 404ing in
a way that looks exactly like a tenant refusal; registering outside the block
loses the 60-second timeout instead.

## `syncing` is not evidence of a live worker, and the response says so

This is the bullet PR #42's second review added to this plan, discharged in
the response shape rather than only in prose.

`mark_started` projects `syncing`, and **`defer` writes no projection at
all** — deliberately, because the suspended-installation path must not show
`syncing` for an hour of waiting. So three different things leave `syncing`
behind with nobody working, and always have: a worker that crashed, a worker
whose connection died mid-job, and a job deferred part-way through a
shutdown.

The endpoint therefore answers "is this actually running?" from the **job
row** and never from the projection:

```sql
(state = 'running'
 AND (lease_expires_at IS NULL OR lease_expires_at < NOW())) AS stalled
```

Three things about that expression are load-bearing.

- **It is the same predicate `claimSQL` and `_SWEEP_SQL` use.** If it
  disagreed with them, a job could read "healthy" while the sweeper was about
  to dead-letter it.
- **The `lease_expires_at IS NULL` half is not decoration.** `NULL < NOW()`
  is NULL rather than true, so `lease_expires_at < NOW()` alone calls a
  null-lease row healthy — and that row is exactly the strand 21-RESEARCH's
  second correction added the clause to catch: invisible to a naive liveness
  check, and still holding the partial unique index against every future job
  for its repository. **Mutation M4 is that shortening, and one test kills
  it.**
- **It is evaluated in SQL, against the database clock** — the clock the
  claim and the sweeper compare against. A Go-side comparison would answer a
  slightly different question on any machine whose clock differs from the
  server's, which is every machine.

Three of the eleven test cases exist only for this rule, and each sets
`sync_state = 'syncing'` and **asserts that it really is `syncing`** before
asking the endpoint anything — without that premise the test would pass for
having never set the projection.

## The tests

`services/backend/pkg/api/handlers/jobs_isolation_test.go`, **11 cases** in
seven scenarios, against the shared `postgres:16-alpine` harness through the
real router and the real middleware.

| Scenario | What it pins |
|---|---|
| 1 | a **member** (not an owner) reads their own organization's job: every field, and the response's **exact key set** |
| 2 | orgA reading orgB's job is 404 and **byte-identical** to the random-UUID and malformed-id 404s — and orgB can still read it, so a handler that 404'd everything would not pass |
| 3 | five malformed spellings `uuid.Parse` accepts (`urn:uuid:`, `{…}`, unhyphenated, uppercase, garbage) all give the same 404; a bare `/api/admin/jobs/` is chi's own 404 |
| 4 | a claim-less token is 403 through the middleware |
| 5 | the **handler itself** refuses an organization-less context, with no chi route context installed — which also pins the order of its two checks |
| 6 | the route is `/api/admin/jobs/{id}` exactly once: `/api/api/…` is 404, and unauthenticated is 401 |
| 7a | a crashed worker (expired lease, `sync_state = 'syncing'`) reads `running` **and `stalled: true`** |
| 7b | a `running` job with a **NULL** lease reads `stalled: true` |
| 7c | a job deferred during a shutdown reads `queued`, `attempts = 0`, `stalled: false`, with `last_stage` kept |
| 7d | a retrying job is `queued` with `attempts > 0` — there is no `failed` state (decision O2) |

**The key-set assertion is sharper than it looks, and it is the 21-04/21-05/
21-06 lesson applied again — ask what the fixture cannot distinguish.**
`require.NotContains(body, "lease_owner")` passes against a handler that
returns the worker id under any other name, and against any future column
added without thought. Asserting the exact set makes "never return
`lease_owner` or `payload`" a property of the *response* rather than of two
string literals, and it is what kills M7 and M8. The seeded job carries
sentinel values (`worker-a-must-not-be-returned`,
`payload-must-not-be-returned`) so the values are checked too.

**Scenario 5 is a different statement from Scenario 4, and both are needed.**
Scenario 4 proves `TenantMiddleware` refuses; Scenario 5 proves the *handler*
refuses. The middleware could be dropped from the group, or the route moved
out of it, and Scenario 4 would still pass. Mutation M3 kills only
Scenario 5.

### `pkg/jobs/handler_guard_test.go` — 21-05's deferred question, answered "build it"

PR #41's review raised it as a nit: *"never run inside a request handler" is
a docstring, not a guard.* 21-05 declined, correctly — "there is no handler
to guard yet, and a gate with nothing to catch is a gate nobody maintains" —
and carried the decision here, because **this is the plan that puts the first
HTTP handler over this table.** So it is built.

`TestClaimAndSweepNeverReachARequestHandler` walks every `.go` file under
`pkg/api`, tokenizes it with `go/scanner`, and fails on `claimSQL`,
`sweepSQL`, `FOR UPDATE SKIP LOCKED`, `attempts >= max_attempts` or
`attempts < max_attempts` appearing in an identifier or a string literal.
What it catches is a **paste** — the only route this SQL has into `pkg/api`,
since both constants are unexported and live in an internal test file.

**⚠ The first cut scanned raw bytes and failed immediately — on `jobs.go`'s
own comment explaining why the claim must not run in a handler.** A gate that
forbids describing the rule it enforces gets deleted, so it reads code and
not prose. It also asserts three premises, because each is a way a text gate
stops being one while still passing: the directory exists, more than twenty
files were walked, and the scanner really can see string literals and
identifiers and really cannot see comments.

Its limits are in the file rather than implied: a text scan over one
directory, blind to SQL assembled at run time or spelled differently. The
cheap half of a rule whose expensive half is the reasoning in `pkg/jobs/doc.go`.

## Mutation results

**11 mutations, 11 killed.** Each applied to a **copy** of `services/backend`
rebuilt from the committed worktree before every run, with the pattern
asserted to match **exactly once** and the mutated text asserted present —
and the original absent, except where the edit is an insertion. **The
committed tree was never mutated.** Baseline on the copy: the jobs tests and
the guard test both pass.

| # | Mutation | Result |
|---|---|---|
| **M1** | `jobByIDSQL`'s tenant guard is **neutered** to `AND $2::text IS NOT NULL` | **Killed: 1** — `Scenario2`, and only that one. This is the mutation the file exists for |
| M1a | the guard is **deleted outright** | **Killed: 10** — but for the wrong reason; see below |
| M2 | a malformed id answers 400 instead of the shared 404 | **Killed: 2** — `Scenario2` (the byte-identical comparison) and `Scenario3` |
| M3 | the handler's own organization guard is removed | **Killed: 1** — `Scenario5`, and **only** that one. `Scenario4` correctly survives: the middleware still refuses, which is why the handler needs its own test |
| M4 | `stalled` drops the `lease_expires_at IS NULL` half | **Killed: 1** — `ARunningJobWithANullLeaseReadsAsStalled` |
| M5 | `stalled` is read from the `sync_state` projection instead of the job row | **Killed: 4** — including both crashed-worker cases. 21-06's ruling, as a mutation |
| M6 | the route is registered as `/api/admin/jobs/{id}` **inside** the `/api` block | **Killed: 9** — the double-prefix mistake the plan warned about |
| M7 | `lease_owner` is added back to the response | **Killed: 1** — `Scenario1`, on the key set |
| M8 | `payload` is added back to the response | **Killed: 1** — `Scenario1`, on the key set |
| M9 | a miss answers 403 rather than the shared 404 | **Killed: 2** — `Scenario2`, `Scenario3` |
| M10 | `claimSQL` is pasted into `jobs.go` | **Killed: 1** — `TestClaimAndSweepNeverReachARequestHandler`. The guard bites |

### ⚠ M1a is a finding about the HARNESS, not about the code, and it is why M1 is worded the way it is

Deleting `AND organization_id = $2` outright was the obvious mutation and it
was tried first. It **failed every scenario**, including ones that read only
the caller's own job — because the statement then takes one parameter while
the handler still passes two, and pgx refuses it. Measured:

```
Scenario1: expected 200, actual 500
body={"status":"error","error":"Internal server error"}
```

A green-looking kill that exercises **nothing** about the tenant boundary. It
would have been perfectly possible to write it up as "M1 killed, 10 tests"
and to have proven nothing at all. The faithful form is the neutered one —
`AND $2::text IS NOT NULL`, the same shape 21-05's mutations A and S2–S6 use
— which keeps the arity and is killed by `Scenario2` **alone**. Both are
recorded, because the difference between them is the whole lesson.

## ISS-016 is closed, on evidence re-run before any of this existed

The plan says the issue closes only on evidence, so the evidence was produced
first: the nine racing and guard tests were run at `RAG-Doc/main` (`de6b6e9`)
against PostgreSQL 16 **before a line of 21-07 was written**.

| Guard | Test | Result on `main` |
|---|---|---|
| two live jobs per repository are unrepresentable | `TestIngestionJobs_OneLiveJobPerRepository` | PASS |
| supersede before enqueue, including the silent-loss case | `TestIngestionJobs_SupersedeBeforeEnqueue` (3 subtests) | PASS |
| **this issue's own scenario** | `TestRepositoriesConnect_RelinkSupersedesARunningJob` | PASS |
| two relinks racing | `TestRepositoriesConnect_ConcurrentRelinksLeaveOneLiveJob` | PASS |
| two first connects racing | `TestRepositoriesConnect_ConcurrentFirstConnectsDoNotDoubleIngest` | PASS |
| sixteen concurrent enqueues, five warm rounds | `TestEnqueue_ConcurrentEnqueuesResolveToOneLiveJob` | PASS |
| the bulk case that once queued 1 of 3 while reporting success | `TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository` | PASS |
| the same, at the producer | `TestEnqueue_BulkRacingALiveJobHandlesEveryRow` | PASS |
| the same, at the statement | `TestIngestionJobs_BulkEnqueueRacingALiveJobHandlesEveryRow` | PASS |

The ordering bullet's 2026-09-14 correction is kept in the issue: through the
enqueue upsert the wrong order raises **nothing** — it flags `needs_rerun` on
the job about to leave the live set and the repository ends with no live job
— rather than the `23505` a plain `INSERT` would give. Silent loss, which is
worse.

**ISS-023 stays open**, by decision O1, and that split is precisely what let
ISS-016 close cleanly: the first draft of this phase had three files giving
three different answers about whether retry was in scope.

**ISS-012 gains this endpoint by name**, with the reason it deserves its own
line: everywhere else a stale claim still has to get past a row-level
security policy, and here it does not.

**ISS-031 and ISS-033 are untouched**, with their existing notes.

## Verification

| Check | Command | Result |
|---|---|---|
| Whole backend module | from `services/backend`: `go test ./... -count=1 -p 1 -timeout 15m` | **505 passing**, 1 failing — `TestSignatureComparisonIsConstantTime`, the known CRLF failure (below) |
| This plan's tests | `go test ./pkg/api/handlers/ -run TestJobsIsolation` | **11 cases pass** |
| Repeated — the flake check | the same, `-count=5` | passed every time |
| The guard | `go test ./pkg/jobs/ -run TestClaimAndSweepNeverReachARequestHandler` | pass |
| Race detector | `-race ./pkg/api/... ./pkg/jobs/...` inside `golang:1.25` with the Docker socket mounted and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal` (no C toolchain on this machine) | **no data races.** `pkg/jobs` ok in 14.4s; `pkg/api/handlers` fails only the CRLF test |
| Build and vet | `go build ./...`, `go vet ./...` | clean |
| Mutations | 11, on a copy, each proven present before the run | **11 killed** |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | `{"missing": [], "skipped": [], "covered": []}` — **it asked for nothing**, which is this plan's premise measured rather than assumed |
| Python | `git diff --stat RAG-Doc/main..HEAD -- services/workers` | **empty.** Nothing under `services/workers` changed, so `pytest` was not run |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

**The one failing test also fails on `main`, and it is a line-ending
artefact — measured, not assumed.** `TestSignatureComparisonIsConstantTime`
reads `github_webhook.go` as text and looks for `"\n}\n"`; with
`core.autocrlf=true` the checkout is CRLF, so the search returns −1 and the
test reports "could not find the end of verifySignature". Two independent
checks: `git diff RAG-Doc/main..HEAD` over the only two files it reads is
**empty**, and converting that file's 380 CRLFs to LF in a scratch copy makes
the test **pass** with no other change. It fails identically inside the Linux
race container, which reads the same CRLF bytes through the mount.

**Containers.** Two scratch containers on free ports, both started for this
plan: `rag2107-pg` (`postgres:16-alpine`, port **55521**, migrations applied
with `migrate` for `DATABASE_TEST_URL`, since `pkg/auth`'s helpers default to
**5434**) and `rag2107-redis` (port **63797**, for `pkg/auth`'s state-store
tests). Both removed afterwards. **The docker-compose Postgres (port 5434)
and Qdrant were never started or touched** — port 5434 was already occupied
by an unrelated container and was left alone — and the shared Go harness
container `rag-doc-isolation-tests` was left on the real schema.

## Deviations from the plan

1. **`lease_expires_at` and a computed `stalled` are in the response.** The
   plan's SELECT list has neither. The bullet 21-06's review added to this
   same plan names `lease_expires_at` as the evidence, and an endpoint that
   *applies* the rule is stronger than one that documents it — especially
   since Phase 23 is told to use the same predicate. `lease_owner` stays out.
2. **The `stalled` predicate carries `lease_expires_at IS NULL OR`, which the
   plan's bullet does not quote.** The bullet says the predicate is
   `lease_expires_at < NOW()`; `claimSQL` and `_SWEEP_SQL` both actually say
   `(lease_expires_at IS NULL OR lease_expires_at < NOW())`, and the null half
   is one of 21-RESEARCH's two corrections. The longer form is the one that
   matches the statements the bullet points at. M4 pins it.
3. **The `claim`/`sweep` guard was BUILT rather than merely decided.** The
   plan asks for a decision either way. It is built, in the shape the plan
   suggested (a gate where the rule lives), as a Go test rather than a shell
   grep so it runs inside `go test ./...` — the gate CI already has.
4. **Eleven test cases rather than the plan's six**, and four of the extras
   are Scenario 7, which exists because of the bullet in deviation 1. The
   fifth extra is the member-token choice in Scenario 1.
5. **Eleven mutations rather than the plan's one.** The extras follow 21-02
   through 21-06's practice, and one of them (M1a) produced a finding about
   the harness rather than the code.
6. **`docs/api-github-webhooks.md` was edited**, which the plan's file list
   does not include. It carried three statements that stopped being true when
   21-05 and 21-06 shipped — "nothing consumes the queue yet", "nothing reads
   `needs_rerun` yet", and two "*from 21-05*, the worker is to …" futures. The
   plan asks to *check* that page; checking it found them.

---

# The phase close-out

Phase 21 built a durable queue, put every producer on it, wrote the consumer
and gave it one reader. **Nothing claims a real job**, deliberately:
`python -m workers` finds `workers.jobs.handlers.REGISTRY` empty, says so and
exits 2 **before reading any configuration**. That is the safe ending — 21-03
and 21-04 put real work in the queue, and a worker with no handler would
claim it, fail it five times and dead-letter it, turning a queue that is
merely waiting into one that has to be repaired by hand.

## What Phase 22 inherits

### The four steps that turn it on

Collected in `docs/api-ingestion-jobs.md` as well, because that is where
someone starting Phase 22 will look.

1. **Register the handlers.** `REGISTRY["full_ingest"]` and
   `REGISTRY["incremental"]`. The two keys are fixed by migration 000014's
   `CHECK (job_type IN ('full_ingest','incremental'))`, not by convention. A
   handler takes a `JobContext` and returns a `write_results` callback — the
   callback is what makes the chunks and the completion commit together,
   which is most of the argument for putting the queue in Postgres (L1).
2. **Add `DATABASE_URL` to the compose `workers` service.** It has
   `ENV=development` and nothing else. No compose change was made in this
   phase, deliberately.
3. **Set `max_job_duration`.** It defaults to `None`, and until it is set a
   hung handler holds its lease indefinitely, nothing can reclaim the job,
   the repository sits `syncing`, and that worker's sweeper never runs again
   either. The mechanism is built and tested; the number is a multiple of an
   ingest nobody has measured. An unset bound logs a WARNING at startup so
   the hazard cannot ship silently.
4. **Measure the pool.** One job at a time per process; scale by processes.
   The claim is safe across any number of them — pinned by a barrier race
   from **both** Go and Python. *How many* is an open input: 21-RESEARCH
   withdrew the arithmetic that answered it. Each worker holds **two**
   connections while a job runs (the loop's and the heartbeat's), so a pool
   of N wants 2N.

### The contract a handler has to be written against

Three endings, and **the exception type is the contract**:

```
return               done             -> complete
raise Unfinished     stopped part-way -> defer, attempt handed back (SHUTDOWN ONLY)
raise anything else  failed           -> fail, attempt consumed, backoff applied
```

⚠ **A bare `return` after stopping early writes `completed` and
`sync_state = synced` over work that did not happen.** Not a style point: PR
#42's review built the handler and produced the row
(`state=completed last_stage=parse sync_state=synced`, `last_synced_at`
stamped, the partial unique index freed so nothing re-queues it).

⚠ **`Unfinished` outside a shutdown is a `fail`, on purpose.** Unbounded it
was measured at **193 re-claims in 6 seconds** with `attempts` pinned at 0,
past *both* of the phase's backstops. Phase 22's handler will carry
`raise Unfinished(...)` behind a condition, and misclassifying a clone
precondition as "unfinished" is an ordinary mistake to make.

### Still Phase 22's, from 21-01 through 21-06

- **`chunks.organization_id` under D5** (21-01 `affects`) — the same
  trigger-maintained denormalised tenant this phase gave `ingestion_jobs`.
- **Wire the pipeline to runs** (21-05). `PostgresWriter.create_ingestion_run`
  has **no `ON CONFLICT`**, so a repeat of the same commit raises `23505` —
  the exact error W6 exists to prevent. The shape is
  `resolve_ingestion_run(cur, …)` then `attach_ingestion_run(cur, job, …)` in
  one tenant-scoped transaction, with `PostgresWriter` taking the run id.
- **Make chunk writes idempotent per run** (**ISS-027**). The completion
  transaction removes **torn** writes, not **duplicated work**.
- **Distinguish `incremental` from `full_ingest` downstream** (21-04). Both
  types exist today; nothing acts on the difference.
- **A `statement_timeout` on the heartbeat connection** (21-06) — named as a
  candidate, not added. It is the only thing that would cover a beat that
  *blocks* rather than raises.
- **A way to find a repository's job id** (this plan). Nothing returns one;
  Phase 23 needs a `job_id` on the repository response or a
  list-by-repository endpoint, and Phase 22 is where the repository API is
  next likely to be open.

### Two questions Phase 22 does NOT need to re-open

- **L5's drift-on-re-parent.** 21-CONTEXT asks Phase 22 to revisit it. The
  answer is already in the schema: 21-01's composite key plus 21-02's
  composite foreign key mean a repository cannot change organization, so a
  job's `organization_id` cannot go stale. Revisit only if
  same-organisation project moves become a feature.
- **The backoff ceiling.** 21-CONTEXT left it open and the plan mapping moved
  it from 21-02 to 21-05, which settled it:
  `min(60s × 4^(n-1), 60 minutes) × U[0.5, 1.0)`, worst case ~81 minutes over
  five attempts, with a test pinning the total.

## Every gap this phase does NOT pin

Collected from 21-01 through 21-07, following 21-05's and 21-06's practice of
naming the gaps rather than leaving them to be discovered. **A guard nobody
has seen fail proves nothing, and neither does a phase that only lists what
it covered.**

### Deliberate mutation survivors, each with its reason

| Plan | Survivor | Why it is kept |
|---|---|---|
| 21-01 | D5's `TG_OP <> 'UPDATE'` guard | `trg_reject_cross_org_reparent` is attached `BEFORE UPDATE OF project_id`, so the branch is unreachable. The attachment is pinned by test 9; the guard alone is dead code, and the two together break every insert |
| 21-03 | `Enqueue`'s projection row-count check | unreachable today: reaching it needs a transaction scoped to cover the enqueue but not the projection, and the enqueue is scoped by the same `repositories` read, so it fails first with 42501. Cheap insurance against a future caller |
| 21-04 | `FOR UPDATE OF r` in the `added` lookup (M6) | **survives 15 rounds** of the barrier test. What guarantees "one live job" is the partial unique index plus the upsert's own row locks; the repository lock is defence in depth over the read-then-write of `installation_id` |
| 21-04 | `SupersedeLive`'s `AND organization_id = $2` (M11) | survives the whole webhook suite, because the handlers resolve repositories inside an RLS-scoped transaction and never hand it a foreign id. `pkg/jobs`' own test proves the predicate is there for the day that changes |
| 21-04 | `AND r.installation_id IS NOT NULL` (M17) | redundant with the inner join; now documented as such |
| 21-05 | truncate-before-redact (X) | re-measured over eighteen inputs, zero leaks in either order: truncation removes a **suffix** and every pattern anchors on a **prefix**. The code's claim was corrected rather than the mutation explained away |
| 21-06 | moving the heartbeat's give-up rules back into the `except` | with `connect_timeout` in place every way a beat can fail now raises, so the two placements are behaviourally identical. The relocation is defence in depth against a future non-raising path |

### Structural limits, recorded rather than closed

- **21-01 — the 000010 oracle is left alone.**
  `assert_installation_matches_repository_tenant` discloses the same way
  000013's mismatch branch did before it was fixed. No writer names the
  column, so it is latent rather than reachable.
- **21-01 — the RLS policy simplification is deliberately out of scope** for
  `repositories`, `ingestion_runs` and `chunks`. Changing an RLS policy
  deserves its own isolation review.
- **21-01 — three minor items, none blocking:**
  `CheckRepositoryTenantDrift` joins `projects` **inner**, so a repository
  whose project vanished is dropped by the check meant to survive it
  (unreachable while the foreign key stands); `SchemaShape` pins
  `pg_get_constraintdef` output **verbatim**, which PostgreSQL may reword
  across major versions; the backfill is **O(organizations × repositories)**.
- **21-01 — mutation-testing a migration needs a fresh container per run.**
  golang-migrate never re-applies a recorded version, so a reused container
  silently tests the old schema. Recorded, not automated.
- **21-02 — the silent loss is durable-looking.** When the wrong order runs,
  the `needs_rerun` flag the upsert set **survives on the terminal row**,
  where nothing will ever look at it.
- **21-02 — `ingestion_jobs` is deliberately NOT in `protectedTables`**: no
  RLS and no `trg_assert_tenant`, by design. The reason is stated at the
  ratchet itself.
- **21-02 — `SchemaShape/the_indexes` pins `pg_get_indexdef`'s normalised
  text**, with the same cross-version caveat.
- **21-03 — the relink race never reaches the upsert's conflict branch.**
  `FOR UPDATE OF r` serialises the two callers into `relink` + `unchanged`,
  so the second enqueues nothing: `flagged_existing_job=true` appears **0
  times** across the whole clean handler suite. The gap that hid — two
  concurrent *first* connects — was found in PR #39 and fixed.
- **21-03 — `40001` is deliberately not retried** by
  `RetryOnLockContention`: a serialization failure is a statement about the
  data, not about who won a lock race.
- **21-03 — the GitHub round trip stays outside the write transaction**, and
  this phase did not touch that structure.
- **21-03 — ISS-032 is closed but which half does the work is NOT
  established.** Removing only the `lock_timeout` and keeping the six retries
  also passed five rounds; the deadlock was a one-in-N event this machine
  would not reproduce on demand.
- **21-04 — the migration's tenant guard can be made vacuous.**
  `organizations` and `projects` carry no row-level security, so
  `FOR org IN SELECT id FROM organizations` cannot be filtered to zero — **if
  either ever gains RLS, the assertion becomes vacuous.**
- **21-04 — the bulk barrier test's strength is the SHAPE, not the race.**
  Mutation 18 proves it; the race makes the fixture realistic and is the
  weaker of the two things the test buys. Kept in the test's own comment.
- **21-04 — ISS-019 gained nothing.** No payload field was widened;
  `githubWebhookEnvelope` is byte-identical to 20-05's, so the `push` and
  `installation_repositories` field names are still documentation-derived and
  not evidence.
- **21-05 — `FOR UPDATE SKIP LOCKED` was not pinned from Python** at the
  time, deliberately; 21-06's barrier test closed it.
- **21-05 — `assert_no_older_claimable` carries its own copy of the claim
  predicate**, so it is invisible to a mutation of the original. A premise,
  not coverage.
- **21-06 — `assert_only_claimable` has the same property**, and says so.
- **21-06 — the heartbeat's own connection is pinned STRUCTURALLY**, by
  counting connections. The review tried to provoke the shared-connection
  failure and could not; it raises either way, through psycopg2's
  `with conn:` reentrancy guard.
- **21-06 — a row carrying BOTH `suspended_at` and `uninstalled_at`** is
  measured (uninstalled wins, correctly) but not pinned by a test, so a
  mutation swapping the branches would survive.
- **21-06 — signal delivery is `# pragma: no cover`**, the idle poll's jitter
  is not asserted, and the sweeper pauses while its worker is busy (a
  property, not a gap — `max_job_duration` bounds the worst case once it is
  set).
- **21-07 — the `claim`/`sweep` gate is a text scan over one directory.**
  Blind to SQL assembled at run time, read from a file, or spelled
  differently.
- **21-07 — nothing returns a repository's job id**, so the endpoint is
  usable by anything holding one and not by a UI starting from a repository.

### Issues open at the phase boundary

| Issue | State at the close |
|---|---|
| **ISS-016** | **CLOSED here, on evidence.** Nine racing and guard tests re-run at `main` |
| **ISS-023** | **OPEN by decision O1.** The state machine makes the retry possible; nothing exposes it. The pieces now exist — a `dead` job is outside the live set, and this endpoint can say why it died |
| **ISS-012** | **OPEN.** This endpoint is now named in its affected surfaces, and it is the one route where a stale claim has no row-level security behind it |
| **ISS-031** | **OPEN, with its current notes.** 21-01 recommended a CI check that applies migrations to a **seeded** database; it was never built. 000014 (DDL only) and 000015 (the `DO` block is the last statement) each dodged the hazard structurally, so the guard is still "the author read the comment" |
| **ISS-033** | **OPEN, with its current notes.** The claim-time abandon it was filed against now exists (21-06), so the cost is one round trip and a briefly wrong `sync_state`, not a wrong terminal state. The producer-side check stays open as defence in depth, and it narrows the window rather than closing it |
| **ISS-027** | **OPEN**, and HIGH before Phase 22 ships: re-indexing leaves every earlier run's vectors searchable |
| **ISS-013** | **OPEN.** Pinned for `ingestion_jobs` by `TestIngestionJobs_EnqueueingNeedsTenantScope`; not otherwise resolved |
| **ISS-019** | **OPEN and unchanged.** The `push` and `installation_repositories` payload shapes are still unverified against a real delivery |

### One thing Phase 24 depends on

The pruning `DELETE` advertised in `ingestion_jobs`' own `COMMENT ON TABLE`
keys on `updated_at`, which is why **every statement in this phase writes
`updated_at = NOW()` explicitly** and why there is deliberately no update
trigger. Dropping it from any statement leaves the row at its INSERT-time
default and prunes a week-long job on its creation date — visible a month
later, and nowhere sooner.

---
*Phase: 21-ingestion-job-infrastructure — 7 of 7 plans. **Phase complete.***
*Completed: 2026-09-16*
