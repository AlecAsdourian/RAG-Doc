---
phase: 21-ingestion-job-infrastructure
plan: 06
subsystem: workers
tags: [postgres, queue, worker, python, leases, heartbeat, threads, shutdown, entrypoint]

requires:
  - phase: 21-02
    provides: migration 000014 — the five states, the lease columns, the partial unique index and `max_attempts`
  - phase: 21-03
    provides: the Go producer, so a connect creates a real job for a worker to claim
  - phase: 21-04
    provides: the webhook producers, and ISS-033's premise that an uninstalled installation is ABANDONED at claim time
  - phase: 21-05
    provides: "workers.jobs.transitions — claim, mark_started, complete, fail, defer, abandon, sweep, new_worker_id, sanitize_error, LeaseLost; and the rule that `claim` writes no projection because the installation check comes first"
  - phase: 17-04
    provides: the Python isolation harness (`tests/isolation/conftest.py`, `fixtures.py`) and `workers.db.require_tenant`
provides:
  - "workers.jobs.runtime — `Worker`, `JobContext`, `Handler`, `WriteResults`, `UnknownJobType`, and the three statements this layer owns: HEARTBEAT_SQL, INSTALLATION_SQL, PROGRESS_SQL"
  - "workers.jobs.handlers.REGISTRY — empty in Phase 21, with the Phase 22 recipe in its docstring"
  - "workers/__main__.py — `python -m workers`, which FAILS CLOSED with exit 2 while the registry is empty, before it reads any configuration"
  - "the claim-time installation check: abandon / defer 60 min / run, with `mark_started` after it"
  - "19 integration tests against a real PostgreSQL 16, including the barrier claim race that pins `FOR UPDATE SKIP LOCKED` from Python"
affects: [21-07 (reads last_stage, progress and last_error this writes), 22 (registers the handlers, adds DATABASE_URL to compose, and measures the pool)]

tech-stack:
  added: []
  patterns:
    - "Drive the transitions, reimplement none of them: the only SQL in the runtime is the three statements `transitions.py` has no home for"
    - "A heartbeat thread with its OWN connection — a shared psycopg2 connection shares its TRANSACTION, so a heartbeat commit would commit the handler's half-written work"
    - "Fail closed BEFORE reading configuration, so the message an operator sees is about the real problem rather than the first missing environment variable"
    - "Simulate a dead worker with a heartbeat interval longer than the lease, rather than a `disable_heartbeat` flag that production never exercises"
    - "Count the CALLS of a write callback, not the rows it leaves: a fenced write rolls its rows back either way, so only the call count distinguishes 'refused' from 'never attempted'"
    - "A handler that must not run RECORDS that it ran; raising is not enough, because the runtime catches every handler exception and turns it into `fail`"

key-files:
  created:
    - services/workers/workers/jobs/runtime.py
    - services/workers/workers/jobs/handlers.py
    - services/workers/workers/__main__.py
    - services/workers/tests/isolation/test_job_worker_runtime.py
  modified:
    - services/workers/workers/jobs/__init__.py
    - .planning/ROADMAP.md
    - .planning/STATE.md
    - .planning/ISSUES.md

