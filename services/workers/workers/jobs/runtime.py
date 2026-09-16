"""The worker process: the loop, the heartbeat and the sweeper's schedule.

21-05 built the transitions -- `claim`, `mark_started`, `complete`, `fail`,
`defer`, `abandon`, `sweep` -- as plain functions over psycopg2, each one
fenced on the lease and each one proven against PostgreSQL 16. THIS MODULE
DRIVES THEM AND REIMPLEMENTS NONE OF THEM. If you find yourself writing SQL
here that already exists in `transitions.py`, stop: the two copies will
drift, and the fence is the thing that would drift first.

There are exactly three statements in this file, and each is here because
`transitions.py` has no home for it: the heartbeat's lease extension, the
claim-time installation read, and the handler's progress report.

=====================================================================
THE LOOP
=====================================================================

Until `stop` is set:

  1. **Sweep**, if a heartbeat interval has passed since the last sweep.
     The sweeper is the backstop for a worker that died before it could
     write anything; it is queue-wide and cross-tenant by construction.
  2. **Claim.** Nothing claimable -> wait `idle_poll`, jittered, or until
     `stop`.
  3. **Check the installation**, per the table below, BEFORE anything
     that could write a `sync_state` projection.
  4. **No handler for this job type** -> `fail` it with `UnknownJobType`.
     Defensive only: `workers/__main__` refuses to start with an empty
     registry, so a deployed worker cannot reach this.
  5. **`mark_started`** -- and not before step 3. See the table.
  6. **Start the heartbeat thread**, on ITS OWN CONNECTION.
  7. **Run the handler.** Returned normally -> `complete` with whatever
     `write_results` it gave back, UNLESS the lease was lost meanwhile, in
     which case write nothing. Raised `Unfinished` -> `defer` with the
     attempt handed back. Raised anything else -> `fail`. `LeaseLost` from
     either -> log a warning and carry on; the job belongs to someone else
     now.
  8. **Stop the heartbeat thread** and join it.

=====================================================================
⚠ THE INSTALLATION IS READ AT CLAIM TIME, NOT AT ENQUEUE TIME
=====================================================================

21-CONTEXT L2 is explicit that `ingestion_jobs.payload` carries no
credentials and no installation id: two reconnects racing produce ONE job
(L8), and a job that had snapshotted the loser's installation would
silently use stale credentials. So the worker reads the repository's
CURRENT installation after it claims, and acts on what it finds:

| What the worker finds             | Action                                  |
|-----------------------------------|-----------------------------------------|
| `installation_id IS NULL`         | `abandon`; `sync_state = never_synced`  |
| `uninstalled_at` set              | `abandon`; `sync_state = never_synced`  |
| `suspended_at` set                | `defer` 60 min, NO attempt consumed     |
| otherwise                         | `mark_started`, then the handler        |

**Why abandon rather than fail.** There is nothing to do and nothing went
wrong, so the queue must not retry it: `github_webhook_events.go`'s
uninstall stand-down says "'failed' is deliberately not used -- nothing
failed, and the queue must not retry these", and `abandon` writes exactly
what that handler writes.

**Why defer rather than fail.** A suspension heals on its own and nobody
has to send us a webhook about it; an hour is soon enough to notice. And
`DEFER_SQL` hands the claim's attempt back, so a week of suspension cannot
walk a healthy repository to `dead` -- which a `fail` here would do in five
hours.

⚠ `mark_started` RUNS AFTER THE CHECK, NEVER BEFORE IT. That ordering is
why `claim` deliberately writes no `sync_state` projection (see its
docstring): a job under a dead installation must be abandoned to
`never_synced` without ever having told the UI it was `syncing`.

⚠ THE ABANDON PATH IS ISS-033's ENDING. That issue is filed on the premise
that this check exists -- "if it does not, this issue's priority rises with
it". The producers do not check `uninstalled_at`, so a push racing an
uninstall queues a job under a dead installation; here it costs one claim's
round trip and a briefly wrong `sync_state`, rather than five attempts and
a `failed` terminal state.

=====================================================================
⚠ THE HEARTBEAT THREAD GETS ITS OWN CONNECTION
=====================================================================

psycopg2 connections may be SHARED between threads -- and sharing one here
would be a data-loss bug, because a shared connection shares its
TRANSACTION. The heartbeat's commit would commit whatever the handler had
half-written on the main connection, or its rollback would discard it. The
thread therefore opens its own connection and closes it when the job ends.

It also GIVES UP rather than beating into the void: once a full lease has
passed with no beat landing (or `MAX_HEARTBEAT_FAILURES` in a row), the
lease has demonstrably expired, so the abort flag goes up and the handler
stops working on a job somebody else may already hold. And it reopens its
connection in place, so a drop heals within the job rather than only at the
next one.

The heartbeat's `UPDATE` carries the SAME two-part fence as every terminal
write, `lease_owner = %s AND state = 'running'`, and that is what detects a
supersede: `supersedeLiveSQL` deliberately leaves the lease attached to the
row it supersedes, so `lease_owner` alone still matches. Zero rows back
means the job was superseded (L4) or the lease expired and someone else
reclaimed it. The abort flag goes up, the thread stops beating, and the
handler sees `should_abort()`.

=====================================================================
SHUTDOWN
=====================================================================

`run(stop)` returns after the job in flight reaches a terminal write. It
never abandons a job mid-write, and it never kills a handler.

⚠ TWO QUESTIONS, TWO METHODS, AND THE FIRST CUT CONFLATED THEM.

  - `should_abort()` -- **this job is not ours any more.** The lease is
    gone. The worker will write nothing whatever the handler does.
  - `is_shutting_down()` -- **the worker is going away; the job is still
    ours.** What gets written depends entirely on how the handler ends.

Three endings, and a handler picks one:

  return              done            -> `complete`
  raise Unfinished    stopped early   -> `defer`, attempt handed back
  raise anything else failed          -> `fail`, attempt consumed

The first cut folded shutdown into `should_abort()` and told handlers, in a
docstring, to raise if they had not finished. PR #42's review reproduced
what that costs: a Phase-22-shaped handler that stopped after `clone` was
written `state=completed, last_stage=parse, sync_state=synced` -- a row
that contradicts itself, a UI that says the repository is ingested, and a
freed unique index so nothing re-queues it. `Unfinished` makes that
unrepresentable, and `defer` rather than `fail` means an operator's
restarts cannot walk a healthy repository towards `dead`.

=====================================================================
WORKER-POOL SIZING (Phase 22 measures it)
=====================================================================

ONE JOB AT A TIME PER PROCESS. Scale by running more processes; the claim
is safe across any number of them (`FOR UPDATE SKIP LOCKED`, pinned from
Python by 21-06's barrier test). How many processes is an OPEN INPUT --
21-RESEARCH withdrew the arithmetic that answered it, because it applied a
full-ingest duration to push jobs and nothing has yet ingested end to end.
Phase 22 measures the incremental duration and sizes the pool from it.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Dict, Mapping, Optional

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
from psycopg2.extras import Json, RealDictCursor

from workers.db import require_tenant
from workers.jobs.transitions import (
    Job,
    LeaseLost,
    _interval,
    _sanitize_text,
    _unscoped,
    abandon,
    claim,
    complete,
    defer,
    fail,
    mark_started,
    new_worker_id,
    sanitize_error,
    sweep,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Defaults (21-CONTEXT L3)
# =====================================================================

#: ⚠ THE SAME FIVE MINUTES AS 20-05's `abandonedProcessingAfter`
#: (`services/backend/pkg/api/handlers/github_webhook.go:270`), which is
#: how long a webhook delivery may sit `processing` before we assume the
#: attempt died. L3 says to keep the two consistent and to say why if one
#: ever changes: both answer the same question -- how long a dead process
#: may hold work before something else may take it -- and an operator
#: reading one number should not have to learn a second.
#:
#: Short, because recovery from a dead worker is bounded by it. An ingest
#: takes minutes, so a lease as long as the worst-case job would strand a
#: repository for that long; the heartbeat is what lets a long job keep a
#: short lease.
DEFAULT_LEASE = timedelta(minutes=5)

#: ⚠ ONE FIFTH OF THE LEASE -- 60s into 300s. (The first cut said "one
#: twelfth", which would be 25 s; PR #42's review caught it. The sentence
#: matters because it is what somebody re-tuning these numbers would use to
#: re-derive them.) Four consecutive heartbeats may therefore be lost
#: before the job becomes reclaimable: beats due at 60, 120, 180 and 240
#: all miss, and the lease expires at 300. Also the sweeper's schedule: the sweep is
#: two indexed `UPDATE`s and there is no reason to give it a timer of its
#: own.
DEFAULT_HEARTBEAT = timedelta(seconds=60)

#: How long to wait after an empty claim. At our load (~0.1 jobs/second,
#: 21-RESEARCH) this is the difference between noticing a push in 5 seconds
#: and hammering the claim query; `LISTEN/NOTIFY` is the upgrade if that
#: ever matters, and it does not yet.
DEFAULT_IDLE_POLL = timedelta(seconds=5)

#: How long a job waits when its installation is suspended. An hour, so an
#: unsuspend is noticed without anybody sending us a webhook about it --
#: GitHub does send `installation.unsuspend`, but 21-04 deliberately does
#: not make the queue depend on it, because a missed delivery would then
#: strand the repository forever.
DEFAULT_SUSPENDED_DEFER = timedelta(minutes=60)

#: ±20% on the idle poll, so a pool of workers restarted together does not
#: stay in lockstep on the claim query.
IDLE_POLL_JITTER = 0.2

#: `application_name` for the two connections a busy worker holds. They
#: fail differently and they recover differently, so `pg_stat_activity`
#: should say which is which -- and it is what lets a test kill exactly one
#: of them.
LOOP_APPLICATION_NAME = "rag-doc-worker"
HEARTBEAT_APPLICATION_NAME = "rag-doc-worker-heartbeat"

#: The reconnect backoff for the loop's connection: 1 s, doubling, capped.
#: Bounded rather than indefinite, because a worker that cannot reach the
#: database is not doing anything a supervisor could not do better by
#: restarting it.
RECONNECT_BACKOFF_BASE = timedelta(seconds=1)
RECONNECT_BACKOFF_MAX = timedelta(seconds=30)
MAX_RECONNECT_ATTEMPTS = 10

#: Consecutive heartbeat failures after which the lease is assumed lost,
#: regardless of the clock. The clock rule below is the principled one --
#: once a full lease has passed with no beat landing, the lease has
#: demonstrably expired -- and this is the backstop for a deployment that
#: configures a very long lease, where "a full lease" could be an hour of
#: a handler working on a job somebody else already owns.
MAX_HEARTBEAT_FAILURES = 5

#: How long `run` waits for the heartbeat thread to notice the job ended.
#: It is a bound on a thread that only ever waits on an Event and runs one
#: short UPDATE, so exceeding it means something is wrong and the log line
#: is the point.
HEARTBEAT_JOIN_TIMEOUT = timedelta(seconds=30)


# =====================================================================
# The three statements this file owns
# =====================================================================

# ⚠ BOTH HALVES OF THE FENCE, exactly as every terminal write in
# `transitions.py` carries them, and for the same reason: a superseded row
# KEEPS its lease (`supersedeLiveSQL` leaves it attached deliberately), so
# `lease_owner = %s` alone still matches one. `state = 'running'` is what
# turns a supersede into zero rows here, and zero rows is the only way this
# worker learns it has been superseded.
#
# The lease is extended to `NOW() + lease`, not by `lease`: an interval
# added to the EXISTING expiry would drift further out every beat and a
# stalled worker would keep its job for as long as it stayed stalled.
#
# %s lease interval, %s id, %s lease_owner.
HEARTBEAT_SQL = """
UPDATE ingestion_jobs
SET lease_expires_at = NOW() + %s::interval, updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'
RETURNING id"""

# The claim-time installation read. TENANT-SCOPED: both tables carry
# `FORCE ROW LEVEL SECURITY`, so this runs inside
# `require_tenant(conn, job.organization_id)` and an unscoped version would
# return nothing -- which would look exactly like "no installation" and
# abandon every job in the queue.
#
# A LEFT JOIN, not an inner one, because the three findings are different
# actions: no `installation_id` at all is an abandon, and so is an
# `installation_id` pointing at a row this tenant cannot see -- but they
# are different sentences in `last_error`, and an inner join would collapse
# both into "no row" alongside "the repository is gone".
#
# %s repository_id.
INSTALLATION_SQL = """
SELECT r.installation_id::text AS installation_id,
       gi.id::text            AS resolved_installation,
       gi.suspended_at        AS suspended_at,
       gi.uninstalled_at      AS uninstalled_at
