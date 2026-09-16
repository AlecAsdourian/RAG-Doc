"""The consumer's state transitions, against a real PostgreSQL 16.

The Python counterpart of `services/backend/pkg/jobs/schema_test.go`, and
the other half of 21-02's guarantee: those statements have now run from
both languages that use them.

ONE BEHAVIOUR PER TEST, and every write goes through `workers.jobs` rather
than through a hand-written copy of its SQL -- the same reason
`schema_test.go` became an internal test package in 21-03. A test holding
its own copy of a statement keeps passing after someone changes the real
one.

⚠ WHAT THE TEST SETUP MAY AND MAY NOT DO.

  - It seeds through `seed_job`, which runs the PRODUCER's two statements
    (21-02's enqueue upsert, then the `sync_state = 'pending'` projection),
    so a job here is indistinguishable from one a connect or a push made.
  - It reaches for raw SQL only to manufacture states a producer cannot:
    an expired lease, a supersede, an exhausted attempt counter. Those are
    the conditions the fences exist for, and there is no API that creates
    them.
  - IT DELETES NOTHING. `claim` and `sweep` are queue-wide by construction
    (21-CONTEXT L5), so the temptation is to empty the table first; 21-03
    removed exactly that helper from the Go suite. Ordering replaces it:
    a test's own job is backdated to `CLAIM_TEST_EPOCH`, which makes it the
    oldest claimable row in the table, and `assert_no_older_claimable`
    turns the one way that argument can fail -- a leaked row from a crashed
    run, backdated further -- into a plain sentence.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.jobs import (
    LeaseLost,
    abandon,
    attach_ingestion_run,
    claim,
    complete,
    defer,
    fail,
    mark_started,
    new_worker_id,
    resolve_ingestion_run,
    sweep,
)
from workers.jobs.transitions import CLEAR_RERUN_SQL, ENQUEUE_UPSERT_SQL, Job

# A decade in the past, so a test's own job sorts ahead of anything a
# concurrently-running test enqueued at NOW(). Mirrors 21-03's
# `claimTestEpoch`, and exists for the same reason: the claim query
# deliberately has no repository filter, because production has one queue.
CLAIM_TEST_EPOCH = "2015-01-01 00:00:00+00"

LEASE = timedelta(minutes=5)

# A token-shaped string, so every failure assertion doubles as a redaction
# assertion. It is not a real credential: `ghs_` plus 36 characters is the
# published shape of a GitHub App installation token.
FAKE_TOKEN = "ghs_" + "0123456789abcdefghijABCDEFGHIJ012345"


# =====================================================================
# Fixtures and helpers
# =====================================================================


@pytest.fixture
def dsn(test_db_container) -> str:
    return test_db_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )


@pytest.fixture
def worker_id() -> str:
    return new_worker_id()


def app_conn(dsn: str):
    """A fresh connection as the NOSUPERUSER app role.

    Connecting as `rag_doc_app` rather than the container superuser is
    load-bearing: a superuser bypasses row-level security even with FORCE,
    so every isolation assertion here would silently pass.

    FRESH matters in one test: a connection that has never committed a
    `SET LOCAL` holds an UNSET `app.current_tenant`, which is the other
    half of ISS-013 from the one `db_conn` is in by the time a fixture has
    run. See `test_the_unscoped_statements_do_not_depend_on_connection_history`.
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    conn.autocommit = False
    return conn


def sql(conn: Any, statement: str, params: tuple = ()) -> None:
    """Run one statement with NO tenant scope and commit it.

    Only ever pointed at `ingestion_jobs`, which has no row-level security
    -- see the module docstring on what the setup may manufacture.
    """
    with conn.cursor() as cur:
        cur.execute(statement, params)
    conn.commit()


def query(conn: Any, statement: str, params: tuple = ()) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def seed_job(conn: Any, org: Any, job_type: str = "full_ingest",
             repository_id: Optional[str] = None) -> str:
    """Enqueue one job exactly as the Go producer does, and return its id.

    Two statements in one tenant-scoped transaction: 21-02's enqueue upsert
    (`pkg/jobs/producer.go:enqueueUpsertSQL`), then the `sync_state =
    'pending'` projection for a repository that got a NEW job. A job seeded
    any other way would not be the thing this module consumes.
    """
    repo_id = repository_id or org.repo_id
    with require_tenant(conn, org.id) as cur:
        cur.execute(ENQUEUE_UPSERT_SQL, (org.id, repo_id, job_type))
        job_id, was_existing = cur.fetchone()
        if not was_existing:
            cur.execute(
                "UPDATE repositories SET sync_state = 'pending', updated_at = NOW() "
                "WHERE id = %s",
                (repo_id,),
            )
    return job_id


