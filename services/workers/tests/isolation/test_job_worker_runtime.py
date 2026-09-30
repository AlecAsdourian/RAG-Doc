"""The worker runtime, against a real PostgreSQL 16.

21-05 proved the transitions one statement at a time. This file proves the
PROCESS: that a claimed job reaches a handler, that the heartbeat keeps a
long job's lease alive and notices when it is taken away, that a dead
worker's job is reclaimed and eventually dead-lettered, that a suspended
installation waits instead of failing, and that `python -m workers` refuses
to start while nothing knows how to run a job.

⚠ THIS FILE IS WHAT PINS `FOR UPDATE SKIP LOCKED` FROM PYTHON.
`test_job_transitions.py`'s docstring says so in as many words: nothing
there runs two claims concurrently, so deleting the clause leaves that
suite green, and 21-05 deliberately left the gap rather than writing a test
that could not fail. `test_eight_threads_racing_for_one_job_...` below is
the test it names -- eight threads, their own connections, released
together through a `threading.Barrier`, five rounds on a warm pool. A
single cold round proves nothing (21-CONTEXT): the first round pays for
connection setup, which spreads the threads out and hides exactly the
contention the test exists to create.

⚠ TIMINGS ARE SECONDS HERE, NOT MINUTES. Every duration is a constructor
parameter for this reason. Lease 2s, heartbeat 0.5s, idle poll 0.1s,
suspended deferral 1s. Nothing sleeps for a fixed period waiting for a
state change: `until()` polls against a deadline, so a slow machine takes
longer rather than failing.

⚠ A WORKER CLAIMS QUEUE-WIDE, and this suite starts real ones. The claim
carries no organization filter and `ingestion_jobs` has no row-level
security (21-CONTEXT L5), so a `Worker` here would happily pick up a job
some other test left behind and run THIS test's handler on it. Two things
stop that being silent: every test backdates its own job to
`CLAIM_TEST_EPOCH` so it sorts first, and `assert_only_claimable` turns "my
job is the only claimable row" from an assumption into a failure message.
Nothing in this file DELETES a row it did not create -- the transitions
suite's claim tests and `pkg/jobs`' depend on the queue not being emptied.

⚠ `assert_only_claimable` CARRIES ITS OWN COPY OF THE CLAIMABLE PREDICATE,
so it evaluates the UNMUTATED rule and can never catch a mutation of
`CLAIM_SQL`. It is a premise helper, not a test. PR #41's review found the
same shape in `assert_no_older_claimable`; the note is here so the next
reader does not mistake it for coverage.
"""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable, List, Optional

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.jobs import Unfinished, Worker, claim, new_worker_id
from workers.jobs.runtime import (
    CONNECT_TIMEOUT_SECONDS,
    HEARTBEAT_APPLICATION_NAME,
    LOOP_APPLICATION_NAME,
    InstallationSuspended,
    InstallationUninstalled,
    Rejected,
)
from workers.jobs.transitions import ENQUEUE_UPSERT_SQL, LeaseLost

# Same decade-in-the-past epoch `test_job_transitions.py` and 21-03 use, and
# for the same reason: the claim query has no repository filter, because
# production has one queue.
CLAIM_TEST_EPOCH = "2015-01-01 00:00:00+00"

# The short timings. See the module docstring.
LEASE = timedelta(seconds=2)
HEARTBEAT = timedelta(seconds=0.5)
IDLE_POLL = timedelta(seconds=0.1)
SUSPENDED_DEFER = timedelta(seconds=1)

# How long any `until()` waits before calling it a failure. Generous,
# because it costs nothing when the assertion holds and it is the
# difference between a flaky suite and a slow one on a loaded CI runner.
SETTLE = 30.0

# A token-shaped string, so a handler that raises one proves the redaction
# reaches `last_error` through the runtime too. Not a real credential.
FAKE_TOKEN = "ghs_" + "0123456789abcdefghijABCDEFGHIJ012345"

# services/workers, for the `python -m workers` subprocess.
SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[2]


# =====================================================================
# Fixtures
# =====================================================================


# The DSN forced onto the NOSUPERUSER app role is conftest.py's `app_dsn`
# fixture (lifted from here in 22-03). ITS `options` PARAMETER IS
# LOAD-BEARING: a `Worker` opens its own connections from a DSN, so there is
# no cursor to `SET ROLE` on, and the superuser would bypass row-level
# security even with FORCE -- every tenant assertion below, above all the
# installation read scoped by `require_tenant`, would pass for the wrong
# reason. `test_the_worker_connects_as_the_unprivileged_app_role` asserts it
# rather than trusting it.


@pytest.fixture
def conn(app_dsn: str):
    """The test's own connection, separate from every worker's."""
    connection = psycopg2.connect(app_dsn)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def superuser_conn(test_db_container):
    """A connection that can `pg_terminate_backend`, which `rag_doc_app` cannot.

    ⚠ DELIBERATELY WITHOUT the `role` option every other connection here
    carries. Terminating another backend needs superuser or
    `pg_signal_backend`, and `rag_doc_app` is NOSUPERUSER on purpose --
    that is the whole reason the rest of this file uses it. The container's
    own `isolation` account is the superuser, and it is used for nothing
    except killing connections.

    It is how the two failure tests below reproduce a Postgres restart, a
    failover or an idle-connection reaper, which is otherwise not
    something a test can cause.
    """
    connection = psycopg2.connect(
        test_db_container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
    )
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


# =====================================================================
# Helpers
# =====================================================================


def sql(connection: Any, statement: str, params: tuple = ()) -> None:
    """Run one unscoped statement and commit it.

    Only ever pointed at `ingestion_jobs`, which has no row-level security.
    Used to manufacture states no producer can make -- an exhausted attempt
    counter, a supersede, a reset between race rounds.
    """
    with connection.cursor() as cur:
        cur.execute(statement, params)
    connection.commit()


def query(connection: Any, statement: str, params: tuple = ()) -> list:
    with connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
    connection.commit()
    return rows


def seed_job(connection: Any, org: Any, job_type: str = "full_ingest") -> str:
    """Enqueue one job exactly as the Go producer does, and return its id."""
    with require_tenant(connection, org.id) as cur:
        cur.execute(ENQUEUE_UPSERT_SQL, (org.id, org.repo_id, job_type))
        job_id, was_existing = cur.fetchone()
        if not was_existing:
            cur.execute(
                "UPDATE repositories SET sync_state = 'pending', updated_at = NOW() "
                "WHERE id = %s",
                (org.repo_id,),
            )
    return job_id


def backdate(connection: Any, job_id: str) -> None:
    """Make this job the oldest claimable row in the queue."""
    sql(
        connection,
        "UPDATE ingestion_jobs SET run_after = TIMESTAMPTZ %s WHERE id = %s",
        (CLAIM_TEST_EPOCH, job_id),
    )


def link_installation(
    connection: Any,
    org: Any,
    *,
    suspended: bool = False,
    uninstalled: bool = False,
) -> str:
    """Give this org's repository a GitHub App installation, and return its id.

    ⚠ WITHOUT THIS EVERY REPOSITORY IN THE SHARED FIXTURE HAS
    `installation_id IS NULL`, which is the ABANDON branch -- so a suite
    built on the bare fixture would take the stand-down path in every test
    and could not tell `defer` from `abandon` from `run`. That is the
    fixture question 21-04 and 21-05 both got wrong once: what can this
    setup NOT distinguish?

    Both writes are tenant-scoped: `github_installations` carries FORCE
    ROW LEVEL SECURITY, and `repositories.installation_id` fires
    `trg_assert_installation_tenant` (000010 section 4).
    """
    github_id = int(time.time() * 1000) % 2_000_000_000 + int.from_bytes(
        os.urandom(2), "big"
    )
    with require_tenant(connection, org.id) as cur:
        cur.execute(
            """
            INSERT INTO github_installations
                (organization_id, github_installation_id, account_login,
                 account_type, repository_selection, suspended_at, uninstalled_at)
            VALUES (%s, %s, %s, 'Organization', 'all',
                    CASE WHEN %s THEN NOW() END,
                    CASE WHEN %s THEN NOW() END)
            RETURNING id::text
            """,
            (org.id, github_id, org.slug, suspended, uninstalled),
        )
        installation_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE repositories SET installation_id = %s, updated_at = NOW() "
            "WHERE id = %s",
            (installation_id, org.repo_id),
        )
    return installation_id


def set_suspension(connection: Any, org: Any, installation_id: str, on: bool) -> None:
    with require_tenant(connection, org.id) as cur:
        cur.execute(
            "UPDATE github_installations SET suspended_at = CASE WHEN %s THEN NOW() END, "
            "updated_at = NOW() WHERE id = %s",
            (on, installation_id),
        )


def installation_row(connection: Any, org: Any, repository_id: str) -> dict:
    """What the worker's claim-time read will see, read the same way it reads it."""
    with require_tenant(connection, org.id, cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT r.installation_id::text AS installation_id,
                   gi.suspended_at, gi.uninstalled_at
            FROM repositories r
            LEFT JOIN github_installations gi ON gi.id = r.installation_id
            WHERE r.id = %s
            """,
            (repository_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"repository {repository_id} is invisible under {org.id}"
    return dict(row)


def job_row(connection: Any, job_id: str) -> dict:
    rows = query(connection, "SELECT * FROM ingestion_jobs WHERE id = %s", (job_id,))
    assert rows, f"job {job_id} disappeared"
    return rows[0]


def repo_row(connection: Any, org_id: str, repository_id: str) -> dict:
    """Read a repository UNDER ITS OWN TENANT SCOPE.

    `repositories` is FORCE ROW LEVEL SECURITY, so an unscoped read returns
    nothing and every "unchanged" assertion would pass for the wrong reason.
    """
    with require_tenant(connection, org_id, cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT sync_state, last_synced_at FROM repositories WHERE id = %s",
            (repository_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"repository {repository_id} is invisible under {org_id}"
    return dict(row)


def assert_only_claimable(connection: Any, job_id: str) -> None:
    """The premise every worker test here rests on: my job is the only one.

    A `Worker` claims whatever the queue offers, so "the worker ran my
    handler on my job" is only sound while my job is the only claimable
    row. A row leaked by a crashed run would break that quietly.

    ⚠ IT CARRIES ITS OWN COPY OF THE PREDICATE and is therefore blind to a
    mutation of `CLAIM_SQL`'s. See the module docstring.
    """
    rows = query(
        connection,
        """
        SELECT id::text FROM ingestion_jobs
        WHERE attempts < max_attempts
          AND ((state = 'queued' AND run_after <= NOW())
            OR (state = 'running'
                AND (lease_expires_at IS NULL OR lease_expires_at < NOW())))
          AND id <> %s
        """,
        (job_id,),
    )
    assert rows == [], (
        "another claimable job exists, so 'the worker claimed my job' proves "
        f"nothing. Leaked rows: {rows}"
    )


def job_when(
    connection: Any,
    job_id: str,
    predicate: Callable[[dict], bool],
    what: str,
    timeout: float = SETTLE,
) -> dict:
    """Poll the job row until `predicate` holds, and return THAT row.

    One read per poll, so every field asserted afterwards comes from the
    same snapshot. Three separate `job_row` calls inside one condition can
    each see a different row, which is how a timing test ends up asserting
    a state that never existed at once.
    """

    def check():
        row = job_row(connection, job_id)
        return row if predicate(row) else None

    return until(check, what, timeout)


def kill_backends(superuser: Any, application_name: str) -> int:
    """`pg_terminate_backend` every backend with this `application_name`.

    The worker tags its two connections differently -- `rag-doc-worker` for
    the loop and `rag-doc-worker-heartbeat` for the beat -- so a test can
    kill exactly one of them and watch the right recovery path. That tag is
    not test scaffolding: it is what `pg_stat_activity` shows an operator
    trying to work out which connection is wedged.

    Returns how many were killed, so a caller can assert it actually hit
    something. A test that silently kills nothing passes for free.
    """
    with superuser.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s AND pid <> pg_backend_pid()",
            (application_name,),
        )
        return len(cur.fetchall())


def until(predicate: Callable[[], Any], what: str, timeout: float = SETTLE) -> Any:
    """Poll `predicate` against a deadline. Returns its first truthy value.

    ⚠ NOT A SLEEP. A fixed sleep long enough to be reliable on a loaded
    runner is long enough to make the suite unpleasant, and one short
    enough to be pleasant is a flake. This is the shape every timing
    assertion in this file uses.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last={last!r})")