FROM repositories r
LEFT JOIN github_installations gi ON gi.id = r.installation_id
WHERE r.id = %s"""

# `JobContext.report_progress`. Fenced like everything else a stale worker
# could otherwise land: `last_stage` is what L2 calls coarse resumability
# ("skip a clone we already completed on a retry"), so a reclaimed worker
# writing ITS stage onto the new attempt's row would make the new attempt
# skip work it has not done.
#
# %s last_stage, %s progress, %s id, %s lease_owner.
PROGRESS_SQL = """
UPDATE ingestion_jobs
SET last_stage = %s, progress = %s, updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'
RETURNING id"""


# =====================================================================
# Types
# =====================================================================


class DatabaseUnavailable(RuntimeError):
    """Reconnection kept failing. The worker gives up so it can be restarted.

    ⚠ IT IS RAISED OUT OF `run`, DELIBERATELY. A worker that cannot reach
    the database is doing nothing, and a process that stays up doing
    nothing produces no exit code, so no `restart:` policy fires and the
    container looks healthy while its queue backs up. `__main__` turns this
    into a non-zero exit, which is the only thing a supervisor can act on.
    """


def _is_dead(conn: Any) -> bool:
    """True when this connection can no longer be used, for either reason.

    `closed` covers the ordinary case. `TRANSACTION_STATUS_UNKNOWN` is the
    one that bites: it is what psycopg2 reports for a connection whose
    BACKEND is gone but whose object has not been closed, and it is what
    `_unscoped`'s idle precondition used to misread as "you are inside a
    transaction" -- sending the operator to the wrong file.
    """
    if conn.closed:
        return True
    try:
        return conn.info.transaction_status == TRANSACTION_STATUS_UNKNOWN
    except Exception:  # noqa: BLE001 - asking a dead object is answer enough
        return True


class Unfinished(Exception):
    """A handler stopped early WITHOUT finishing. Not a failure, not a success.

    ⚠ RAISE THIS TO WIND DOWN. It is the only way to stop a job part-way
    and have the row say so. The worker `defer`s it: `DEFER_SQL` hands the
    claim's attempt back, writes no `sync_state`, and puts the job straight
    back in the queue for whoever claims next.

    A bare `return` means "this job is DONE" and gets `complete` --
    `state = 'completed'`, `sync_state = 'synced'`, `last_synced_at`
    stamped and the partial unique index freed, so nothing re-queues it.
    PR #42's review reproduced exactly that against a real database with a
    Phase-22-shaped handler that stopped after `clone`:

        state=completed  last_stage=parse  sync_state=synced

    a row that contradicts itself and a UI that says the repository is
    ingested. `raise Unfinished(...)` is what makes that unrepresentable.

    ⚠ AND NOT `fail`, WHICH IS WHY THIS EXISTS RATHER THAN "just raise
    something". A failure consumes an attempt and applies the backoff, so
    five operator-initiated restarts spread across an ingest's retries walk
    a HEALTHY repository to `dead` -- the same shape `defer` exists to
    prevent for a suspended installation. Nothing went wrong here; the
    worker is going away.

    The message becomes the job's `last_error`, sanitized like every other
    one, so 21-07 can say why the job went back in the queue.
    """


class UnknownJobType(Exception):
    """No handler is registered for a claimed job's `job_type`.

    Unreachable in a deployed worker -- `workers/__main__` refuses to start
    with an empty registry, and migration 000014's `CHECK` constrains
    `job_type` to the two keys Phase 22 registers. It exists so that a
    worker started from a REPL with a partial map fails the job with a
    readable `last_error` instead of raising `KeyError` out of the loop.
    """


#: What a handler returns: a callback that writes its results inside
#: `complete`'s transaction, or None when there is nothing to write.
#:
#: ⚠ THE CALLBACK IS WHY THE RESULTS AND THE COMPLETION COMMIT TOGETHER,
#: which is most of the argument for putting the queue in Postgres at all
#: (L1). It is handed `complete`'s own cursor, inside the job's tenant
#: scope, and it must not commit, roll back, or open a transaction.
WriteResults = Callable[[Any], None]

#: A job handler. Takes the context, does the work, and returns its
#: `write_results` callback -- or None.
#:
#: THREE ENDINGS, and each of them writes something different:
#:
#:   return        the job is DONE          -> `complete`
#:   raise Unfinished   stopped part-way    -> `defer`, attempt handed back
#:   raise anything else    it FAILED       -> `fail`, attempt consumed
#:
#: ⚠ THE FIRST TWO ARE THE ONES THAT GET CONFUSED. The worker cannot tell
#: an unfinished return from a finished one, so a bare `return` during a
#: shutdown writes `completed` over work that did not happen. `Unfinished`
#: is the difference; see its docstring for the row PR #42's review
#: produced without it.
Handler = Callable[["JobContext"], Optional[WriteResults]]


def _sanitize_progress(value: Any) -> Any:
    """Redact the string leaves of a `progress` value, keys included.

    `progress` is handler-supplied JSON that 21-07 hands back over HTTP,
    and its documented `current_file` is exactly where a clone URL carrying
    an installation token ends up. `_sanitize_text` already knows every
    shape worth redacting (21-05 widened it to all six GitHub prefixes,
    JWTs and PEM blocks specifically because this plan puts them in the
    worker's reach); this walks the structure so it reaches them.

    ⚠ KEYS TOO. A handler that builds `{"<the failing url>": 3}` is not
    the obvious shape, but nothing stops it, and a redaction with a hole in
    it is worse than none because it is trusted.

    Numbers, booleans and `None` are returned unchanged -- `files_parsed`
    and `chunks_embedded` are the point of the column.
    """
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key)): _sanitize_progress(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_progress(item) for item in value]
    return value


def _stamp(value: Any) -> str:
    """Render a timestamp for a `last_error` sentence.

    ISO-8601 to the second. The column is read by a human through 21-07's
    admin endpoint, and microseconds are noise there.
    """
    return value.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass(frozen=True)
class _Installation:
    """What the claim-time read found, and what the worker should do."""

    #: "run", "defer" or "abandon".
    action: str
    #: The sentence written to `last_error` for `defer` and `abandon`.
    reason: str


class JobContext:
    """What a handler is given: its job, an abort signal, a progress channel.

    Deliberately NOT a connection. Phase 22's handlers clone, parse and
    embed -- network work that must not hold a Postgres transaction open
    for minutes (see `claim`'s docstring) -- and their one transactional
    write goes through the `write_results` callback they return, so that it
    commits with the completion or not at all.
    """

    def __init__(
        self,
        job: Job,
        worker_id: str,
        conn: Any,
        lease_lost: threading.Event,
        stopping: threading.Event,
    ) -> None:
        self.job = job
        self.worker_id = worker_id
        self._conn = conn
        self._lease_lost = lease_lost
        self._stopping = stopping

    def should_abort(self) -> bool:
        """True when THIS JOB IS NO LONGER OURS. Stop; nothing will be written.

        One cause, and only one: the heartbeat's fenced `UPDATE` matched no
        row, so the job was superseded (a relink, L4) or its lease expired
        and someone else reclaimed it. `report_progress` sets it too, for
        the same reason and by the same fence.

        Whatever the handler does next, the worker writes **nothing** --
        not a completion, not a failure, not a deferral. So every second
        spent after this is wasted, and a bare `return` is as safe as a
        raise.

        ⚠ SHUTDOWN IS A DIFFERENT QUESTION AND HAS A DIFFERENT METHOD.
        `is_shutting_down()`. The first cut folded the two together, on the
        argument that a handler wants one signal; PR #42's review ruled
        against it and was right. The job IS still ours during a shutdown,
        so what the worker writes depends entirely on whether the handler
        finished -- and a method called `should_abort()` reads as "stop
        now", which is precisely the wrong thing to do silently when the
        answer will be written down as `completed`.
        """
        return self._lease_lost.is_set()

    def is_shutting_down(self) -> bool:
        """True once SIGTERM/SIGINT has arrived. The job is still OURS.

        A handler may ignore this and finish -- the worker will not
        interrupt it, and the job is completed normally ("finish the
        current job"). If it would rather stop, it must
        `raise Unfinished(...)`, which defers the job with its attempt
        handed back so the replacement worker picks it straight up.

        ⚠ RETURNING EARLY BECAUSE OF THIS WRITES `completed`. See
        `Unfinished`.
        """
        return self._stopping.is_set()

    def report_progress(self, stage: str, progress: Optional[dict] = None) -> bool:
        """Record coarse progress on the job row. Fenced. Returns whether it landed.

        `last_stage` is L2's resumability breadcrumb (`clone|parse|embed|
        store`) and `progress` its detail (`files_parsed`, `chunks_embedded`,
        `current_file`). Both are read by 21-07's admin endpoint.

        ⚠ IT RUNS ON THE WORKER'S MAIN CONNECTION, which is idle for as
        long as a handler is running: every transition opens and closes its
        own transaction, and the handler runs between two of them.

        ⚠ AND IF A FUTURE HANDLER PARALLELISES AND CALLS THIS FROM TWO
        THREADS, WHAT REFUSES IT IS NOT THE IDLE PRECONDITION. The first
        version of this docstring said it was. **Measured by PR #42's
        review, on a real connection:** psycopg2 begins its transaction
        LAZILY, so after one `_unscoped` scope has been entered and before
        it executes, `transaction_status` is still `IDLE` and a second
        scope sails straight past the check. What actually refuses it is
        psycopg2's own `with conn:` reentrancy guard, one line later:
        `ProgrammingError: the connection cannot be re-entered recursively`.

        The conclusion is the same and is worth having stated correctly:
        **this failure mode is LOUD, not silent.** Two concurrent scopes on
        one connection raise; they do not quietly commit each other's work.
        Trust the reentrancy guard, not the idle check.

        ⚠ BOTH ARGUMENTS ARE REDACTED BEFORE THEY ARE WRITTEN. `last_stage`
        is bare `TEXT` (000014) with **no `CHECK`**, so the documented
        `clone|parse|embed|store` enum is a comment and nothing enforces
        it; `progress`'s documented `current_file` is exactly the shape a
        clone URL ends up in --
        `https://x-access-token:ghs_...@github.com/org/repo.git`. 21-07
        hands both columns back over HTTP, so they go through the same
        `_sanitize_text` that `last_error` and `defer`/`abandon`'s reasons
        do. `progress` is walked to its string leaves, keys included.

        Returns:
            True if the row was updated; False if the lease was lost, in
            which case the abort flag is raised so `should_abort()` tells
            the handler the same thing.
        """
        safe_stage = _sanitize_text(stage)
        with _unscoped(self._conn) as cur:
            cur.execute(
                PROGRESS_SQL,
                (
                    safe_stage,
                    Json(_sanitize_progress(progress)) if progress is not None else None,
                    str(self.job.id),
                    self.worker_id,
                ),
            )
            landed = cur.fetchone() is not None

        if not landed:
            logger.warning(
                "job %s: progress report '%s' matched no row -- the lease is "
                "not ours (reclaimed or superseded) worker=%s repo=%s",
                self.job.id,
                safe_stage,
                self.worker_id,
                self.job.repository_id,
            )
            self._lease_lost.set()
            return False

        logger.info(
            "job %s: stage=%s org=%s repo=%s attempt=%d/%d worker=%s",
            self.job.id,
            safe_stage,
            self.job.organization_id,
            self.job.repository_id,
            self.job.attempts,
            self.job.max_attempts,
            self.worker_id,
        )
        return True


# =====================================================================
# The worker
# =====================================================================


class Worker:
    """One long-running consumer process. ONE JOB AT A TIME.

    Args:
        dsn: a libpq connection string. The worker opens one connection for
            the loop and one MORE per job for the heartbeat thread, so a
            pool of N workers wants 2N connections.
        handlers: `job_type` -> `Handler`. ⚠ AN EMPTY MAP IS A `ValueError`:
            a worker with no handler fails every job it claims five times
            and dead-letters it, which is the accident Phase 21 exists to
            not have. `workers/__main__` refuses earlier and more loudly,
            before it reads any configuration; this is the backstop for a
            caller that builds a `Worker` directly.
        worker_id: the `lease_owner`. Defaults to `new_worker_id()`, a
            UUID4 generated ONCE here -- never per job, or the heartbeat's
            fence would disagree with the claim's.
        lease, heartbeat, idle_poll, suspended_defer: see the module
            constants. They are parameters so that tests can run in
            seconds; nothing in production passes them.
        max_job_duration: how long a handler may run before the heartbeat
            stops beating and the job is handed back to the reclaim path.
            **None by default, which means no bound** -- and that is not an
            oversight. A hung handler is a real hazard (PR #42's n2: the
            lease is extended forever, nothing can reclaim, the repository
            sits `syncing`, and this worker's sweeper never runs again
            either), but the right number is a multiple of a typical
            ingest and NOTHING HAS INGESTED END TO END YET. The mechanism
            is here and tested; Phase 22 sets the number once it has one
            to multiply. Inventing it now is the mistake 21-RESEARCH
            already made once with pool sizing.
    """

    def __init__(
        self,
        dsn: str,
        handlers: Mapping[str, Handler],
        *,
        worker_id: Optional[str] = None,
        lease: timedelta = DEFAULT_LEASE,
        heartbeat: timedelta = DEFAULT_HEARTBEAT,
        idle_poll: timedelta = DEFAULT_IDLE_POLL,
        suspended_defer: timedelta = DEFAULT_SUSPENDED_DEFER,
        max_job_duration: Optional[timedelta] = None,
    ) -> None:
        if not handlers:
            raise ValueError(
                "a Worker needs at least one handler: with an empty map it "
                "would claim real jobs, fail each of them max_attempts "
                "times and dead-letter them. Register handlers in "
                "workers.jobs.handlers.REGISTRY (Phase 22)."
            )
        self.dsn = dsn
        self.worker_id = worker_id or new_worker_id()
        self.lease = lease
        self.heartbeat = heartbeat
        self.idle_poll = idle_poll
        self.suspended_defer = suspended_defer
        self.max_job_duration = max_job_duration

        self._handlers: Dict[str, Handler] = dict(handlers)
        self._rng = random.random

    # -----------------------------------------------------------------
    # The loop
    # -----------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Claim and run jobs until `stop` is set. Returns when it is.

        The loop body is guarded: a database blip while claiming or
        sweeping logs one line and waits an idle poll rather than killing
        the process. A container that exits on the first transient error
        restarts into the same error, and the restart loop is what an
        operator ends up debugging instead of the error.
        """
        logger.info(
            "worker %s starting: job_types=%s lease=%.0fs heartbeat=%.0fs "
            "idle_poll=%.1fs suspended_defer=%.0fs",
            self.worker_id,
            ",".join(sorted(self._handlers)),
            self.lease.total_seconds(),
            self.heartbeat.total_seconds(),
            self.idle_poll.total_seconds(),
            self.suspended_defer.total_seconds(),
        )
        conn = self._connect(LOOP_APPLICATION_NAME)
        last_sweep: Optional[float] = None
        try:
            while not stop.is_set():
                try:
                    # ⚠ HEALTH FIRST, so a dead connection is replaced
                    # before `claim` or `sweep` is asked to use it.
                    conn = self._ensure_live(conn, stop)
                    if conn is None:
                        return
                    last_sweep = self._maybe_sweep(conn, last_sweep)
                    job = claim(conn, self.worker_id, self.lease)
                    if job is None:
                        stop.wait(self._idle_delay())
                        continue
                    self._run_job(conn, job, stop)
                except DatabaseUnavailable:
                    # ⚠ THE ONE THING THE BROAD CATCH BELOW MUST NOT EAT.
                    # It is raised only after reconnection has failed
                    # `MAX_RECONNECT_ATTEMPTS` times, and its entire purpose
                    # is to end the process; logging it and continuing
                    # would restore the wedged-forever behaviour PR #42's
                    # review found.
                    raise
                except Exception as exc:  # noqa: BLE001 - see the docstring
                    logger.error(
                        "worker %s: loop iteration failed, continuing: %s",
                        self.worker_id,
                        sanitize_error(exc),
                    )
                    stop.wait(self._idle_delay())
        finally:
            if conn is not None:
                conn.close()
        logger.info("worker %s stopped", self.worker_id)

    def _connect(self, application_name: str):
        """Open a connection, tagged so `pg_stat_activity` says what it is.

        A worker holds two while a job runs and they behave differently
        under failure, so telling them apart from the database's side is
        worth one parameter -- and it is what lets a test kill one of them
        precisely.
        """
        return psycopg2.connect(self.dsn, application_name=application_name)

    def _ensure_live(self, conn: Any, stop: threading.Event) -> Optional[Any]:
        """Return a usable connection, reconnecting if this one has died.

        ⚠ psycopg2 CONNECTIONS DO NOT SELF-HEAL. Once the backend is gone
        the object is permanently bad, and the loop's connection was opened
        ONCE outside the `while`. PR #42's review reproduced what that
        costs: `pg_terminate_backend` on the loop's backend produced 44
        identical error lines in 15 seconds, the next job was never
        claimed, and the process stayed alive -- so no `restart:` policy
        fired and the container looked healthy while the queue backed up
        behind it. A Postgres restart, a failover or an idle-connection
        reaper in front of the database is enough to trigger it.

        The heartbeat already reconnects per job; this is the same
        property for the connection that could not heal.

        Returns:
            A live connection, or **None** when reconnection has failed
            `MAX_RECONNECT_ATTEMPTS` times in a row, or when `stop` was set
            while backing off. On None the loop returns and `run` raises
            `DatabaseUnavailable`, so a supervisor restarts the process --
            which is the whole point of giving up rather than looping.
        """
        if conn is not None and not _is_dead(conn):
            return conn

        if conn is not None:
            logger.warning(
                "worker %s: the loop's connection is dead; reopening",
                self.worker_id,
            )
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing a dead socket
                pass

        delay = RECONNECT_BACKOFF_BASE.total_seconds()
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            if stop.is_set():
                return None
            try:
                fresh = self._connect(LOOP_APPLICATION_NAME)
            except Exception as exc:  # noqa: BLE001 - the thing we retry
                logger.error(
                    "worker %s: reconnect %d/%d failed: %s",
                    self.worker_id,
                    attempt,
                    MAX_RECONNECT_ATTEMPTS,
                    sanitize_error(exc),
                )
                if stop.wait(delay):
                    return None
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX.total_seconds())
                continue
            logger.info(
                "worker %s: reconnected on attempt %d", self.worker_id, attempt
            )
            return fresh

        # ⚠ LOUDLY, AND BY EXITING. A worker that cannot reach the database
        # is not doing anything useful, and a process that stays up doing
        # nothing is invisible to every restart policy there is.
        raise DatabaseUnavailable(
            f"worker {self.worker_id}: could not reconnect after "
            f"{MAX_RECONNECT_ATTEMPTS} attempts; exiting so the supervisor "
            "can restart this process"
        )

    def _maybe_sweep(self, conn: Any, last_sweep: Optional[float]) -> float:
        """Dead-letter exhausted jobs, at most once per heartbeat interval.

        On the heartbeat's schedule rather than a timer of its own: the
        sweeper is the backstop for a worker that died without writing
        anything, so the useful frequency is "about as often as a live
        worker proves it is alive".

        ⚠ QUEUE-WIDE AND CROSS-TENANT. Every worker sweeps every tenant's
        jobs, which is `sweep`'s documented design and the reason it may
        never run inside a request handler.
        """
        now = time.monotonic()
        if last_sweep is not None and now - last_sweep < self.heartbeat.total_seconds():
            return last_sweep
        moved = sweep(conn)
        if moved:
            logger.warning(
                "worker %s: swept %d exhausted job(s) to dead", self.worker_id, moved
            )
        return now

    def _idle_delay(self) -> float:
        """`idle_poll`, jittered ±20% -- see IDLE_POLL_JITTER."""
        base = self.idle_poll.total_seconds()
        return base * (1.0 + IDLE_POLL_JITTER * (2.0 * self._rng() - 1.0))

    # -----------------------------------------------------------------
    # One job
    # -----------------------------------------------------------------

    def _run_job(self, conn: Any, job: Job, stop: threading.Event) -> None:
        handler = self._handlers.get(job.job_type)

        # ⚠ THE INSTALLATION CHECK COMES FIRST, BEFORE EVERY PROJECTION
        # THIS METHOD CAN WRITE -- including the unknown-job-type failure
        # below. PR #42's review found that one exception to the rule the
        # module docstring states: `fail` writes `PROJECT_FAILED_SQL`, so
        # ordering it ahead of this projected `failed` for a repository
        # whose App was uninstalled, which is the retry-looking terminal
        # state the abandon path exists to avoid. Unreachable in practice
        # (000014's CHECK bounds `job_type`, and `__main__` refuses an
        # empty registry), and the rule is cheaper to keep true than to
        # qualify.
        found = self._read_installation(conn, job)
        if found.action == "abandon":
            self._guarded(
                job, "abandon", lambda: abandon(conn, job, self.worker_id, found.reason)
            )
            return
        if found.action == "defer":
            self._guarded(
                job,
                "defer",
                lambda: defer(
                    conn, job, self.worker_id, self.suspended_defer, found.reason
                ),
            )
            return

        if handler is None:
            # Defensive. `__main__` cannot start with an empty registry and
            # 000014's CHECK bounds `job_type` to the two keys Phase 22
            # registers, so reaching this means a caller built a Worker by
            # hand with a partial map.
            self._fail(conn, job, UnknownJobType(f"no handler for {job.job_type}"))
            return

        # Only now, and never before the check above: a job under a dead
        # installation must never have told the UI it was `syncing`.
        mark_started(conn, job, self.worker_id)

        lease_lost = threading.Event()
        finished = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, lease_lost, finished),
            name=f"heartbeat-{job.id}",
            daemon=True,
        )
        beat.start()
        try:
            self._invoke(conn, job, handler, lease_lost, stop)
        finally:
            finished.set()
            beat.join(HEARTBEAT_JOIN_TIMEOUT.total_seconds())
            if beat.is_alive():
                logger.error(
                    "job %s: heartbeat thread did not stop within %.0fs worker=%s",
                    job.id,
                    HEARTBEAT_JOIN_TIMEOUT.total_seconds(),
                    self.worker_id,
                )

    def _invoke(
        self,
        conn: Any,
        job: Job,
        handler: Handler,
        lease_lost: threading.Event,
        stop: threading.Event,
    ) -> None:
        """Run the handler and write its ending."""
        context = JobContext(job, self.worker_id, conn, lease_lost, stop)
        try:
            write_results = handler(context)
        except LeaseLost as exc:
            # The handler itself hit a fenced write that matched no row --
            # `attach_ingestion_run`, say. Not a job failure: some other
            # worker owns this job now, and `fail` would clobber it.
            #
            # Redacted like every other exception rendering in this file.
            # `LeaseLost` is built from ids by `transitions.py` today, but
            # THIS clause catches one raised by the HANDLER, which in Phase
            # 22 is code holding an installation token.
            logger.warning(
                "job %s: handler lost the lease: %s", job.id, sanitize_error(exc)
            )
            return
        except Unfinished as exc:
            # ⚠ NOT A FAILURE AND NOT A COMPLETION. The handler stopped
            # part-way -- normally because `is_shutting_down()` went true.
            # `defer` with a zero delay hands the claim's attempt back
            # (DEFER_SQL decrements) and puts the job straight back in the
            # queue, so the replacement worker picks it up immediately and
            # an operator restart costs nothing. `fail` here would consume
            # an attempt, and five restarts would walk a healthy repository
            # to `dead`.
            reason = str(exc) or "the handler stopped before finishing"
            logger.info(
                "job %s: handler stopped unfinished; deferring worker=%s "
                "org=%s repo=%s",
                job.id,
                self.worker_id,
                job.organization_id,
                job.repository_id,
            )
            self._guarded(
                job,
                "defer",
                lambda: defer(conn, job, self.worker_id, timedelta(0), reason),
            )
            return
        except Exception as exc:  # noqa: BLE001 - every failure is the queue's
            # Handed to a method rather than closed over here: Python
            # UNBINDS an `except ... as` name at the end of the block, so a
            # lambda that captured it would be a `NameError` waiting for
            # the day someone defers the call.
            self._fail(conn, job, exc)
            return

        if lease_lost.is_set():
            # ⚠ THE CHECK THAT MAKES A COOPERATIVE ABORT MEAN ANYTHING.
            # Without it `complete` still refuses the write -- its fence
            # matches no row and it raises `LeaseLost` -- but it calls
            # `write_results` FIRST, so the handler's results are built and
            # rolled back for nothing, and on the supersede path the new
            # attempt's row is touched by a worker that no longer owns it.
            logger.warning(
                "job %s: the lease was lost while the handler ran; writing "
                "nothing worker=%s repo=%s",
                job.id,
                self.worker_id,
                job.repository_id,
            )
            return

        self._guarded(
            job,
            "complete",
            lambda: complete(conn, job, self.worker_id, write_results),
        )

    def _fail(self, conn: Any, job: Job, error: BaseException) -> None:
        """Record a failed attempt. `last_error` is redacted by `sanitize_error`."""
        self._guarded(job, "fail", lambda: fail(conn, job, self.worker_id, error))

    def _guarded(self, job: Job, transition: str, action: Callable[[], Any]) -> None:
        """Run a terminal write, treating `LeaseLost` as news rather than failure.

        ⚠ IT MUST NOT RETRY AND MUST NOT FALL BACK TO `fail`. A lost lease
        means another worker owns this job (or a producer replaced it);
        anything further this worker writes would clobber that.
        """
        try:
            action()
        except LeaseLost as exc:
            logger.warning(
                "job %s: %s wrote nothing: %s",
                job.id,
                transition,
                sanitize_error(exc),
            )

    # -----------------------------------------------------------------
    # The installation check
    # -----------------------------------------------------------------

    def _read_installation(self, conn: Any, job: Job) -> _Installation:
        """Read the repository's CURRENT installation and decide. TENANT-SCOPED.

        See the module docstring's table. The order of the branches is the
        order of that table, and `uninstalled_at` is tested before
        `suspended_at` because a reinstall clears BOTH (21-04) -- a row
        carrying each of them is uninstalled, not suspended.
        """
        with require_tenant(
            conn, job.organization_id, cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(INSTALLATION_SQL, (str(job.repository_id),))
            row = cur.fetchone()

        if row is None:
            # `repository_id` is `ON DELETE CASCADE`, so a deleted
            # repository takes its jobs with it and this is normally
            # unreachable; it is reachable if the row is invisible under
            # this tenant, which would be a drift the composite foreign key
            # is supposed to make impossible. Abandoning is the safe
            # ending either way -- the fenced write then matches nothing
            # and raises `LeaseLost` if the row really is gone.
            return _Installation(
                "abandon", "the repository is not visible under its organization"
            )
        if row["installation_id"] is None:
            return _Installation(
                "abandon", "the repository has no GitHub App installation"
            )
        if row["resolved_installation"] is None:
            return _Installation(
                "abandon",
                "the repository's installation is not visible under its organization",
            )
        if row["uninstalled_at"] is not None:
            when = _stamp(row["uninstalled_at"])
            return _Installation(
                "abandon", f"the GitHub App was uninstalled at {when}"
            )
        if row["suspended_at"] is not None:
            when = _stamp(row["suspended_at"])
            return _Installation(
                "defer",
                f"the GitHub App is suspended (since {when}); "
                "waiting for an unsuspend",
            )
        return _Installation("run", "")

    # -----------------------------------------------------------------
    # The heartbeat
    # -----------------------------------------------------------------

    def _heartbeat_loop(
        self, job: Job, lease_lost: threading.Event, finished: threading.Event
    ) -> None:
        """Extend the lease every interval until the job ends or is taken away.

        ⚠ ITS OWN CONNECTION, opened here and closed here. See the module
        docstring: sharing the loop's connection would share its
        transaction, and a heartbeat commit would commit the handler's
        half-written work.

        The first beat is one interval in, not immediate: `claim` has just
        set the lease, so an immediate beat would write the same value it
        just read. It is also what lets a test simulate a DEAD process
        without a hook into this code -- a heartbeat interval longer than
        the lease means no beat ever lands before the lease expires, which
        is exactly what a worker that has stopped running looks like.

        ⚠ IT GIVES UP, ON TWO RULES, AND THE FIRST CUT HAD NEITHER.
        A single failed beat is a blip and is retried; a RUN of them is
        not. PR #42's review killed the heartbeat's backend mid-job and
        watched the lease expire underneath a running handler while
        `should_abort()` stayed False -- the fence stops the corruption, so
        the cost is a whole ingest thrown away and duplicated, unbounded in
        duration.

          - **The clock rule.** Once a full `lease` has passed with no beat
            landing, the lease has demonstrably expired and somebody else
            may already hold the job. Set the flag and stop.
          - **The count rule.** `MAX_HEARTBEAT_FAILURES` consecutive
            failures, as a backstop for a very long configured lease.

        It also REOPENS its connection when that is what broke, so a
        transient drop heals within the job rather than only at the next
        one.

        ⚠ AND IT ENFORCES `max_job_duration`, when one is set. A handler
        that hangs -- a clone against an unresponsive remote with no socket
        timeout -- is worse than one that crashes: the beat would keep the
        lease alive forever, so nothing could ever reclaim the job, the
        repository would sit `syncing`, and this worker's sweeper (which
        lives in the same loop) would never run again either. Stopping the
        beat hands the job to the existing reclaim-and-dead-letter path.
        """
        conn = None
        interval = self.heartbeat.total_seconds()
        started = time.monotonic()
        last_ok = started
        failures = 0
        try:
            conn = self._connect(HEARTBEAT_APPLICATION_NAME)
            while not finished.wait(interval):
                if self.max_job_duration is not None:
                    ran_for = time.monotonic() - started
                    if ran_for >= self.max_job_duration.total_seconds():
                        logger.error(
                            "job %s: handler has run %.0fs, past max_job_duration "
                            "%.0fs; stopping the heartbeat so the lease can "
                            "expire and another worker can reclaim worker=%s "
                            "org=%s repo=%s",
                            job.id,
                            ran_for,
                            self.max_job_duration.total_seconds(),
                            self.worker_id,
                            job.organization_id,
                            job.repository_id,
                        )
                        lease_lost.set()
                        return
                try:
                    if _is_dead(conn):
                        # Reopen in place: the "heals at the next job"
                        # property does nothing for the job in flight.
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001 - a dead socket
                            pass
                        conn = self._connect(HEARTBEAT_APPLICATION_NAME)
                    with _unscoped(conn) as cur:
                        cur.execute(
                            HEARTBEAT_SQL,
                            (_interval(self.lease), str(job.id), self.worker_id),
                        )
                        extended = cur.fetchone() is not None
                except Exception as exc:  # noqa: BLE001 - a blip is not a loss
                    failures += 1
                    logger.warning(
                        "job %s: heartbeat failed (%d in a row) worker=%s: %s",
                        job.id,
                        failures,
                        self.worker_id,
                        sanitize_error(exc),
                    )
                    silent_for = time.monotonic() - last_ok
                    if (
                        silent_for >= self.lease.total_seconds()
                        or failures >= MAX_HEARTBEAT_FAILURES
                    ):
                        logger.error(
                            "job %s: no heartbeat has landed for %.1fs (%d "
                            "consecutive failures) and the lease is %.1fs; "
                            "assuming it is lost worker=%s org=%s repo=%s",
                            job.id,
                            silent_for,
                            failures,
                            self.lease.total_seconds(),
                            self.worker_id,
                            job.organization_id,
                            job.repository_id,
                        )
                        lease_lost.set()
                        return
                    continue
                failures = 0
                last_ok = time.monotonic()
                if not extended:
                    logger.warning(
                        "job %s: heartbeat matched no row -- superseded or "
                        "reclaimed; aborting worker=%s org=%s repo=%s "
                        "attempt=%d/%d",
                        job.id,
                        self.worker_id,
                        job.organization_id,
                        job.repository_id,
                        job.attempts,
                        job.max_attempts,
                    )
                    lease_lost.set()
                    return
                logger.debug(
                    "job %s: lease extended worker=%s", job.id, self.worker_id
                )
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            logger.error(
                "job %s: heartbeat thread stopped worker=%s: %s",
                job.id,
                self.worker_id,
                sanitize_error(exc),
            )
        finally:
            if conn is not None:
                conn.close()
