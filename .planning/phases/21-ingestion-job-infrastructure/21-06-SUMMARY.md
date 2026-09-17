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
  - "workers.jobs.runtime — `Worker`, `JobContext`, `Handler`, `WriteResults`, `Unfinished`, `UnknownJobType`, `DatabaseUnavailable`, and the three statements this layer owns: HEARTBEAT_SQL, INSTALLATION_SQL, PROGRESS_SQL"
  - "workers.jobs.handlers.REGISTRY — empty in Phase 21, with the Phase 22 recipe in its docstring"
  - "workers/__main__.py — `python -m workers`, which FAILS CLOSED with exit 2 while the registry is empty, before it reads any configuration"
  - "the claim-time installation check: abandon / defer 60 min / run, with `mark_started` after it"
  - "30 integration tests against a real PostgreSQL 16, including the barrier claim race that pins `FOR UPDATE SKIP LOCKED` from Python, three that kill a backend with `pg_terminate_backend`, and one that connects to a socket which accepts and never speaks"
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
    - "An exception type as the contract: three endings a handler can choose, so the one that needs a special response cannot be reached by accident"
    - "Never assert in a context manager's `finally` -- it REPLACES the exception already propagating and hides the real failure behind a teardown symptom"
    - "Tag each connection with an `application_name`, so `pg_stat_activity` says which is which and a test can kill exactly one"
    - "Tie a free pass to the condition that makes it free: an ending that neither consumes an attempt nor delays a retry must be reachable only where something else bounds it"
    - "A test for an unbounded loop must be bounded BY CONSTRUCTION -- the handler counts its own calls and changes behaviour at a cap, so the assertion fails instead of the clock"
    - "Give every connect a `connect_timeout`: a refused connection fails fast and a DROPPED one blocks, and the timeout is what turns a silence into an error a policy can act on"

key-files:
  created:
    - services/workers/workers/jobs/runtime.py
    - services/workers/workers/jobs/handlers.py
    - services/workers/workers/__main__.py
    - services/workers/tests/isolation/test_job_worker_runtime.py
  modified:
    - services/workers/workers/jobs/__init__.py
    - services/workers/workers/jobs/transitions.py
    - services/workers/workers/db/tenant.py
    - .planning/phases/21-ingestion-job-infrastructure/21-07-PLAN.md
    - .planning/ROADMAP.md
    - .planning/STATE.md
    - .planning/ISSUES.md

key-decisions:
  - "THREE ENDINGS FOR A HANDLER, and the type is the contract: `return` means done (`complete`), `raise Unfinished` means stopped part-way (`defer` DURING A SHUTDOWN, attempt handed back; `fail` at any other time -- the free pass is bounded to the one trigger that is self-limiting, after the second review measured 193 re-claims in 6 seconds past both of the phase's backstops), anything else means failed (`fail`). The first cut had two, folded shutdown into `should_abort()` and left the rest to a docstring; PR #42's review reproduced a Phase-22-shaped handler stopping after `clone` and being written `state=completed last_stage=parse sync_state=synced`, and ruled against it. `should_abort()` is now lease-only, `is_shutting_down()` carries the other signal, and `defer` rather than `fail` means an operator's restarts cannot walk a healthy repository towards `dead`."
  - "The loop RECONNECTS from the FIRST connect onwards, with a bounded backoff and a `connect_timeout`, and gives up loudly. psycopg2 connections do not self-heal and the loop's was opened once outside the `while`: PR #42's review killed its backend and measured 44 identical errors in 15s with the next job never claimed and the process still alive, so no restart policy could fire. Ten attempts, 1s doubling to 30s, then `DatabaseUnavailable` out of `run` and exit 1 -- distinct from the entrypoint's 2, which means `this build is configured not to run`."
  - "The heartbeat GIVES UP on two rules and REOPENS in between. A single dropped connection is a blip and is reconnected in place, because giving up on the first `InterfaceError` would throw away minutes of ingest; but once a full lease has passed with no beat landing (or `MAX_HEARTBEAT_FAILURES` in a row) the lease has demonstrably expired, so the abort flag goes up. Without it the review measured a handler working on for the rest of its job on a lease somebody else already held."
  - "`max_job_duration` exists and defaults to **None**. A hung handler otherwise has its lease extended forever, so nothing can reclaim the job and that worker's sweeper never runs again either. The mechanism is built and tested; the NUMBER is deliberately not invented, because it is a multiple of a typical ingest and nothing has ingested end to end yet -- the same mistake 21-RESEARCH already made once with pool sizing."
  - "A dead worker is simulated with `heartbeat=30s` against a `lease=2s`, not with a test hook. A process that has died stops extending its lease; a heartbeat interval longer than the lease is indistinguishable from that, from the database's side, and it exercises the production code path rather than a second one that only tests run."
  - "The sweeper runs on the heartbeat's schedule, from inside the claim loop. It therefore PAUSES while that worker is running a job — with every worker busy, nothing sweeps until one frees up. That is acceptable because the sweeper is a backstop for a worker that died, not a deadline, and adding a third thread to do it would be a second connection per worker for a job that is two indexed `UPDATE`s."
  - "`report_progress` runs on the worker's MAIN connection, which is idle for exactly as long as a handler is running: every transition opens and closes its own transaction, and the handler runs between two of them. ⚠ WHAT PROTECTS IT IS NOT THE IDLE PRECONDITION, and the first cut credited the wrong guard. PR #42's review measured it: psycopg2 begins its transaction LAZILY, so after one `_unscoped` scope is entered `transaction_status` still reads IDLE and a second scope sails past the check. What refuses it is psycopg2's own `with conn:` reentrancy guard, `ProgrammingError: the connection cannot be re-entered recursively`. The conclusion is unchanged and now stated correctly: the failure is LOUD, not silent."
  - "The heartbeat opens its connection per JOB, inside the thread, AND reopens it in place mid-job. Per-job alone only heals at the next job, which does nothing for the job in flight -- PR #42's review is where that gap was measured. A job lasts minutes, so the connect is free either way."
  - "`UnknownJobType` is a named exception rather than a `RuntimeError`, so `last_error` reads `UnknownJobType: no handler for full_ingest`. `sanitize_error` keeps the class name (21-05), and the class is the half that says whether this was a deployment mistake or a job that genuinely failed."
  - "The entrypoint exits 2, not 1. A scheduler with `restart: on-failure` treats 0 as healthy, and 1 is what an unhandled exception already produces — so 1 would be indistinguishable from a crash in the logs. 2 means 'this build is configured not to run'."
  - "The refusal messages are ASCII. They are read out of a container's stderr, whose encoding is whatever the host decided, and a refusal that raises `UnicodeEncodeError` on its way out is a refusal nobody can read."