def build_worker(app_dsn: str, handlers: dict, **kwargs) -> Worker:
    """A `Worker` on this file's short timings."""
    kwargs.setdefault("lease", LEASE)
    kwargs.setdefault("heartbeat", HEARTBEAT)
    kwargs.setdefault("idle_poll", IDLE_POLL)
    kwargs.setdefault("suspended_defer", SUSPENDED_DEFER)
    return Worker(app_dsn, handlers, **kwargs)


@contextmanager
def running(worker: Worker):
    """Run a worker in a thread; stop it and join on the way out.

    Yields the `stop` event, so a test can shut the worker down inside the
    block and watch what it does.
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=worker.run,
        args=(stop,),
        name=f"worker-{worker.worker_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield stop
    finally:
        stop.set()
        thread.join(timeout=SETTLE)
        alive = thread.is_alive()

    # ⚠ OUTSIDE THE `finally`, DELIBERATELY. An exception raised in a
    # `finally` REPLACES the one already propagating, so asserting there
    # hid every real failure inside the block behind "the worker did not
    # return" -- which is usually a CONSEQUENCE of the real failure, since
    # a test that fails mid-block never releases the handler it is
    # blocking. Cost two debugging rounds while these tests were written.
    # Code after a try/finally does not run when the body raised, which is
    # exactly the behaviour wanted here.
    assert not alive, (
        f"worker {worker.worker_id} did not return after stop was set"
    )


class Probe:
    """A `write_results` callback that records being CALLED, and leaves a row.

    ⚠ THE CALL COUNT IS THE POINT, and the row alone would not be. A worker
    that skips the abort check still writes nothing -- `complete`'s fence
    matches no row and `LeaseLost` rolls the callback's write back -- so
    "the probe row is absent" is true whether the abort check exists or
    not. What differs is whether the callback RAN. `calls` is how the
    supersede test can tell the two apart, and without it the mutation that
    deletes the check survives the whole suite.

    It writes to `ingestion_runs`, which carries row-level security and
    `trg_assert_tenant`, so the row also proves the callback ran inside the
    tenant scope `complete` opened.
    """

    def __init__(self, repository_id: str, commit_sha: str) -> None:
        self.repository_id = repository_id
        self.commit_sha = commit_sha
        self.calls = 0

    def __call__(self, cur: Any) -> None:
        self.calls += 1
        cur.execute(
            "INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status) "
            "VALUES (%s, %s, 'main', 'processing')",
            (self.repository_id, self.commit_sha),
        )


def runs_for(connection: Any, org_id: str, commit_sha: str) -> list:
    with require_tenant(connection, org_id) as cur:
        cur.execute(
            "SELECT id::text FROM ingestion_runs WHERE commit_sha = %s", (commit_sha,)
        )
        return [r[0] for r in cur.fetchall()]


class Recorder:
    """A handler that records what it saw and returns a `write_results` probe."""

    def __init__(self, probe: Optional[Probe] = None) -> None:
        self.probe = probe
        self.job_ids: List[str] = []
        self.saw_abort_at_return: Optional[bool] = None
        self.saw_shutdown_at_return: Optional[bool] = None

    def __call__(self, ctx) -> Optional[Probe]:
        self.job_ids.append(str(ctx.job.id))
        self.saw_abort_at_return = ctx.should_abort()
        self.saw_shutdown_at_return = ctx.is_shutting_down()
        return self.probe


class NeverCalled:
    """A handler that must not run -- and that RECORDS it if it does.

    ⚠ RAISING IS NOT ENOUGH, and a plain `assert False` handler would be a
    test that cannot fail. `_invoke` catches EVERY exception a handler
    raises and turns it into `fail`, which is exactly right for a real
    handler and swallows an assertion whole. So the test reads `calls`
    afterwards instead of trusting the raise to escape.
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __call__(self, ctx):
        self.calls.append(str(ctx.job.id))
        raise AssertionError(
            f"the handler ran for job {ctx.job.id}, and this test says it must not"
        )


# =====================================================================
# Premises
# =====================================================================


def test_the_worker_connects_as_the_unprivileged_app_role(app_dsn):
    """Every RLS assertion in this file rests on this, so it is asserted.

    A PostgreSQL superuser bypasses row-level security even under FORCE. If
    the DSN a `Worker` is given connected as the container superuser, the
    claim-time installation read would see every tenant's rows, the tenant
    trigger would never fire, and each isolation assertion below would pass
    for a reason that has nothing to do with the code.
    """
    connection = psycopg2.connect(app_dsn)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT current_user, "
                "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            )
            user, is_super, bypasses_rls = cur.fetchone()
        connection.commit()
    finally:
        connection.close()

    assert user == "rag_doc_app", (
        f"the worker DSN connects as {user!r}; the `options=-c role=...` "
        "parameter in the `app_dsn` fixture is not taking effect"
    )
    assert is_super is False, "the worker role must not bypass row-level security"
    assert bypasses_rls is False


def test_an_unset_max_job_duration_is_announced_rather_than_silent(
    conn, app_dsn, with_two_orgs, caplog
):
    """The omission has to be visible, and only a log record can show it.

    Shipping the mechanism without a number is the right call — it is a
    multiple of a typical ingest and nothing has ingested end to end yet,
    the same discipline that withdrew 21-RESEARCH's pool arithmetic. But
    silence would let whoever writes Phase 22 skip the hand-off step and
    never know the hazard had shipped: a hung handler holds its lease
    indefinitely and stops that worker sweeping.

    ⚠ IT READS THE ACTUAL `LogRecord`, for the same reason the supersede
    test does. A docstring saying "we warn about this" is not a warning.
    """
    org, _ = with_two_orgs
    caplog.set_level(logging.INFO, logger="workers.jobs.runtime")

    with running(build_worker(app_dsn, {"full_ingest": lambda ctx: None})):
        warnings = until(
            lambda: [
                record.getMessage()
                for record in caplog.records
                if record.levelno == logging.WARNING
                and "no upper bound on handler runtime" in record.getMessage()
            ],
            "the missing-bound warning",
        )
        startup = [
            record.getMessage()
            for record in caplog.records
            if "starting: job_types=" in record.getMessage()
        ]

    assert len(warnings) == 1, warnings
    assert "Set max_job_duration" in warnings[0]
    assert startup and "max_job_duration=none" in startup[0], (
        f"the startup line does not say whether a bound is set: {startup}"
    )

    # And with one set, the line says so and nothing warns.
    caplog.clear()
    bounded = build_worker(
        app_dsn,
        {"full_ingest": lambda ctx: None},
        max_job_duration=timedelta(seconds=120),
    )
    with running(bounded):
        startup = until(
            lambda: [
                record.getMessage()
                for record in caplog.records
                if "starting: job_types=" in record.getMessage()
            ],
            "the second worker's startup line",
        )

    assert "max_job_duration=120s" in startup[0]
    assert not [
        record
        for record in caplog.records
        if "no upper bound on handler runtime" in record.getMessage()
    ]


def test_require_tenant_says_a_dead_connection_is_dead(conn, superuser_conn):
    """`require_tenant` carries the same message `_unscoped` learned.

    ⚠ IT IS THE ONE AN OPERATOR ACTUALLY READS. Every terminal write and
    the claim-time installation read go through `require_tenant` —
    `mark_started`, `complete`, `fail`, `defer`, `abandon` — so a
    connection that dies MID-JOB produced "psycopg2's `with conn:` idiom
    does not nest" for precisely the failure the new message exists for.
    `_unscoped` was fixed in the first round and this was missed.

    Recovery never depended on the message (`_is_dead` drives it), so this
    is cosmetic — which is why it is asserted on the text.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    conn.commit()

    with superuser_conn.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
        assert cur.fetchone()[0] is True, "the test's own backend was not killed"

    # Make psycopg2 notice. `transaction_status` is a client-side cached
    # value, so it still reads IDLE until an operation touches the socket —
    # which is exactly why the loop met this on its NEXT iteration rather
    # than on the one where the backend died. No `rollback()` afterwards:
    # psycopg2 has closed the connection by then and rolling back raises.
    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

    # Any valid UUID: `require_tenant` validates the id and THEN checks the
    # connection, so no tenant has to exist for this to be the real path.
    with pytest.raises(psycopg2.InterfaceError, match="no longer usable"):
        with require_tenant(conn, "00000000-0000-4000-8000-000000000000"):
            pass  # pragma: no cover - the scope must never open


def test_a_worker_refuses_an_empty_handler_map(app_dsn):
    """The backstop for a caller that builds a `Worker` directly.

    `__main__` refuses earlier and more loudly, but a `Worker` with no
    handler fails every job it claims `max_attempts` times and dead-letters
    it -- the accident this whole plan is arranged around.
    """
    with pytest.raises(ValueError, match="at least one handler"):
        Worker(app_dsn, {})


# =====================================================================
# The happy path
# =====================================================================


def test_a_claimed_job_runs_its_handler_and_completes(conn, app_dsn, with_two_orgs):
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    probe = Probe(org.repo_id, f"sha-happy-{job_id[:8]}")
    handler = Recorder(probe)

    with running(build_worker(app_dsn, {"full_ingest": handler})):
        until(
            lambda: job_row(conn, job_id)["state"] == "completed",
            "the job to complete",
        )

    row = job_row(conn, job_id)
    assert handler.job_ids == [job_id], (
        f"the handler ran for {handler.job_ids}, not this test's job {job_id}"
    )
    assert probe.calls == 1, "the completion must run the handler's write_results once"
    assert runs_for(conn, org.id, probe.commit_sha), (
        "the probe row is missing, so `write_results` did not commit with the "
        "completion"
    )
    assert row["attempts"] == 1
    assert row["lease_owner"] is None
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "synced"


def test_the_heartbeat_extends_the_lease_of_a_job_that_outlives_it(
    conn, app_dsn, with_two_orgs
):
    """A handler that runs for three leases still completes, and the lease moved.

    ⚠ "IT COMPLETED" IS NOT THE ASSERTION, and on its own it would prove
    nothing: `COMPLETE_SQL`'s fence is `lease_owner` and `state`, not the
    expiry, so a job whose lease quietly ran out but that NOBODY reclaimed
    still completes. What the heartbeat buys is that the row does not sit
    there claimable while its worker is busy -- so the assertion is that
    `lease_expires_at` moved FORWARD while the handler was running.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    duration = 3 * LEASE.total_seconds()
    elapsed: List[float] = []
    probe = Probe(org.repo_id, f"sha-beat-{job_id[:8]}")

    def slow_handler(ctx):
        started = time.monotonic()
        while time.monotonic() - started < duration:
            time.sleep(0.05)
        elapsed.append(time.monotonic() - started)
        return probe

    with running(build_worker(app_dsn, {"full_ingest": slow_handler})):
        first = until(
            lambda: job_row(conn, job_id)["lease_expires_at"], "the job to be claimed"
        )
        # ⚠ THE ONLY PLACE `syncing` IS OBSERVABLE, and therefore the only
        # test that can tell `mark_started` was called at all: every other
        # test here reads `sync_state` after the job has reached a terminal
        # state, which overwrites it. A long handler is what holds the
        # projection still long enough to look at.
        until(
            lambda: repo_row(conn, org.id, org.repo_id)["sync_state"] == "syncing",
            "mark_started to project `syncing`",
        )
        until(
            lambda: job_row(conn, job_id)["lease_expires_at"] > first,
            "the heartbeat to push the lease out",
        )
        until(
            lambda: job_row(conn, job_id)["state"] == "completed",
            "the long job to complete",
            timeout=SETTLE + duration,
        )

    assert elapsed and elapsed[0] >= duration, (
        f"the handler ran {elapsed}s, which is not longer than the "
        f"{LEASE.total_seconds()}s lease it is supposed to outlive"
    )
    assert probe.calls == 1