key-decisions:
  - "`should_abort()` is true for BOTH a lost lease and a shutdown, but only the LOST LEASE stops the completion. A handler needs one signal meaning 'wind down'; the worker needs two meanings, because 'finish the current job' (the plan's shutdown rule) and 'write nothing' (the plan's abort rule) are contradictory if one flag drives both. A handler that stops without finishing must RAISE — the worker cannot tell an unfinished return from a finished one, and a shutdown is not a reason to write `completed` over work that did not happen."
  - "A dead worker is simulated with `heartbeat=30s` against a `lease=2s`, not with a test hook. A process that has died stops extending its lease; a heartbeat interval longer than the lease is indistinguishable from that, from the database's side, and it exercises the production code path rather than a second one that only tests run."
  - "The sweeper runs on the heartbeat's schedule, from inside the claim loop. It therefore PAUSES while that worker is running a job — with every worker busy, nothing sweeps until one frees up. That is acceptable because the sweeper is a backstop for a worker that died, not a deadline, and adding a third thread to do it would be a second connection per worker for a job that is two indexed `UPDATE`s."
  - "`report_progress` runs on the worker's MAIN connection, which is idle for exactly as long as a handler is running: every transition opens and closes its own transaction, and the handler runs between two of them. `_unscoped`'s idle precondition turns a future violation into a loud `RuntimeError` rather than a silent commit of someone else's work."
  - "The heartbeat opens its connection per JOB, inside the thread, rather than once per worker. A connection that breaks mid-job then heals at the next job instead of silently never beating again, and a job lasts minutes, so the connect is free."
  - "`UnknownJobType` is a named exception rather than a `RuntimeError`, so `last_error` reads `UnknownJobType: no handler for full_ingest`. `sanitize_error` keeps the class name (21-05), and the class is the half that says whether this was a deployment mistake or a job that genuinely failed."
  - "The entrypoint exits 2, not 1. A scheduler with `restart: on-failure` treats 0 as healthy, and 1 is what an unhandled exception already produces — so 1 would be indistinguishable from a crash in the logs. 2 means 'this build is configured not to run'."
  - "The refusal messages are ASCII. They are read out of a container's stderr, whose encoding is whatever the host decided, and a refusal that raises `UnicodeEncodeError` on its way out is a refusal nobody can read."

issues-created: []
issues-closed: []
review: pending
duration: ~4h
completed: 2026-09-16
---

# Phase 21 Plan 06: the worker runtime

**There is a worker process now, and it refuses to run.** `python -m
workers` starts, finds `workers.jobs.handlers.REGISTRY` empty, says why, and
exits 2 — before it reads a single environment variable. Everything behind
that refusal is built and tested: the claim loop, the claim-time
installation check, the heartbeat that extends the lease and notices a
supersede, the sweeper's schedule, cooperative abort and graceful shutdown.

**Nothing claims a real job before Phase 22**, which is the whole point.
21-03 and 21-04 put real work in the queue; a worker with no handler would
claim it, fail it five times and dead-letter it, turning a queue that was
merely waiting into one that has to be repaired by hand.

**`FOR UPDATE SKIP LOCKED` is pinned from Python at last.** 21-05 left that
gap deliberately and named this plan's barrier test as what would close it.
It does: eight threads, their own connections, released together, five
rounds on a warm pool, and the mutation that deletes the clause fails it.

## The API

`services/workers/workers/jobs/runtime.py`, plus `handlers.py` (the
registry) and `workers/__main__.py` (the entrypoint).

```python
class JobContext:
    job: Job
    worker_id: str
    def should_abort(self) -> bool
    def report_progress(self, stage: str, progress: dict | None = None) -> bool

WriteResults = Callable[[cursor], None]
Handler      = Callable[[JobContext], WriteResults | None]

class Worker:
    def __init__(self, dsn: str, handlers: Mapping[str, Handler], *,
                 worker_id: str | None = None,
                 lease=timedelta(minutes=5),
                 heartbeat=timedelta(seconds=60),
                 idle_poll=timedelta(seconds=5),
                 suspended_defer=timedelta(minutes=60)) -> None
    def run(self, stop: threading.Event) -> None

class UnknownJobType(Exception): ...

# handlers.py
REGISTRY: dict[str, Handler] = {}       # empty; Phase 22 fills it
```

**The loop, until `stop` is set:**

1. **Sweep**, if a heartbeat interval has passed since the last sweep.
2. **Claim.** Nothing claimable → wait `idle_poll`, jittered ±20%, or until
   `stop`.
3. **No handler for the job type** → `fail` with `UnknownJobType`.
4. **Check the installation** (table below) → `abandon`, `defer`, or go on.
5. **`mark_started`**, and never before step 4.
6. **Start the heartbeat thread**, on its own connection.
7. **Run the handler.** Returned → `complete` with its `write_results`,
   unless the lease was lost meanwhile, in which case write nothing.
   Raised → `fail`. `LeaseLost` from either → log a warning and carry on.