def flag_rerun(conn: Any, org: Any, repository_id: Optional[str] = None) -> bool:
    """Run the producer's upsert against a live job: the `push` case (L7).

    Returns `was_existing` -- True when the statement took its `ON CONFLICT`
    branch and set `needs_rerun` rather than inserting.
    """
    repo_id = repository_id or org.repo_id
    with require_tenant(conn, org.id) as cur:
        cur.execute(ENQUEUE_UPSERT_SQL, (org.id, repo_id, "incremental"))
        _, was_existing = cur.fetchone()
    return bool(was_existing)


def backdate(conn: Any, job_id: str, offset_seconds: int = 0) -> None:
    """Make this job the oldest claimable row in the queue.

    `offset_seconds` orders several of a test's own jobs against each
    other while keeping all of them ahead of anything at NOW().
    """
    sql(
        conn,
        "UPDATE ingestion_jobs "
        "SET run_after = TIMESTAMPTZ %s + (%s * INTERVAL '1 second') "
        "WHERE id = %s",
        (CLAIM_TEST_EPOCH, offset_seconds, job_id),
    )


def assert_no_older_claimable(conn: Any, job_id: str) -> None:
    """The premise every claim assertion in this file rests on.

    `claimSQL` is queue-wide -- no repository filter, no organization
    filter -- so "the claim returned MY job" is only sound while my job is
    the oldest claimable row. A row leaked by a crashed run and backdated
    further would break that quietly; this says so loudly instead.
    """
    rows = query(
        conn,
        """
        SELECT id::text FROM ingestion_jobs
        WHERE attempts < max_attempts
          AND ((state = 'queued' AND run_after <= NOW())
            OR (state = 'running'
                AND (lease_expires_at IS NULL OR lease_expires_at < NOW())))
          AND run_after < (SELECT run_after FROM ingestion_jobs WHERE id = %s)
        """,
        (job_id,),
    )
    assert rows == [], (
        "a claimable job older than this test's own exists, so 'the claim "
        f"returned my job' proves nothing. Leaked rows: {rows}"
    )


def job_row(conn: Any, job_id: str) -> dict:
    """Read one job with NO tenant scope, as 21-07's handler would not."""
    rows = query(conn, "SELECT * FROM ingestion_jobs WHERE id = %s", (job_id,))
    assert rows, f"job {job_id} disappeared"
    return rows[0]


def jobs_for(conn: Any, repository_id: str) -> list:
    return query(
        conn,
        "SELECT id::text, state, job_type, attempts, needs_rerun "
        "FROM ingestion_jobs WHERE repository_id = %s ORDER BY created_at, id",
        (repository_id,),
    )


def repo_row(conn: Any, org_id: str, repository_id: str) -> dict:
    """Read a repository UNDER ITS OWN TENANT SCOPE.

    `repositories` carries FORCE ROW LEVEL SECURITY, so an unscoped read
    here would return nothing and every "unchanged" assertion would pass
    for the wrong reason.
    """
    with require_tenant(conn, org_id, cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, sync_state, last_synced_at FROM repositories WHERE id = %s",
            (repository_id,),
        )
        row = cur.fetchone()
    assert row is not None, (
        f"repository {repository_id} is invisible under organization {org_id}"
    )
    return dict(row)


def claimed_job(conn: Any, worker: str, job_id: str) -> Job:
    """Claim, and assert the claim returned the job the test meant."""
    assert_no_older_claimable(conn, job_id)
    job = claim(conn, worker, LEASE)
    assert job is not None, "nothing was claimable"
    assert str(job.id) == job_id, (
        f"the claim returned {job.id}, not this test's job {job_id}"
    )
    return job


def expire_lease(conn: Any, job_id: str) -> None:
    """Age a lease out without waiting five minutes for it."""
    sql(
        conn,
        "UPDATE ingestion_jobs SET lease_expires_at = NOW() - INTERVAL '1 second' "
        "WHERE id = %s",
        (job_id,),
    )


def supersede(conn: Any, repository_id: str) -> None:
    """`supersedeLiveSQL`, verbatim from `pkg/jobs/producer.go`.

    ⚠ IT DELIBERATELY LEAVES `lease_owner` AND `lease_expires_at` ATTACHED,
    which is precisely why every fenced statement also carries
    `AND state = 'running'`. Copying the Go statement rather than nulling
    the lease here is what makes these tests exercise the reachable shape.
    """
    sql(
        conn,
        "UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW() "
        "WHERE repository_id = %s AND state IN ('queued','running')",
        (repository_id,),
    )


def exhaust(conn: Any, job_id: str) -> None:
    sql(
        conn,
        "UPDATE ingestion_jobs SET attempts = max_attempts WHERE id = %s",
        (job_id,),
    )


def simple_probe(repository_id: str, commit_sha: str):
    """A `write_results` callback that leaves one findable row behind.

    Stands in for Phase 22's chunk write. It writes to `ingestion_runs`,
    which carries row-level security and `trg_assert_tenant`, so the probe
    also proves the callback runs inside the tenant scope `complete`
    opened -- a callback running unscoped would be refused rather than
    write.
    """

    def write(cur):
        cur.execute(
            "INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status) "
            "VALUES (%s, %s, 'main', 'processing')",
            (repository_id, commit_sha),
        )

    return write


