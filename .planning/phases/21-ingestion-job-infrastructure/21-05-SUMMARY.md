---
phase: 21-ingestion-job-infrastructure
plan: 05
subsystem: workers
tags: [postgres, queue, consumer, python, leases, tenancy, backoff, redaction]

requires:
  - phase: 21-02
    provides: migration 000014 and the shared statements as named constants — claimSQL, sweepSQL, completeSQL, failSQL, clearRerunSQL, resolveRunSQL — each with a passing test on PostgreSQL 16, including BOTH halves of the lease fence
  - phase: 21-03
    provides: pkg/jobs/producer.go's enqueueUpsertSQL and supersedeLiveSQL, and the rule that a rerun flag on a job at `queued`/`attempts = 0` is cleared by the producer itself
  - phase: 21-04
    provides: jobs of both types reachable from webhooks, `needs_rerun = true` on a RUNNING job reachable from a push, and ISS-033's premise that an uninstalled installation is ABANDONED at claim time
  - phase: 17-04
    provides: the Python isolation harness (`tests/isolation/conftest.py`, `fixtures.py`) and `workers.db.require_tenant`
provides:
  - "workers.jobs — claim, mark_started, complete, fail, defer, abandon, sweep, resolve_ingestion_run, attach_ingestion_run, new_worker_id, next_run_after_delay, sanitize_error, and the LeaseLost exception"
  - "workers/jobs/transitions.py — 21-02's statements ported verbatim ($n -> %s), with the reasons kept beside them"
  - "workers/jobs/backoff.py — min(60s * 4**(n-1), 60 min) * U[0.5,1.0), settling 21-CONTEXT's backoff-ceiling open question"
  - "73 tests: 50 integration against a real PostgreSQL 16 and 23 pure-function unit tests"
  - "the sync_state projection for every state after `pending`"