8. **Stop the heartbeat thread** and join it.

The loop body is guarded: a database blip while claiming or sweeping logs
one line and waits an idle poll rather than killing the process. A
container that exits on the first transient error restarts into the same
error, and the restart loop is what an operator ends up debugging instead
of the error.

## The three statements this layer owns

Everything else is 21-05's, driven rather than copied. These three have no
home in `transitions.py`, and each carries the same two-part fence every
terminal write there carries.

| Statement | What it is for | Fence |
|---|---|---|
| `HEARTBEAT_SQL` | extend the lease; zero rows means the lease is gone | `id + lease_owner + state = 'running'` |
| `INSTALLATION_SQL` | the claim-time read, tenant-scoped, `LEFT JOIN github_installations` | tenant scope |
| `PROGRESS_SQL` | `last_stage` / `progress` from a handler | `id + lease_owner + state = 'running'` |

**The heartbeat extends to `NOW() + lease`, not by `lease`.** An interval
added to the existing expiry would drift further out every beat, and a
stalled worker would keep its job for as long as it stayed stalled.

**`PROGRESS_SQL` is fenced because `last_stage` is L2's resumability
breadcrumb** — "skip a clone we already completed on a retry". A reclaimed
worker writing ITS stage onto the new attempt's row would make that attempt
skip work nobody has done.

**`INSTALLATION_SQL` is a LEFT JOIN, not an inner one,** because "no
`installation_id` at all" and "an `installation_id` pointing at a row this
tenant cannot see" are different sentences in `last_error`, and an inner
join would collapse both into "no row" alongside "the repository is gone".

## The timing defaults, and why

All four are constructor parameters, so the tests run in seconds and
nothing in production passes them.

| Parameter | Default | Why that number |
|---|---|---|
| `lease` | **5 minutes** | 21-CONTEXT L3. Recovery from a dead worker is bounded by it, and an ingest takes minutes — a lease as long as the worst-case job would strand a repository for that long. **The same five minutes as 20-05's `abandonedProcessingAfter`** (`github_webhook.go:270`), deliberately: both answer "how long may a dead process hold work before something else may take it", and an operator reading one number should not have to learn a second. L3 says to keep them consistent and to say why if one changes. |
| `heartbeat` | **60 seconds** | One twelfth of the lease, so four consecutive beats may be lost before the job becomes reclaimable. |
| `idle_poll` | **5 seconds** | At ~0.1 jobs/second (21-RESEARCH) this is the difference between noticing a push in five seconds and hammering the claim query. `LISTEN/NOTIFY` is the upgrade if it ever matters; it does not yet. Jittered ±20% so a pool restarted together does not stay in lockstep. |
| `suspended_defer` | **60 minutes** | An unsuspend is noticed within the hour with no webhook work needed. GitHub does send `installation.unsuspend`, but 21-04 deliberately does not make the queue depend on it — a missed delivery would strand the repository forever. |

**The sweeper has no interval of its own:** it runs on the heartbeat's
schedule, because the useful frequency for a backstop is "about as often as
a live worker proves it is alive", and it is two indexed `UPDATE`s.

## Installation state, checked at claim time

21-CONTEXT L2 is explicit that `ingestion_jobs.payload` carries no
credentials and no installation id: two reconnects racing produce ONE job
(L8), and a job that had snapshotted the loser's installation would
silently use stale credentials. So the worker reads the repository's
**current** installation after it claims, under
`require_tenant(conn, job.organization_id)`.

| What the worker finds | Action | `sync_state` | `attempts` |
|---|---|---|---|
| `installation_id IS NULL` | `abandon` → `superseded` | `never_synced` | left alone |
| the installation row is not visible under this tenant | `abandon` → `superseded` | `never_synced` | left alone |
| `uninstalled_at` set | `abandon` → `superseded` | `never_synced` | left alone |
| `suspended_at` set | `defer` 60 minutes | **unchanged** | **returned** |
| otherwise | `mark_started`, then the handler | `syncing` | — |

Four things about that table are worth their sentence.