def runs_for(conn: Any, org_id: str, commit_sha: str) -> list:
    with require_tenant(conn, org_id) as cur:
        cur.execute(
            "SELECT id::text FROM ingestion_runs WHERE commit_sha = %s", (commit_sha,)
        )
        return [r[0] for r in cur.fetchall()]


# =====================================================================
# The happy path
# =====================================================================


def test_claim_start_and_complete(db_conn, with_two_orgs, worker_id):
    """enqueue -> claim -> mark_started -> complete, with the projection."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)

    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending"

    job = claimed_job(db_conn, worker_id, job_id)
    assert job.job_type == "full_ingest"
    assert job.attempts == 1
    assert job.max_attempts == 5
    assert job.needs_rerun is False
    assert str(job.organization_id) == org_a.id
    assert str(job.repository_id) == org_a.repo_id

    row = job_row(db_conn, job_id)
    assert row["state"] == "running"
    assert row["lease_owner"] == worker_id
    assert row["lease_expires_at"] is not None
    # ⚠ The claim writes NO projection: the installation check comes first
    # (21-06), and a job under a dead installation must be abandoned to
    # `never_synced` without ever having claimed to be `syncing`.
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending"

    mark_started(db_conn, job, worker_id)
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "syncing"

    assert complete(db_conn, job, worker_id) is False

    row = job_row(db_conn, job_id)
    assert row["state"] == "completed"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["needs_rerun"] is False

    repo = repo_row(db_conn, org_a.id, org_a.repo_id)
    assert repo["sync_state"] == "synced"
    assert repo["last_synced_at"] is not None

    assert len(jobs_for(db_conn, org_a.repo_id)) == 1, (
        "a completion with no rerun flag must enqueue nothing"
    )


def test_completion_commits_the_results_with_the_job(db_conn, with_two_orgs, worker_id):
    """Phase 22's chunk write and the completion land in one transaction.

    That single property is most of the argument for putting the queue in
    Postgres (21-CONTEXT L1): there is no state where a job is done and its
    chunks are missing.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)

    complete(db_conn, job, worker_id, simple_probe(org_a.repo_id, "a" * 40))

    assert job_row(db_conn, job_id)["state"] == "completed"
    assert len(runs_for(db_conn, org_a.id, "a" * 40)) == 1


# =====================================================================
# The rerun follow-up (W4, W5, L7)
# =====================================================================


def test_a_rerun_flagged_mid_run_enqueues_exactly_one_follow_up(
    db_conn, with_two_orgs, worker_id
):
    """A push arriving while a job runs is covered, exactly once."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    mark_started(db_conn, job, worker_id)

    assert flag_rerun(db_conn, org_a) is True, (
        "the producer's upsert must take its ON CONFLICT branch against a "
        "RUNNING job -- if it inserted, this test is not about a rerun"
    )
    assert job_row(db_conn, job_id)["needs_rerun"] is True

    assert complete(db_conn, job, worker_id) is True

    rows = jobs_for(db_conn, org_a.repo_id)
    assert len(rows) == 2, f"expected the completed job and one follow-up, got {rows}"

    original = next(r for r in rows if r["id"] == job_id)
    followup = next(r for r in rows if r["id"] != job_id)

    assert original["state"] == "completed"
    # ⚠ The flag must be CONSUMED, not merely outrun. A terminal row still
    # carrying `needs_rerun = true` is 21-02's durable-looking silent loss:
    # a reader inspecting the table sees a rerun that reads as pending and
    # that nothing will ever act on.
    assert original["needs_rerun"] is False

    assert followup["state"] == "queued"
    assert followup["job_type"] == "incremental"
    assert followup["attempts"] == 0
    assert followup["needs_rerun"] is False


def test_the_rerun_flag_survives_a_completion_that_loses_its_lease(
    db_conn, with_two_orgs, worker_id
):
    """A worker that cannot complete must not consume the rerun either.

    The rollback is what guarantees it here: `CLEAR_RERUN_SQL` and
    `COMPLETE_SQL` are in one transaction, so a `LeaseLost` from the second
    undoes the first. `test_clear_rerun_sql_is_fenced_on_the_running_state`
    below pins the statement's own predicate, which this cannot see.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    flag_rerun(db_conn, org_a)
    supersede(db_conn, org_a.repo_id)

    with pytest.raises(LeaseLost):
        complete(db_conn, job, worker_id)

    row = job_row(db_conn, job_id)
    assert row["state"] == "superseded"
    assert row["needs_rerun"] is True, (
        "the rerun flag is the only breadcrumb left after a superseded "
        "worker's completion fails; consuming it loses the rerun silently"
    )


# =====================================================================
# Fencing: reclaimed
# =====================================================================