affects: [21-06 (builds the loop, the heartbeat and the sweeper's schedule on these functions), 21-07 (reads the rows and the last_error this writes), 22 (passes write_results, and owns wiring the pipeline to runs)]

tech-stack:
  added: []
  patterns:
    - "Port a proven statement verbatim and keep its comment: the comment is what stops the next reader deleting the clause that was measured to matter"
    - "An `_unscoped` context manager shaped exactly like `require_tenant`, so the ONE difference a reader has to see is the `SET LOCAL` that is not there"
    - "Raise inside the tenant-scoped transaction to roll a caller's results back: `LeaseLost` from the completion write is what makes 'a lost lease cannot commit results' true rather than hoped for"
    - "Jitter that MULTIPLIES, not tenacity's `wait_random`, which adds — an added jitter on a capped exponential pushes the tail above the cap"
    - "Redact before persisting an exception message: `last_error` is a column an admin endpoint hands back over HTTP"
    - "When a guard is unobservable through the function that uses it, test the STATEMENT — otherwise the mutation survives and the port silently drops what the original plan added"
    - "A premise helper that carries its own copy of the logic under test is invisible to a mutation of it: `assert_no_older_claimable` evaluates the UNMUTATED claim predicate, so every claim guard needs its own test"
    - "A rule stated in a docstring is not a rule until one test reads the thing the docstring is about — here, an actual LogRecord"

key-files:
  created:
    - services/workers/workers/jobs/__init__.py
    - services/workers/workers/jobs/transitions.py
    - services/workers/workers/jobs/backoff.py
    - services/workers/workers/jobs/test_backoff.py
    - services/workers/tests/isolation/test_job_transitions.py
  modified:
    - .planning/ROADMAP.md
    - .planning/STATE.md
    - .planning/phases/21-ingestion-job-infrastructure/21-07-PLAN.md

key-decisions:
  - "`lease_owner` is a UUID4 generated at worker start, in `new_worker_id()`. 21-CONTEXT left this open; hostname plus PID is reused by a container scheduler restarting a crashed worker onto the same host, and the lease fence is only as strong as the uniqueness of the value it compares."
  - "Backoff is `min(60s * 4**(n-1), 60 minutes)` multiplied by U[0.5, 1.0). tenacity is NOT used: `wait_random` adds a uniform term, and no tenacity strategy multiplies — an added jitter on a capped exponential pushes the tail above the cap, so the cap stops being one. tenacity is also a retry-LOOP driver, and nothing here loops; the delay is a column value."
  - "`abandon` leaves `attempts` alone, unlike `defer`. The row is terminal (`superseded`), so the counter is no longer a budget that could be consumed — it is the record that a worker claimed the job once, which 21-07 reads. ISS-033's 'no attempt consumed' is the statement that this path cannot walk a repository towards `dead`, and that holds because the job leaves the live set rather than returning to `queued`."
  - "Only `mark_started`'s projection carries an `EXISTS` fence. Every other projection runs in a transaction whose FIRST statement is a lease-fenced write that raises `LeaseLost` on zero rows, so it is unreachable for a stale worker; and copying the `EXISTS` onto them would BREAK them, because after `COMPLETE_SQL` the job is no longer `running`."
  - "`fail` returns the state the DATABASE chose (`RETURNING state`) rather than re-deriving `failSQL`'s CASE in Python against a snapshot of `attempts`. `sweep` gained a `RETURNING` list for the same kind of reason — per-job logging. Both are additions to proven statements that select no different rows and write nothing extra."
  - "`attach_ingestion_run` exists rather than leaving the caller to write a fenced UPDATE. The plan says 'the caller sets ingestion_jobs.ingestion_run_id, fenced'; leaving a fence to a caller is exactly how 21-02's `clearRerunSQL` defect happened."
  - "`sanitize_error` keeps the exception CLASS. `psycopg2.errors.UniqueViolation` and a bare `Exception` with the same text mean different things to whoever reads 21-07."
  - "The redact-then-truncate ORDER is not a security guard, and the first version of the comment claiming it was has been corrected. Measured: a truncated token's prefix still matches the pattern, so the other order redacts the fragment too. Mutation X is the deliberate survivor, re-measured independently by PR #41's review over eighteen inputs."
  - "`_sanitize_text` strips NUL and ONLY NUL, and strips it BEFORE redacting. psycopg2 refuses a NUL client-side, so without the strip the failure write is lost and the job is stranded `running` with nothing recorded — PR #41's one important correctness finding. A scratch PG16 measured 34 other control code points accepted and one rejected, so stripping more would throw away the tab or newline that makes a subprocess dump readable. Stripping BEFORE redacting matters because a NUL ends a pattern match and would leave the tail of a token visible."
  - "The redaction covers all six GitHub prefixes, JWTs and PEM blocks, none of which this module can reach today. 21-06 adds the claim-time installation read, and whatever mints an installation token holds an App JWT — widening before that path exists costs a regular expression; widening after costs whatever was written in between. A 40-hex arm was DECLINED because it also matches a git commit SHA, which is the useful half of these messages."

issues-created: []
issues-closed: []
review: "PR #41 — APPROVE WITH NITS, no critical findings. The six Go constants were extracted from `main` and diffed mechanically: byte-identical. Eleven independent probes against the real container all passed. One important correctness finding (a NUL byte in an exception message lost the failure write and stranded the job), eight neutering mutations that survived the Python suite, and the ruling that deviation 5 is right but unpinned. All applied; sixteen round-2 mutations, all killed."

duration: ~4h, plus ~2h applying PR #41's review
completed: 2026-09-16
---

# Phase 21 Plan 05: the Python consumer's transitions

**The queue now has a consumer side. Every statement 21-02 proved on
PostgreSQL 16 has been ported into Python verbatim and re-run from there,
every terminal write is fenced on both halves of the lease, and a worker
that has lost its lease cannot commit a single row — proven by rolling a
probe write back.**

**Revised after PR #41's review** (APPROVE WITH NITS, no critical
findings), which extracted the Go constants and confirmed the port is
byte-identical, then found what the *tests* did not cover: eight neutering
mutations that survived the Python suite — one of them the claim's
live-lease exclusion, the predicate the whole lease design rests on — and
one real defect, a NUL byte in an exception message losing the failure
write and stranding the job. All applied; see "Round 2" below.

There is still no long-running process. 21-06 builds the loop, the
heartbeat and the sweeper's schedule on top of these functions.

## The API

`services/workers/workers/jobs/`. Three modules: `transitions.py` (the
state machine), `backoff.py` (the retry delay), `__init__.py` (the
exports).

```python
@dataclass(frozen=True)
class Job:
    id: UUID; organization_id: UUID; repository_id: UUID; job_type: str
    attempts: int; max_attempts: int; needs_rerun: bool; payload: dict | None

class LeaseLost(Exception): ...

# UNSCOPED — `ingestion_jobs` only, no tenant
def claim(conn, worker_id: str, lease: timedelta) -> Job | None
def sweep(conn) -> int

# TENANT-SCOPED — inside require_tenant(conn, job.organization_id)
def mark_started(conn, job: Job, worker_id: str) -> None
def complete(conn, job: Job, worker_id: str,
             write_results: Callable[[cursor], None] | None = None) -> bool
def fail(conn, job: Job, worker_id: str, error: BaseException) -> str   # "queued" | "dead"
def defer(conn, job: Job, worker_id: str, delay: timedelta, reason: str) -> None
def abandon(conn, job: Job, worker_id: str, reason: str) -> None

# TENANT-SCOPED, on the CALLER'S cursor and inside the caller's transaction
def resolve_ingestion_run(cur, repository_id, commit_sha, branch) -> UUID
def attach_ingestion_run(cur, job: Job, worker_id: str, run_id: UUID) -> None

# Pure
def new_worker_id() -> str
def next_run_after_delay(attempts: int, rng=random.random) -> timedelta
def sanitize_error(error: BaseException) -> str
```

`Job` is a **snapshot**. `needs_rerun` on it is the value at claim time and
is deliberately not what `complete` acts on: a push arriving mid-run sets
the flag afterwards, and `CLEAR_RERUN_SQL` re-reads it from the row.

## The `sync_state` projection, as implemented

`repositories.sync_state` is a projection of job state, never a queue
(21-CONTEXT L2). 21-03's producer writes `pending` for a repository that
got a **new** job; 21-04's handlers write `never_synced` on stand-down.
Everything else is this module's.

| Transition | `sync_state` written | Statement |
|---|---|---|
| **claim** | **not written** | — |
| **mark_started** (`running`) | `syncing` | `PROJECT_SYNCING_SQL`, fenced through the job |
| **complete** | `synced`, **plus `last_synced_at = NOW()`** | `PROJECT_SYNCED_SQL` |
| **fail**, retrying (`queued`, `attempts > 0`) | `failed` | `PROJECT_FAILED_SQL` |
| **fail**, `dead` | `failed` | `PROJECT_FAILED_SQL` |
| **defer** (suspended installation) | **unchanged** | — |
| **abandon** (uninstalled, or no installation) | `never_synced` | `PROJECT_NEVER_SYNCED_SQL` |
| superseded by a producer | not written | — |

Three of those rows are worth their sentence:

- **The claim writes nothing** because the installation check comes first
  (21-06). A job under a dead installation must be abandoned to
  `never_synced` without ever having claimed to be `syncing`.
- **`defer` leaves it alone.** Writing `failed` would call a healthy
  repository broken; writing `pending` would flap every hour the App stays
  suspended. A deferral is not something the UI should see.
- **`abandon` writes `never_synced`, never `failed`** — matching
  `github_webhook_events.go`'s uninstall stand-down exactly ("'failed' is
  deliberately not used — nothing failed, and the queue must not retry
  these"). ISS-033 is filed on the premise that 21-06 calls this rather
  than letting such a job fail its way to `dead`.

"Currently retrying" and "dead" are told apart by the **job's** state, not
by `sync_state`: there is no `failed` job state (decision O2), so retrying
is `state = 'queued' AND attempts > 0`.

**`last_synced_at` had never been written by anything.** The column has
existed since 000010 and the completion projection is its first writer.

## The backoff, as implemented

```
after attempt n fails:   min(60s * 4 ** (n - 1), 60 minutes)  *  U[0.5, 1.0)
```

| n | uncapped | capped | actual range |
|---|---|---|---|
| 1 | 60s | 60s | 30–60s |
| 2 | 240s | 240s | 120–240s |
| 3 | 960s | 960s | 480–960s |
| 4 | 3840s | **3600s** | 1800–3600s |
| 5 | 15360s | **3600s** | *(never used: attempt 5 writes `dead`)* |

**Worst case with `max_attempts = 5`: 60 + 240 + 960 + 3600 = 4860s = 81
minutes.** That is the number 21-CONTEXT's "pick a cap and write it down"
asked for, and it is a test
(`test_the_total_wait_for_five_attempts_is_about_81_minutes`).

**⚠ tenacity is not used, although `21-RESEARCH.md` says "don't hand-roll …
use the existing pattern from `tenacity`".** Two reasons, both structural:

1. **The jitter has to MULTIPLY.** `wait_random` and
   `wait_exponential + wait_random` *add* a uniform term; no tenacity
   strategy multiplies. Adding jitter to a capped exponential pushes the
   tail **above** the cap, so the cap stops being a cap. Multiplying keeps
   every delay inside `[cap/2, cap)` and still spreads a herd.
2. **Nothing here loops.** tenacity drives a retry loop; a failed attempt
   writes `run_after` and *releases* the worker. The delay is a column
   value, not a sleep.

**Why 4 rather than 2:** with only five attempts, doubling puts the last
retry about sixteen minutes out in total, which is shorter than a single
large ingest — it would retry a transient outage while it was still
happening. A test also pins `MULTIPLIER * JITTER_FLOOR > 1.0`, because at
2 × 0.5 the sequence stops being monotonic across two jitter draws.

## `last_error`: safe to write, and safe to show

`_sanitize_text` does three things, in this order: **strip NUL, redact,
truncate to 2,000 characters.** The motivating shape is a failing clone:
`last_error` is persisted **and** returned by 21-07's admin endpoint, and a
clone URL is `https://x-access-token:ghs_…@github.com/…`.

### The NUL strip — PR #41's second important finding

**A NUL byte in an exception message lost the whole failure write.**
psycopg2 refuses a NUL in a text parameter **client-side**
(`ValueError: A string literal cannot contain NUL (0x00) characters`,
raised while building the statement, so it is not a `psycopg2.Error` and
cannot be caught as one). Raised inside `require_tenant`, it rolled the
transaction back and left the job:

```
state=running  attempts=1  lease_owner=<A>  last_error=NULL
```

— an attempt consumed, nothing recorded, the row holding the partial unique
index until the lease expired five minutes later. **Deterministic input, so
it repeated every attempt:** five wasted worker slots and a repository that
dead-letters with no reason on it. **Reachable from a hostile repository**
— binary file content echoed through a parser error, a `git`/subprocess
stderr dump, a tree-sitter failure carrying raw bytes.

**It is exactly one character, measured, not "control characters."** A
scratch `postgres:16-alpine` was given every code point in `0x00-0x1F`,
`0x7F` and two C1 points inside a `TEXT` value: **34 accepted, one
rejected.** Stripping more would throw away the tab or newline that makes a
subprocess dump readable, so the strip is `\x00` and nothing else.

**⚠ The strip runs BEFORE the redaction, and that ordering *is*
load-bearing** — unlike redact-before-truncate. A NUL is in none of the
pattern's character classes, so it ENDS a match: `ghs_ABCDEF\x00GHIJKL`
redacted first leaves `GHIJKL` visible. Stripping first hands the pattern
one contiguous token. Mutation **S11** is that swap, and it is killed.

### The redaction, widened before 21-06 rather than after

| Arm | Covers |
|---|---|
| `-----BEGIN … PRIVATE KEY-----…-----END … PRIVATE KEY-----` | a PEM block, **whole** — one marker, not one per base64 line |
| `github_pat_[A-Za-z0-9_]+` | fine-grained PAT |
| `gh[psuor]_[A-Za-z0-9]+` | **all six** GitHub prefixes: `ghs_`, `ghp_`, `gho_`, `ghu_`, `ghr_` |
| `sk-[A-Za-z0-9_-]+` | OpenAI, including `sk-proj-` |
| `eyJ…\.…\.…` | a JWT — **both** the GitHub App JWT and Supabase's service-role key |

The first cut said "GitHub's documented token prefixes" and had three of
six; PR #41's review measured `gho_`, `ghu_`, `ghr_`, a JWT and a PEM block
all passing through. **None is reachable from this module today** — the
worker holds only `ghs_` and `sk-`. They are here because **21-06 adds the
claim-time installation read**, and whatever mints an installation token
holds an App JWT signed with the App private key. Widening before that path
exists costs one regular expression; widening afterwards costs whatever was
written to the column in between.

**Two shapes were considered and declined, and the reasons are in the
code:**

- **A bare 40-hex run** (the App client secret) also matches a **git commit
  SHA**, which is legitimate, useful context in exactly these messages.
  Redacting it would blind the admin endpoint to which commit failed.
  `test_a_commit_sha_is_deliberately_not_redacted` pins the choice so the
  next reader finds the reason rather than the gap.
- **A generic `://user:password@` DSN arm.** psycopg2's connection errors
  do not quote the password, the clone-URL case is already covered by the
  `ghs_` arm, and a generic arm would redact the visible half of a
  credential-free URL for nothing.

Redaction keeps the surrounding context (`github.com/acme/widgets.git`,
`exited 128`) — the point is to keep the error useful, not to blank it.

### And the log line, not just the column

`defer` and `abandon` wrote `_sanitize_text(reason)` to the column and
passed the **caller's raw string** to the logger — contradicting `_log`'s
own docstring, which says a log line must not become the second place a
token lives. Both now sanitize **once** and use that value for both.
`test_the_caller_supplied_reason_is_sanitized_in_the_log_too` reads the
actual `LogRecord`; it is the only test in the file that does, and without
it mutations S15/S16 survive the whole suite.

### The order that is *not* a guard

**One claim in the first cut was wrong and is corrected rather than left
standing.** The comment said redact-before-**truncate** was a security
guard, because "truncating first can cut a token in half and leave most of
it in the column". **Measured false**, and PR #41's review re-measured it
independently over **eighteen constructed inputs — zero leaks in either
order**, including a token starting exactly at the cut index and the prefix
straddling the cut at every offset. The reason is structural: truncation
removes a **suffix** and every pattern anchors on a **prefix**, so whatever
survives the cut still begins with the prefix and still matches. Mutation X
is the deliberate survivor, and what redacting first actually buys — tidier
output length — is now stated as tidiness.

## Two connection modes, and the `_unscoped` helper

- **Unscoped:** `claim` and `sweep`. They touch only `ingestion_jobs`,
  which has no row-level security, and neither statement touches
  `organization_id` or `repository_id`, so `trg_ingestion_jobs_tenant` does
  not fire. The claim is genuinely pre-tenant: the worker learns its tenant
  **from the row it claimed**.
- **Tenant-scoped:** everything else, inside
  `require_tenant(conn, job.organization_id)`.

`_unscoped` is written to the **same shape** as `require_tenant` — same
idle precondition, same autocommit save/restore, same commit-on-success —
so the one difference a reader has to notice is the `SET LOCAL` that is not
there. Its idle precondition is the same one `require_tenant` carries and
for the same reason: psycopg2's `with conn:` does not nest, so entering
mid-transaction would silently commit the caller's outer work.
`test_a_transition_refuses_a_connection_that_is_mid_transaction` pins both.

### ISS-013 cannot reach either statement, and that is tested

An unscoped read of an RLS table is **silently empty** on a fresh
connection and raises **22P02** on one that has committed a `SET LOCAL`.
`test_the_unscoped_statements_do_not_depend_on_connection_history` asserts
**both premises before the thing it is testing**: a fresh connection's
`SELECT count(*) FROM repositories` must return 0, and `db_conn`'s must
raise 22P02 — otherwise "the claim worked on both" proves nothing. It then
claims and sweeps successfully on each.

They are immune because `ingestion_jobs` has no policy in which
`current_setting('app.current_tenant', true)::uuid` is ever evaluated.

## The ordering rule, and the silence

`complete` runs, in one tenant-scoped transaction:

1. `write_results(cur)` — Phase 22's chunk write
2. `CLEAR_RERUN_SQL`, fenced — a row back means a push arrived mid-run
3. `COMPLETE_SQL`, fenced — **zero rows raises `LeaseLost`**
4. the `synced` projection
5. the rerun's follow-up job, through the producer's upsert

**Steps 3 and 5 in that order, and the wrong order raises nothing.** Run
backwards, the upsert finds this job still in the live set, takes its
`ON CONFLICT` branch, sets `needs_rerun = TRUE` on the row that is about to
become `completed`, and creates nothing at all. No error. The repository is
left with no live job and a terminal row carrying a flag nothing will ever
read. 21-CONTEXT L4 and L7 recorded `23505` here; that is what a plain
`INSERT` does, and the only enqueue path is an upsert (correction dated
2026-09-14). Mutation B is exactly that reordering, and the test that
catches it fails on **the follow-up job being absent**, not on an error.

**Step 3's `LeaseLost` is what makes "a lost lease cannot commit results"
true rather than hoped for.** It is raised *inside* the transaction, so
`require_tenant` rolls step 1 back with it.
`test_a_reclaimed_workers_completion_raises_and_commits_nothing` writes a
probe row in `write_results` and then asserts the probe is gone.

## What behaved differently in Python than in Go

Three things, none of them a change to a statement.

**1. `clearRerunSQL`'s `AND state = 'running'` is UNOBSERVABLE through
`complete`, and needed a statement-level test.** In Go, 21-02 measured the
defect by running the two statements separately: against a superseded row
the unfenced clear reported `UPDATE 1` while `completeSQL` correctly
reported `UPDATE 0`. In Python they share one transaction, so a bad clear
is rolled back by the `LeaseLost` the completion raises — the transition's
observable behaviour is identical with and without the predicate.
**Measured: mutation G survived every transition-level test.**
`test_clear_rerun_sql_is_fenced_on_the_running_state` runs the statement
directly, mirroring the Go test, and kills it. Without that test this port
would have silently dropped the predicate PR #38's review added.

**2. psycopg2 returns `uuid` columns as `str`.** pgx hands back a typed
UUID; psycopg2 registers no UUID typecaster unless
`psycopg2.extras.register_uuid()` is called (verified: `string_types` has
no entry for oid 2950, and a live `SELECT gen_random_uuid()` returns
`str`). `Job` therefore coerces with `UUID(str(...))` on the way in and
every statement is given `str(...)` on the way out, so the dataclass's
declared types are true regardless of what a future caller registers.
`jsonb` **does** come back as a `dict` by default, so `payload` needs no
coercion.

**3. `$n` → `%s`, and three `RETURNING` clauses.** PR #41's review diffed
all seven constants mechanically against `main` and **six are
byte-identical** once `$n` is rewritten, including the parenthesisation of
the claim's `OR` branches. The three that differ do so only by a
`RETURNING` clause — same rows selected, nothing extra written:
`sweepSQL` gains one (so each dead-lettered job can be logged with its id,
repository and attempts), `failSQL` gains `RETURNING state` (so `fail`
reports the state the database chose rather than re-deriving the `CASE`
against a snapshot), and `enqueueUpsertSQL` returns `id::text` rather than
`id` (the cast psycopg2 would apply anyway, said at the statement). The
first version of this summary declared two of those three; see deviation 9.

**4. A NUL in a parameter is a CLIENT-side failure in Python, and that
changes what it breaks.** pgx would send the bytes and let PostgreSQL
refuse them, producing a `PgError` inside the transaction like any other.
psycopg2 raises `ValueError` while *building* the statement, before
anything is sent — so it is not a `psycopg2.Error`, no `except
psycopg2.Error` catches it, and in `fail` it aborted the whole failure
write and stranded the job. The Go side has no equivalent hazard, and the
fix (`_sanitize_text` strips `\x00`) has no Go counterpart to mirror. Found
by PR #41's review; see the `last_error` section.

## Mutation results

**25 mutations, each applied to a COPY of `services/workers` plus
`services/backend/migrations`** (the conftest resolves migrations at
`parents[3]`, so the copy is `<scratch>/workers` beside
`<scratch>/backend/migrations`). The harness restores the pristine files
from the committed worktree before each run, **asserts the pattern matched
exactly once and that the mutated text is present after the write**, then
runs `pytest tests/isolation/test_job_transitions.py workers/jobs -q`. The
committed tree was never mutated.

Baseline on the copy: **51 passed.**

### The plan's six

| # | Mutation | Result |
|---|---|---|
| A | `complete`'s fence removed entirely (`WHERE id = %s AND %s IS NOT NULL`) | **Killed: 4.** `a_reclaimed_workers_completion_raises_and_commits_nothing`, `the_rerun_flag_survives_a_completion_that_loses_its_lease`, `the_new_owner_is_unaffected_by_the_stale_worker`, `superseded_workers_writes_all_raise_lease_lost[complete]` |
| B | Re-enqueue **before** the completion write | **Killed: 1, and nothing raised.** `a_rerun_flagged_mid_run_enqueues_exactly_one_follow_up` fails on the follow-up job being missing — the silent failure mode, caught as a missing row rather than an error |
| C | The naive `... WHERE id AND lease_owner RETURNING needs_rerun` | **Killed: 4.** Including `claim_start_and_complete`, because the naive form returns a row on **every** completion, so a follow-up job is enqueued for a job that had no rerun |
| D | Drop `attempts - 1` from `defer` | **Killed: 2.** `defer_returns_the_attempt_and_leaves_the_projection_alone`, and `a_repeatedly_deferred_job_never_dead_letters` |
| E | Remove the backoff cap | **Killed: 3.** `the_cap_holds`, `the_jitter_stays_inside_its_interval`, `the_total_wait_for_five_attempts_is_about_81_minutes` |
| F | Drop the redaction | **Killed: 7.** Six `sanitize_error` unit tests plus `fail_below_max_attempts_requeues_with_a_jittered_backoff`, which asserts the token never reaches the column |

### The guards this plan added

| # | Mutation | Result |
|---|---|---|
| G | Drop `AND state = 'running'` from `CLEAR_RERUN_SQL` — **the 21-02 defect exactly** | **Killed: 1** — `clear_rerun_sql_is_fenced_on_the_running_state`, and **only** that one. See "What behaved differently" above: this is unobservable through `complete` |
| H | `mark_started` loses its `EXISTS` fence | **Killed: 1** — `a_reclaimed_workers_mark_started_writes_nothing` |
| I | `COMPLETE_SQL` keeps `lease_owner` but drops `state = 'running'` | **Killed: 2** — both superseded cases; the reclaimed case correctly survives, since `lease_owner` already covers it |
| J | `FAIL_SQL` drops `state = 'running'` | **Killed: 1** — `superseded_workers_writes_all_raise_lease_lost[fail]` |
| K | `DEFER_SQL` drops `state = 'running'` | **Killed: 1** — `…[defer]` |
| L | `ABANDON_SQL` drops `state = 'running'` | **Killed: 1** — `…[abandon]` |
| M | `abandon` writes `dead` instead of `superseded` | **Killed: 2** — `abandon_supersedes_and_stands_the_repository_down`, `the_sweeper_leaves_an_abandoned_job_alone` |
| N | `abandon` projects `failed` instead of `never_synced` | **Killed: 2** — the same two. This is ISS-033's wrong ending, as a mutation |
| O | `complete` writes no projection | **Killed: 2** |
| P | `attach_ingestion_run` loses its fence | **Killed: 1** — `attaching_a_run_is_fenced` |
| Q | `resolve_ingestion_run` becomes a plain `INSERT` (W6) | **Killed: 1** — `resolving_a_run_twice_returns_the_same_row`, with `23505` |
| R | `sweep` drops the `state = 'queued'` branch | **Killed: 1** — `the_sweeper_dead_letters_a_queued_job_at_max_attempts` |
| S | `sweep` ignores the live lease | **Killed: 1** — `the_sweeper_leaves_a_running_job_with_a_live_lease_alone` |
| V | The rerun follow-up enqueues `full_ingest` | **Killed: 1** |
| W | `_unscoped` drops its idle precondition | **Killed: 1** |
| X | Truncate before redacting | **SURVIVED — deliberate.** See below |

### The three that survived first, and what they added

Following 21-01's practice: a guard nobody has seen fail proves nothing.

| # | Mutation | First result | After |
|---|---|---|---|
| T | Claim drops `attempts < max_attempts` | **SURVIVED, 51 passed** | Added `test_a_job_at_max_attempts_is_not_claimed` → **killed** |
| U | Claim drops `lease_expires_at IS NULL` | **SURVIVED, 51 passed** | Added `test_a_running_job_with_a_null_lease_is_reclaimed` → **killed** |
| Y | Claim drops `ORDER BY run_after` | **SURVIVED** even with an ordering test present | The test seeded its rows in `run_after` order, so a scan with `LIMIT 1` and no ordering returned the physically first row — the right answer for the wrong reason. Seeding them in the OPPOSITE order → **killed**, with `the claim returned <other id>, not this test's job` |

**T and U are the finding that matters.** Both clauses are 21-RESEARCH's
two corrections — the poison-job guard and the null-lease strand, each the
subject of a paragraph — and both were **ported correctly and tested only
in Go**. A port that drops them fails nothing on the Python side, which is
precisely the shape of failure the plan's mutation list exists to find. The
same argument applied to the ordering, which is why Y was added.

**Y is also a fixture finding, the same class as 21-04's.** The test was
right about what it asserted and wrong about what its fixtures could
distinguish. Asking "what can this fixture *not* catch?" is what turned it
into a real test.

**X is the deliberate survivor.** Truncate-then-redact still redacts,
because `ghs_[A-Za-z0-9]+` matches a truncated token's prefix — measured
directly, the fragment `ghs_ZZZZZZZZZZZZZZZ` left by the cut becomes
`[REDACTED]`. The ordering claim in the code has been corrected rather than
the mutation explained away, and the test that guarded it was rewritten to
assert the property both orders have. **PR #41's review re-measured this
independently** over eighteen constructed inputs and found zero leaks in
either order, and agreed the honest docstring beats the confident wrong one.

## Round 2: PR #41's review, and the eight clauses it found unguarded

**APPROVE WITH NITS, no critical findings.** The review extracted the six
Go constants from `main`, rewrote `$n` → `%s` mechanically and diffed them:
**byte-identical**. It wrote eleven probes of its own against the real
container — reclaim rollback with two writes in the callback, superseded,
cross-tenant, live lease, sweeper null-lease, exception chains — and **the
implementation passed all eleven**. What it reported was almost entirely
**coverage, not correctness**, and the one exception is the NUL byte.

### What changed

| Finding | Applied |
|---|---|
| **NUL loses the failure write** | `_sanitize_text` strips `\x00`, before the redaction. One unit test per property plus an end-to-end `fail` test. See the `last_error` section. |
| **Eight neutering mutations survive** | Six new tests. Table below. |
| Raw `reason` in `defer`/`abandon`'s log | Sanitized once, used for both; a test that reads the `LogRecord`. |
| Deviation 5 unpinned | `assert row["attempts"] == 1` in the abandon test, with the ruling's reasoning beside it. |
| Redaction misses three GitHub prefixes, JWTs, PEM | All five arms added, each tested, before 21-06 puts them in reach. |
| `ENQUEUE_UPSERT_SQL` not verbatim | Declared — see deviation 9 below, and the module docstring now lists all three statements that differ from Go. |

### The mutation table for the eight survivors

Same harness discipline as round 1: a fresh copy of `services/workers` plus
`services/backend/migrations`, restored from the committed worktree before
each run, the pattern asserted to match **exactly once** and the mutated
text asserted present **and** the original absent before the suite runs.
Baseline on the copy: **73 passed.**

| # | Mutation | Round 1 | Now |
|---|---|---|---|
| S1 | The claim's reclaim branch → bare `OR (state = 'running')` | **SURVIVED** | **Killed** — `test_a_running_job_with_a_live_lease_is_not_claimed` |
| S2 | `_FAIL_SQL` `AND lease_owner = %s` → `AND %s IS NOT NULL` | **SURVIVED** | **Killed** — `…terminal_writes_are_all_refused[fail]` |
| S3 | `DEFER_SQL`, same | **SURVIVED** | **Killed** — `…[defer]` |
| S4 | `ABANDON_SQL`, same | **SURVIVED** | **Killed** — `…[abandon]` |
| S5 | `ATTACH_RUN_SQL`, same | **SURVIVED** | **Killed** — `test_attaching_a_run_is_fenced[reclaimed]` |
| S6 | `CLEAR_RERUN_SQL`, same | **SURVIVED** | **Killed** — `…fenced_on_both_halves[lease_owner]` |
| S7 | `PROJECT_SYNCING_SQL`'s `EXISTS` drops `state = 'running'` | **SURVIVED** | **Killed** — `test_a_superseded_workers_mark_started_writes_nothing` |
| S8 | `_SWEEP_SQL` drops `lease_expires_at IS NULL OR` | **SURVIVED** | **Killed** — `…dead_letters_a_running_job_with_a_null_lease` |
| S9 | `ABANDON_SQL` **gains** `attempts - 1` (deviation 5) | **SURVIVED** | **Killed** — `test_abandon_supersedes_and_stands_the_repository_down` |
| S10 | No NUL strip | *(the defect)* | **Killed: 3** — the end-to-end `fail` test and two unit tests |
| S11 | Redact **before** stripping NUL | *(the defect)* | **Killed** — `…a_nul_inside_a_token_does_not_split_the_redaction` |
| S12 | `gh[psuor]_` narrowed back to `gh[ps]_` | *(new arm)* | **Killed: 3** — one per new prefix |
| S13 | Drop the JWT arm | *(new arm)* | **Killed: 2** |
| S14 | Drop the PEM arm | *(new arm)* | **Killed** |
| S15 | `defer` logs the raw `reason` | **SURVIVED** | **Killed** — `…sanitized_in_the_log_too[defer]` |
| S16 | `abandon` logs the raw `reason` | **SURVIVED** | **Killed** — `…[abandon]` |

**Sixteen run, sixteen killed.** S15 and S16 survived the first attempt at
this round too: nothing in the suite read a log record. Rather than report
them as expected survivors, one test now reads the `LogRecord` — which is
the only way a rule stated in a docstring becomes a rule.

### Why five of the eight needed a shape nothing here had

The `lease_owner` half of the fence was untested on five statements for one
structural reason: **the parametrized superseded test cannot reach it.** A
superseded row keeps A's lease — that is `supersedeLiveSQL`'s deliberate
behaviour — so `lease_owner = %s` matches there by construction, and only
`state = 'running'` does any work. `complete` was the only transition with
a **reclaimed-worker** test, where the row is `running` again under B and
the owner is the only thing refusing. The new tests supply that shape, and
each asserts its premise first (`state == 'running'`, or `lease_owner ==
worker_a` for the superseded direction) so it cannot pass for the other
half's reason.

**`assert_no_older_claimable` could not have caught S1**, and this is worth
recording: the helper carries **its own correct copy** of the claimable
predicate, so it evaluates the unmutated rule. A premise helper that
duplicates the logic under test is invisible to a mutation of it. The new
claim test also re-claims after expiring the lease, so it cannot pass
because the job was simply unclaimable.

### The three nits, and what was chosen

| Nit | Choice |
|---|---|
| `MAX_ERROR_LENGTH` counts characters, not bytes (2,000 chars = 3,974 UTF-8 bytes) | **Clarified, and pinned.** The constant's comment now says characters and why that is right — the column is `TEXT` with no declared limit, and the budget being bounded is what a human reads in 21-07's response. `test_the_cap_counts_characters_not_bytes` asserts both halves so the comment and the code cannot drift. |
| `FOR UPDATE SKIP LOCKED` is covered in Go but not in Python | **Documented, not tested here.** Nothing in this file runs two claims concurrently, so there is no lock for a second claimer to skip; a test would be theatre. The module docstring now has a "what this file does NOT pin" section naming it, the Go test that does cover it, and **21-06's barrier test** as what will cover it from Python. |
| "Never run inside a request handler" is a docstring, not a guard | **Carried to 21-07**, which is the phase that first puts an HTTP handler over this table. Its plan now asks for a decision — the cheapest shape is a `grep` gate beside the one it already runs — and says to record the answer either way. Not built here: there is no handler to guard yet, and a gate with nothing to catch is a gate nobody maintains. |

## What is deliberately NOT here

- **`PostgresWriter` and `IngestionPipeline` are unchanged.** `git diff`
  shows no file under `services/workers/workers/storage/` or
  `.../pipeline/` in this PR.
- **No migration.** Nothing in this plan changes the schema, so the shared
  harness container was never rebuilt.
- **No process.** 21-06 owns the loop, the heartbeat, the sweeper's
  schedule and `python -m workers`.

## Phase 22 notes

Two changes Phase 22 owns, both named by this plan's context and neither
made here.

**1. Wire the pipeline to runs.** `PostgresWriter.create_ingestion_run`
**has no `ON CONFLICT`**, so a repeat of the same commit raises `23505`
today — the exact error W6 exists to prevent. `resolve_ingestion_run` fixes
it *for jobs*, and this plan deliberately did not change `PostgresWriter`:
the fix belongs with the change that makes the pipeline run under a job.
The shape is
`resolve_ingestion_run(cur, …)` then `attach_ingestion_run(cur, job, …)`
in one tenant-scoped transaction, with `PostgresWriter` taking the run id
rather than minting one.

**2. Make chunk writes idempotent per run.** The completion transaction
removes **torn** writes, not **duplicated work**: a crash mid-embedding
re-runs the job from the start, and the retry reuses the same
`ingestion_runs` row (W6). So `write_results` must be
delete-by-run-then-insert, or an upsert. `21-RESEARCH.md`'s "What the
transaction actually buys" says this in as many words, and **ISS-027** is
the live half of it — re-indexing leaves every earlier run's vectors
searchable.

Also for Phase 22, smaller:

- **Worker-pool sizing** stays an open input (21-06 records it, Phase 22
  measures it). Each worker process runs one job at a time; the claim is
  safe across any number of them.
- **21-CONTEXT L5's re-parent question** was already answered by 21-01's
  composite key plus 21-02's: a repository cannot change organization, so a
  job's `organization_id` cannot go stale.

## What 21-06 inherits

- **`abandon` with the semantics ISS-033 assumes:** `superseded`, lease
  cleared, `sync_state = never_synced`, never `failed`, and the job out of
  the live set so a reinstall can enqueue freely. Call it when the
  claim-time installation read finds `installation_id IS NULL` or
  `uninstalled_at` set. Mutations M and N are what hold it.
- **`defer` that cannot dead-letter.** `attempts - 1` gives the claim's
  increment back;
  `test_a_repeatedly_deferred_job_never_dead_letters` runs
  claim-then-defer `max_attempts + 2` times and the job is still `queued`
  at `attempts = 0`.
- **`new_worker_id()`** — call it ONCE at worker start, never per job, or
  the heartbeat's fence disagrees with the claim's.
- **The two connection modes.** The heartbeat thread needs its own
  connection (a shared psycopg2 connection shares its transaction); its
  fenced `UPDATE … SET lease_expires_at = …` is unscoped, like the claim,
  because it touches only `ingestion_jobs`.
- **`LeaseLost` is not a job failure.** Log it and move on: some other
  worker owns the job now, and writing anything further would clobber that.
- **`sweep` returns a count and logs each job it dead-letters.** It is
  queue-wide and cross-tenant by construction and must never run inside a
  request handler.
- **A rerun follow-up is `incremental`,** never `full_ingest` (mutation V).
- **The redaction already covers the App JWT and the App private key**, so
  the claim-time installation read can quote whatever it fails on. Anything
  21-06 puts into `last_error` or into a `defer`/`abandon` reason goes
  through `sanitize_error` / `_sanitize_text` — **including the log line**,
  which is why both values are sanitized once and reused.
- **The heartbeat's fenced `UPDATE` needs BOTH halves,** `lease_owner = %s`
  **and** `state = 'running'`, like every other statement here. The
  reclaimed-worker and superseded tests in this file are the shapes to copy
  for it: each asserts its premise first so it cannot pass for the other
  half's reason.
- **`FOR UPDATE SKIP LOCKED` is not pinned from Python**, deliberately —
  nothing here runs two claims concurrently. **21-06's barrier test is what
  covers it**, and the test module's docstring says so, so the next reader
  does not assume it is already held.

## Verification

| Check | Command | Result |
|---|---|---|
| Workers, CI's environment | from `services/workers`: `REDIS_URL=redis://localhost:63793/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, **no `DATABASE_URL`**, no reachable `.env` (only `.env.example`), **a fresh venv built from `requirements.txt` for the round-2 run** | **254 passed**, 0 failed, 0 skipped (19 pre-existing `utcnow` deprecation warnings). `main` is 181, so this plan adds **73** — 51 in round 1, 22 more applying PR #41's review |
| This plan's tests alone | `pytest tests/isolation/test_job_transitions.py workers/jobs -q` | **73 passed** — 50 integration, 23 unit |
| Repeated | the same command, **5 consecutive runs** on the fresh venv | 73 passed every time, 10.8-11.8s; no flakes |
| Mutations, round 1 | 25, on a copy, each proven present in the file before the run | 24 killed, 1 deliberate survivor (X) |
| Mutations, round 2 | 16 more (S1-S16), same discipline, fresh copy | **16 killed, none surviving** |
| Backend | `git diff --stat RAG-Doc/main..HEAD -- services/backend` | **empty.** No Go file, no migration, so `go test` was not run and the harness container was not rebuilt |
| `PostgresWriter` / `IngestionPipeline` | `git diff RAG-Doc/main..HEAD -- services/workers/workers/storage services/workers/workers/pipeline` | empty |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | `{"missing": [], "skipped": [], "covered": []}` — no route line changed |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

**Containers.** The Python isolation harness starts its own
`postgres:16-alpine` per pytest session and stops it on teardown — that is
what `tests/isolation/conftest.py` has always done, and the mutation copy
does the same, so each mutation run got a fresh database. One scratch Redis
(`rag2105-redis`, port **63793**) was started for the semantic-cache tests,
and round 2 added one scratch `postgres:16-alpine` (`rag2105b-pg`, port
**55506**) for the control-character probe; both were removed afterwards. **The docker-compose Postgres (port 5434) and
Qdrant were never started or touched**, and the shared Go harness
container `rag-doc-isolation-tests` was left untouched on the real schema
(`schema_migrations = 15`); it was read once, with a `SELECT` of generated
values, to check psycopg2's UUID typecasting.

## Deviations from the plan

1. **`sanitize_error` lives in `transitions.py`, not `backoff.py`,** beside
   the `fail` transition that writes its output. `test_backoff.py` is still
   where both pure functions are tested, and its docstring says why: a test
   that needs a container to check a regular expression is a test people
   stop running.
2. **`attach_ingestion_run` exists.** The plan says "the caller sets
   `ingestion_jobs.ingestion_run_id`, fenced, in the same transaction";
   leaving a fence to a caller is how 21-02's `clearRerunSQL` defect
   happened, so it is a function with a test and a mutation (P).
3. **`new_worker_id()` exists.** The plan settles the decision in prose;
   this puts it where the next reader will look, with the container-restart
   reasoning in the docstring.
4. **`sweepSQL` and `failSQL` gained `RETURNING` lists.** Recorded above;
   neither changes which rows are selected or what is written.
5. **`abandon` does not decrement `attempts`.** The brief lists "no attempt
   consumed" as a property; taken literally that would mean decrementing,
   which destroys the record that a worker claimed the job once —
   information 21-07's admin endpoint reads. The row is terminal, so the
   counter is no longer a budget.

   **PR #41's review ruled KEEP**, after checking the two statements that
   could make it matter: `CLAIM_SQL`'s `attempts < max_attempts` and
   `_SWEEP_SQL`'s `attempts >= max_attempts` are reachable only from
   `queued` or `running`, so on a `superseded` row the column is
   **behaviourally inert**. The distinction from `defer` is principled
   rather than inconsistent: `defer` returns the attempt because the row
   goes back to `queued`, where the counter *is* a budget; here it is
   history, and decrementing would write a falsehood into an audit row.

   **Now pinned**, which the first cut was not: adding `attempts - 1`
   survived the whole suite (mutation S9). The abandon test asserts
   `attempts == 1` with the reasoning beside it, and
   `the_sweeper_leaves_an_abandoned_job_alone` — which forces `attempts =
   max_attempts` and watches the job stay `superseded` — remains the test
   for the property ISS-033 actually asks for.
6. **Four tests beyond the plan's list**, each added because a mutation
   survived: the claim's attempt guard, its null-lease branch, its
   ordering, and the statement-level rerun-clear fence.
7. **Three tests beyond the plan's list that no mutation demanded**, and
   which earn their place differently: the two connection-mode
   preconditions, the ISS-013 non-dependence test (which asserts both of
   its premises first), and `a_tenant_scoped_write_for_the_wrong_organization_is_refused`,
   which pins the tenant trigger's 42501 and its non-oracle message from
   the Python side.
8. **The mutation count is 41, not 6** — 25 in round 1, 16 more applying
   PR #41's review. The extras follow 21-02's and 21-03's practice, and
   **eleven of them found real gaps**: three in round 1 (the claim's two
   guards and its ordering) and eight in round 2.
9. **`ENQUEUE_UPSERT_SQL` returns `id::text` where Go returns `id`** —
   the eighth deviation, which the first version of this summary did not
   declare while calling the port byte-identical. Found by PR #41's review
   diffing all seven constants mechanically. Same row, nothing extra
   written, and the cast is the right choice for psycopg2, which hands an
   unqualified `uuid` back as `str` anyway (see "What behaved differently"
   above). The module docstring now lists **all three** statements that
   differ from their Go twin, each by a `RETURNING` clause only.
10. **Six tests, one production fix and three arms of the redaction were
    added after the review**, beyond anything either the plan or round 1
    called for. See "Round 2" above for the table.

## Next Phase Readiness

21-06 can import `workers.jobs` and build the loop, the heartbeat and the
sweeper's schedule on functions that have run against PostgreSQL 16.

---
*Phase: 21-ingestion-job-infrastructure — 5 of 7 plans*
*Completed: 2026-09-16*