- **`mark_started` runs AFTER the check, never before it.** That ordering is
  why `claim` deliberately writes no projection (21-05): a job under a dead
  installation must be abandoned to `never_synced` without ever having told
  the UI it was `syncing`. Mutation **M5** moves the call earlier and the
  suspended test catches it — a deferred job would otherwise sit at
  `syncing` for as long as the App stayed suspended, because `defer` writes
  no projection.
- **`uninstalled_at` is tested before `suspended_at`,** because a reinstall
  clears both (21-04): a row carrying each of them is uninstalled, not
  suspended.
- **Abandon, not fail.** Nothing went wrong and nothing should retry.
  `github_webhook_events.go`'s uninstall stand-down says "'failed' is
  deliberately not used — nothing failed, and the queue must not retry
  these", and `abandon` writes exactly what that handler writes.
- **Defer, not fail.** `DEFER_SQL` hands the claim's attempt back, so a week
  of suspension cannot walk a healthy repository to `dead` — which a `fail`
  here would do in five hours.

**This is ISS-033's ending, built rather than planned.** That issue is
filed on the premise that this check exists — "if it does not, this issue's
priority rises with it". It does; the issue is updated to say so, and its
producer-side fix stays open as defence in depth.

## The heartbeat, and the thread it runs on

**Its own connection.** psycopg2 connections may be shared between threads,
and sharing one here would be a data-loss bug rather than a slow one: a
connection shares its **transaction**, so a heartbeat's commit would commit
whatever `complete` had half-written on the main connection, or its
rollback would throw the results away. The thread opens its own connection
and closes it when the job ends.

**Both halves of the fence, and the second one is what does the work.**
`supersedeLiveSQL` deliberately leaves the lease attached to the row it
supersedes, so `lease_owner = %s` alone still matches one; `state =
'running'` is what turns a supersede into zero rows. Zero rows sets the
abort flag, stops the beating and returns.

**A database error is not a lost lease.** The beat logs one line and
continues: the lease is still ours as far as the database is concerned, and
if the connection stays broken the lease simply expires and another worker
reclaims — which is the crash path the sweeper and the claim's reclaim
branch already handle.

**The first beat is one interval in, not immediate.** `claim` has just set
the lease, so an immediate beat would write the value it just read. It is
also what lets a test simulate a dead process with no hook in the runtime:
a heartbeat interval longer than the lease means no beat ever lands before
the lease expires, which is what a process that stopped running looks like.

## Shutdown

`run(stop)` returns after the job in flight reaches a terminal write. It
never abandons a job mid-write and it never kills a handler.

`should_abort()` is true while the worker is shutting down **as well as**
when the lease is lost, because a handler wants one signal meaning "wind
down". **The two are not the same to the worker,** and the completion
decision uses only the lease:

- a handler that **finishes** during a shutdown gets its job `completed` —
  the plan's "finish the current job";
- a handler that **returns without finishing** must **raise**, so the
  attempt is recorded and retried. The worker cannot tell an unfinished
  return from a finished one, and a shutdown is not a reason to write
  `completed` over work that did not happen.

On the lease-lost path it makes no difference — the worker writes nothing
either way.

## `python -m workers`, and why the order of its two checks matters

```
1. handlers   -- REGISTRY empty?      -> log why, exit 2
2. config     -- DATABASE_URL unset?  -> log why, exit 2
3. SIGTERM/SIGINT set `stop`, then run
```

**The order is what makes the compose service say something useful.** The
`workers` service's environment is `ENV=development` and nothing else —
there is no `DATABASE_URL`. Read configuration first and the container dies
complaining about a missing DSN, which is a true statement about the wrong
problem and would send whoever reads it off to add one. Check the handlers
first and it says the thing that is actually true: there is no work this
build knows how to do.

The entrypoint test runs the subprocess **both with and without**
`DATABASE_URL` for exactly that reason, and mutation **M9** — which swaps
the two checks — fails the run without a DSN.

`Worker.__init__` refuses an empty map with `ValueError` as a backstop for
a caller who builds one directly.

## The tests