def test_a_reclaimed_workers_completion_raises_and_commits_nothing(
    db_conn, with_two_orgs
):
    """⚠ The core guarantee: a lost lease can never commit results."""
    org_a, _ = with_two_orgs
    worker_a, worker_b = new_worker_id(), new_worker_id()

    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job_a = claimed_job(db_conn, worker_a, job_id)

    expire_lease(db_conn, job_id)
    job_b = claimed_job(db_conn, worker_b, job_id)
    assert job_b.attempts == 2, "reclaim is a retry; it must increment attempts"

    with pytest.raises(LeaseLost):
        complete(db_conn, job_a, worker_a, simple_probe(org_a.repo_id, "b" * 40))

    assert runs_for(db_conn, org_a.id, "b" * 40) == [], (
        "the stale worker's results were committed -- the fence raised "
        "AFTER write_results ran, so the transaction must roll back"
    )
    row = job_row(db_conn, job_id)
    assert row["state"] == "running"
    assert row["lease_owner"] == worker_b


def test_a_reclaimed_workers_mark_started_writes_nothing(db_conn, with_two_orgs):
    """The projection is fenced THROUGH the job, not only by the job."""
    org_a, _ = with_two_orgs
    worker_a, worker_b = new_worker_id(), new_worker_id()

    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job_a = claimed_job(db_conn, worker_a, job_id)

    expire_lease(db_conn, job_id)
    claimed_job(db_conn, worker_b, job_id)

    mark_started(db_conn, job_a, worker_a)
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending", (
        "a worker that has lost its lease told the UI that a run it does "
        "not own is under way"
    )


def test_the_new_owner_is_unaffected_by_the_stale_worker(db_conn, with_two_orgs):
    """B finishes normally after A's fenced writes all bounced off."""
    org_a, _ = with_two_orgs
    worker_a, worker_b = new_worker_id(), new_worker_id()

    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job_a = claimed_job(db_conn, worker_a, job_id)
    expire_lease(db_conn, job_id)
    job_b = claimed_job(db_conn, worker_b, job_id)

    with pytest.raises(LeaseLost):
        complete(db_conn, job_a, worker_a)
    mark_started(db_conn, job_a, worker_a)

    mark_started(db_conn, job_b, worker_b)
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "syncing"

    assert complete(db_conn, job_b, worker_b, simple_probe(org_a.repo_id, "c" * 40)) is False
    assert job_row(db_conn, job_id)["state"] == "completed"
    assert len(runs_for(db_conn, org_a.id, "c" * 40)) == 1
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "synced"


# =====================================================================
# Fencing: superseded (the half that is NOT covered by lease_owner)
# =====================================================================


@pytest.mark.parametrize("transition", ["complete", "fail", "defer", "abandon"])
def test_a_superseded_workers_writes_all_raise_lease_lost(
    db_conn, with_two_orgs, worker_id, transition
):
    """⚠ The lease is STILL ATTACHED here, which is the whole point.

    `supersedeLiveSQL` leaves `lease_owner` and `lease_expires_at` on the
    row deliberately -- it is the only record of which worker was running
    when the job was taken away. So `lease_owner = %s` alone still matches,
    and only `AND state = 'running'` refuses the write.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)

    supersede(db_conn, org_a.repo_id)
    before = job_row(db_conn, job_id)
    assert before["state"] == "superseded"
    assert before["lease_owner"] == worker_id, (
        "the supersede nulled the lease, so this test would pass even "
        "without the state predicate it exists to check"
    )

    calls = {
        "complete": lambda: complete(db_conn, job, worker_id),
        "fail": lambda: fail(db_conn, job, worker_id, RuntimeError("boom")),
        "defer": lambda: defer(db_conn, job, worker_id, timedelta(hours=1), "suspended"),
        "abandon": lambda: abandon(db_conn, job, worker_id, "uninstalled"),
    }
    with pytest.raises(LeaseLost):
        calls[transition]()

    after = job_row(db_conn, job_id)
    assert after["state"] == "superseded", (
        f"{transition} moved a superseded job to {after['state']}"
    )
    assert after["lease_owner"] == worker_id
    # The replacement job a producer enqueued after superseding this one
    # must not be disturbed either; `sync_state` belongs to it now.
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending"


def test_clear_rerun_sql_is_fenced_on_the_running_state(db_conn, with_two_orgs, worker_id):
    """The one guard `complete` cannot show you, pinned at the statement.

    PR #38's review measured `clearRerunSQL` without `AND state = 'running'`
    matching (`UPDATE 1`) on a superseded row while `completeSQL` correctly
    matched zero -- the worker consumed the flag and could then do nothing
    with it, losing the rerun without even leaving the breadcrumb the
    wrong-order case leaves behind.

    ⚠ IT IS UNOBSERVABLE THROUGH `complete` IN PYTHON, because the two
    statements share a transaction and the `LeaseLost` rolls the bad clear
    back. Testing the statement directly is the only way this port keeps
    the predicate 21-02 added; without this test the mutation survives.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    claimed_job(db_conn, worker_id, job_id)
    flag_rerun(db_conn, org_a)
    supersede(db_conn, org_a.repo_id)

    with db_conn.cursor() as cur:
        cur.execute(CLEAR_RERUN_SQL, (job_id, worker_id))
        matched = cur.rowcount
        cur.fetchall()
    db_conn.commit()

    assert matched == 0, (
        "the rerun clear fired on a superseded row; `lease_owner` alone "
        "does not mean 'still mine'"
    )
    assert job_row(db_conn, job_id)["needs_rerun"] is True