def test_the_heartbeat_thread_opens_its_own_connection(
    conn, app_dsn, with_two_orgs, monkeypatch
):
    """The one invariant here whose failure mode is a race, not a result.

    psycopg2 connections may be shared between threads, and sharing one
    here would be a DATA-LOSS bug rather than a slow one: a connection
    shares its TRANSACTION, so a heartbeat's commit would commit whatever
    `complete` had half-written on the main connection -- or its rollback
    would throw the results away. Neither shows up as a wrong value; both
    show up as an intermittently wrong database.

    ⚠ SO THIS TEST COUNTS CONNECTIONS, which is a structural assertion and
    is admitted as one. The behaviour it protects cannot be provoked
    reliably -- `require_tenant` and `_unscoped` both refuse a connection
    that is mid-transaction, so the shared version fails LOUDLY on some
    interleavings and silently on others, and a test that waits for the
    unlucky one is a flake. Counting is what makes the invariant killable
    by a mutation at all.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    opened: List[Any] = []
    real_connect = psycopg2.connect

    def counting(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(psycopg2, "connect", counting)

    running_long = threading.Event()
    may_finish = threading.Event()

    def slow(ctx):
        running_long.set()
        may_finish.wait(timeout=SETTLE)
        return None

    with running(build_worker(app_dsn, {"full_ingest": slow})):
        assert running_long.wait(timeout=SETTLE), "the handler never started"
        # Both connections are open right now: the loop's and the
        # heartbeat thread's.
        until(lambda: len(opened) >= 2, "the heartbeat to open a connection")
        may_finish.set()
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    assert len({id(c) for c in opened}) == len(opened), (
        "the worker handed the same connection out twice"
    )
    assert len(opened) >= 2, (
        f"the worker opened {len(opened)} connection(s) for one job; the "
        "heartbeat must not run on the loop's"
    )


# =====================================================================
# Supersede
# =====================================================================


def test_a_supersede_mid_run_aborts_the_handler_and_writes_nothing(
    conn, app_dsn, with_two_orgs
):
    """L4 step 3: a superseded worker stops cooperatively and writes nothing.

    The handler loops on `should_abort()` while the test supersedes the job
    with `supersedeLiveSQL` -- verbatim, so it leaves the lease ATTACHED,
    which is the shape that makes `state = 'running'` the half of the
    heartbeat's fence that does the work.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    # Long enough that a heartbeat which never notices the supersede runs
    # far past the deadline asserted below.
    patience = 20.0
    probe = Probe(org.repo_id, f"sha-super-{job_id[:8]}")
    observed: dict = {}

    def cooperative(ctx):
        started = time.monotonic()
        while not ctx.should_abort() and time.monotonic() - started < patience:
            time.sleep(0.02)
        observed["aborted"] = ctx.should_abort()
        observed["elapsed"] = time.monotonic() - started
        return probe

    with running(build_worker(app_dsn, {"full_ingest": cooperative})):
        until(lambda: job_row(conn, job_id)["state"] == "running", "the claim")
        # `supersedeLiveSQL` from pkg/jobs/producer.go, verbatim: it does
        # NOT clear the lease.
        sql(
            conn,
            "UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW() "
            "WHERE repository_id = %s AND state IN ('queued','running')",
            (org.repo_id,),
        )
        until(lambda: observed.get("aborted") is not None, "the handler to return")

    row = job_row(conn, job_id)
    assert observed["aborted"] is True
    assert observed["elapsed"] < 8 * HEARTBEAT.total_seconds(), (
        f"the handler took {observed['elapsed']:.2f}s to notice a supersede; "
        f"the heartbeat interval is {HEARTBEAT.total_seconds()}s, so this is "
        "the fence in HEARTBEAT_SQL not doing its job"
    )
    # ⚠ THE CALL COUNT, NOT THE ROW. See `Probe`: the row is absent either
    # way, because `complete` would roll it back. That the callback never
    # RAN is what says the worker checked the abort flag before completing.
    assert probe.calls == 0, (
        "the worker ran write_results after losing the lease -- the abort "
        "check before `complete` is missing"
    )
    assert runs_for(conn, org.id, probe.commit_sha) == []
    assert row["state"] == "superseded", "a superseded job must stay superseded"
    assert row["lease_owner"] is not None, (
        "supersedeLiveSQL leaves the lease attached; if this is NULL the test "
        "is no longer exercising the state half of the fence"
    )


def test_a_supersede_is_reported_in_the_log(conn, app_dsn, with_two_orgs, caplog):
    """The supersede's only operator-visible signal, read as a LogRecord.

    ⚠ A TEST THAT READS NO LOG RECORD CANNOT CATCH A LOGGING BUG, which is
    how 21-05 shipped a fix whose mutation survived the whole suite until
    one test read an actual record. A supersede leaves NOTHING behind in
    the database that says a worker was stopped -- the row looks the same
    whether the worker noticed or is still grinding away -- so this line is
    the whole of the evidence, and it has to name the job.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    caplog.set_level(logging.WARNING, logger="workers.jobs.runtime")
    stopped = threading.Event()

    def cooperative(ctx):
        while not ctx.should_abort() and not stopped.wait(0.02):
            pass
        return None

    with running(build_worker(app_dsn, {"full_ingest": cooperative})):
        until(lambda: job_row(conn, job_id)["state"] == "running", "the claim")
        sql(
            conn,
            "UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW() "
            "WHERE id = %s",
            (job_id,),
        )
        messages = until(
            lambda: [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING
                and "heartbeat matched no row" in record.getMessage()
            ],
            "the heartbeat to log the lost lease",
        )
    stopped.set()

    assert len(messages) == 1, messages
    line = messages[0]
    assert job_id in line, f"the log line does not name the job: {line}"
    assert str(org.id) in line and str(org.repo_id) in line, (
        f"the log line carries no tenant or repository: {line}"
    )
    assert "superseded or reclaimed" in line
    assert FAKE_TOKEN not in line


def test_a_handlers_progress_report_lands_on_the_job_row(conn, app_dsn, with_two_orgs):
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    reported: List[bool] = []

    def reporting(ctx):
        reported.append(ctx.report_progress("clone", {"files_parsed": 7}))
        return None

    with running(build_worker(app_dsn, {"full_ingest": reporting})):
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    row = job_row(conn, job_id)
    assert reported == [True]
    assert row["last_stage"] == "clone"
    assert row["progress"] == {"files_parsed": 7}


def test_a_progress_report_from_a_worker_that_lost_its_lease_is_refused(
    conn, app_dsn, with_two_orgs
):
    """`PROGRESS_SQL` is fenced, and `last_stage` is why it has to be.

    L2 calls `last_stage` coarse resumability -- "skip a clone we already
    completed on a retry". A reclaimed worker writing ITS stage onto the
    new attempt's row would make that attempt skip work nobody has done.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    first_done = threading.Event()
    superseded = threading.Event()
    reported: List[bool] = []

    def reporting(ctx):
        reported.append(ctx.report_progress("clone", {"files_parsed": 1}))
        first_done.set()
        superseded.wait(timeout=SETTLE)
        reported.append(ctx.report_progress("embed", {"files_parsed": 99}))
        return None

    with running(build_worker(app_dsn, {"full_ingest": reporting})):
        assert first_done.wait(timeout=SETTLE), "the handler never reported"
        sql(
            conn,
            "UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW() "
            "WHERE id = %s",
            (job_id,),
        )
        superseded.set()
        until(lambda: len(reported) == 2, "the second progress report")

    row = job_row(conn, job_id)
    assert reported == [True, False]
    assert row["last_stage"] == "clone", (
        "a worker that no longer owns the job overwrote last_stage"
    )
    assert row["progress"] == {"files_parsed": 1}


# =====================================================================
# Crash, reclaim, dead-letter
# =====================================================================