`services/workers/tests/isolation/test_job_worker_runtime.py`, **19 tests**
against the session's real `postgres:16-alpine`. Lease 2 s, heartbeat
0.5 s, idle poll 0.1 s, suspended deferral 1 s. Nothing sleeps waiting for
a state change: `until()` and `job_when()` poll against a deadline, so a
slow machine takes longer rather than failing.

**Three premises are asserted rather than assumed,** because each is a way
the whole file could pass for the wrong reason.

- **The worker connects as `rag_doc_app`.** A `Worker` opens its own
  connections from a DSN, so there is no cursor for a fixture to `SET ROLE`
  on; the DSN carries `options=-c role=rag_doc_app`. A superuser bypasses
  row-level security even under FORCE, and the claim-time installation read
  would then see every tenant's rows.
  `test_the_worker_connects_as_the_unprivileged_app_role` checks
  `current_user`, `rolsuper` and `rolbypassrls`.
- **My job is the only claimable row.** A `Worker` claims queue-wide — no
  organization filter, no row-level security — so it would happily pick up
  another test's job and run this test's handler on it. Every test
  backdates its job to `CLAIM_TEST_EPOCH` and calls `assert_only_claimable`.
  ⚠ That helper **carries its own copy of the claimable predicate** and is
  therefore blind to a mutation of `CLAIM_SQL`'s; it is a premise, not
  coverage, and the docstring says so.
- **The installation shape each test needs.** Every repository in
  `with_two_orgs` starts with `installation_id IS NULL`, which is the
  ABANDON branch — a suite built on the bare fixture would take that branch
  everywhere and could not tell `defer` from `abandon` from `run`. Every
  test that is not about a dead installation calls `link_installation`
  first, and the suspended and uninstalled tests assert what the read will
  find before they start a worker.

**Two assertions are sharper than they look, and both come from asking what
the fixtures could not distinguish.**

- **The supersede test counts the write callback's CALLS, not its rows.**
  Remove the abort check before `complete` and the probe row is *still*
  absent — `complete`'s fence matches nothing and `LeaseLost` rolls the
  callback's write back. What differs is whether the callback RAN.
  Without `Probe.calls`, mutation **M2** survives the whole suite.
- **A handler that must not run RECORDS that it ran.** A bare `assert False`
  handler would be a test that cannot fail: `_invoke` catches every
  exception a handler raises and turns it into `fail`, swallowing the
  assertion. `NeverCalled` appends the job id and the test reads the list.

**And one test reads an actual `LogRecord`.** A supersede leaves nothing
behind in the database that says a worker was stopped — the row looks the
same whether the worker noticed or is still grinding away — so the log line
is the whole of the evidence. `test_a_supersede_is_reported_in_the_log`
asserts it exists at WARNING and names the job, the organization and the
repository. Without it, mutation **M13** (the heartbeat says nothing)
survives. This is 21-05's lesson applied: a rule stated in a docstring is
not a rule until one test reads the thing the docstring is about.

### The claim race

Eight threads, each with its **own** connection — a shared connection would
serialise them inside psycopg2 and there would be no race to lose —
released together through a `threading.Barrier`, over **five rounds** on a
pool that is warm after the first. A single cold round proves nothing
(21-CONTEXT): the first round pays for connection setup, which spreads the
threads out and hides exactly the contention the test exists to create.

Two witnesses per round: **exactly one claim returns a job**, and
**`attempts == 1`**. The second matters because every claim that touched
the row incremented it, so eight claims read as eight even if the test
misread a `RETURNING`.

Without `FOR UPDATE SKIP LOCKED` the inner `SELECT` takes no lock, so all
eight `UPDATE`s resolve to the same id, queue on the row lock, and each
re-checks `id = <that id>` after the winner commits — which is still true.
Every one of them then claims. Mutation **M3** is exactly that, and it is
killed **by this file only**: the 45 transition tests all still passed
under it, which is 21-05's "nothing here runs two claims concurrently"
measured rather than asserted.

### Crash → reclaim → dead-letter

