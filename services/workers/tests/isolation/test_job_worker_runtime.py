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

import os
import pathlib
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
from workers.jobs import Worker, claim, new_worker_id
from workers.jobs.transitions import ENQUEUE_UPSERT_SQL

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


@pytest.fixture
def dsn(test_db_container) -> str:
    """The container DSN, forced onto the NOSUPERUSER app role.

    ⚠ THE `options` PARAMETER IS LOAD-BEARING, and it is the only way to
    say this: a `Worker` opens its own connections from a DSN, so there is
    no cursor for a fixture to `SET ROLE` on. Connecting as the container
    superuser instead would bypass row-level security even with FORCE, and
    every tenant assertion below -- above all the installation read, which
    is scoped by `require_tenant` -- would pass for the wrong reason.
    `test_the_worker_connects_as_the_unprivileged_app_role` asserts it
    rather than trusting it.
    """
    base = test_db_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    return f"{base}?options=-c%20role%3Drag_doc_app"


@pytest.fixture
def conn(dsn: str):
    """The test's own connection, separate from every worker's."""
    connection = psycopg2.connect(dsn)
    connection.autocommit = False
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


def build_worker(dsn: str, handlers: dict, **kwargs) -> Worker:
    """A `Worker` on this file's short timings."""
    kwargs.setdefault("lease", LEASE)
    kwargs.setdefault("heartbeat", HEARTBEAT)
    kwargs.setdefault("idle_poll", IDLE_POLL)
    kwargs.setdefault("suspended_defer", SUSPENDED_DEFER)
    return Worker(dsn, handlers, **kwargs)


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
        assert not thread.is_alive(), (
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

    def __call__(self, ctx) -> Optional[Probe]:
        self.job_ids.append(str(ctx.job.id))
        self.saw_abort_at_return = ctx.should_abort()
        return self.probe


def never_called(ctx):  # pragma: no cover - the assertion is that it is not
    raise AssertionError(
        f"the handler ran for job {ctx.job.id}, and this test says it must not"
    )


# =====================================================================
# Premises
# =====================================================================


def test_the_worker_connects_as_the_unprivileged_app_role(dsn):
    """Every RLS assertion in this file rests on this, so it is asserted.

    A PostgreSQL superuser bypasses row-level security even under FORCE. If
    the DSN a `Worker` is given connected as the container superuser, the
    claim-time installation read would see every tenant's rows, the tenant
    trigger would never fire, and each isolation assertion below would pass
    for a reason that has nothing to do with the code.
    """
    connection = psycopg2.connect(dsn)
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
        "parameter in the `dsn` fixture is not taking effect"
    )
    assert is_super is False, "the worker role must not bypass row-level security"
    assert bypasses_rls is False


def test_a_worker_refuses_an_empty_handler_map(dsn):
    """The backstop for a caller that builds a `Worker` directly.

    `__main__` refuses earlier and more loudly, but a `Worker` with no
    handler fails every job it claims `max_attempts` times and dead-letters
    it -- the accident this whole plan is arranged around.
    """
    with pytest.raises(ValueError, match="at least one handler"):
        Worker(dsn, {})


# =====================================================================
# The happy path
# =====================================================================


def test_a_claimed_job_runs_its_handler_and_completes(conn, dsn, with_two_orgs):
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    probe = Probe(org.repo_id, f"sha-happy-{job_id[:8]}")
    handler = Recorder(probe)

    with running(build_worker(dsn, {"full_ingest": handler})):
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
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"full_ingest": slow_handler})):
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
    conn, dsn, with_two_orgs, monkeypatch
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

    with running(build_worker(dsn, {"full_ingest": slow})):
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
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"full_ingest": cooperative})):
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


def test_a_handlers_progress_report_lands_on_the_job_row(conn, dsn, with_two_orgs):
    org, _ = with_two_orgs
    link_installation(conn, org)
    job_id = seed_job(conn, org)
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)

    reported: List[bool] = []

    def reporting(ctx):
        reported.append(ctx.report_progress("clone", {"files_parsed": 7}))
        return None

    with running(build_worker(dsn, {"full_ingest": reporting})):
        until(lambda: job_row(conn, job_id)["state"] == "completed", "completion")

    row = job_row(conn, job_id)
    assert reported == [True]
    assert row["last_stage"] == "clone"
    assert row["progress"] == {"files_parsed": 7}