issues-created: []
issues-closed: []
review: "PR #42, TWO ROUNDS. Round 1 CHANGES REQUESTED: three important findings, all reproduced against a real PostgreSQL 16 and all fixed -- a shutdown could write `completed` over unfinished work, `Worker.run` never reconnected, and the heartbeat never gave up -- plus five minors and both nits. Round 2 APPROVE WITH NITS: the reviewer re-ran its own I1/I2/I3 reproductions rather than trusting the summary and confirmed all three, then found five more, all fixed here -- `Unfinished` could re-claim without bound (193 times in 6 seconds, past BOTH phase backstops), the FIRST connect bypassed the reconnect policy, no `connect_timeout` left the heartbeat give-up rules blind to a hang, `require_tenant` still carried the misleading message `_unscoped` had been fixed for, and `max_job_duration` was silent in both directions. Three of the PR's claims were independently re-measured IN ITS FAVOUR across the two rounds (the both-flags installation row; the heartbeat holding a long lease, 110 samples, minimum headroom 1.426s; and M2 killed on the callback's call count rather than on a row), and one summary claim was withdrawn as overstated."
duration: ~4h, plus ~3h applying PR #42's first review and ~2h applying its second
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
    def should_abort(self) -> bool        # the LEASE is gone; nothing will be written
    def is_shutting_down(self) -> bool    # the WORKER is going; the job is still ours
    def report_progress(self, stage: str, progress: dict | None = None) -> bool

WriteResults = Callable[[cursor], None]
Handler      = Callable[[JobContext], WriteResults | None]

class Worker:
    def __init__(self, dsn: str, handlers: Mapping[str, Handler], *,
                 worker_id: str | None = None,
                 lease=timedelta(minutes=5),
                 heartbeat=timedelta(seconds=60),
                 idle_poll=timedelta(seconds=5),
                 suspended_defer=timedelta(minutes=60),
                 max_job_duration: timedelta | None = None) -> None
    def run(self, stop: threading.Event) -> None   # raises DatabaseUnavailable

class Unfinished(Exception): ...          # stopped part-way  -> defer
class UnknownJobType(Exception): ...      # no handler        -> fail
class DatabaseUnavailable(RuntimeError):  # reconnect gave up -> exit 1

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
   Raised `Unfinished` → `defer`, with the attempt handed back. Raised
   anything else → `fail`. `LeaseLost` from either → log a warning and
   carry on.
8. **Stop the heartbeat thread** and join it.