`max_attempts` forced to 3. Three workers built with `heartbeat=30s`
against a `lease=2s`, each blocked in its handler — no beat ever lands, so
from the database's side each is a process that has died. Worker A claims,
its lease expires, B reclaims, C reclaims, and `attempts` reaches 3. A
fourth worker — alive, sweeping on the heartbeat schedule, and unable to
claim the job at all now that `attempts < max_attempts` is false — moves it
to `dead`.

Then the three stale handlers are released. Each returns its probe, each
`complete` calls it and hits the fence, each raises `LeaseLost`, and every
probe row is rolled back. The test asserts all three callbacks ran exactly
once and that none of their rows survived — a stale worker attempted its
write and the database refused it, which is a stronger statement than "no
rows appeared".

## Mutation results

**14 mutations, 14 killed, no survivors.** Each was applied to a COPY of
`services/workers` plus `services/backend/migrations` — the conftest
resolves migrations at `parents[3]`, so the copy is `<scratch>/workers`
beside `<scratch>/backend/migrations`. The harness rebuilds the copy from
the committed worktree before every run, asserts the pattern matched
**exactly once**, and asserts the mutated text is present **and** the
original absent afterwards. The committed tree was never mutated.

Baseline on the copy: **64 passed** (19 runtime + 45 transitions).

### The plan's four

| # | Mutation | Result |
|---|---|---|
| M1 | The heartbeat loses its fence (`WHERE id = %s AND %s IS NOT NULL`) | **Killed: 2** — `a_supersede_mid_run_aborts_the_handler_and_writes_nothing` (the handler runs to its 20 s patience instead of aborting in about two beats) and `a_supersede_is_reported_in_the_log` |
| M2 | The abort check before `complete` is removed | **Killed: 1** — `a_supersede_mid_run_aborts_the_handler_and_writes_nothing`, **on `Probe.calls`, not on a row.** The probe row is absent either way |
| M3 | `CLAIM_SQL` drops `FOR UPDATE SKIP LOCKED` | **Killed: 1** — `eight_threads_racing_for_one_job_produce_exactly_one_claim`. ⚠ **The 45 transition tests all still passed**, which measures 21-05's claim rather than repeating it |
| M4 | A suspended installation is `fail`ed instead of `defer`red | **Killed: 1** — `a_suspended_installation_defers_without_consuming_an_attempt`, on `attempts == 0` |

### The guards this plan added

| # | Mutation | Result |
|---|---|---|
| M5 | `mark_started` runs BEFORE the installation check | **Killed: 1** — the suspended test, on `sync_state == 'pending'`: a deferred job would otherwise sit at `syncing` for as long as the App stayed suspended, because `defer` writes no projection |
| M6 | The heartbeat thread shares the loop's connection | **Killed: 1** — `the_heartbeat_thread_opens_its_own_connection`, and **only** that one. Every other test passed under it, which is the measured form of "this failure mode is a race, not a result" |
| M7 | `PROGRESS_SQL` loses its fence | **Killed: 1** — `a_progress_report_from_a_worker_that_lost_its_lease_is_refused` |
| M8 | The loop never sweeps | **Killed: 1** — `an_expired_lease_is_reclaimed_until_the_job_dead_letters` |
| M9 | The entrypoint reads `DATABASE_URL` before checking the handlers | **Killed: 1** — `the_entrypoint_refuses_to_start_without_handlers[False]`, the run with no DSN. The `[True]` case correctly survives, because with a DSN set the order makes no difference |
| M10 | A `Worker` accepts an empty handler map | **Killed: 1** — `a_worker_refuses_an_empty_handler_map` |
| M11 | `mark_started` is never called | **Killed: 1** — `the_heartbeat_extends_the_lease_of_a_job_that_outlives_it`, the only test that can see `syncing`, because every other one reads `sync_state` after a terminal state has overwritten it |
| M12 | An uninstalled installation is run instead of abandoned | **Killed: 1** — `a_dead_installation_abandons_the_job[uninstalled]`. This is ISS-033's wrong ending, as a mutation |
| M13 | The heartbeat says nothing when it loses the lease | **Killed: 1** — `a_supersede_is_reported_in_the_log`, the only test in the file that reads a `LogRecord` |
| M14 | The entrypoint refuses with exit code 1 rather than 2 | **Killed: 2** — both entrypoint cases |