def test_a_progress_report_from_a_worker_that_lost_its_lease_is_refused(
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"full_ingest": reporting})):
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
    conn, dsn, with_two_orgs
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
            dsn,
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
        # now -- so `never_called` doubles as the assertion that the claim
        # guard holds, and the sweeper is the only thing that can move it.
        sweeper = build_worker(dsn, {"full_ingest": never_called})
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
    conn, dsn, with_two_orgs
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
    connections = [psycopg2.connect(dsn) for _ in range(racers)]
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
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"full_ingest": handler})):
        # ⚠ `last_error`, NOT `state == 'queued'`. The job STARTS `queued`
        # with no lease, so a condition on the state alone is satisfied
        # before the worker has even claimed it, and the assertions below
        # would then all run against the seeded row.
        deferred = job_when(
            conn,
            job_id,
            lambda row: row["last_error"] is not None,
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
def test_a_dead_installation_abandons_the_job(conn, dsn, with_two_orgs, shape):
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

    with running(build_worker(dsn, {"full_ingest": handler})):
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


def test_a_shutdown_finishes_the_job_in_flight(conn, dsn, with_two_orgs):
    """`stop` asks; it does not interrupt.

    The handler is told through `should_abort()` -- one signal for "the
    lease is gone" and "the worker is going away" -- but a handler that
    finishes anyway gets its job COMPLETED. Killing it mid-write is what
    the lease and the sweeper exist to recover from, and it is not
    something a graceful shutdown should cause.
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

    with running(build_worker(dsn, {"full_ingest": slow})) as stop:
        assert in_handler.wait(timeout=SETTLE), "the handler never started"
        stop.set()
        # The worker must not touch the job while the handler still holds it.
        assert job_row(conn, job_id)["state"] == "running"
        may_finish.set()
        until(lambda: job_row(conn, job_id)["state"] == "completed", "the last job")

    assert handler.saw_abort_at_return is True, (
        "should_abort() must be true while the worker is shutting down"
    )
    assert probe.calls == 1
    assert runs_for(conn, org.id, probe.commit_sha)


def test_a_job_type_with_no_handler_is_failed_rather_than_left_running(
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"incremental": never_called})):
        until(
            lambda: job_row(conn, job_id)["last_error"] is not None,
            "the job to be failed",
        )

    row = job_row(conn, job_id)
    assert row["state"] == "queued", "below max_attempts a failure re-queues (O2)"
    assert row["attempts"] == 1
    assert "no handler for full_ingest" in row["last_error"]


def test_a_handler_that_raises_fails_the_job_with_a_redacted_error(
    conn, dsn, with_two_orgs
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

    with running(build_worker(dsn, {"full_ingest": exploding})):
        until(
            lambda: job_row(conn, job_id)["last_error"] is not None,
            "the failure to be recorded",
        )

    row = job_row(conn, job_id)
    assert row["state"] == "queued"
    assert row["attempts"] == 1
    assert FAKE_TOKEN not in row["last_error"]
    assert "[REDACTED]" in row["last_error"]
    assert "RuntimeError" in row["last_error"]
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"


# =====================================================================
# The entrypoint
# =====================================================================


@pytest.mark.parametrize("with_dsn", [False, True])
def test_the_entrypoint_refuses_to_start_without_handlers(with_dsn):
    """`python -m workers` exits 2 whatever the environment says.

    ⚠ BOTH RUNS MATTER, and the one WITH `DATABASE_URL` is the interesting
    one. The order of the two checks in `__main__` is what makes the
    compose service -- which has no `DATABASE_URL` -- print the message
    about handlers rather than one about a missing DSN. If somebody swaps
    them, the run without a DSN starts failing for the other reason and
    this parametrisation is what says so.
    """
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    if with_dsn:
        env["DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:1/unused"

    result = subprocess.run(
        [sys.executable, "-m", "workers"],
        cwd=str(SERVICE_ROOT),
        env=env,
        capture_output=True,
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")

    assert result.returncode == 2, (
        f"exit code {result.returncode}, not 2. Output:\n{output}"
    )
    assert "no job handlers registered" in output, output
    assert "Phase 22" in output, output
    assert "DATABASE_URL is not set" not in output, (
        "the configuration check ran before the handler check"
    )