def test_an_expired_lease_is_reclaimed_until_the_job_dead_letters(
    conn, app_dsn, with_two_orgs
):
    """The crash path end to end: reclaim, reclaim, exhaust, sweep.

    ⚠ HOW A DEAD WORKER IS SIMULATED, AND WHY IT NEEDS NO HOOK IN THE
    RUNTIME. A process that has died stops extending its lease; a worker
    whose heartbeat interval is longer than its lease never extends one.
    The three blocked workers below are built with `heartbeat=30s` against
    a 2s lease, so not one beat lands before the lease expires -- which is
    indistinguishable, from the database's side, from the process being
    gone. A boolean `disable_heartbeat` flag would have been a second code
    path that production never runs.

    `max_attempts` is forced to 3 so the exhaustion is three claims rather
    than five.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    sql(conn, "UPDATE ingestion_jobs SET max_attempts = 3 WHERE id = %s", (job_id,))
    assert_only_claimable(conn, job_id)

    release = threading.Event()
    probes = [Probe(org.repo_id, f"sha-dead-{job_id[:8]}-{i}") for i in range(3)]
    claimed: List[str] = []

    def blocking(probe: Probe):
        def handler(ctx):
            claimed.append(str(ctx.job.id))
            release.wait(timeout=SETTLE)
            return probe

        return handler

    dead_workers = [
        build_worker(
            app_dsn,
            {"full_ingest": blocking(probe)},
            heartbeat=timedelta(seconds=30),
        )
        for probe in probes
    ]

    with running(dead_workers[0]), running(dead_workers[1]), running(dead_workers[2]):
        until(
            lambda: job_row(conn, job_id)["attempts"] == 3,
            "three claims (one per expired lease)",
        )
        assert job_row(conn, job_id)["state"] == "running"

        # A fourth worker, alive and sweeping on the heartbeat schedule.
        # It can never CLAIM this job -- `attempts < max_attempts` is false
        # now -- so `idle_sweeper` doubles as the assertion that the claim
        # guard holds, and the sweeper is the only thing that can move it.
        idle_sweeper = NeverCalled()
        sweeper = build_worker(app_dsn, {"full_ingest": idle_sweeper})
        with running(sweeper):
            until(
                lambda: job_row(conn, job_id)["state"] == "dead",
                "the sweeper to dead-letter the exhausted job",
            )

        # Only now let the three stale workers try to finish.
        release.set()
        until(
            lambda: all(probe.calls for probe in probes),
            "every stale worker to attempt its completion",
        )

    row = job_row(conn, job_id)
    assert idle_sweeper.calls == [], (
        "the sweeping worker CLAIMED the exhausted job; `attempts < "
        f"max_attempts` should have excluded it. Claims: {idle_sweeper.calls}"
    )
    assert sorted(set(claimed)) == [job_id], f"a worker ran on another job: {claimed}"
    assert len(claimed) == 3
    assert row["state"] == "dead"
    assert row["attempts"] == 3
    for probe in probes:
        assert probe.calls == 1, "each stale worker should have tried exactly once"
        assert runs_for(conn, org.id, probe.commit_sha) == [], (
            "a stale worker's write survived; `complete`'s fence should have "
            "raised LeaseLost and rolled it back"
        )


# =====================================================================
# The claim race
# =====================================================================


def test_eight_threads_racing_for_one_job_produce_exactly_one_claim(
    conn, app_dsn, with_two_orgs
):
    """`FOR UPDATE SKIP LOCKED`, pinned from Python at last.

    Eight threads, each with its OWN connection -- a shared connection
    would serialise them inside psycopg2 and there would be no race to
    lose -- released together through a `threading.Barrier`, over five
    rounds on a pool that is warm after the first.

    Without `FOR UPDATE SKIP LOCKED` the inner `SELECT` takes no lock, so
    all eight `UPDATE`s resolve to the same id, queue on the row lock, and
    each re-checks `id = <that id>` after the winner commits -- which is
    still true. Every one of them then claims, stealing the lease and
    incrementing `attempts` seven extra times. Both assertions below see
    it: one claim, and one attempt.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)

    racers = 8
    rounds = 5
    connections = [psycopg2.connect(app_dsn) for _ in range(racers)]
    workers = [new_worker_id() for _ in range(racers)]
    try:
        for round_number in range(rounds):
            sql(
                conn,
                "UPDATE ingestion_jobs SET state = 'queued', attempts = 0, "
                "lease_owner = NULL, lease_expires_at = NULL WHERE id = %s",
                (job_id,),
            )
            backdate(conn, job_id)
            assert_only_claimable(conn, job_id)

            barrier = threading.Barrier(racers)
            results: List[Optional[Any]] = [None] * racers
            errors: List[Optional[BaseException]] = [None] * racers

            def race(index: int) -> None:
                try:
                    barrier.wait(timeout=SETTLE)
                    results[index] = claim(connections[index], workers[index], LEASE)
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors[index] = exc

            threads = [
                threading.Thread(target=race, args=(i,), name=f"racer-{i}")
                for i in range(racers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=SETTLE)
                assert not thread.is_alive(), "a racer thread hung"

            assert errors == [None] * racers, f"round {round_number}: {errors}"
            winners = [i for i, result in enumerate(results) if result is not None]
            assert len(winners) == 1, (
                f"round {round_number}: {len(winners)} of {racers} threads claimed "
                f"the same job -- FOR UPDATE SKIP LOCKED is not holding"
            )
            won = results[winners[0]]
            assert str(won.id) == job_id
            row = job_row(conn, job_id)
            # ⚠ THE ATTEMPT COUNT IS THE SECOND WITNESS, and it is the one
            # that survives a loser whose UPDATE landed but whose RETURNING
            # this test happened to read as None: every claim that touched
            # the row incremented it, so eight claims read as eight.
            assert row["attempts"] == 1, (
                f"round {round_number}: attempts is {row['attempts']}, so more "
                "than one UPDATE touched the row"
            )
            assert row["lease_owner"] == workers[winners[0]], (
                f"round {round_number}: the row is leased to "
                f"{row['lease_owner']}, not to the thread that claimed it"
            )
            assert row["state"] == "running"
    finally:
        for connection in connections:
            connection.close()


# =====================================================================
# Installation state at claim time
# =====================================================================


def test_a_suspended_installation_defers_without_consuming_an_attempt(
    conn, app_dsn, with_two_orgs
):
    """ISS-033's other half: a suspension heals on its own, so it must wait.

    A `fail` here would consume an attempt, and five suspended hours would
    put a healthy repository in `dead`. `DEFER_SQL` hands the claim's
    attempt back, which is what `attempts == 0` below is asserting.
    """
    org, _ = with_two_orgs
    installation_id = link_installation(conn, org, suspended=True)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    # The premise, so the test cannot pass down the abandon branch instead.
    found = installation_row(conn, org, org.repo_id)
    assert found["installation_id"] == installation_id
    assert found["uninstalled_at"] is None
    assert found["suspended_at"] is not None

    probe = Probe(org.repo_id, f"sha-susp-{job_id[:8]}")
    handler = Recorder(probe)

    with running(build_worker(app_dsn, {"full_ingest": handler})):
        # ⚠ `last_error`, NOT `state == 'queued'`. The job STARTS `queued`
        # with no lease, so a condition on the state alone is satisfied
        # before the worker has even claimed it, and the assertions below
        # would then all run against the seeded row.
        # ⚠ A SETTLED DEFERRED ROW, not just one with a `last_error`. The
        # worker re-claims this job every `suspended_defer` seconds and
        # defers it again, so a read that lands inside one of those claims
        # would see `running` and every assertion below would be about the
        # wrong moment.
        deferred = job_when(
            conn,
            job_id,
            lambda row: row["last_error"] is not None
            and row["state"] == "queued"
            and row["lease_owner"] is None,
            "the job to be deferred",
        )
        assert handler.job_ids == [], "the handler ran for a suspended installation"
        assert deferred["state"] == "queued", (
            "a suspended installation must not fail the job or take it out of "
            "the live set"
        )
        assert deferred["lease_owner"] is None
        assert deferred["attempts"] == 0, (
            "the deferral consumed an attempt; a suspended installation would "
            "dead-letter a healthy repository after max_attempts hours"
        )
        assert deferred["run_after"] > deferred["updated_at"], (
            "the job was not pushed into the future"
        )
        assert "suspended" in (deferred["last_error"] or "")
        # `defer` writes no projection, deliberately: a deferral is not a
        # state change the UI should see.
        assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "pending"

        # Now clear the suspension and let the deferral elapse.
        set_suspension(conn, org, installation_id, False)
        until(
            lambda: job_row(conn, job_id)["state"] == "completed",
            "the unsuspended job to run",
        )

    row = job_row(conn, job_id)
    assert handler.job_ids == [job_id]
    assert probe.calls == 1
    assert row["attempts"] == 1, "the re-run should be the job's first attempt"
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "synced"


@pytest.mark.parametrize("shape", ["uninstalled", "no-installation"])
def test_a_dead_installation_abandons_the_job(conn, app_dsn, with_two_orgs, shape):
    """ISS-033's ending, built: `superseded` and `never_synced`, never `failed`.

    Both shapes are here because the fixture cannot distinguish them on its
    own -- every repository in `with_two_orgs` starts with
    `installation_id IS NULL`, so a suite that never linked one would take
    this branch everywhere and prove nothing about the other two.
    """
    org, _ = with_two_orgs
    if shape == "uninstalled":
        link_installation(conn, org, uninstalled=True)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    found = installation_row(conn, org, org.repo_id)
    if shape == "uninstalled":
        assert found["installation_id"] is not None, (
            "this shape is about an installation that EXISTS and is dead"
        )
        assert found["uninstalled_at"] is not None
    else:
        assert found["installation_id"] is None

    probe = Probe(org.repo_id, f"sha-gone-{job_id[:8]}")
    handler = Recorder(probe)

    with running(build_worker(app_dsn, {"full_ingest": handler})):
        until(
            lambda: job_row(conn, job_id)["state"] == "superseded",
            "the job to be abandoned",
        )

    row = job_row(conn, job_id)
    assert handler.job_ids == [], "the handler ran under a dead installation"
    assert row["state"] == "superseded", "not `dead`: nothing failed"
    assert row["lease_owner"] is None
    # `attempts` is deliberately LEFT ALONE by `abandon` (21-05 deviation 5,
    # upheld by PR #41's review): the row is terminal, so the counter is no
    # longer a budget -- it is the record that a worker claimed this once.
    assert row["attempts"] == 1
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "never_synced", (
        "`failed` is the retry-looking terminal state the uninstall "
        "stand-down exists to forbid"
    )


# =====================================================================
# Shutdown, and the defensive paths
# =====================================================================


def test_a_shutdown_finishes_the_job_in_flight(conn, app_dsn, with_two_orgs):
    """`stop` asks; it does not interrupt. A handler that FINISHES is completed.

    ⚠ AND `should_abort()` STAYS FALSE THROUGHOUT, which is the half PR
    #42's review made the plan enforce. The job is still ours during a
    shutdown; only `is_shutting_down()` goes true. Folding the two together
    is what let an unfinished handler's bare return be written `completed`
    -- see the `Unfinished` test below for the other side of the split.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    in_handler = threading.Event()
    may_finish = threading.Event()
    probe = Probe(org.repo_id, f"sha-stop-{job_id[:8]}")
    handler = Recorder(probe)

    def slow(ctx):
        in_handler.set()
        may_finish.wait(timeout=SETTLE)
        return handler(ctx)

    with running(build_worker(app_dsn, {"full_ingest": slow})) as stop:
        assert in_handler.wait(timeout=SETTLE), "the handler never started"
        stop.set()
        # The worker must not touch the job while the handler still holds it.
        assert job_row(conn, job_id)["state"] == "running"
        may_finish.set()
        until(lambda: job_row(conn, job_id)["state"] == "completed", "the last job")

    assert handler.saw_shutdown_at_return is True, (
        "is_shutting_down() must be true once stop is set"
    )
    assert handler.saw_abort_at_return is False, (
        "should_abort() means 'this job is not ours'; a shutdown does not "
        "take the job away, and conflating the two is what wrote `completed` "
        "over unfinished work"
    )
    assert probe.calls == 1
    assert runs_for(conn, org.id, probe.commit_sha)
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "synced"


def test_a_handler_that_stops_early_on_shutdown_defers_instead_of_completing(
    conn, app_dsn, with_two_orgs
):
    """`Unfinished` is how a handler says "I stopped part-way".

    ⚠ THE ROW THIS PREVENTS was built by PR #42's review against a real
    database, with a Phase-22-shaped handler that stopped after `clone`:

        state=completed  last_stage=parse  sync_state=synced

    -- a job row contradicting itself, `last_synced_at` stamped, the UI
    saying the repository is ingested, and the partial unique index freed
    so nothing re-queues it. A bare `return` means "done"; only a raise can
    mean anything else.

    ⚠ AND IT DEFERS RATHER THAN FAILS, which is the second half. `fail`
    consumes an attempt, so five operator restarts spread across an
    ingest's retries would walk a HEALTHY repository to `dead` -- the same
    shape `defer` exists to prevent for a suspended installation.
    `attempts == 0` below is that property.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    in_handler = threading.Event()
    probe = Probe(org.repo_id, f"sha-unfin-{job_id[:8]}")
    stages: List[str] = []

    def winds_down(ctx):
        ctx.report_progress("clone")
        stages.append("clone")
        in_handler.set()
        while not ctx.is_shutting_down():
            time.sleep(0.02)
        # Everything the runtime must NOT do is expressed by what this
        # handler does NOT do: it never returns `probe`.
        raise Unfinished("worker shutting down after clone")

    with running(build_worker(app_dsn, {"full_ingest": winds_down})) as stop:
        assert in_handler.wait(timeout=SETTLE), "the handler never started"
        stop.set()
        row = job_when(
            conn,
            job_id,
            lambda r: r["lease_owner"] is None and r["state"] != "running",
            "the job to be handed back",
        )

    assert stages == ["clone"]
    assert row["state"] == "queued", (
        f"an unfinished job must go back in the queue, not to {row['state']!r}"
    )
    assert row["attempts"] == 0, (
        "the deferral must hand the attempt back; a `fail` here would walk a "
        "healthy repository to `dead` after max_attempts restarts"
    )
    assert row["last_stage"] == "clone", "the breadcrumb survives the deferral"
    assert "shutting down" in (row["last_error"] or "")
    assert probe.calls == 0
    assert runs_for(conn, org.id, probe.commit_sha) == []
    # ⚠ `syncing`, NOT `pending` -- measured, and right. The handler DID
    # start, so `mark_started` projected `syncing`, and `defer` writes no
    # projection at all (21-05, deliberately). The repository is therefore
    # left exactly where a CRASHED worker leaves it, which is the truth:
    # the job is claimable this instant and the work is about to resume.
    # What matters is the thing below it.
    repo = repo_row(conn, org.id, org.repo_id)
    assert repo["sync_state"] == "syncing"
    assert repo["sync_state"] != "synced"
    assert repo["last_synced_at"] is None, (
        "nothing may stamp last_synced_at for an ingest that did not finish"
    )


def test_unfinished_outside_a_shutdown_fails_rather_than_re_claiming_forever(
    conn, app_dsn, with_two_orgs
):
    """`Unfinished` is only free during a shutdown, and this is the bound.

    ⚠ WHAT IT PREVENTS, measured by PR #42's second review with a handler
    that always raises `Unfinished`:

        193 re-claims in 6 seconds, attempts pinned at 0,
        repository pinned at `syncing`

    `defer(timedelta(0))` is the only ending that neither consumes an
    attempt nor moves `run_after`, so **both** of the phase's backstops are
    bypassed: `CLAIM_SQL`'s `attempts < max_attempts` never trips because
    the counter never rises, and `_SWEEP_SQL`'s `attempts >= max_attempts`
    never matches, so the sweeper can never dead-letter it. And nothing
    raises, so nothing pages anyone.

    The shutdown path keeps the free pass -- `..._defers_instead_of_completing`
    above is that half -- because a shutdown happens once per process and
    the loop exits immediately afterwards.

    ⚠ HOW THIS TEST IS BOUNDED, because a test for a spin must not spin.
    The handler counts its calls and switches to a plain `Exception` after
    `SPIN_CAP`, which fails the job and ends the loop whatever the runtime
    does. So the un-fixed code (and the mutation below) makes this test
    FAIL in about a second rather than hang.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    spin_cap = 25
    calls: List[str] = []

    def always_unfinished(ctx):
        calls.append(str(ctx.job.id))
        if len(calls) > spin_cap:
            # The guard rail: make the spin terminate so the assertions
            # below are what fails, not the clock.
            raise RuntimeError("spin cap reached")
        raise Unfinished("a handler bug, not a shutdown")

    with running(build_worker(app_dsn, {"full_ingest": always_unfinished})):
        row = job_when(
            conn,
            job_id,
            lambda r: r["last_error"] is not None and r["lease_owner"] is None,
            "the job to be failed",
        )
        # ⚠ A FIXED SLEEP, DELIBERATELY, AND THE ONLY ONE IN THIS FILE.
        # Every other wait here is for something to HAPPEN, which is what
        # `until()` is for. This one asserts that nothing happens, and
        # there is no deadline to poll against for that. 1.5 s is ~45 spins
        # at the rate the review measured (193 in 6 s), and the fix puts
        # `run_after` a 30-60 s backoff away, so it cannot re-claim.
        time.sleep(1.5)

    assert len(calls) == 1, (
        f"the handler ran {len(calls)} times; `Unfinished` outside a shutdown "
        "must consume an attempt and take a backoff, or it re-claims without "
        "bound"
    )
    assert row["state"] == "queued", "below max_attempts a failure re-queues (O2)"
    assert row["attempts"] == 1, (
        "the attempt must be CONSUMED here -- that is what lets "
        "`attempts < max_attempts` and the sweeper eventually stop it"
    )
    assert row["run_after"] > row["updated_at"], (
        "the failure must push `run_after` into the future; a zero delay is "
        "what made the spin possible"
    )
    assert "Unfinished" in (row["last_error"] or "")

    final = job_row(conn, job_id)
    assert final["attempts"] == 1, "a second claim happened after the failure"


def test_a_worker_whose_connection_dies_reconnects_and_claims_the_next_job(
    conn, superuser_conn, app_dsn, with_two_orgs
):
    """psycopg2 connections do not self-heal, and the loop's was opened once.

    ⚠ REPRODUCED BY PR #42's REVIEW before it was fixed: a
    `pg_terminate_backend` on the loop's backend produced 44 identical
    error lines in 15 seconds, the next job was never claimed, and the
    process stayed alive -- so no `restart:` policy fired and the container
    looked healthy while its queue backed up. A Postgres restart, a
    failover, or an idle-connection reaper in front of the database is
    enough to cause it.

    The heartbeat already reconnected per job; this is the same property
    for the connection that could not heal.
    """
    org, other = with_two_orgs
    link_installation(conn, org)
    link_installation(conn, other)

    first = seed_job(conn, org)
    backdate(conn, first)
    assert_only_claimable(conn, first)

    handler = Recorder(None)

    with running(build_worker(app_dsn, {"full_ingest": handler})):
        until(lambda: job_row(conn, first)["state"] == "completed", "the first job")

        # Kill the loop's backend, and only it. `application_name` is what
        # tells the two connections apart -- the heartbeat's is tagged
        # separately and is not running right now anyway.
        killed = kill_backends(superuser_conn, "rag-doc-worker")
        assert killed >= 1, (
            "no loop backend was killed, so this test would pass without "
            "exercising the reconnect at all"
        )

        second = seed_job(conn, other)
        backdate(conn, second)
        until(
            lambda: job_row(conn, second)["state"] == "completed",
            "the worker to reconnect and claim the next job",
        )

    assert handler.job_ids == [first, second]


def test_an_unreachable_database_is_retried_and_then_gives_up_with_exit_1(
    monkeypatch, caplog, tmp_path
):
    """The FIRST connect goes through the reconnect policy like every other.

    ⚠ IT DID NOT, AND THAT IS THE ONE MOST LIKELY TO FAIL. The compose
    `workers` service has no `depends_on`, so on a stack restart this
    process starts before Postgres is ready — every time. PR #42's second
    review measured the old shape: one attempt, no retry, and a bare
    `OperationalError` escaping `main`'s `except DatabaseUnavailable`, so
    the operator got a traceback instead of the one clean line the new code
    was written to give them.

    The backoff constants are shrunk here rather than waited out: ten
    attempts at 1 s doubling to 30 s is about two and a half minutes, which
    is right in production and absurd in a test. The POLICY is what is under
    test, not the arithmetic.
    """
    import workers.jobs.handlers as handlers_module
    import workers.jobs.runtime as runtime_module
    from workers.__main__ import main as workers_main

    monkeypatch.setattr(runtime_module, "MAX_RECONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(
        runtime_module, "RECONNECT_BACKOFF_BASE", timedelta(seconds=0.05)
    )
    monkeypatch.setattr(
        runtime_module, "RECONNECT_BACKOFF_MAX", timedelta(seconds=0.1)
    )
    caplog.set_level(logging.INFO, logger="workers.jobs.runtime")

    # Port 1 is reserved and nothing listens on it, so the connect is
    # REFUSED rather than dropped -- fast and deterministic.
    unreachable = "postgresql://nobody:nobody@127.0.0.1:1/nothing"

    worker = build_worker(unreachable, {"full_ingest": lambda ctx: None})
    with pytest.raises(runtime_module.DatabaseUnavailable):
        worker.run(threading.Event())

    attempts = [
        record.getMessage()
        for record in caplog.records
        if "reconnect" in record.getMessage() and "failed" in record.getMessage()
    ]
    assert len(attempts) == 3, (
        f"expected 3 retries before giving up, saw {len(attempts)}: {attempts}"
    )

    # And the entrypoint turns that into a non-zero exit, so a supervisor
    # can restart the process. Exit 1, not 2: 2 means "this build is
    # configured not to run", which no restart fixes. Since 22-05 the
    # entrypoint also needs the ingest configuration before it gets as far
    # as the database, and sweeps its workdir at startup, so both are
    # given (the workdir a private temporary one).
    previous = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    monkeypatch.setitem(handlers_module.REGISTRY, "full_ingest", lambda ctx: None)
    monkeypatch.setenv("DATABASE_URL", unreachable)
    monkeypatch.setenv("INTERNAL_API_URL", "http://backend:8081")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("WORKER_WORKDIR", str(tmp_path))
    try:
        assert workers_main() == 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_a_connect_that_hangs_times_out_instead_of_blocking_forever(app_dsn):
    """`connect_timeout` — and its absence was a hole in the give-up rules.

    ⚠ A REFUSED CONNECTION FAILS AT ONCE, which is every case the tests and
    the review's probes reached. A path that DROPS instead — a firewall, a
    failing-over proxy, a partitioned network — blocks inside
    `psycopg2.connect` for the OS TCP timeout. The heartbeat reopens its
    connection **inside** its `try`, so a reopen that hangs raises nothing,
    neither give-up rule can see it, and `lease_lost` is never set. That is
    I3 one layer further in.

    The black hole here is a real listening socket that accepts the TCP
    connection and then never speaks, which is what a silently-dropping
    middlebox looks like to libpq: the handshake is what hangs, not the
    connect. Deterministic, local, and needs no network conditions.

    ⚠ BOUNDED BY CONSTRUCTION. The attempt runs on a daemon thread with a
    deadline, so without `connect_timeout` this test FAILS on the deadline
    instead of hanging the suite.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    accepted: List[Any] = []

    def accept_and_say_nothing():
        try:
            client, _ = listener.accept()
            accepted.append(client)  # held open, never written to
        except OSError:  # pragma: no cover - the socket closed under us
            pass

    acceptor = threading.Thread(target=accept_and_say_nothing, daemon=True)
    acceptor.start()

    worker = build_worker(
        f"postgresql://nobody:nobody@127.0.0.1:{port}/nothing",
        {"full_ingest": lambda ctx: None},
    )

    done = threading.Event()
    outcome: dict = {}

    def attempt():
        started = time.monotonic()
        try:
            worker._connect("rag-doc-worker-probe").close()
            outcome["error"] = None
        except Exception as exc:  # noqa: BLE001 - the point of the test
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started
            done.set()

    threading.Thread(target=attempt, daemon=True).start()
    try:
        landed = done.wait(timeout=CONNECT_TIMEOUT_SECONDS + 20)
    finally:
        for client in accepted:
            client.close()
        listener.close()

    assert landed, (
        f"psycopg2.connect was still blocked after "
        f"{CONNECT_TIMEOUT_SECONDS + 20:.0f}s on a socket that accepts and "
        "never speaks -- connect_timeout is missing"
    )
    assert isinstance(outcome["error"], psycopg2.OperationalError), (
        f"expected an OperationalError, got {outcome['error']!r}"
    )
    assert outcome["elapsed"] < CONNECT_TIMEOUT_SECONDS + 15


def test_a_heartbeat_whose_connection_dies_reopens_it_and_the_job_survives(
    conn, superuser_conn, app_dsn, with_two_orgs
):
    """One dropped heartbeat connection must not cost a whole ingest.

    ⚠ THIS IS THE CASE THE GIVE-UP RULE BELOW MUST NOT SWALLOW, and it is
    why the heartbeat reopens IN PLACE rather than only at the next job.
    A single dropped connection -- a failover, a reaper -- is survivable:
    the next beat notices the socket is dead, reconnects and carries on,
    the lease is never allowed to expire, and the handler is never
    disturbed. Giving up on the first `InterfaceError` would throw away
    minutes of clone/parse/embed for a blip.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    started_work = threading.Event()
    may_finish = threading.Event()
    observed: dict = {}
    probe = Probe(org.repo_id, f"sha-hbheal-{job_id[:8]}")

    def patient(ctx):
        started_work.set()
        may_finish.wait(timeout=SETTLE)
        observed["aborted"] = ctx.should_abort()
        return probe

    with running(build_worker(app_dsn, {"full_ingest": patient})):
        assert started_work.wait(timeout=SETTLE), "the handler never started"
        # ⚠ WAIT FOR A BEAT TO LAND BEFORE KILLING, not merely for the
        # claim. The lease is set by `claim`, and the heartbeat thread
        # opens its connection a moment LATER -- so a kill issued as soon
        # as `lease_expires_at` is non-NULL races the connection into
        # existence and hits nothing. Measured: it killed 0 backends about
        # half the time, and the test then proved nothing while passing
        # its own `killed >= 1` check in the other half.
        claimed_at = until(
            lambda: job_row(conn, job_id)["lease_expires_at"], "the claim"
        )
        beaten = until(
            lambda: (
                row["lease_expires_at"]
                if (row := job_row(conn, job_id))["lease_expires_at"] > claimed_at
                else None
            ),
            "the first heartbeat, so its connection exists to be killed",
        )
        killed = kill_backends(superuser_conn, "rag-doc-worker-heartbeat")
        assert killed >= 1, "no heartbeat backend was killed"
        # The beat that follows the kill has to reconnect to land at all,
        # so a lease that moves forward again IS the reconnect.
        until(
            lambda: job_row(conn, job_id)["lease_expires_at"] > beaten,
            "the heartbeat to reconnect and extend the lease again",
        )
        may_finish.set()
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    assert observed["aborted"] is False, (
        "a single dropped heartbeat connection aborted the handler; it is "
        "supposed to reconnect and carry on"
    )
    assert probe.calls == 1


def test_a_heartbeat_that_cannot_beat_for_a_full_lease_gives_up(
    conn, superuser_conn, app_dsn, with_two_orgs
):
    """A heartbeat that cannot land must stop the handler, not beat into the void.

    ⚠ REPRODUCED BY PR #42's REVIEW: kill the heartbeat's backend mid-job
    and the lease expires underneath the running handler while
    `should_abort()` stays False. The fence stops the corruption -- the
    stale `complete` is refused -- so the cost is a whole ingest thrown
    away and then duplicated, unbounded in duration. For Phase 22 that is
    minutes of clone, parse and embed against a token-authenticated remote.

    The rule that fires is the clock one: once a full `lease` has passed
    with no beat landing, the lease has demonstrably expired and somebody
    else may already hold the job.

    ⚠ HOW THE FAILURE IS MADE PERSISTENT, and why a kill alone will not do
    it any more: the heartbeat now REOPENS its connection (the test above),
    so one kill heals within a beat. The outage has to survive reconnection
    to exercise the give-up rule at all.

    Revoking `UPDATE` on `ingestion_jobs` from `rag_doc_app` does that: the
    connection comes back and the statement still cannot run. It bites
    because the worker's sessions authenticate as the superuser and then
    drop to `rag_doc_app` through the DSN's `options=-c role=...`, and
    `SET ROLE` to a non-superuser gives up superuser privilege -- the same
    property the whole isolation harness rests on.

    ⚠ TWO LEVERS WERE TRIED AND MEASURED USELESS FIRST, both because
    SUPERUSERS ARE EXEMPT: `REVOKE rag_doc_app FROM isolation` (a superuser
    may `SET ROLE` to anything, membership or not, so every reconnect
    succeeded) and a `CONNECTION LIMIT` (not enforced for superusers).
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    beating = threading.Event()
    observed: dict = {}
    patience = 25.0

    def cooperative(ctx):
        beating.set()
        started = time.monotonic()
        while not ctx.should_abort() and time.monotonic() - started < patience:
            time.sleep(0.02)
        observed["aborted"] = ctx.should_abort()
        observed["elapsed"] = time.monotonic() - started
        raise Unfinished("the lease went away")

    try:
        with running(build_worker(app_dsn, {"full_ingest": cooperative})):
            assert beating.wait(timeout=SETTLE), "the handler never started"
            # Let one beat land first: `last_ok` must be a real timestamp
            # rather than the start of the job, and the heartbeat's
            # connection must exist before there is anything to kill.
            claimed_at = until(
                lambda: job_row(conn, job_id)["lease_expires_at"], "the claim"
            )
            until(
                lambda: job_row(conn, job_id)["lease_expires_at"] > claimed_at,
                "one heartbeat to land before the outage",
            )
            with superuser_conn.cursor() as cur:
                cur.execute("REVOKE UPDATE ON ingestion_jobs FROM rag_doc_app")
            # Kill it too, so the path under test is the reviewer's:
            # the connection dies, it reconnects, and the beat still
            # cannot land.
            killed = kill_backends(superuser_conn, "rag-doc-worker-heartbeat")
            assert killed >= 1, "no heartbeat backend was killed"
            until(lambda: observed.get("aborted") is not None, "the handler to stop")
    finally:
        with superuser_conn.cursor() as cur:
            cur.execute("GRANT UPDATE ON ingestion_jobs TO rag_doc_app")

    assert observed["aborted"] is True, (
        "the heartbeat could not land for a full lease and the handler was "
        "never told; it would have kept working on a lease it no longer held"
    )
    # The clock rule fires one lease after the last beat that landed, and
    # the handler notices within a heartbeat interval of that.
    bound = LEASE.total_seconds() + 4 * HEARTBEAT.total_seconds() + 8.0
    assert observed["elapsed"] < bound, (
        f"the handler took {observed['elapsed']:.1f}s to be told, and the "
        f"lease is only {LEASE.total_seconds()}s"
    )


def test_a_handler_that_overruns_max_job_duration_is_cut_loose(
    conn, app_dsn, with_two_orgs
):
    """A hung handler must stop having its lease renewed for it.

    Without a bound the heartbeat extends the lease forever: nothing can
    reclaim the job, the repository sits `syncing`, and this worker's
    sweeper -- which lives in the same loop -- never runs again either.
    21-07's admin endpoint would show a job that looks perfectly alive.

    The default is `None` (no bound) because the right number is a multiple
    of a typical ingest and nothing has ingested end to end yet. The
    mechanism is what this test pins; Phase 22 picks the number.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    observed: dict = {}
    patience = 25.0

    def hangs(ctx):
        started = time.monotonic()
        while not ctx.should_abort() and time.monotonic() - started < patience:
            time.sleep(0.02)
        observed["aborted"] = ctx.should_abort()
        observed["elapsed"] = time.monotonic() - started
        raise Unfinished("cut loose")

    worker = build_worker(
        app_dsn,
        {"full_ingest": hangs},
        max_job_duration=timedelta(seconds=1),
    )
    with running(worker):
        until(lambda: observed.get("aborted") is not None, "the handler to stop")

    assert observed["aborted"] is True
    assert observed["elapsed"] < 10.0, (
        f"max_job_duration was 1s and the handler ran {observed['elapsed']:.1f}s"
    )


def test_progress_fields_are_redacted_before_they_reach_the_column(
    conn, app_dsn, with_two_orgs
):
    """`last_stage` and `progress` are handler-supplied and 21-07 hands them back.

    ⚠ EVERY OTHER HANDLER-SUPPLIED STRING WAS ALREADY REDACTED and these
    two were not: `last_error` goes through `sanitize_error`, and `defer`'s
    and `abandon`'s reasons through `_sanitize_text`. `last_stage` is bare
    `TEXT` with **no `CHECK`** (000014), so the documented
    `clone|parse|embed|store` enum is a comment and nothing enforces it,
    and `progress`'s documented `current_file` is exactly the shape a clone
    URL ends up in.

    Keys as well as values, because a redaction with a hole in it is worse
    than none: it gets trusted.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    clone_url = f"https://x-access-token:{FAKE_TOKEN}@github.com/acme/widgets.git"

    def leaky(ctx):
        ctx.report_progress(
            f"clone {clone_url}",
            {
                "current_file": clone_url,
                "files_parsed": 7,
                "nested": [clone_url, {"deeper": clone_url}],
                clone_url: "a key, too",
            },
        )
        return None

    with running(build_worker(app_dsn, {"full_ingest": leaky})):
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    row = job_row(conn, job_id)
    written = f"{row['last_stage']} {row['progress']}"
    assert FAKE_TOKEN not in written, f"the token reached the column: {written}"
    assert row["last_stage"].startswith("clone ")
    assert "[REDACTED]" in row["last_stage"]
    assert row["progress"]["files_parsed"] == 7, "counters must survive intact"
    assert "[REDACTED]" in row["progress"]["current_file"]
    assert FAKE_TOKEN not in str(row["progress"]["nested"])
    assert not any(FAKE_TOKEN in key for key in row["progress"])


def test_a_job_type_with_no_handler_is_failed_rather_than_left_running(
    conn, app_dsn, with_two_orgs
):
    """Defensive: `__main__` cannot start with an empty registry.

    A worker built by hand with a partial map must not strand the job
    `running` until its lease expires, over and over.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org, job_type="full_ingest")
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    wrong_type = NeverCalled()
    with running(build_worker(app_dsn, {"incremental": wrong_type})):
        row = job_when(
            conn,
            job_id,
            lambda r: r["last_error"] is not None and r["lease_owner"] is None,
            "the job to be failed",
        )

    assert wrong_type.calls == [], "the incremental handler ran on a full_ingest job"
    assert row["state"] == "queued", "below max_attempts a failure re-queues (O2)"
    assert row["attempts"] == 1
    assert "no handler for full_ingest" in row["last_error"]


def test_a_handler_that_raises_fails_the_job_with_a_redacted_error(
    conn, app_dsn, with_two_orgs
):
    """Whatever a handler raises reaches `last_error`, and it is redacted first.

    21-05 widened the redaction ahead of this plan precisely because the
    claim-time installation read puts an App JWT and a private key in the
    worker's reach. This is the runtime path that proves the value a
    handler raises actually goes through `sanitize_error`.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    def exploding(ctx):
        raise RuntimeError(f"clone failed: https://x-access-token:{FAKE_TOKEN}@github.com/a/b.git")

    with running(build_worker(app_dsn, {"full_ingest": exploding})):
        row = job_when(
            conn,
            job_id,
            lambda r: r["last_error"] is not None and r["lease_owner"] is None,
            "the failure to be recorded",
        )

    assert row["state"] == "queued"
    assert row["attempts"] == 1
    assert FAKE_TOKEN not in row["last_error"]
    assert "[REDACTED]" in row["last_error"]
    assert "RuntimeError" in row["last_error"]
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"


# =====================================================================
# The endings 22-05 added (the module docstring's "THE ENDINGS")
# =====================================================================


def _settled(row: dict) -> bool:
    """A job this worker has written an ending for.

    ⚠ `last_error` IS PART OF IT: the seeded row is already `queued` with no
    lease, so without it this would hold before the worker had claimed
    anything, and every assertion would be about the seed.
    """
    return (
        row["last_error"] is not None
        and row["lease_owner"] is None
        and row["state"] != "running"
    )


@pytest.mark.parametrize(
    "raised, state, attempts, sync_state",
    [
        # A U6 cap: dead in ONE attempt, not five 500 MB retries.
        (lambda: Rejected("archive exceeds 500 MB"), "dead", 1, "failed"),
        # Mid-run suspension: the claim-time ending -- deferred, attempt
        # handed back, the projection left alone (`syncing`, since
        # mark_started ran; the documented hour).
        (lambda: InstallationSuspended("suspended mid-run"), "queued", 0, "syncing"),
        # Mid-run uninstall: the claim-time ending -- abandoned.
        (lambda: InstallationUninstalled("uninstalled mid-run"), "superseded", 1, "never_synced"),
        # Anything else: an ordinary failure, retried.
        (lambda: RuntimeError("an ordinary failure"), "queued", 1, "failed"),
    ],
    ids=["rejected", "suspended", "uninstalled", "other"],
)
def test_each_handler_exception_takes_its_ending(
    conn, app_dsn, with_two_orgs, raised, state, attempts, sync_state
):
    """The runtime half of the endings table, with a handler that only raises.

    The ingest handler's half -- WHICH exception each real situation
    raises -- is `tests/ingest/test_handler.py`, and the two meet end to end
    in `tests/isolation/test_ingest_end_to_end.py`.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    def raising(ctx):
        raise raised()

    worker = build_worker(
        app_dsn, {"full_ingest": raising}, suspended_defer=timedelta(minutes=60)
    )
    with running(worker):
        row = job_when(conn, job_id, _settled, "the job's ending")

    assert row["state"] == state, row
    assert row["attempts"] == attempts, row
    assert row["last_error"], "every ending says why"
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == sync_state
    if state == "queued" and attempts == 0:
        delay = query(
            conn,
            "SELECT EXTRACT(EPOCH FROM (run_after - updated_at)) AS s FROM ingestion_jobs WHERE id = %s",
            (job_id,),
        )[0]["s"]
        assert abs(float(delay) - 3600.0) < 5.0, "a mid-run suspension waits the hour"


def test_a_lease_lost_raised_by_the_handler_writes_nothing(conn, app_dsn, with_two_orgs):
    """`LeaseLost` from a handler -- a refused token, `should_abort()` -- is not a
    failure, and above all not a completion: the job is left exactly as it
    was, `running` under this worker's lease, for whoever reclaims it."""
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    ran = threading.Event()

    def lost(ctx):
        ran.set()
        raise LeaseLost("the token route refused this job's lease")

    # A lease far longer than the test and a heartbeat that never ticks, so
    # nothing else touches the row while it is read.
    worker = build_worker(
        app_dsn, {"full_ingest": lost}, lease=timedelta(seconds=60), heartbeat=timedelta(seconds=120)
    )
    with running(worker):
        assert ran.wait(timeout=SETTLE), "the handler never ran"
        time.sleep(0.5)  # a write, if there were one, would land well within this
        row = job_row(conn, job_id)

    assert row["state"] == "running", "nothing may be written for a job that is not ours"
    assert row["lease_owner"] == worker.worker_id
    assert row["last_error"] is None
    assert row["attempts"] == 1
    # Take this test's own job out of the live set, so no later test's
    # worker reclaims it when the lease runs out.
    sql(conn, "UPDATE ingestion_jobs SET state = 'superseded' WHERE id = %s", (job_id,))


def test_a_write_results_that_raises_fails_the_job_instead_of_stranding_it(
    conn, app_dsn, with_two_orgs, caplog
):
    """`write_results` raising inside `complete()` is settled, not left `running`.

    ⚠ FOUND WRITING 22-05. Phase 21's callbacks were one INSERT; the ingest
    handler's replaces a repository's chunks, which can fail. Before this,
    an exception from `complete` escaped `_invoke`, the loop logged it and
    moved on, and the job sat `running` until its lease expired -- then was
    reclaimed, raised again, and reached `dead` through the sweeper with
    `last_error` NULL. Now the transaction rolls back (the results with the
    completion) and the job fails with its reason.

    The runtime's warning for it is read as a `LogRecord` too (PR #58's
    review, B-L2): the exception's text reaches it, so it must be redacted
    there as surely as in `last_error`.
    """
    caplog.set_level(logging.WARNING, logger="workers.jobs.runtime")
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    sha = f"sha-wr-{job_id[:8]}"

    def exploding_write(cur):
        cur.execute(
            "INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status) "
            "VALUES (%s, %s, 'main', 'processing')",
            (org.repo_id, sha),
        )
        raise RuntimeError(f"the chunk insert failed near {FAKE_TOKEN}")

    with running(build_worker(app_dsn, {"full_ingest": lambda ctx: exploding_write})):
        row = job_when(conn, job_id, _settled, "the failure to be recorded")

    assert row["state"] == "queued", "below max_attempts a failure re-queues (O2)"
    assert row["attempts"] == 1
    assert "RuntimeError" in (row["last_error"] or "")
    assert FAKE_TOKEN not in row["last_error"]
    assert runs_for(conn, org.id, sha) == [], "the results rolled back with the completion"
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"

    [warning] = [
        r.getMessage()
        for r in caplog.records
        if r.name == "workers.jobs.runtime"
        and f"job {job_id}: the completion transaction raised" in r.getMessage()
    ]
    assert "the chunk insert failed near" in warning, "premise: the exception's text reached the line"
    assert "[REDACTED]" in warning
    assert FAKE_TOKEN not in warning


# =====================================================================
# The heartbeat's connection (22-05, P16)
# =====================================================================


def test_the_heartbeat_keeps_its_callers_identity(conn, app_dsn, with_two_orgs, monkeypatch):
    """The `statement_timeout` is MERGED into the DSN's options, never replacing them.

    ⚠ `psycopg2.connect(dsn, options=...)` REPLACES the DSN's `options`, and
    `options` is where `app_dsn` sets `-c role=rag_doc_app` -- so a
    replacing keyword would make the heartbeat's connection the container
    SUPERUSER, which bypasses row-level security, and would drop an
    operator's own options on the way. Nothing would fail: the beat's
    UPDATE works either way. So the identity is read off the real
    connections the worker opens -- every one of them, at the moment it is
    opened, on the thread that opened it -- with an operator option
    (`work_mem`) in the DSN to show that nothing else was dropped.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    role_option = "options=-c%20role%3Drag_doc_app"
    assert role_option in app_dsn, "premise: the fixture sets the role through options"
    operator_dsn = app_dsn.replace(role_option, role_option + "%20-c%20work_mem%3D7MB")

    identities: dict = {}
    real_connect = psycopg2.connect

    def identifying(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT current_setting('application_name'), current_user, "
                "current_setting('work_mem'), current_setting('statement_timeout')"
            )
            name, user, work_mem, statement_timeout = cur.fetchone()
        connection.rollback()
        identities.setdefault(name, (user, work_mem, statement_timeout))
        return connection

    monkeypatch.setattr(psycopg2, "connect", identifying)

    def waits_for_the_heartbeat(ctx):
        until(lambda: HEARTBEAT_APPLICATION_NAME in identities, "the heartbeat's connection")
        return None

    with running(build_worker(operator_dsn, {"full_ingest": waits_for_the_heartbeat})):
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    loop_user, loop_work_mem, loop_timeout = identities[LOOP_APPLICATION_NAME]
    beat_user, beat_work_mem, beat_timeout = identities[HEARTBEAT_APPLICATION_NAME]
    assert beat_user == "rag_doc_app", (
        f"the heartbeat connected as {beat_user!r}: its options REPLACED the DSN's, "
        "so the role in them was lost"
    )
    assert beat_user == loop_user, "both connections must be the same role"
    assert beat_work_mem == "7MB", "the operator's option was dropped from the heartbeat's"
    assert loop_work_mem == "7MB"
    assert beat_timeout == "15s", "P16: the heartbeat's statement_timeout"
    assert loop_timeout == "0", "the loop's connection carries no statement_timeout"


def test_the_heartbeat_keeps_an_identity_set_through_pgoptions(
    conn, app_dsn, with_two_orgs, monkeypatch
):
    """The same guarantee when the role comes from `PGOPTIONS` (PR #58's review, A-N3).

    libpq reads `PGOPTIONS` only for a connection string that carries no
    `options` of its own. The merge used to add the heartbeat's
    `statement_timeout` as the ONLY options, so libpq skipped `PGOPTIONS`
    for that one connection -- and here, where `PGOPTIONS` is what sets the
    role, the heartbeat would have connected as the role the DSN
    AUTHENTICATES as: the container superuser, which bypasses row-level
    security. Read off the real connections, as the test above does.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    bare_dsn, _, query = app_dsn.partition("?")
    assert query == "options=-c%20role%3Drag_doc_app", "premise: the role is the DSN's only option"
    monkeypatch.setenv("PGOPTIONS", "-c role=rag_doc_app -c work_mem=7MB")

    identities: dict = {}
    real_connect = psycopg2.connect

    def identifying(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT current_setting('application_name'), current_user, "
                "current_setting('work_mem'), current_setting('statement_timeout')"
            )
            name, user, work_mem, statement_timeout = cur.fetchone()
        connection.rollback()
        identities.setdefault(name, (user, work_mem, statement_timeout))
        return connection

    monkeypatch.setattr(psycopg2, "connect", identifying)

    def waits_for_the_heartbeat(ctx):
        until(lambda: HEARTBEAT_APPLICATION_NAME in identities, "the heartbeat's connection")
        return None

    with running(build_worker(bare_dsn, {"full_ingest": waits_for_the_heartbeat})):
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    loop_user, loop_work_mem, _ = identities[LOOP_APPLICATION_NAME]
    beat_user, beat_work_mem, beat_timeout = identities[HEARTBEAT_APPLICATION_NAME]
    assert loop_user == "rag_doc_app" and loop_work_mem == "7MB", (
        "premise: libpq applied PGOPTIONS to the loop's connection"
    )
    assert beat_user == "rag_doc_app", (
        f"the heartbeat connected as {beat_user!r}: its options made libpq skip PGOPTIONS"
    )
    assert beat_work_mem == "7MB"
    assert beat_timeout == "15s"


def test_the_merged_options_follow_libpqs_pgoptions_rule(monkeypatch):
    """`_merged_options_dsn` on its own, with and without `PGOPTIONS` (A-N3).

    A DSN with no `options` starts from `PGOPTIONS`, as libpq would have; a
    DSN that names `options` keeps them and ignores `PGOPTIONS`, which is
    libpq's own rule; with neither, only the added option.
    """
    from psycopg2.extensions import parse_dsn

    from workers.jobs.runtime import _merged_options_dsn

    timeout = "-c statement_timeout=15000"
    monkeypatch.setenv("PGOPTIONS", "-c work_mem=7MB")
    assert parse_dsn(_merged_options_dsn("postgresql://u:p@h:5432/db", timeout))["options"] == (
        "-c work_mem=7MB -c statement_timeout=15000"
    )
    named = "postgresql://u:p@h:5432/db?options=-c%20role%3Drag_doc_app"
    assert parse_dsn(_merged_options_dsn(named, timeout))["options"] == (
        "-c role=rag_doc_app -c statement_timeout=15000"
    )
    monkeypatch.delenv("PGOPTIONS")
    assert parse_dsn(_merged_options_dsn("postgresql://u:p@h:5432/db", timeout))["options"] == timeout


@pytest.mark.parametrize("bad", [timedelta(0), timedelta(milliseconds=-1)])
def test_a_heartbeat_timeout_that_is_not_positive_is_refused(bad):
    """A zero or negative timeout is refused, not quietly dropped (PR #58's review, B-N2).

    `None` is how a caller asks for no timeout. A zero `timedelta` is falsy,
    so the heartbeat's connection would be opened with no
    `statement_timeout` at all -- the guard switched off without a word --
    and in PostgreSQL `statement_timeout = 0` means none anyway.
    """
    with pytest.raises(ValueError, match="heartbeat_statement_timeout must be positive"):
        Worker(
            "postgresql://nobody@127.0.0.1:1/nothing",
            {"full_ingest": lambda ctx: None},
            heartbeat_statement_timeout=bad,
        )


def test_a_heartbeat_that_blocks_on_a_row_lock_times_out_and_gives_up(
    conn, app_dsn, with_two_orgs, caplog
):
    """The failure 21-06 left open: a beat that BLOCKS raises nothing.

    A second connection holds `SELECT ... FOR UPDATE` on the job row, so the
    heartbeat's `UPDATE` waits on the lock. Without a `statement_timeout` it
    waits for as long as the lock is held, and because the give-up rules are
    evaluated at the top of the NEXT beat, neither ever runs: the handler
    is never told, while the lease runs out underneath it. With the timeout
    the beat raises `57014` (`QueryCanceled`), counts as a failure, and the
    clock rule sets `should_abort()` one lease after the last beat landed.

    Mutation: without the option this test fails on its own deadline --
    the handler's patience runs out with `should_abort()` still False.
    """
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)
    caplog.set_level(logging.WARNING, logger="workers.jobs.runtime")

    beating = threading.Event()
    observed: dict = {}
    patience = 25.0

    def cooperative(ctx):
        if observed:
            raise LeaseLost("already observed")  # a reclaim after the test: stop at once
        beating.set()
        started = time.monotonic()
        while not ctx.should_abort() and time.monotonic() - started < patience:
            time.sleep(0.02)
        observed["aborted"] = ctx.should_abort()
        observed["elapsed"] = time.monotonic() - started
        raise LeaseLost("the heartbeat gave up")

    worker = build_worker(
        app_dsn,
        {"full_ingest": cooperative},
        heartbeat_statement_timeout=timedelta(milliseconds=300),
    )
    blocker = psycopg2.connect(app_dsn)
    try:
        with running(worker) as stop:
            assert beating.wait(timeout=SETTLE), "the handler never started"
            claimed_at = until(lambda: job_row(conn, job_id)["lease_expires_at"], "the claim")
            until(
                lambda: job_row(conn, job_id)["lease_expires_at"] > claimed_at,
                "one heartbeat to land before the lock",
            )
            with blocker.cursor() as cur:
                cur.execute("SELECT id FROM ingestion_jobs WHERE id = %s FOR UPDATE", (job_id,))
                assert cur.fetchone() is not None, "premise: the blocker holds the job row"
            until(lambda: observed.get("aborted") is not None, "the handler to stop")
            # Stop the loop BEFORE releasing the row, so it cannot reclaim it.
            stop.set()
            blocker.rollback()
    finally:
        blocker.close()

    assert observed["aborted"] is True, (
        "the heartbeat blocked on the row lock and the handler was never told; "
        "without statement_timeout a blocked beat is invisible to the give-up rules"
    )
    bound = LEASE.total_seconds() + 4 * HEARTBEAT.total_seconds() + 8.0
    assert observed["elapsed"] < bound, (
        f"the handler took {observed['elapsed']:.1f}s to be told; the lease is "
        f"{LEASE.total_seconds()}s"
    )
    timeouts = [
        r.getMessage()
        for r in caplog.records
        if "heartbeat failed" in r.getMessage() and "QueryCanceled" in r.getMessage()
    ]
    assert timeouts, "no beat was cancelled by statement_timeout (57014)"
    assert "statement timeout" in timeouts[0]
    sql(conn, "UPDATE ingestion_jobs SET state = 'superseded' WHERE id = %s", (job_id,))


# =====================================================================
# The entrypoint
# =====================================================================


#: Runs the REAL `workers/__main__` with the registry emptied first.
#: `main()` imports `REGISTRY` lazily, inside the function, so it sees the
#: cleared map -- which is how the refusal stays testable once 22-05 filled
#: the shipped registry.
EMPTIED_REGISTRY_ENTRYPOINT = (
    "import workers.jobs.handlers as h; h.REGISTRY.clear(); "
    "import runpy; runpy.run_module('workers', run_name='__main__')"
)


def _entrypoint(args: List[str], env: dict) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(SERVICE_ROOT),
        env=env,
        capture_output=True,
        timeout=120,
    )