**Four of these found real gaps while the file was being written**, which
is why they are here at all: M2 (the call count, not the row), M6 (nothing
could observe the connection split), M11 (nothing could observe `syncing`)
and M13 (nothing read a log record). Each was a test that was right about
what it asserted and wrong about what its fixtures could distinguish —
21-04's and 21-05's finding, twice more.

## What this plan does NOT pin, so nobody assumes it does

Following 21-05's practice of naming the gaps rather than leaving them to
be discovered.

- **The heartbeat's own connection is pinned STRUCTURALLY, by counting
  connections, and that is admitted in the test's docstring.** The
  behaviour it protects cannot be provoked reliably: `require_tenant` and
  `_unscoped` both refuse a connection that is mid-transaction, so the
  shared version fails loudly on some interleavings and silently on others,
  and a test that waits for the unlucky one is a flake. Counting is what
  makes the invariant killable by a mutation at all (**M6**).
- **A row carrying BOTH `suspended_at` and `uninstalled_at`.** No fixture
  builds one, so swapping the order of those two branches would survive.
  It is reachable only if GitHub suspends and then uninstalls, and a
  reinstall clears both — the ordering is right, and it is not tested.
- **Signal delivery.** `_install_signal_handlers` is `# pragma: no cover`:
  the `stop` event's effect is tested (graceful shutdown), but nothing
  here sends the process a SIGTERM.
- **The idle poll's jitter.** The ±20% is not asserted; it is a herd
  control, not a correctness property.
- **The sweeper pauses while its worker is busy.** With every worker
  running a job, nothing sweeps until one frees up. That is a property, not
  a gap — the sweeper is a backstop for a worker that died, not a deadline
  — but it is written down so the next reader does not treat sweeps as
  periodic under load.

## Deviations from the plan

1. **A dead worker is simulated with a long heartbeat interval, not a test
   hook.** The plan says "its heartbeat is disabled via a test hook". A
   `heartbeat` longer than the `lease` is indistinguishable from a dead
   process from the database's side, it needs no second code path, and it
   uses a constructor parameter the API already has. Recorded in the test's
   docstring.
2. **`should_abort()` covers shutdown as well as a lost lease, and only the
   lost lease stops the completion.** The plan's step 7 ("unless the abort
   flag is set, write nothing") and its shutdown rule ("finish the current
   job") are contradictory if one flag drives both. See "Shutdown" above
   for the split and the handler contract that comes with it.
3. **`last_error` for an unhandled job type reads
   `UnknownJobType: no handler for full_ingest`,** not the plan's bare
   `no handler for <type>`. `sanitize_error` prefixes the exception class
   by design (21-05), and the class is the half that says whether this was
   a deployment mistake or a job that genuinely failed.
4. **`report_progress` takes an optional `progress`,** so a handler that
   only wants to record a stage does not have to invent a dict.
5. **Seven tests beyond the plan's list.** The three premise tests (the app
   role, the empty-handler `ValueError`, the heartbeat's own connection),
   the two progress tests that pin `PROGRESS_SQL`'s fence, the log-record
   test, and a handler-raises test that proves the redaction reaches
   `last_error` through the runtime path. Four of them exist because a
   mutation would otherwise have survived.
6. **The mutation count is 14, not 4.** The extras follow 21-02's, 21-03's
   and 21-05's practice, and four of them found real gaps: M2, M6, M11 and
   M13, each of which was a guard nothing could observe until the test was
   changed.
7. **`REGISTRY` and the runtime types are exported from `workers.jobs`.**
   The plan lists `workers/jobs/__init__.py` among the files; this is what
   changed in it, so Phase 22 can `from workers.jobs import REGISTRY,
   JobContext, Handler` rather than reaching into submodules.

## The Phase 22 hand-off

Three things, and the first two are what make the entrypoint start.