The loop body is guarded: a database blip while claiming or sweeping logs
one line and waits an idle poll rather than killing the process. A
container that exits on the first transient error restarts into the same
error, and the restart loop is what an operator ends up debugging instead
of the error. ⚠ **But "continuing" has to mean it, and in the first cut it
did not** — see "The loop's connection reconnects, and gives up loudly".

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
| `heartbeat` | **60 seconds** | **One fifth** of the lease, so four consecutive beats may be lost before the job becomes reclaimable: beats due at 60, 120, 180 and 240 all miss and the lease expires at 300. (The first cut said "one twelfth", which would be 25 s. PR #42's review caught it; the conclusion was right and only the fraction was wrong, and it matters because this is the sentence someone re-tuning the numbers would use to re-derive them.) |
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

**One database error is not a lost lease; a run of them is.** The first cut
treated every heartbeat error as a blip, on the argument that "the lease is
still ours as far as the database is concerned". That is true for one
missed beat and stops being true after `lease` seconds of them — and PR
#42's review measured the difference: it killed the heartbeat's backend
mid-job and watched the lease expire underneath the running handler while
`should_abort()` stayed `False`. The fence stops the corruption (the stale
`complete` is refused), so the cost is a whole ingest thrown away and then
duplicated, **unbounded in duration**. For Phase 22 that is minutes of
clone, parse and embed against a token-authenticated remote.

So the heartbeat now does two things it did not:

- **It reopens its connection in place.** A single drop is survivable, and
  the next beat reconnects and carries on. "Heals at the next job" does
  nothing for the job in flight.
- **It gives up when beats genuinely cannot land.** Once a full `lease` has
  passed with no beat landing — or `MAX_HEARTBEAT_FAILURES` in a row, a
  backstop for a very long configured lease — the lease has demonstrably
  expired, so the abort flag goes up and the thread stops.
- **⚠ Both rules are evaluated on EVERY beat, not only after an
  exception.** The first cut put them in the `except`, which made them a
  property of the control flow that happened to reach them rather than of
  the elapsed time they are about. The reopen-in-place runs **inside** the
  `try`, so a reopen that HANGS raises nothing, neither rule ran, and
  `lease_lost` was never set — I3 one layer further in, found by the second
  review.
- **And every connect carries a `connect_timeout`,** which is what turns
  that hang into an `OperationalError` the rules can already handle. The
  two fixes are belt and braces for the same hole; see "What this plan does
  NOT pin" for what the relocation alone can and cannot be tested for.

Both halves are tested, and they pull in opposite directions, so both need
a test: one dropped connection must **not** abort the handler, and a
sustained outage must.

**It also enforces `max_job_duration`,** when one is set. A handler that
hangs — a clone against an unresponsive remote with no socket timeout — is
worse than one that crashes: the beat would keep the lease alive forever,
nothing could reclaim the job, the repository would sit `syncing`, and this
worker's sweeper (same loop) would never run again either. **The default is
`None`,** because the right number is a multiple of a typical ingest and
nothing has ingested end to end yet. The mechanism is built and tested;
Phase 22 picks the number.

**⚠ And the absence is announced, which the first cut left silent.** The
startup line now carries `max_job_duration=none|<n>s`, and an unset bound
logs one WARNING: *"no upper bound on handler runtime; a hung handler will
hold its lease indefinitely and stop this worker sweeping. Set
max_job_duration."* The review's ruling, which this follows: not inventing
the number is right — the same discipline that withdrew 21-RESEARCH's pool
arithmetic — but it must not be silent, or Phase 22 can skip the hand-off
step and never know the hazard shipped. A test reads both records.

## The loop's connection reconnects, and gives up loudly

`conn = psycopg2.connect(...)` was outside the `while`, and psycopg2
connections do not self-heal: once the backend is gone the object is
permanently bad. **PR #42's review reproduced what that cost:**
`pg_terminate_backend` on the loop's backend produced **44 identical error
lines in ~15 seconds**, the next job was never claimed, and the process
stayed alive — so no `restart:` policy fired, no exit code was ever
produced, and the container looked healthy while its queue backed up behind
it. A Postgres restart, a failover or an idle-connection reaper in front of
the database is enough to trigger it. The heartbeat already reconnected per
job; the loop's was the connection that could not heal.

- **Health is checked at the top of each iteration**, before `claim` or
  `sweep` is handed the connection, so the dead-connection path never
  reaches them.
- **The FIRST connect goes through it too**, which the first cut missed:
  `run` opened its connection above the `try`, outside `_ensure_live`, so
  the ten-attempt backoff covered every connection except the one most
  likely to fail. The compose `workers` service has no `depends_on`, so on
  a stack restart this process starts before Postgres is ready **every
  time**, and the old shape raised a bare `OperationalError` past `main`'s
  `except DatabaseUnavailable` — a traceback instead of the one clean line
  the code was written to give. The fix is `conn = None`: `_ensure_live`
  already began `if conn is not None and not _is_dead(conn)` and the
  `finally` already guarded `if conn is not None`, so it **deletes** a
  special case.
- **Reconnect is bounded:** `MAX_RECONNECT_ATTEMPTS = 10`, 1 s doubling to
  30 s.
- **Every connect carries `connect_timeout = 5 s`.** A refused connection
  fails at once — which is every case the tests and the review's probes
  reached — but a path that DROPS instead (a firewall, a failing-over
  proxy, a partitioned network) blocks inside `psycopg2.connect` for the OS
  TCP timeout. See the heartbeat section: that silence was a hole in the
  give-up rules, not a latency nit.
- **Then it gives up loudly:** `DatabaseUnavailable` out of `run`, which
  `__main__` turns into **exit 1**. Distinct from the entrypoint's **2**,
  which means "this build is configured not to run" and which no restart
  can fix; 1 is what a supervisor is for.

**And the error an operator saw blamed the wrong thing.** A dead
connection's `transaction_status` is `UNKNOWN`, which `_unscoped`'s idle
precondition read as "you are inside a transaction" and reported as
psycopg2's `with conn:` not nesting — sending the reader to the wrong file.
`_unscoped` now names the `UNKNOWN` case for what it is and raises
`InterfaceError`, telling the caller to reconnect rather than to commit
something.

**The first beat is one interval in, not immediate.** `claim` has just set
the lease, so an immediate beat would write the value it just read. It is
also what lets a test simulate a dead process with no hook in the runtime:
a heartbeat interval longer than the lease means no beat ever lands before
the lease expires, which is what a process that stopped running looks like.

## Shutdown, and the handler's three endings

`run(stop)` returns after the job in flight reaches a terminal write. It
never abandons a job mid-write and it never kills a handler.

**Two questions, two methods.** The first cut had one, and PR #42's review
ruled against it:

| Method | Means | What the worker will write |
|---|---|---|
| `should_abort()` | **this job is not ours any more** — the lease is gone | nothing, whatever the handler does |
| `is_shutting_down()` | **the worker is going away; the job is still ours** | whatever the handler's ending says |

**Three endings, and the type is the contract:**

```
return               done             -> complete
raise Unfinished     stopped part-way -> defer, attempt handed back
raise anything else  failed           -> fail, attempt consumed
```

### Why the first cut was wrong, measured rather than argued

The deviation folded shutdown into `should_abort()` and left "raise if you
have not finished" to a docstring. **PR #42's review built the handler that
breaks** — Phase-22-shaped: report `clone`, work, notice the signal, stop,
report the stage it reached, return — and ran it against a real PostgreSQL
16 with SIGTERM mid-handler:

```
state=completed  last_stage=parse  progress={'files_parsed': 3}
sync_state=synced  last_synced_at=2026-09-16 22:17:04+00
```

A job row that contradicts itself, `last_synced_at` stamped, a UI saying
the repository is ingested, and the partial unique index freed so nothing
re-queues it — until the next push, which is `incremental` and will diff
against a run that never happened.

**And the justification did not hold.** The deviation claimed the plan's
step 7 ("unless the abort flag is set, write nothing") contradicted its
shutdown rule ("finish the current job"). `21-06-PLAN.md:73` defines the
flag as *lease-only*; under that definition the two rules are about
different things and there was never a contradiction. The deviation
manufactured the problem it then resolved — with a docstring, on a method
called `should_abort()`, whose obvious reading is "stop now" and whose
correct response was the one nobody would guess.

**`defer`, not `fail`, and that is the second half.** A failure consumes an
attempt and applies the backoff, so five operator-initiated restarts spread
across an ingest's retries would walk a **healthy** repository to `dead` —
the same shape `defer` exists to prevent for a suspended installation.
`Unfinished` defers with a zero delay: the attempt comes back, nothing is
written `completed`, and the replacement worker claims it immediately.

### ⚠ And the free pass is bounded to the shutdown, which the first cut was not

`defer(timedelta(0))` is the **only** ending that neither consumes an
attempt nor moves `run_after`. PR #42's second review measured what that
costs a handler that raises `Unfinished` for any other reason:

```
193 re-claims in 6 seconds | attempts pinned at 0 | sync_state pinned at syncing
```

**Both of the phase's backstops are bypassed.** `CLAIM_SQL`'s
`attempts < max_attempts` never trips because the counter never rises, and
`_SWEEP_SQL`'s `attempts >= max_attempts` never matches, so the sweeper can
never dead-letter it. Nothing raises, so nothing pages anyone; it runs
until a human notices.

So the branch is tied to the thing that makes it free:

```python
if not stop.is_set():
    self._fail(conn, job, exc)      # a handler bug: attempt + backoff
    return
defer(conn, job, self.worker_id, timedelta(0), reason)   # a shutdown
```

On the intended path nothing changes — a shutdown happens once per process
and the loop exits immediately afterwards, so it was always self-limiting
there. What is added is a floor under a handler that gets it wrong, and
Phase 22's handler will carry `raise Unfinished(...)` behind a condition:
misclassifying a clone precondition as "unfinished" rather than "failed" is
an ordinary mistake to make.

The repository is left at `syncing` (the handler had started, so
`mark_started` projected it, and `defer` writes no projection). That is
exactly where a **crashed** worker leaves it, and it is the truth: the work
is about to resume. What matters is that `last_synced_at` is untouched and
`sync_state` is not `synced`, and the test asserts both.

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

`services/workers/tests/isolation/test_job_worker_runtime.py`, **25 tests**
against the session's real `postgres:16-alpine`. Lease 2 s, heartbeat
0.5 s, idle poll 0.1 s, suspended deferral 1 s. Nothing sleeps waiting for
a state change: `until()` and `job_when()` poll against a deadline, so a
slow machine takes longer rather than failing.

**Two of them kill a real backend.** `pg_terminate_backend` is how a
Postgres restart, a failover or an idle-connection reaper is reproduced,
and it needs a superuser — which `rag_doc_app` deliberately is not — so
there is one `superuser_conn` fixture used for nothing else. The worker
tags its two connections `rag-doc-worker` and `rag-doc-worker-heartbeat`,
so a test kills exactly one and watches the right recovery path. That tag
is not scaffolding: `pg_stat_activity` is where an operator looks to find
out which of a worker's connections is wedged.

**⚠ `running()` does not assert in its `finally`, and that cost two
debugging rounds.** An exception raised in a `finally` **replaces** the one
already propagating, so "the worker did not return after stop was set" hid
every real failure inside the block — and it is usually a *consequence* of
the real failure, because a test that fails mid-block never releases the
handler it is blocking. The assert now sits after the `try/finally`, where
it runs only if the body did not raise.

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

**Two tests pull in opposite directions on the heartbeat, and both are
needed.** One dropped connection must **not** abort the handler — it
reconnects in place, because giving up on the first `InterfaceError` would
throw away minutes of ingest for a blip. A sustained outage **must** abort
it. Making the second deterministic took three attempts, and the two that
failed are recorded in the test's docstring because they are the same
trap: **superusers are exempt.** `REVOKE rag_doc_app FROM isolation` did
nothing (a superuser may `SET ROLE` to anything), and a `CONNECTION LIMIT`
did nothing (not enforced for superusers). What works is revoking `UPDATE`
on `ingestion_jobs` from `rag_doc_app`: the worker's sessions authenticate
as the superuser and then drop to `rag_doc_app` through the DSN's
`options=-c role=...`, and `SET ROLE` to a non-superuser gives up superuser
privilege — the property the whole isolation harness rests on.

**A third thing measured rather than assumed:** the first version of both
killed the heartbeat as soon as `lease_expires_at` was non-NULL. That is
**claim** time, and the heartbeat thread opens its connection a moment
later — so the kill hit nothing about half the time, and the test then
proved nothing while still passing its own `killed >= 1` check on the other
half. Both now wait for a beat to LAND first. Found with a standalone probe
against a real container, after the symptom made no sense.

**And one test reads an actual `LogRecord`.** A supersede leaves nothing
behind in the database that says a worker was stopped — the row looks the
same whether the worker noticed or is still grinding away — so the log line
is the whole of the evidence. `test_a_supersede_is_reported_in_the_log`
asserts it exists at WARNING and names the job, the organization and the
repository. Without it, mutation **M13** (the heartbeat says nothing)
survives. This is 21-05's lesson applied: a rule stated in a docstring is
not a rule until one test reads the thing the docstring is about.

### The five the SECOND review added

| Test | What it pins |
|---|---|
| `unfinished_outside_a_shutdown_fails_rather_than_re_claiming_forever` | the bound: one call, `attempts == 1`, `run_after` pushed out, and still one call 1.5 s later |
| `an_unreachable_database_is_retried_and_then_gives_up_with_exit_1` | the first connect goes through the policy — three retries logged, `DatabaseUnavailable`, and `main()` returning 1 |
| `a_connect_that_hangs_times_out_instead_of_blocking_forever` | `connect_timeout`, against a socket that accepts and never speaks |
| `an_unset_max_job_duration_is_announced_rather_than_silent` | the WARNING and the startup line, read as `LogRecord`s, in both directions |
| `require_tenant_says_a_dead_connection_is_dead` | the message an operator reads on every terminal write |

**⚠ How the spin test is bounded, because a test for a spin must not
spin.** The handler counts its calls and switches to a plain `Exception`
after 25, which fails the job and ends the loop whatever the runtime does.
So the unfixed code — and mutation M24 — makes it FAIL in about a second
rather than hang. It also carries the one fixed `sleep` in the file, and
the docstring says why: every other wait is for something to happen, and
this one asserts that nothing does, which has no deadline to poll against.

### The six the FIRST review added

| Test | What it pins |
|---|---|
| `a_handler_that_stops_early_on_shutdown_defers_instead_of_completing` | `Unfinished` → `defer`; `state = queued`, `attempts == 0`, `last_stage` kept, no probe call, `last_synced_at` untouched |
| `a_shutdown_finishes_the_job_in_flight` *(rewritten)* | a handler that FINISHES during a shutdown is completed — and `should_abort()` is **False** throughout while `is_shutting_down()` is True |
| `a_worker_whose_connection_dies_reconnects_and_claims_the_next_job` | the loop's backend is killed between two jobs; the second is still claimed and completed |
| `a_heartbeat_whose_connection_dies_reopens_it_and_the_job_survives` | one drop is a blip: the lease moves forward again and the handler is never disturbed |
| `a_heartbeat_that_cannot_beat_for_a_full_lease_gives_up` | a sustained outage sets the abort flag within a bounded time |
| `a_handler_that_overruns_max_job_duration_is_cut_loose` | the hung-handler bound, so the lease can expire and the job be reclaimed |
| `progress_fields_are_redacted_before_they_reach_the_column` | `last_stage` and `progress` — values, nested values and **keys** — with counters left intact |

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

**28 mutations, 28 killed, one deliberate survivor** — fourteen from the
first cut, nine for the guards PR #42's first review added, and five more
for its second. Each was applied to a COPY of
`services/workers` plus `services/backend/migrations` — the conftest
resolves migrations at `parents[3]`, so the copy is `<scratch>/workers`
beside `<scratch>/backend/migrations`. The harness rebuilds the copy from
the committed worktree before every run, asserts the pattern matched
**exactly once**, and asserts the mutated text is present **and** the
original absent afterwards. The committed tree was never mutated.

Baseline on the copy: **75 passed** (30 runtime + 45 transitions).

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

### The nine the review added

| # | Mutation | Result |
|---|---|---|
| M15 | `should_abort()` is widened back to include shutdown | **Killed: 1** — `a_shutdown_finishes_the_job_in_flight`, on the assertion that `should_abort()` is False while `is_shutting_down()` is True. This is deviation 2, as a mutation |
| M16 | a handler that returns during a shutdown is NOT completed | **Killed: 1** — the same test, from the other side: "finish the current job" has to keep working |
| M17 | `Unfinished` is treated as an ordinary failure (`fail`, not `defer`) | **Killed: 1** — `a_handler_that_stops_early_on_shutdown_defers_instead_of_completing`, on `attempts == 0` |
| M18 | **`Unfinished` falls through to `complete`** — the review's defect, exactly | **Killed: 1** — the same test |
| M19 | the loop never checks its connection, so it cannot reconnect | **Killed: 1** — `a_worker_whose_connection_dies_reconnects_and_claims_the_next_job` |
| M20 | the heartbeat never gives up (every failure is a blip) | **Killed: 1** — `a_heartbeat_that_cannot_beat_for_a_full_lease_gives_up` |
| M21 | `max_job_duration` is never enforced | **Killed: 1** — `a_handler_that_overruns_max_job_duration_is_cut_loose` |
| M22 | the progress fields are written raw | **Killed: 1** — `progress_fields_are_redacted_before_they_reach_the_column` |
| M23 | `_sanitize_progress` leaves dict **keys** alone | **Killed: 1** — the same test. A redaction with a hole in it is worse than none, because it gets trusted |

### The five the SECOND review added

| # | Mutation | Result |
|---|---|---|
| M24 | `Unfinished` always defers, shutdown or not | **Killed: 1** — `unfinished_outside_a_shutdown_fails_rather_than_re_claiming_forever`, in about a second, because the test is bounded by construction |
| M25 | the first connect bypasses the reconnect policy | **Killed: 1** — `an_unreachable_database_is_retried_and_then_gives_up_with_exit_1` |
| M26 | no `connect_timeout` | **Killed: 1** — `a_connect_that_hangs_times_out_instead_of_blocking_forever`, on its deadline rather than by hanging |
| M27 | nothing warns when `max_job_duration` is unset | **Killed: 1** — `an_unset_max_job_duration_is_announced_rather_than_silent` |
| M28 | `require_tenant` keeps the misleading message | **Killed: 1** — `require_tenant_says_a_dead_connection_is_dead`. The 45 transition tests all still passed under it, so nothing else depended on the wording |

**And one deliberate survivor, recorded rather than explained away.**
Moving the heartbeat's give-up rules back into the `except` survives the
whole suite, because with `connect_timeout` in place every way a beat can
fail now raises — so the two placements are behaviourally identical. The
only non-raising failure left blocks inside `cur.execute`, where the
top-of-loop check cannot run either. See "What this plan does NOT pin".

Two of the re-run fourteen now kill more than they did, which is worth
recording because it says the new tests overlap the old invariants rather
than sitting beside them: **M6** (the heartbeat sharing the loop's
connection) takes down all three heartbeat tests rather than only the
connection count, and **M11** (`mark_started` never called) takes the
`Unfinished` test with it, because that test reads `last_stage` on a row
whose projection never happened.

**Four of the original fourteen found real gaps while the file was being
written**, which
is why they are here at all: M2 (the call count, not the row), M6 (nothing
could observe the connection split), M11 (nothing could observe `syncing`)
and M13 (nothing read a log record). Each was a test that was right about
what it asserted and wrong about what its fixtures could distinguish —
21-04's and 21-05's finding, twice more.

## Two rulings recorded rather than acted on

Both were questions this plan asked the review, and both answers are "leave
it" — with reasons stronger than the ones the summary had.

### `sync_state = 'syncing'` after an `Unfinished` deferral stays. **Do not make `defer` project.**

1. **On the intended path the state is accurate, not stale.** The job is
   claimable that instant and another worker resumes it. Projecting
   `pending` would flicker the UI `syncing → pending → syncing` inside a
   second, which is strictly worse than leaving it alone.
2. **Changing `defer` would break the path it was designed for.** 21-05
   made it projection-free deliberately, and the suspended-installation
   deferral depends on that — it must not show `syncing` for an hour of
   waiting. Making it project would mean branching on whether
   `mark_started` had run, which puts new state into a statement 21-05
   froze and PR #41 reviewed. Too much blast radius for a cosmetic gain.
3. **The question that actually matters is bounded elsewhere.** "How long
   can `syncing` persist with no progress" is answered by the `Unfinished`
   bound above plus the lease, not by the projection.

**⚠ THE CONSEQUENCE FOR 21-07 AND PHASE 23, in one sentence:
`sync_state = 'syncing'` is not evidence of a live worker.** The evidence
is on the job row — `state`, `lease_expires_at`, `updated_at`, `attempts` —
and both the endpoint and the UI have to handle that already, because a
crashed worker produces the identical shape and always has. If Phase 23
wants a "stalled" badge, the predicate is `lease_expires_at < NOW()`, which
is the one `CLAIM_SQL` and `_SWEEP_SQL` already use.

### `max_job_duration = None` stays, with the logging above.

Shipping the mechanism without a number is right; shipping it silently was
not. See the heartbeat section.

## Three claims the review re-measured, and two came out the other way

Recorded because the summary had all three filed as unpinned, unprovable or
merely asserted, and being wrong in that direction costs the next reader
work they do not need to do.

| Claim | Verdict |
|---|---|
| A row carrying **both** `suspended_at` and `uninstalled_at` — listed as unpinned, "the ordering is right, and it is not tested" | **Correct.** Built and run at: `superseded`, `last_error = "the GitHub App was uninstalled at …"`, `sync_state = never_synced`, handler never called |
| The heartbeat keeps a long job's lease alive — the summary could only say the expiry "moved forward once" | **Correct, and sharper than claimed.** A 12 s handler against a 2 s lease, sampled every 100 ms: **110 samples, minimum headroom 1.426 s, maximum 1.984 s.** The lease never came within 1.4 s of expiring |
| M2 is killed on the callback's **call count**, not on a row | **Correct.** Re-run independently: `assert 1 == 0 where 1 = Probe.calls`. The row assertion did not fire; the call count did |
| The shared-connection failure mode "fails loudly on some interleavings and **silently on others**" | **Overstated — withdrawn.** See the entry below; it raises either way |

## What this plan does NOT pin, so nobody assumes it does

Following 21-05's practice of naming the gaps rather than leaving them to
be discovered.

- **The heartbeat's own connection is pinned STRUCTURALLY, by counting
  connections, and that is admitted in the test's docstring.** The
  behaviour it protects cannot be provoked on demand — and **PR #42's
  review tried and could not**, which settles the shape of this entry
  rather than leaving it hedged. The first cut said the shared version
  "fails loudly on some interleavings and silently on others"; **that
  overstated the risk and is withdrawn.** Measured: after one `_unscoped`
  scope is entered, `transaction_status` still reads `IDLE` (psycopg2
  begins lazily), so the idle precondition does **not** catch it — what
  does is psycopg2's own `with conn:` reentrancy guard,
  `ProgrammingError: the connection cannot be re-entered recursively`.
  Either way it **raises**. The M6 run agrees: 1 failed, 18 passed, and the
  18 included every test that writes results through `complete`. So
  counting connections is the right instrument, not a compromise.
- **A row carrying BOTH `suspended_at` and `uninstalled_at`** — this entry
  is **withdrawn, and the case is now measured**. PR #42's review built one
  and ran a worker at it: `superseded`, `last_error = "the GitHub App was
  uninstalled at …"`, `sync_state = never_synced`, handler never called.
  The branch order is right. It is still not pinned by a test in this
  suite, so a mutation swapping the two branches would survive; what has
  changed is that the behaviour is known rather than argued.
- **Signal delivery.** `_install_signal_handlers` is `# pragma: no cover`:
  the `stop` event's effect is tested (graceful shutdown), but nothing
  here sends the process a SIGTERM.
- **The idle poll's jitter.** The ±20% is not asserted; it is a herd
  control, not a correctness property.
- **The sweeper pauses while its worker is busy.** With every worker
  running a job, nothing sweeps until one frees up. That is a property, not
  a gap — the sweeper is a backstop for a worker that died, not a deadline
  — but it is written down so the next reader does not treat sweeps as
  periodic under load. **`max_job_duration` now bounds the worst case**: a
  hung handler used to pause that worker's sweeper *forever*, which is the
  half PR #42's review named and which is no longer true once a duration is
  set.
- **The heartbeat's give-up rules moved out of the `except`, and that move
  alone is not separately killable.** With `connect_timeout` in place every
  way a beat can fail now RAISES, so the relocated rules and the old ones
  are behaviourally identical — the mutation that puts them back survives,
  deliberately. The only non-raising failure left is a beat that BLOCKS
  (a row lock on `ingestion_jobs`, say), and that blocks inside
  `cur.execute`, so the top-of-loop check cannot run either: what would fix
  *that* is a `statement_timeout` on the heartbeat connection, which is
  named here as a Phase 22 candidate rather than added. The relocation is
  defence in depth against a future non-raising path, and it is recorded as
  such rather than claimed as covered.

## Deviations from the plan

1. **A dead worker is simulated with a long heartbeat interval, not a test
   hook.** The plan says "its heartbeat is disabled via a test hook". A
   `heartbeat` longer than the `lease` is indistinguishable from a dead
   process from the database's side, it needs no second code path, and it
   uses a constructor parameter the API already has. Recorded in the test's
   docstring.
2. **~~`should_abort()` covers shutdown as well as a lost lease~~ —
   WITHDRAWN, and reversed.** PR #42's review ruled against it and was
   right: `21-06-PLAN.md:73` defines the flag as lease-only, so the two
   rules it claimed to reconcile were never in conflict, and the deviation
   manufactured the problem it then resolved. `should_abort()` is lease-only
   again, `is_shutting_down()` is separate, and `Unfinished` is the third
   ending. See "Shutdown, and the handler's three endings" above.
3. **`last_error` for an unhandled job type reads
   `UnknownJobType: no handler for full_ingest`,** not the plan's bare
   `no handler for <type>`. `sanitize_error` prefixes the exception class
   by design (21-05), and the class is the half that says whether this was
   a deployment mistake or a job that genuinely failed.
4. **`report_progress` takes an optional `progress`,** so a handler that
   only wants to record a stage does not have to invent a dict.
5. **Thirteen tests beyond the plan's list.** Seven from the first cut:
   three premise tests (the app role, the empty-handler `ValueError`, the
   heartbeat's own connection), two progress tests pinning `PROGRESS_SQL`'s
   fence, the log-record test, and a handler-raises test proving the
   redaction reaches `last_error` through the runtime. Six more applying
   the review: `Unfinished`, the loop's reconnect, the heartbeat reopening,
   the heartbeat giving up, `max_job_duration`, and the progress
   redaction. Ten of the thirteen exist because a mutation would otherwise
   have survived.
6. **The mutation count is 28, not 4.** The extras
   follow 21-02's, 21-03's and 21-05's practice, and several found real
   gaps -- see the mutation table.
7. **`REGISTRY` and the runtime types are exported from `workers.jobs`.**
   The plan lists `workers/jobs/__init__.py` among the files; this is what
   changed in it, so Phase 22 can `from workers.jobs import REGISTRY,
   JobContext, Handler, Unfinished` rather than reaching into submodules.
8. **`max_job_duration` is a new constructor parameter the plan does not
   mention** (PR #42's n2), defaulting to `None` so nothing changes until
   Phase 22 sets it.
9. **Two files outside this plan's list gained the same four lines**, and
   nothing else in either moved: `transitions.py`'s `_unscoped` and
   `db/tenant.py`'s `require_tenant` now distinguish a DEAD connection
   (`transaction_status` `UNKNOWN`) from one that is mid-transaction, and
   raise `InterfaceError` telling the caller to reconnect. No statement
   changed in either, and `require_tenant`'s behaviour is unchanged for
   every connection that is not dead. The second file was the second
   review's m4: every terminal write goes through `require_tenant`, so a
   mid-job death was still printing the old message.
10. **`connect_timeout` and the `Unfinished` condition are not in the plan
   either** (the second review's m1 and m3), nor is `max_job_duration`'s
   logging (m5). Each is recorded above with the measurement that produced
   it.
11. **`21-07-PLAN.md` gained one bullet**, carrying forward the ruling that
   `sync_state = 'syncing'` is not evidence of a live worker. It is a
   documentation obligation this plan created and 21-07 discharges.

## The Phase 22 hand-off

**Five things**, and the first two are what make the entrypoint start.
Items 3 and 4 are the two that most need reading as a checklist: item 3 is
the whole of the mitigation for the hung-handler hazard, and item 4 is the
contract a handler has to be written against.

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
3. **Set `max_job_duration`,** once there is a typical ingest to take a
   multiple of. It defaults to `None` — no bound — and until it is set a
   hung handler holds its lease indefinitely and stops that worker's
   sweeper.
4. **Write handlers against the three endings.** `return` when the job is
   done, `raise Unfinished(...)` when stopping part-way (a shutdown: check
   `ctx.is_shutting_down()`), and raise anything else for a real failure.
   ⚠ A bare `return` after stopping early writes `completed` and
   `sync_state = synced` over work that did not happen — not a style
   point, but the row PR #42's review produced.
5. **Measure the pool.** **One job at a time per process; scale by
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
| Workers, CI's environment | from `services/workers`: `REDIS_URL=redis://localhost:63796/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, **no `DATABASE_URL`**, no reachable `.env` (only `.env.example`), a fresh venv built from `requirements.txt` | **284 passed**, 0 failed, 0 skipped (19 pre-existing `utcnow` deprecation warnings). `main` is 254, so this plan adds **30** |
| This plan's tests alone | `pytest tests/isolation/test_job_worker_runtime.py -q` | **30 passed** |
| Repeated — the flake check | the same command, **5 consecutive runs** | 30 passed every time: 50.61 s, 50.54 s, 50.32 s, 53.63 s, 50.89 s. **No flakes** |
| Under load | this file plus `test_job_transitions.py` in one session, while a second pytest session and its own container ran concurrently | **75 passed in 57.12 s**, and the concurrent session's full suite passed 284 at the same time |
| The claim race | inside that file: 8 threads, own connections, one `threading.Barrier`, **5 rounds** on a warm pool | exactly one claim and `attempts == 1` in every round, and the winning thread's id is the row's `lease_owner` |
| Mutations | **28**, on a copy, each proven present in the file before the run | **28 killed, 1 deliberate survivor.** Baseline on the copy: 75 passed |
| Entrypoint | `python -m workers` from `services/workers`, with and without `DATABASE_URL` | exit **2** both times, with the handler message and no DSN message |
| Backend | `git diff --stat RAG-Doc/main..HEAD -- services/backend` | **empty.** No Go file and no migration, so `go test` was not run and the shared harness container was not rebuilt |
| `transitions.py` and `db/tenant.py` | `git diff RAG-Doc/main..HEAD` on each | the same four lines in each: the precondition now tells a DEAD connection from one mid-transaction. No statement changed in either |
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