def _entrypoint_env(**overrides: Optional[str]) -> dict:
    """The test process's environment minus everything the worker reads."""
    env = dict(os.environ)
    for name in (
        "DATABASE_URL",
        "INTERNAL_API_URL",
        "WORKER_WORKDIR",
        "WORKER_MAX_JOB_DURATION_SECONDS",
        "WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS",
    ):
        env.pop(name, None)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


@pytest.mark.parametrize("with_dsn", [False, True])
def test_the_entrypoint_refuses_to_start_without_handlers(with_dsn):
    """An EMPTY registry exits 2 whatever the environment says.

    ⚠ CHANGED DELIBERATELY IN 22-05 (fact-check a5). This test used to run
    the plain `python -m workers` and relied on the shipped registry being
    empty; 22-05 filled it, which would have broken the test for the right
    reason. The refusal still matters -- it is what a build that has lost
    its handlers says instead of claiming jobs and dead-lettering them
    (21-06) -- so it now runs the real `__main__` with the registry CLEARED
    first. The assertions and both parametrisations are unchanged.

    ⚠ BOTH RUNS MATTER, and the one WITH `DATABASE_URL` is the interesting
    one. The order of the checks in `__main__` is what makes a service with
    no `DATABASE_URL` print the message about handlers rather than one
    about a missing DSN. If somebody swaps them, the run without a DSN
    starts failing for the other reason and this parametrisation says so.
    """
    env = _entrypoint_env(
        DATABASE_URL="postgresql://unused:unused@127.0.0.1:1/unused" if with_dsn else None
    )

    result = _entrypoint(["-c", EMPTIED_REGISTRY_ENTRYPOINT], env)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")

    assert result.returncode == 2, (
        f"exit code {result.returncode}, not 2. Output:\n{output}"
    )
    assert "no job handlers registered" in output, output
    assert "Phase 22" in output, output
    assert "DATABASE_URL is not set" not in output, (
        "the configuration check ran before the handler check"
    )