1. **Register the handlers.**
   ```python
   from workers.jobs.handlers import REGISTRY
   REGISTRY["full_ingest"] = run_full_ingest
   REGISTRY["incremental"] = run_incremental
   ```
   The two keys are fixed by migration 000014's
   `CHECK (job_type IN ('full_ingest','incremental'))`, not by convention.
   A handler takes a `JobContext` and returns a `write_results` callback —
   the callback is what makes the chunks and the completion commit together
   or not at all, which is most of the argument for putting the queue in
   Postgres (L1).
2. **Add `DATABASE_URL` to the compose `workers` service.** It has
   `ENV=development` and nothing else today. No compose change was made
   here, deliberately: the service exits 2 with the handler message until
   Phase 22 is ready, which is the safe ending.
3. **Measure the pool.** **One job at a time per process; scale by
   processes.** The claim is safe across any number of them — that is what
   the barrier test now proves from Python as well as Go. How many
   processes is still an **open input**: 21-RESEARCH withdrew the
   arithmetic that answered it (it applied a full-ingest duration to push
   jobs and leaned on a source it files as unread), and nothing has yet
   ingested end to end. Measure the incremental duration first, then size
   from it. Each worker holds **two** connections while a job runs — the
   loop's and the heartbeat's — so a pool of N wants 2N.

Also still Phase 22's, from 21-05 and unchanged here: wire the pipeline
through `resolve_ingestion_run` / `attach_ingestion_run` (`PostgresWriter`
still has no `ON CONFLICT`), and make chunk writes idempotent per run —
the completion transaction removes torn writes, not duplicated work
(**ISS-027**).

## Verification

| Check | Command | Result |
|---|---|---|
| Workers, CI's environment | from `services/workers`: `REDIS_URL=redis://localhost:63796/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, **no `DATABASE_URL`**, no reachable `.env` (only `.env.example`), a fresh venv built from `requirements.txt` | **273 passed**, 0 failed, 0 skipped (19 pre-existing `utcnow` deprecation warnings). `main` is 254, so this plan adds **19** |
| This plan's tests alone | `pytest tests/isolation/test_job_worker_runtime.py -q` | **19 passed** |
| Repeated — the flake check | the same command, **5 consecutive runs** | 19 passed every time: 23.87 s, 23.89 s, 23.82 s, 23.33 s, 22.09 s. **No flakes** |
| The claim race | inside that file: 8 threads, own connections, one `threading.Barrier`, **5 rounds** on a warm pool | exactly one claim and `attempts == 1` in every round, and the winning thread's id is the row's `lease_owner` |
| Mutations | 14, on a copy, each proven present in the file before the run | **14 killed, 0 survivors.** Baseline on the copy: 64 passed |
| Entrypoint | `python -m workers` from `services/workers`, with and without `DATABASE_URL` | exit **2** both times, with the handler message and no DSN message |
| Backend | `git diff --stat RAG-Doc/main..HEAD -- services/backend` | **empty.** No Go file and no migration, so `go test` was not run and the shared harness container was not rebuilt |
| Compose and Dockerfile | `git diff --stat RAG-Doc/main..HEAD -- docker-compose.yml services/workers/Dockerfile` | **empty.** The existing `CMD ["python", "-m", "workers"]` now resolves, and exits 2 until Phase 22 |
| Lint | `flake8` on the four new and changed files | clean (the repo has pre-existing findings elsewhere; none is in this diff) |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | `{"missing": [], "skipped": [], "covered": []}` — no route line changed |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

**Containers.** The Python isolation harness starts its own
`postgres:16-alpine` per pytest session and stops it on teardown, and the
mutation copy does the same, so every mutation run got a fresh database.
One scratch Redis (`rag2106-redis`, port **63796**) was started for the
semantic-cache tests and removed afterwards. **The docker-compose Postgres
(port 5434) and Qdrant were never started or touched**, and the shared Go
harness container `rag-doc-isolation-tests` was left alone.

---
*Phase: 21-ingestion-job-infrastructure — 6 of 7 plans*
*Completed: 2026-09-16*