def test_attaching_a_run_is_fenced(db_conn, with_two_orgs, worker_id):
    """A reclaimed worker cannot repoint the new attempt's job at its run."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    supersede(db_conn, org_a.repo_id)

    with pytest.raises(LeaseLost):
        with require_tenant(db_conn, org_a.id) as cur:
            run_id = resolve_ingestion_run(cur, org_a.repo_id, "d" * 40, "main")
            attach_ingestion_run(cur, job, worker_id, run_id)

    assert job_row(db_conn, job_id)["ingestion_run_id"] is None
    assert runs_for(db_conn, org_a.id, "d" * 40) == [], (
        "the run resolution must roll back with the attachment"
    )


# =====================================================================
# Failure, retry and dead-lettering
# =====================================================================


def test_fail_below_max_attempts_requeues_with_a_jittered_backoff(
    db_conn, with_two_orgs, worker_id
):
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    mark_started(db_conn, job, worker_id)

    outcome = fail(
        db_conn,
        job,
        worker_id,
        RuntimeError(f"clone failed: https://x-access-token:{FAKE_TOKEN}@github.com/a/b"),
    )
    assert outcome == "queued"

    row = job_row(db_conn, job_id)
    assert row["state"] == "queued"
    assert row["attempts"] == 1, "a failed attempt is a CONSUMED attempt"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None

    assert "ghs_" not in row["last_error"]
    assert "[REDACTED]" in row["last_error"]
    assert "github.com/a/b" in row["last_error"], (
        "redaction must keep the context that makes the error useful"
    )

    # `run_after` and `updated_at` are both written from the SAME `NOW()`
    # in one statement, so their difference IS the interval, with no clock
    # slop to allow for.
    delay = query(
        db_conn,
        "SELECT EXTRACT(EPOCH FROM (run_after - updated_at)) AS s "
        "FROM ingestion_jobs WHERE id = %s",
        (job_id,),
    )[0]["s"]
    assert 30.0 <= float(delay) <= 60.0, (
        f"attempt 1's delay must be 60s jittered into [30, 60); got {delay}s"
    )

    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "failed"


def test_fail_on_the_final_attempt_writes_dead_directly(db_conn, with_two_orgs, worker_id):
    """Belt and braces: the sweeper is for workers that die, not for this."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    sql(
        db_conn,
        "UPDATE ingestion_jobs SET attempts = max_attempts - 1 WHERE id = %s",
        (job_id,),
    )

    job = claimed_job(db_conn, worker_id, job_id)
    assert job.attempts == job.max_attempts

    assert fail(db_conn, job, worker_id, ValueError("last straw")) == "dead"

    row = job_row(db_conn, job_id)
    assert row["state"] == "dead"
    assert row["lease_owner"] is None
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "failed"


# =====================================================================
# Deferral (a suspended installation)
# =====================================================================