def test_the_entrypoint_refuses_without_a_dsn_once_handlers_exist():
    """The second refusal, reachable only since 22-05 filled the registry.

    The plain `python -m workers`, the shipped registry, no `DATABASE_URL`:
    exit 2 with `NO_DSN_MESSAGE`, and never the handler message.
    """
    from workers.__main__ import NO_DSN_MESSAGE

    result = _entrypoint(["-m", "workers"], _entrypoint_env())
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")

    assert result.returncode == 2, f"exit code {result.returncode}, not 2. Output:\n{output}"
    assert NO_DSN_MESSAGE in output, output
    assert "no job handlers registered" not in output, (
        "the shipped registry is not empty; the handler check must pass"
    )


@pytest.mark.parametrize("missing", ["INTERNAL_API_URL", "OPENAI_API_KEY"])
def test_the_entrypoint_refuses_without_the_ingest_configuration(tmp_path, missing):
    """A worker that cannot reach the token route or OpenAI must not claim.

    It would fail every job it claimed five times and dead-letter it --
    the accident the handler refusal exists to prevent, one step later. So
    `__main__` builds the ingest configuration after the DSN check and
    refuses with exit 2 before connecting to anything (the DSN here points
    at a port nothing listens on, so a worker that got as far as
    connecting would exit 1 instead).
    """
    env = _entrypoint_env(
        DATABASE_URL="postgresql://unused:unused@127.0.0.1:1/unused",
        INTERNAL_API_URL="http://backend:8081",
        OPENAI_API_KEY="sk-test-dummy",
        WORKER_WORKDIR=str(tmp_path),
    )
    env.pop(missing, None)

    result = _entrypoint(["-m", "workers"], env)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")

    assert result.returncode == 2, f"exit code {result.returncode}, not 2. Output:\n{output}"
    assert f"{missing} is not set" in output, output
    assert "reconnect" not in output, "the worker tried the database before refusing"


