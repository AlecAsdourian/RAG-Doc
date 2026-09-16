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
  3. **No handler for this job type** -> `fail` it with `UnknownJobType`.
     Defensive only: `workers/__main__` refuses to start with an empty
     registry, so a deployed worker cannot reach this.
  4. **Check the installation**, per the table below.
  5. **`mark_started`** -- and not before step 4. See the table.
  6. **Start the heartbeat thread**, on ITS OWN CONNECTION.
  7. **Run the handler.** Returned normally -> `complete` with whatever
     `write_results` it gave back, UNLESS the lease was lost meanwhile, in
     which case write nothing. Raised -> `fail`. `LeaseLost` from either
     -> log a warning and carry on; the job belongs to someone else now.
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

`should_abort()` is true while the worker is shutting down as well as when
the lease is lost, because a handler wants ONE signal meaning "wind down".
⚠ THE TWO ARE NOT THE SAME TO THE WORKER, and the completion decision uses
only the lease: a handler that finishes during a shutdown gets its job
COMPLETED ("finish the current job"), while a handler that returns without
finishing must RAISE, so the attempt is recorded and retried rather than a
half-done job being written `completed`.

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
from psycopg2.extras import Json, RealDictCursor

from workers.db import require_tenant
from workers.jobs.transitions import (
    Job,
    LeaseLost,
    _interval,
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

#: One twelfth of the lease, so four consecutive heartbeats may be lost
#: before the job is reclaimable. Also the sweeper's schedule: the sweep is
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
#: ⚠ A HANDLER THAT STOPS EARLY MUST RAISE, NOT RETURN. Returning means
#: "the job is done"; the worker cannot tell an unfinished return from a
#: finished one, and a shutdown is not a reason to write `completed` over
#: work that did not happen. Raising records the attempt and retries it.
Handler = Callable[["JobContext"], Optional[WriteResults]]


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
        """True when the handler should stop and return as soon as it can.

        Two causes, one signal:

        - **The lease is gone.** The heartbeat's fenced `UPDATE` matched no
          row, so this job was superseded (a relink, L4) or reclaimed. The
          worker will write NOTHING when the handler returns, so every
          second spent after this is wasted.
        - **The worker is shutting down.** SIGTERM arrived.

        ⚠ A HANDLER THAT RETURNS BECAUSE OF THIS, WITHOUT HAVING FINISHED,
        MUST RAISE INSTEAD. See `Handler`. On the lease-lost path it makes
        no difference -- the worker writes nothing either way -- but on the
        shutdown path a bare return is indistinguishable from success.
        """
        return self._lease_lost.is_set() or self._stopping.is_set()

    def report_progress(self, stage: str, progress: Optional[dict] = None) -> bool:
        """Record coarse progress on the job row. Fenced. Returns whether it landed.

        `last_stage` is L2's resumability breadcrumb (`clone|parse|embed|
        store`) and `progress` its detail (`files_parsed`, `chunks_embedded`,
        `current_file`). Both are read by 21-07's admin endpoint.

        ⚠ IT RUNS ON THE WORKER'S MAIN CONNECTION, which is idle for as
        long as a handler is running: every transition opens and closes its
        own transaction, and the handler runs between two of them. That is
        checked rather than assumed -- `_unscoped` refuses a connection
        that is not idle -- so a future caller that gets this wrong fails
        loudly instead of silently committing someone else's work.

        ⚠ NO PAYLOAD-SHAPED VALUES. `progress` is written to a column an
        admin endpoint hands back, so it is for counters and file paths,
        not for anything a token could be hiding in.

        Returns:
            True if the row was updated; False if the lease was lost, in
            which case the abort flag is raised so `should_abort()` tells
            the handler the same thing.
        """
        with _unscoped(self._conn) as cur:
            cur.execute(
                PROGRESS_SQL,
                (
                    stage,
                    Json(progress) if progress is not None else None,
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
                stage,
                self.worker_id,
                self.job.repository_id,
            )
            self._lease_lost.set()
            return False

        logger.info(
            "job %s: stage=%s org=%s repo=%s attempt=%d/%d worker=%s",
            self.job.id,
            stage,
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
        conn = psycopg2.connect(self.dsn)
        last_sweep: Optional[float] = None
        try:
            while not stop.is_set():
                try:
                    last_sweep = self._maybe_sweep(conn, last_sweep)
                    job = claim(conn, self.worker_id, self.lease)
                    if job is None:
                        stop.wait(self._idle_delay())
                        continue
                    self._run_job(conn, job, stop)
                except Exception as exc:  # noqa: BLE001 - see the docstring
                    logger.error(
                        "worker %s: loop iteration failed, continuing: %s",
                        self.worker_id,
                        sanitize_error(exc),
                    )
                    stop.wait(self._idle_delay())
        finally:
            conn.close()
        logger.info("worker %s stopped", self.worker_id)

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
        if handler is None:
            # Defensive. `__main__` cannot start with an empty registry and
            # 000014's CHECK bounds `job_type` to the two keys Phase 22
            # registers, so reaching this means a caller built a Worker by
            # hand with a partial map.
            self._fail(conn, job, UnknownJobType(f"no handler for {job.job_type}"))
            return

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
            logger.warning("job %s: handler lost the lease: %s", job.id, exc)
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
            logger.warning("job %s: %s wrote nothing: %s", job.id, transition, exc)

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
        """
        conn = None
        interval = self.heartbeat.total_seconds()
        try:
            conn = psycopg2.connect(self.dsn)
            while not finished.wait(interval):
                try:
                    with _unscoped(conn) as cur:
                        cur.execute(
                            HEARTBEAT_SQL,
                            (_interval(self.lease), str(job.id), self.worker_id),
                        )
                        extended = cur.fetchone() is not None
                except Exception as exc:  # noqa: BLE001 - a blip is not a loss
                    # Deliberately NOT an abort. The lease is still ours as
                    # far as the database is concerned; if the connection
                    # stays broken the lease simply expires and another
                    # worker reclaims, which is the crash path the sweeper
                    # and the reclaim branch already handle.
                    logger.warning(
                        "job %s: heartbeat failed worker=%s: %s",
                        job.id,
                        self.worker_id,
                        sanitize_error(exc),
                    )
                    continue
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