def test_defer_returns_the_attempt_and_leaves_the_projection_alone(
    db_conn, with_two_orgs, worker_id
):
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    before = job_row(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    assert job.attempts == before["attempts"] + 1

    defer(db_conn, job, worker_id, timedelta(hours=1), "installation suspended")

    row = job_row(db_conn, job_id)
    assert row["state"] == "queued"
    assert row["attempts"] == before["attempts"], (
        "a deferral must give the attempt back; the claim had taken one"
    )
    assert row["lease_owner"] is None
    assert row["last_error"] == "installation suspended"

    delay = query(
        db_conn,
        "SELECT EXTRACT(EPOCH FROM (run_after - updated_at)) AS s "
        "FROM ingestion_jobs WHERE id = %s",
        (job_id,),
    )[0]["s"]
    assert abs(float(delay) - 3600.0) < 1.0

    # ⚠ UNCHANGED. A deferral is not something the UI should see: writing
    # `failed` here would call a healthy repository broken, and writing
    # `pending` would flap every hour the App stays suspended.
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending"


def test_a_repeatedly_deferred_job_never_dead_letters(db_conn, with_two_orgs, worker_id):
    """A week-long suspension must not exhaust a repository's attempts."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    max_attempts = job_row(db_conn, job_id)["max_attempts"]

    for round_no in range(max_attempts + 2):
        job = claimed_job(db_conn, worker_id, job_id)
        assert job.attempts == 1, (
            f"round {round_no}: attempts reached {job.attempts}; a deferral "
            "is consuming the budget it exists to preserve"
        )
        defer(db_conn, job, worker_id, timedelta(seconds=0), "installation suspended")
        backdate(db_conn, job_id)

    assert sweep(db_conn) >= 0
    row = job_row(db_conn, job_id)
    assert row["state"] == "queued", f"after {max_attempts + 2} deferrals: {row['state']}"
    assert row["attempts"] == 0


# =====================================================================
# Abandonment (an uninstalled installation) -- ISS-033
# =====================================================================


def test_abandon_supersedes_and_stands_the_repository_down(
    db_conn, with_two_orgs, worker_id
):
    """⚠ `never_synced`, never `failed`. Nothing failed; there is nothing to do."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)

    abandon(db_conn, job, worker_id, "installation uninstalled")

    row = job_row(db_conn, job_id)
    assert row["state"] == "superseded"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["last_error"] == "installation uninstalled"
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "never_synced"


def test_the_sweeper_leaves_an_abandoned_job_alone(db_conn, with_two_orgs, worker_id):
    """Even at max attempts: `dead` is a failure terminal and this is not."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    abandon(db_conn, job, worker_id, "installation uninstalled")
    exhaust(db_conn, job_id)

    sweep(db_conn)

    assert job_row(db_conn, job_id)["state"] == "superseded"
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "never_synced"


def test_an_abandoned_repository_can_be_queued_again(db_conn, with_two_orgs, worker_id):
    """`superseded` is outside the live set, so a reinstall is not blocked."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)
    abandon(db_conn, job, worker_id, "installation uninstalled")

    second = seed_job(db_conn, org_a)
    assert second != job_id
    assert job_row(db_conn, second)["state"] == "queued"


# =====================================================================
# The sweeper
# =====================================================================


def test_the_sweeper_dead_letters_a_queued_job_at_max_attempts(db_conn, with_two_orgs):
    """⚠ The CLEAN-failure path, and the branch whose absence stranded rows.

    A worker that fails cleanly on its last attempt writes `queued`; the
    claim then skips it on `attempts < max_attempts`, and it holds the
    partial unique index forever.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    exhaust(db_conn, job_id)

    assert sweep(db_conn) >= 1
    assert job_row(db_conn, job_id)["state"] == "dead"


def test_the_sweeper_dead_letters_a_running_job_with_an_expired_lease(
    db_conn, with_two_orgs, worker_id
):
    """The CRASH path: a worker that died before it could write anything."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    claimed_job(db_conn, worker_id, job_id)
    exhaust(db_conn, job_id)
    expire_lease(db_conn, job_id)

    assert sweep(db_conn) >= 1
    assert job_row(db_conn, job_id)["state"] == "dead"


def test_the_sweeper_leaves_a_running_job_with_a_live_lease_alone(
    db_conn, with_two_orgs, worker_id
):
    """Its worker is still working, and the heartbeat is what says so."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    claimed_job(db_conn, worker_id, job_id)
    exhaust(db_conn, job_id)

    sweep(db_conn)

    row = job_row(db_conn, job_id)
    assert row["state"] == "running"
    assert row["lease_owner"] == worker_id


def test_the_sweeper_leaves_a_queued_job_below_max_attempts_alone(db_conn, with_two_orgs):
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    sql(
        db_conn,
        "UPDATE ingestion_jobs SET attempts = max_attempts - 1 WHERE id = %s",
        (job_id,),
    )

    sweep(db_conn)

    assert job_row(db_conn, job_id)["state"] == "queued"


# =====================================================================
# Run resolution (W6)
# =====================================================================


def test_resolving_a_run_twice_returns_the_same_row(db_conn, with_two_orgs):
    """⚠ A retry reuses its run. `PostgresWriter` still raises 23505 here.

    `ingestion_runs` carries `UNIQUE (repository_id, commit_sha)` (000002),
    so attempt 2 inserting a fresh run for the same commit is a determinate
    error on this phase's core path. It was raised in two reviews before it
    was addressed.
    """
    org_a, _ = with_two_orgs

    with require_tenant(db_conn, org_a.id) as cur:
        first = resolve_ingestion_run(cur, org_a.repo_id, "e" * 40, "main")
    with require_tenant(db_conn, org_a.id) as cur:
        second = resolve_ingestion_run(cur, org_a.repo_id, "e" * 40, "main")
        other_commit = resolve_ingestion_run(cur, org_a.repo_id, "f" * 40, "main")

    assert first == second, "a retry must reuse its run, not create a second"
    assert other_commit != first, "a different commit gets its own run"


def test_resolving_a_run_attaches_it_to_the_job(db_conn, with_two_orgs, worker_id):
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)

    with require_tenant(db_conn, org_a.id) as cur:
        run_id = resolve_ingestion_run(cur, org_a.repo_id, "1" * 40, "main")
        attach_ingestion_run(cur, job, worker_id, run_id)

    assert str(job_row(db_conn, job_id)["ingestion_run_id"]) == str(run_id)


# =====================================================================
# Tenancy
# =====================================================================


def test_completing_org_as_job_cannot_touch_org_bs_repository(
    db_conn, with_two_orgs, worker_id
):
    """The projection is scoped by the database, not by the caller's care.

    The shape is a worker holding org A's job with org B's repository id --
    the drift `ingestion_jobs_repo_tenant_fk` makes unrepresentable in the
    ROW, constructed here in the WORKER's copy of it, which no constraint
    covers. Under org A's tenant scope the projection must match nothing.
    """
    org_a, org_b = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    real_job = claimed_job(db_conn, worker_id, job_id)

    before_b = repo_row(db_conn, org_b.id, org_b.repo_id)
    doctored = Job(
        id=real_job.id,
        organization_id=real_job.organization_id,
        repository_id=org_b.repo_id,
        job_type=real_job.job_type,
        attempts=real_job.attempts,
        max_attempts=real_job.max_attempts,
        needs_rerun=real_job.needs_rerun,
        payload=real_job.payload,
    )

    complete(db_conn, doctored, worker_id)

    after_b = repo_row(db_conn, org_b.id, org_b.repo_id)
    assert after_b["sync_state"] == before_b["sync_state"] == "never_synced"
    assert after_b["last_synced_at"] is None, (
        "org A's worker wrote org B's repository -- row-level security is "
        "not filtering the projection"
    )
    # Org A's own repository is untouched too: nothing named it.
    assert repo_row(db_conn, org_a.id, org_a.repo_id)["sync_state"] == "pending"


def test_a_tenant_scoped_write_for_the_wrong_organization_is_refused(
    db_conn, with_two_orgs, worker_id
):
    """The enqueue half: the tenant trigger refuses a mismatched pair.

    `complete`'s rerun follow-up runs the producer's upsert, which fires
    `trg_ingestion_jobs_tenant`. A worker whose `organization_id` disagreed
    with its repository would be refused with 42501 rather than writing
    another tenant's row -- the asymmetry `pkg/jobs/doc.go` describes.
    """
    org_a, org_b = with_two_orgs

    with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
        with require_tenant(db_conn, org_a.id) as cur:
            cur.execute(ENQUEUE_UPSERT_SQL, (org_a.id, org_b.repo_id, "full_ingest"))

    assert excinfo.value.pgcode == "42501"
    # ⚠ NOT an existence oracle: `repositories` carries FORCE ROW LEVEL
    # SECURITY, so from outside the owning tenant the read finds nothing
    # and the message is identical to a repository that exists nowhere.
    assert "does not exist" in str(excinfo.value)
    assert org_b.id not in str(excinfo.value)


# =====================================================================
# ISS-013: the unscoped statements must not depend on connection history
# =====================================================================


def test_the_unscoped_statements_do_not_depend_on_connection_history(
    dsn, db_conn, with_two_orgs, worker_id
):
    """⚠ ISS-013, and why it cannot reach `claim` or `sweep`.

    An unscoped read of an RLS table is SILENTLY EMPTY on a fresh
    connection (the GUC is unset, `current_setting(..., true)` yields NULL,
    every row is filtered) and raises 22P02 on one that has COMMITTED a
    `SET LOCAL` (the GUC is left as `''`, and neither RESET nor DISCARD ALL
    clears it). Both halves are asserted here, so the claim's success on
    each connection means something.

    `ingestion_jobs` has no policy to evaluate `current_setting(...)::uuid`
    in, so neither statement can take either branch.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)

    # --- Half one: a FRESH connection, GUC unset -> silently empty.
    fresh = app_conn(dsn)
    try:
        with fresh.cursor() as cur:
            cur.execute("SELECT count(*) FROM repositories")
            assert cur.fetchone()[0] == 0, (
                "premise failed: an unscoped read on a fresh connection "
                "should be silently empty"
            )
        fresh.commit()

        assert_no_older_claimable(fresh, job_id)
        job = claim(fresh, worker_id, LEASE)
        assert job is not None and str(job.id) == job_id
        assert sweep(fresh) >= 0
    finally:
        fresh.close()

    # --- Half two: `db_conn`, which committed a SET LOCAL in `seed_job`.
    with pytest.raises(psycopg2.errors.InvalidTextRepresentation) as excinfo:
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM repositories")
    assert excinfo.value.pgcode == "22P02", (
        "premise failed: this connection has not been poisoned, so the "
        "claim below proves nothing about ISS-013"
    )
    db_conn.rollback()

    # The job is `running` now; the claim's reclaim branch needs the lease
    # gone before it can be taken again.
    expire_lease(db_conn, job_id)
    assert_no_older_claimable(db_conn, job_id)
    reclaimed = claim(db_conn, worker_id, LEASE)
    assert reclaimed is not None and str(reclaimed.id) == job_id
    assert sweep(db_conn) >= 0