def test_the_deployed_worker_has_a_bound_and_never_warns_about_one(monkeypatch, caplog):
    """P16: `__main__` builds the worker with two hours and fifteen seconds.

    The unset-bound WARNING a bare `Worker` gives (see
    `test_an_unset_max_job_duration_is_announced_rather_than_silent`) must
    therefore never appear from the entrypoint. The worker built exactly as
    `main()` builds it is run against a port nothing listens on, so it logs
    its startup line, cannot claim anything, and gives up at once.
    """
    import workers.jobs.runtime as runtime_module
    from workers.__main__ import build_worker as build_deployed_worker
    from workers.jobs.handlers import REGISTRY

    monkeypatch.setattr(runtime_module, "MAX_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(runtime_module, "RECONNECT_BACKOFF_BASE", timedelta(seconds=0.01))
    caplog.set_level(logging.INFO, logger="workers.jobs.runtime")

    worker = build_deployed_worker("postgresql://nobody:nobody@127.0.0.1:1/nothing", REGISTRY, {})
    assert worker.max_job_duration == timedelta(hours=2), "P16, provisional until 22.1-05"
    assert worker.heartbeat_statement_timeout == timedelta(seconds=15)
    assert sorted(worker._handlers) == ["full_ingest", "incremental"]

    with pytest.raises(runtime_module.DatabaseUnavailable):
        worker.run(threading.Event())

    startup = [r.getMessage() for r in caplog.records if "starting: job_types=" in r.getMessage()]
    assert startup and "max_job_duration=7200s" in startup[0], startup
    assert "heartbeat_statement_timeout=15000ms" in startup[0], startup
    assert not [
        r for r in caplog.records if "no upper bound on handler runtime" in r.getMessage()
    ], "the deployed worker must never run without a bound"

    overridden = build_deployed_worker(
        "postgresql://nobody:nobody@127.0.0.1:1/nothing",
        REGISTRY,
        {"WORKER_MAX_JOB_DURATION_SECONDS": "600", "WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS": "2500"},
    )
    assert overridden.max_job_duration == timedelta(minutes=10)
    assert overridden.heartbeat_statement_timeout == timedelta(milliseconds=2500)

    for bad in ("0", "-5", "two hours", "1.5"):
        with pytest.raises(ValueError, match="WORKER_MAX_JOB_DURATION_SECONDS"):
            build_deployed_worker("postgresql://x", REGISTRY, {"WORKER_MAX_JOB_DURATION_SECONDS": bad})


def test_the_entrypoint_sweeps_stale_job_directories_once_before_it_runs(monkeypatch, tmp_path):
    """Step 5 of `workers/__main__`, called in-process (PR #58's review, B-N2).

    The sweep runs once, on the configured workdir, with a bound of one
    `max_job_duration` plus one lease -- past which no job this host could
    still be running has been alive that long -- and before the loop. The
    database is a port nothing listens on, so `main()` then gives up and
    returns 1; the signal handlers and the handler configuration are kept
    out of the test process.
    """
    import workers.__main__ as entrypoint
    import workers.fetch as fetch_module
    import workers.ingest.handler as handler_module
    import workers.jobs.runtime as runtime_module

    swept: List[tuple] = []
    monkeypatch.setattr(
        fetch_module, "sweep_stale_workdirs", lambda workdir, older_than: swept.append((workdir, older_than)) or 0
    )
    monkeypatch.setattr(entrypoint, "_install_signal_handlers", lambda stop: None)
    monkeypatch.setattr(handler_module, "_default", None)  # restored after `configure`
    monkeypatch.setattr(runtime_module, "MAX_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(runtime_module, "RECONNECT_BACKOFF_BASE", timedelta(seconds=0.01))
    for name in ("WORKER_MAX_JOB_DURATION_SECONDS", "WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nobody@127.0.0.1:1/nothing")
    monkeypatch.setenv("INTERNAL_API_URL", "http://backend:8081")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("WORKER_WORKDIR", str(tmp_path))

    assert entrypoint.main([]) == 1, "an unreachable database is exit 1, after the sweep"
    assert swept == [(str(tmp_path), timedelta(hours=2) + runtime_module.DEFAULT_LEASE)], swept