def test_a_job_at_max_attempts_is_not_claimed(db_conn, with_two_orgs, worker_id):
    """⚠ `attempts < max_attempts`, which applies to BOTH claim branches.

    Without it a job that reliably kills its worker is reclaimed forever
    and never reaches `dead`, because the transition to `dead` was to be
    written by the worker -- which is the thing that does not survive. The
    sweeper is what then reaches the row (below); the claim's job is to
    stop competing with it.

    Added after the mutation table found this clause unguarded on the
    Python side: 21-02's Go suite pins it, and a port can drift from what
    it copied.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    exhaust(db_conn, job_id)

    claim(db_conn, worker_id, LEASE)

    row = job_row(db_conn, job_id)
    assert row["state"] == "queued", "an exhausted job was claimed"
    assert row["lease_owner"] is None
    assert row["attempts"] == row["max_attempts"]


def test_a_running_job_with_a_null_lease_is_reclaimed(db_conn, with_two_orgs):
    """⚠ `lease_expires_at IS NULL`, and why `NULL < NOW()` is not enough.

    `NULL < NOW()` evaluates to NULL, not true, so a `running` row with a
    null lease matched neither branch: invisible to every claim while still
    occupying the partial unique index, and therefore blocking every future
    job for that repository, silently and forever. A null lease is
    reachable from any partial write or manual intervention.
    """
    org_a, _ = with_two_orgs
    worker_a, worker_b = new_worker_id(), new_worker_id()

    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    claimed_job(db_conn, worker_a, job_id)
    sql(
        db_conn,
        "UPDATE ingestion_jobs SET lease_expires_at = NULL WHERE id = %s",
        (job_id,),
    )
    assert job_row(db_conn, job_id)["state"] == "running"

    reclaimed = claimed_job(db_conn, worker_b, job_id)
    assert reclaimed.attempts == 2
    assert job_row(db_conn, job_id)["lease_owner"] == worker_b


def test_the_claim_takes_the_oldest_claimable_job_whatever_its_tenant(
    db_conn, with_two_orgs, worker_id
):
    """⚠ One queue, ordered by `run_after`, with no organization filter.

    `claimSQL` is queue-wide and cross-tenant BY CONSTRUCTION (21-CONTEXT
    L5): a worker learns its tenant FROM the row it claimed, so scoping the
    claim by the answer would be circular. That is also why neither this
    statement nor the sweeper may ever run inside a request handler.

    The ordering half matters on its own: without `ORDER BY run_after` a
    backed-off job could be picked ahead of one that has been waiting, and
    every other claim assertion in this file would start passing by luck.

    ⚠ THE INSERTION ORDER IS DELIBERATELY THE OPPOSITE OF THE `run_after`
    ORDER. Seeded the other way round the test passes with the `ORDER BY`
    deleted, because a scan with `LIMIT 1` and no ordering returns the
    physically first matching row -- which would be the right answer for
    the wrong reason.
    """
    org_a, org_b = with_two_orgs
    inserted_first = seed_job(db_conn, org_a)
    inserted_second = seed_job(db_conn, org_b)
    backdate(db_conn, inserted_first, offset_seconds=100)
    backdate(db_conn, inserted_second, offset_seconds=0)

    job = claimed_job(db_conn, worker_id, inserted_second)
    assert str(job.organization_id) == org_b.id, (
        "the claim must report the tenant of the row it took, because "
        "nothing else will tell the worker which tenant to scope to"
    )
    assert job_row(db_conn, inserted_first)["state"] == "queued"


def test_claim_returns_none_when_nothing_is_claimable(db_conn, with_two_orgs, worker_id):
    """Not an error, and not an empty Job: the caller idles and polls."""
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    # Push it past NOW() so it is not claimable, without deleting anything.
    sql(
        db_conn,
        "UPDATE ingestion_jobs SET run_after = NOW() + INTERVAL '1 hour' WHERE id = %s",
        (job_id,),
    )

    claimed = claim(db_conn, worker_id, LEASE)
    assert claimed is None or str(claimed.id) != job_id, (
        "a job whose backoff has not elapsed must not be claimed"
    )


# =====================================================================
# Connection-mode preconditions
# =====================================================================


def test_a_transition_refuses_a_connection_that_is_mid_transaction(
    db_conn, with_two_orgs, worker_id
):
    """⚠ Two connection modes, never mixed.

    `require_tenant` refuses a non-idle connection because psycopg2's
    `with conn:` does not nest -- it would silently commit the caller's
    outer work at the inner scope's boundary. The unscoped helper carries
    the same precondition for the same reason.
    """
    org_a, _ = with_two_orgs
    job_id = seed_job(db_conn, org_a)
    backdate(db_conn, job_id)
    job = claimed_job(db_conn, worker_id, job_id)

    with db_conn.cursor() as cur:
        cur.execute("SELECT 1")  # leaves the connection in a transaction

    with pytest.raises(RuntimeError, match="idle"):
        claim(db_conn, worker_id, LEASE)
    with pytest.raises(RuntimeError, match="idle"):
        complete(db_conn, job, worker_id)

    db_conn.rollback()
