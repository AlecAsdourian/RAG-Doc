"""The Python consumer's state transitions over `ingestion_jobs`.

This is the other half of `services/backend/pkg/jobs`. The Go side is the
PRODUCER -- enqueue, and supersede-on-relink; this side is the CONSUMER --
claim, start, complete, fail, defer, abandon, sweep and run resolution. The
two halves share one thing, and it is the point of the design: **the SQL**.
Every statement below that also exists in Go was lifted from
`pkg/jobs/schema_test.go` and `pkg/jobs/producer.go` as they stand on
`main`, with `$n` rewritten to psycopg2's `%s`. 21-02 ran each of them
against `postgres:16-alpine`, the version we deploy.

PR #41's review diffed all seven mechanically and **six are byte-identical**
once `$n` is rewritten: `completeSQL`, `claimSQL`, `sweepSQL`, `failSQL`,
`clearRerunSQL` and `resolveRunSQL`. THREE STATEMENTS DIFFER, all three
deliberately and all three by a `RETURNING` clause only -- same rows
selected, nothing extra written:

  - `ENQUEUE_UPSERT_SQL` returns `id::text` where Go returns `id`, because
    psycopg2 hands an unqualified `uuid` back as `str` anyway (see `Job`)
    and the cast says so at the statement rather than leaving it implicit.
  - `SWEEP_SQL` adds a `RETURNING` list the bare `sweepSQL` has none of, so
    each dead-lettered job can be logged with its id, repository and
    attempts.
  - `FAIL_SQL` adds `RETURNING state`, so `fail` reports the state the
    DATABASE chose rather than re-deriving `failSQL`'s `CASE` in Python
    against a snapshot of `attempts`.

There is no long-running process here. 21-06 builds the loop, the
heartbeat and the sweeper's schedule on top of these functions.

=====================================================================
THE RULES, all of them measured rather than asserted
=====================================================================

⚠ EVERY TERMINAL WRITE IS FENCED ON THE LEASE, AND THE FENCE HAS TWO
HALVES: `id = %s AND lease_owner = %s AND state = 'running'`. The second
half is not optional and is not in 21-CONTEXT L7's transcription.
`supersedeLiveSQL` DELIBERATELY LEAVES THE LEASE ATTACHED to a superseded
row -- the pair is the only record of which worker was running when the job
was taken away, which 21-07's admin endpoint wants -- so `lease_owner`
alone does not mean "still mine". PR #38's review measured a superseded
worker's `clearRerunSQL` matching (`UPDATE 1`) while its `completeSQL`
correctly matched zero: the worker consumed the rerun flag and then could
not act on it. THE RERUN CLEAR CARRIES THE STATE PREDICATE FOR THAT REASON,
and so does every terminal write here.

⚠ ORDER MATTERS IN `complete`, AND THE WRONG ORDER RAISES NOTHING. The
completion write comes BEFORE the re-enqueue. Backwards, the enqueue upsert
finds this job still in the live set, takes its `ON CONFLICT` branch and
sets `needs_rerun = TRUE` on the very row that is about to become
`completed`. No error, no follow-up job, and a terminal row left carrying
`needs_rerun = true` that nothing will ever read. 21-CONTEXT L4 and L7
originally recorded `23505` here; that is what a plain `INSERT` does, and
the only enqueue path is an upsert. Correction dated 2026-09-14, measured
both ways by 21-02.

⚠ TWO CONNECTION MODES, NEVER MIXED.
  - UNSCOPED: `claim` and `sweep`. They touch only `ingestion_jobs`, which
    has NO row-level security (21-CONTEXT L5), and neither statement
    touches `organization_id` or `repository_id`, so
    `trg_ingestion_jobs_tenant` does not fire either. The claim is
    genuinely pre-tenant: the worker learns its tenant FROM the row it
    claimed, which is why scoping the claim by the answer would be
    circular.
  - TENANT-SCOPED: everything else. `repositories` and `ingestion_runs`
    carry RLS plus `trg_assert_tenant`, and the enqueue upsert fires
    `trg_ingestion_jobs_tenant`, which reads `repositories`. All of it runs
    inside `require_tenant(conn, job.organization_id)`.

  ⚠ `require_tenant` REFUSES A CONNECTION THAT IS NOT IDLE, so the unscoped
  transaction must be finished or rolled back before entering tenant scope.
  Every function here opens and closes its own transaction, so a caller
  that calls them in sequence on one connection is always legal.

⚠ `claim` AND `sweep` ARE QUEUE-WIDE AND CROSS-TENANT BY CONSTRUCTION.
They carry no organization filter and the table has no RLS, so an unscoped
session running either reaches every tenant's rows -- measured in PR #38's
review, where a claim returned another organization's job. That is the
design, and it means NEITHER MAY EVER RUN INSIDE A REQUEST HANDLER.

⚠ ISS-013 CANNOT REACH THE UNSCOPED PATH, and that is worth stating because
it is the one place a reader would expect it to. An unscoped read of an RLS
table is empty on a fresh connection and raises `22P02` on one that has
COMMITTED a `SET LOCAL` (the GUC is left as `''`, and `RESET`, `DISCARD
ALL` and reconnecting-short-of-a-new-backend do not clear it). `claim` and
`sweep` read only `ingestion_jobs`, which has no policy to evaluate
`current_setting(...)::uuid` in, so their behaviour does not depend on what
the connection did earlier. Pinned by
`test_claim_is_unaffected_by_a_previously_committed_tenant_scope`.

=====================================================================
THE `sync_state` PROJECTION (21-CONTEXT L2)
=====================================================================

`repositories.sync_state` is a PROJECTION of job state, never a queue.
21-03's producer writes `pending` for a repository that got a NEW job, and
the webhook handlers write `never_synced` on stand-down. Every other
transition is this module's:

| Transition                          | `sync_state`                        |
|-------------------------------------|-------------------------------------|
| claim                               | not written                         |
| mark_started (`running`)            | `syncing`                           |
| complete                            | `synced`, + `last_synced_at = NOW()`|
| fail, retrying (`queued`, att > 0)  | `failed`                            |
| fail, `dead`                        | `failed`                            |
| defer (suspended installation)      | unchanged                           |
| abandon (uninstalled, or none)      | `never_synced`                      |
| superseded by a producer            | not written                         |

The claim writes nothing because the installation check comes first
(21-06): a job under a dead installation is abandoned before anything says
`syncing`.

"Currently retrying" and "dead" are told apart by the JOB's state, not by
`sync_state`: there is no `failed` job state (decision O2), so retrying is
`state = 'queued' AND attempts > 0` and dead is `state = 'dead'`.

⚠ `abandon` PROJECTS `never_synced`, NEVER `failed`. An uninstalled App
means "nothing to do", not "this failed", and `failed` is the
retry-looking terminal state `github_webhook_events.go`'s uninstall
stand-down comment exists to forbid. ISS-033 is filed on the premise that
21-06 calls `abandon` here rather than letting such a job fail its way to
`dead`.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterator, Optional
from uuid import UUID, uuid4

from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from psycopg2.extras import RealDictCursor

from workers.db import require_tenant
from workers.jobs.backoff import next_run_after_delay

logger = logging.getLogger(__name__)

# =====================================================================
# The statements
# =====================================================================
#
# Ported from Go. Where a constant has a Go twin the text is that twin's,
# with `$n` -> `%s`; the comments are reproduced because the reasons are
# what stop the next reader "simplifying" one of them.

# CLAIM_SQL is `claimSQL` (pkg/jobs/schema_test.go). %s lease_owner,
# %s lease interval.
#
# Two clauses are load-bearing and both were missing in a first revision:
#
#   - `attempts < max_attempts` applies to BOTH branches. Without it a job
#     that reliably kills its worker is reclaimed forever and never reaches
#     `dead`, because the transition to `dead` was to be written by the
#     worker -- which is the thing that does not survive.
#   - `lease_expires_at IS NULL`. `NULL < NOW()` is NULL, not true, so a
#     `running` row with a null lease matched neither branch: invisible to
#     every claim while still occupying the partial unique index, and so
#     blocking every future job for that repository, silently and forever.
#
# The parentheses are load-bearing too. `AND` binds tighter than `OR`, so
# the intended grouping happens to be the default -- and relying on that is
# how the next person introduces a bug.
CLAIM_SQL = """
UPDATE ingestion_jobs SET
  state             = 'running',
  lease_owner       = %s,
  lease_expires_at  = NOW() + %s::interval,
  attempts          = attempts + 1,
  updated_at        = NOW()
WHERE id = (
  SELECT id FROM ingestion_jobs
  WHERE attempts < max_attempts
    AND (
         (state = 'queued'  AND run_after <= NOW())
      OR (state = 'running'
          AND (lease_expires_at IS NULL
               OR lease_expires_at < NOW()))
    )
  ORDER BY run_after
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *"""

# _SWEEP_SQL is `sweepSQL`, verbatim. It dead-letters exhausted jobs and
# runs on the heartbeat schedule (21-06 owns the schedule).
#
# ⚠ The `state = 'queued'` branch is the CLEAN-failure path and its absence
# recreated the bug two other fixes had just closed: a worker that fails
# cleanly on its last attempt writes `state='queued'` (decision O2), the
# claim query then skips it on `attempts < max_attempts`, and it sits in
# `queued` holding the partial unique index forever.
#
# The `running` branch is the crash path. FAIL_SQL writes `dead` directly
# on the final attempt, so this is the backstop for workers that die before
# they can.
_SWEEP_SQL = """
UPDATE ingestion_jobs
SET state = 'dead', updated_at = NOW()
WHERE attempts >= max_attempts
  AND (
        state = 'queued'
     OR (state = 'running'
         AND (lease_expires_at IS NULL OR lease_expires_at < NOW()))
  )"""

# The only departure from `sweepSQL`: a RETURNING list, so each
# dead-lettered job can be logged with its id, repository and attempts.
# It selects no rows the bare statement would not and writes nothing extra.
SWEEP_SQL = _SWEEP_SQL + """
RETURNING id::text, repository_id::text, organization_id::text, attempts"""

# COMPLETE_SQL is `completeSQL`, verbatim. %s id, %s lease_owner. A
# reclaimed or superseded worker matches zero rows and exits instead of
# clobbering the new attempt's result.
COMPLETE_SQL = """
UPDATE ingestion_jobs
SET state = 'completed', lease_owner = NULL, lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'"""

# _FAIL_SQL is `failSQL`, verbatim. %s id, %s lease_owner, %s backoff
# interval, %s last_error.
_FAIL_SQL = """
UPDATE ingestion_jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
    run_after = NOW() + %s::interval,
    last_error = %s,
    lease_owner = NULL, lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'"""

# The only departure from `failSQL`: `RETURNING state`, so `fail` reports
# the state THE DATABASE chose rather than re-deriving the `CASE` in Python
# against an `attempts` the caller is holding a snapshot of.
FAIL_SQL = _FAIL_SQL + """
RETURNING state"""

# CLEAR_RERUN_SQL is `clearRerunSQL`, verbatim. %s id, %s lease_owner.
#
# ⚠ THE FENCE IN THE `WHERE` IS WHAT CARRIES THE ANSWER. The naive
# `... SET needs_rerun = FALSE RETURNING needs_rerun` returns the NEW
# value, so the worker reads `false` and drops the rerun. `RETURNING OLD.*`
# would say it directly and is PostgreSQL 18; we run 16, where it raises
# 42P01.
#
# ⚠ `AND state = 'running'` IS PART OF THE FENCE. See the module docstring:
# a superseded row keeps its lease, so without this predicate a superseded
# worker's clear still matches, consuming a rerun it cannot act on -- and
# without even leaving the `needs_rerun = true` breadcrumb the wrong-order
# case leaves behind.
#
# The `needs_rerun` predicate is what makes the row count meaningful: a row
# comes back only if there was a flag to clear.
CLEAR_RERUN_SQL = """
UPDATE ingestion_jobs SET needs_rerun = FALSE, updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running' AND needs_rerun
RETURNING id"""

# ENQUEUE_UPSERT_SQL is `enqueueUpsertSQL` (pkg/jobs/producer.go), the
# single enqueue statement L7 mandates for every producer. %s
# organization_id, %s repository_id, %s job_type.
#
# ⚠ THE INFERENCE CLAUSE IS NOT OPTIONAL and two shorter forms both fail.
# Arbiter inference will not select a PARTIAL index unless the predicate is
# repeated, so `ON CONFLICT (repository_id)` raises 42P10, and
# `ON CONFLICT DO UPDATE` with no target at all raises 42601. Both measured
# on PostgreSQL 16.
#
# `xmax <> 0` distinguishes an insert from an update: on a freshly inserted
# tuple xmax is 0, on one the upsert updated it is the locking transaction.
ENQUEUE_UPSERT_SQL = """
INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
VALUES (%s, %s, %s, 'queued')
ON CONFLICT (repository_id) WHERE state IN ('queued','running')
DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()
RETURNING id::text, (xmax <> 0) AS was_existing"""

# RESOLVE_RUN_SQL is `resolveRunSQL`, verbatim. %s repository_id,
# %s commit_sha, %s branch.
#
# ⚠ A RETRY REUSES THE ROW. `ingestion_runs` carries
# `UNIQUE (repository_id, commit_sha)` (000002), so attempt 2 inserting a
# fresh run for the same commit raises 23505 -- a determinate error on this
# phase's core path, raised in two reviews before it was addressed.
#
# `ingestion_runs` has row-level security AND trg_assert_tenant, so this
# one runs under the job's tenant scope, unlike the claim.
RESOLVE_RUN_SQL = """
INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
VALUES (%s, %s, %s, 'pending')
ON CONFLICT (repository_id, commit_sha) DO UPDATE
  SET started_at = NOW()
RETURNING id"""

# ATTACH_RUN_SQL points the job at the run it resolved. Fenced like every
# other write a stale worker could otherwise land, because a reclaimed
# worker repointing the new attempt's job at ITS run is the same class of
# clobber `completeSQL`'s fence exists for. %s ingestion_run_id, %s id,
# %s lease_owner. It has no Go twin: nothing on the producer side sets
# this column.
ATTACH_RUN_SQL = """
UPDATE ingestion_jobs SET ingestion_run_id = %s, updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'"""

# DEFER_SQL puts a job back WITHOUT using up an attempt: the claim
# incremented `attempts`, and this gives it back. %s delay interval,
# %s reason, %s id, %s lease_owner.
#
# ⚠ `attempts = attempts - 1` IS WHAT KEEPS A SUSPENDED INSTALLATION FROM
# DEAD-LETTERING. A suspension heals on its own and needs no webhook work;
# a job deferred for it must be able to wait indefinitely. Without the
# decrement, five suspended hours put an otherwise healthy repository in
# `dead`, where only ISS-023's unwritten API could retrieve it.
#
# No `sync_state` write: the repository keeps whatever the producer or an
# earlier attempt left. A deferral is not a state change the UI should see.
DEFER_SQL = """
UPDATE ingestion_jobs
SET state = 'queued', attempts = attempts - 1, run_after = NOW() + %s::interval,
    lease_owner = NULL, lease_expires_at = NULL, last_error = %s,
    updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'"""

# ABANDON_SQL takes a job that can NEVER run out of the live set:
# `installation_id IS NULL`, or an installation carrying `uninstalled_at`.
# %s reason, %s id, %s lease_owner.
#
# ⚠ `superseded`, NOT `dead` AND NOT `queued`. Nothing failed, so nothing
# should retry and nothing should project `failed`. `superseded` is already
# the state for "this job was taken out of the live set by something other
# than its own completion", which is exactly what an uninstall does.
#
# ⚠ `attempts` IS LEFT ALONE, deliberately, unlike DEFER_SQL above, and
# PR #41's review ruled KEEP after checking the two statements that could
# make it matter. `CLAIM_SQL`'s `attempts < max_attempts` and `_SWEEP_SQL`'s
# `attempts >= max_attempts` are both reachable only from `queued` or
# `running`; a `superseded` row matches neither, so the column is
# BEHAVIOURALLY INERT here. That makes this purely a question of what the
# row records -- and a worker DID claim this job once, so decrementing
# would write a falsehood into an audit row 21-07 reads. The distinction
# from `defer` is principled rather than inconsistent: `defer` returns the
# attempt because the row goes back to `queued`, where the counter IS a
# budget; here it is history.
#
# ISS-033's "no attempt consumed" is about the ENDING -- this path cannot
# walk a repository towards `dead` -- and that holds because the job leaves
# the live set rather than returning to `queued`.
#
# Pinned by test_abandon_supersedes_and_stands_the_repository_down, so a
# reader who takes ISS-033's phrase literally cannot "fix" it with CI
# agreeing.
ABANDON_SQL = """
UPDATE ingestion_jobs
SET state = 'superseded', lease_owner = NULL, lease_expires_at = NULL,
    last_error = %s, updated_at = NOW()
WHERE id = %s AND lease_owner = %s AND state = 'running'"""

# PROJECT_SYNCING_SQL is `mark_started`'s whole body, and the ONLY
# projection here that carries its own fence.
#
# ⚠ WHY THIS ONE NEEDS `EXISTS` AND THE OTHERS DO NOT. Every other
# projection runs in a transaction whose FIRST statement is a lease-fenced
# write on `ingestion_jobs` that raises `LeaseLost` on zero rows -- so the
# projection is unreachable for a worker that has lost its lease, and the
# transaction is rolled back anyway. `mark_started` has no such write to
# hide behind: it touches `repositories` only. The sub-select is that
# missing fence.
#
# ⚠ AND IT MUST NOT BE COPIED ONTO THE OTHERS. After COMPLETE_SQL the job
# is `completed`, so an `EXISTS (... state = 'running')` would be false and
# the projection would silently not happen.
#
# %s repository_id, %s job id, %s lease_owner.
PROJECT_SYNCING_SQL = """
UPDATE repositories SET sync_state = 'syncing', updated_at = NOW()
WHERE id = %s
  AND EXISTS (SELECT 1 FROM ingestion_jobs
              WHERE id = %s AND lease_owner = %s AND state = 'running')"""

# PROJECT_SYNCED_SQL is the completion projection. `last_synced_at` is the
# column the UI reads for "when was this last ingested"; it exists since
# 000010 and nothing had ever written it.  %s repository_id.
PROJECT_SYNCED_SQL = """
UPDATE repositories SET sync_state = 'synced', last_synced_at = NOW(),
    updated_at = NOW()
WHERE id = %s"""

# PROJECT_FAILED_SQL covers BOTH failure shapes -- retrying and dead. They
# are told apart by the JOB's state, not by this column; see the module
# docstring.  %s repository_id.
PROJECT_FAILED_SQL = """
UPDATE repositories SET sync_state = 'failed', updated_at = NOW()
WHERE id = %s"""

# PROJECT_NEVER_SYNCED_SQL matches the webhook stand-down's own write, so a
# repository whose App was uninstalled looks the same whether the uninstall
# reached it through `installation.deleted` or through a worker abandoning
# a job the uninstall raced (ISS-033).  %s repository_id.
PROJECT_NEVER_SYNCED_SQL = """
UPDATE repositories SET sync_state = 'never_synced', updated_at = NOW()
WHERE id = %s"""


# =====================================================================
# Types
# =====================================================================


@dataclass(frozen=True)
class Job:
    """One claimed work item, as the claim returned it.

    A SNAPSHOT, not a live view. `needs_rerun` in particular is the value
    at claim time and is deliberately NOT what `complete` acts on -- a push
    arriving mid-run sets the flag after this was built, and CLEAR_RERUN_SQL
    re-reads it from the row. The field is here for logging and for 21-07.

    `organization_id` is an AUTHORIZATION INPUT: every write this worker
    then makes is scoped to it. It is trustworthy because
    `ingestion_jobs_repo_tenant_fk` makes a job filed under the wrong
    tenant unrepresentable and `trg_ingestion_jobs_tenant` refuses one on
    the way in.
    """

    id: UUID
    organization_id: UUID
    repository_id: UUID
    job_type: str
    attempts: int
    max_attempts: int
    needs_rerun: bool
    payload: Optional[dict]


def new_worker_id() -> str:
    """Return a fresh `lease_owner` for one worker process.

    ⚠ A UUID4, NOT HOSTNAME PLUS PID. 21-CONTEXT left this open ("hostname
    plus PID is the obvious choice and is wrong under container restarts
    that reuse both"); this is where it is settled. A container scheduler
    that restarts a crashed worker onto the same host with the same pid
    namespace gives the new process the OLD one's identity -- and the
    lease fence then reads as "still mine" for a job the dead process had.
    The fence is only as strong as the uniqueness of the value it compares.

    Called ONCE at worker start, never per job: a per-job identity would
    make the heartbeat's fence disagree with the claim's.
    """
    return str(uuid4())


class LeaseLost(Exception):
    """A fenced write matched no row: the job was reclaimed or superseded.

    Not an error in the job -- an error in this worker's belief that it
    still owns the job. The caller logs it and moves on; it must NOT retry
    the write, and it must not treat the job as failed, because some other
    worker now owns it (or a producer has replaced it) and writing anything
    further would clobber that.

    Raised from inside the tenant-scoped transaction, so `require_tenant`
    rolls back everything the transition had written -- including a
    `write_results` callback's rows. That rollback is the guarantee: a
    worker that has lost its lease cannot commit results.
    """


# =====================================================================
# Redaction
# =====================================================================

#: ⚠ `last_error` IS PERSISTED AND IS SHOWN BY 21-07's ADMIN ENDPOINT, so
#: anything that reaches it is as good as logged forever. Exception
#: messages routinely carry the thing that failed, and for this worker that
#: is a clone URL with an installation token in it
#: (`https://x-access-token:ghs_...@github.com/...`), or an OpenAI client
#: error quoting its own key.
#:
#: ALL SIX of GitHub's documented token prefixes, plus OpenAI's, plus the
#: two shapes 21-06 puts into this worker's reach:
#:   ghs_        installation access token (the one a clone URL carries)
#:   ghp_        classic personal access token
#:   gho_        OAuth access token
#:   ghu_        user-to-server token
#:   ghr_        refresh token
#:   github_pat_ fine-grained personal access token
#:   sk-         OpenAI API key, including the `sk-proj-` form
#:   eyJ....     a JWT: three base64url segments. Covers BOTH the GitHub
#:               App JWT that mints an installation token and Supabase's
#:               service-role key, which is also a JWT.
#:   -----BEGIN ... PRIVATE KEY-----  a PEM block, whole.
#:
#: ⚠ THE LAST TWO ARE NOT REACHABLE FROM THIS MODULE TODAY -- the worker
#: holds only `ghs_` and `sk-`. They are here because 21-06 adds the
#: claim-time installation read, and whatever mints an installation token
#: holds an App JWT signed with the App private key. Widening the pattern
#: before that path exists costs one regular expression; widening it
#: afterwards costs whatever was written to the column in between.
#:
#: TWO SHAPES WERE CONSIDERED AND DECLINED, both because the cure is worse:
#:   - a bare 40-hex run (the App client secret). It also matches a git
#:     COMMIT SHA, which is legitimate, useful context in exactly these
#:     messages -- redacting it would blind the admin endpoint to which
#:     commit failed.
#:   - a generic `://user:password@` DSN arm. psycopg2's connection errors
#:     do not quote the password, and the clone-URL case is already covered
#:     by the `ghs_` arm; a generic arm would redact the visible half of a
#:     credential-free URL for nothing.
#:
#: ORDER: the PEM arm first, so a whole block collapses to one marker
#: rather than having its base64 body picked at by the other arms; then
#: `github_pat_`, so it wins against any future prefix that is its own
#: prefix. `re.sub` with an alternation takes the leftmost match and, at
#: equal position, the earliest alternative.
_TOKEN_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]+"
    r"|gh[psuor]_[A-Za-z0-9]+"
    r"|sk-[A-Za-z0-9_-]+"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
)

_REDACTED = "[REDACTED]"

#: ⚠ psycopg2 REFUSES A NUL IN A TEXT PARAMETER, and it refuses it
#: CLIENT-SIDE: `ValueError: A string literal cannot contain NUL (0x00)
#: characters`, raised while building the statement, before anything is
#: sent. So it is not a `psycopg2.Error` and cannot be caught as one.
#:
#: Left in, it loses the whole failure write. The `ValueError` is raised
#: INSIDE `require_tenant`, which rolls back, so `fail` leaves the job
#: `running` at `attempts = 1` with `last_error` still NULL -- holding the
#: partial unique index, with nothing recorded, until the lease expires.
#: Deterministic input, so it repeats every attempt: five wasted worker
#: slots and a repository that dead-letters with no reason on it. Found by
#: PR #41's review and reproduced on the real container.
#:
#: REACHABLE FROM A HOSTILE REPOSITORY: binary or malformed file content
#: echoed back through a parser error, a `git`/subprocess stderr dump, or a
#: tree-sitter failure carrying raw bytes.
#:
#: ⚠ IT IS EXACTLY ONE CHARACTER, MEASURED, not "control characters".
#: Every other code point in 0x01-0x1F, 0x7F and the C1 range inserts into
#: a `TEXT` column and reads back unchanged -- probed on a scratch
#: `postgres:16-alpine`, 34 accepted, one rejected. Stripping more would
#: throw away a tab or a newline that makes the error readable.
_NUL = "\x00"

#: The cap on `last_error`, in CHARACTERS rather than bytes -- a 2,000
#: character message of accented text measures 3,974 UTF-8 bytes. That is
#: deliberate and costs nothing: the column is `TEXT`, which has no
#: declared limit, and the budget this cap exists to bound is what a human
#: reads in 21-07's response, not what the row occupies. A Python traceback
#: repr or a multi-megabyte subprocess dump would otherwise be written
#: verbatim.
MAX_ERROR_LENGTH = 2000

_TRUNCATION_MARKER = "... [truncated]"


def _sanitize_text(text: str) -> str:
    """Make a string safe to WRITE and safe to SHOW: strip NUL, redact, trim.

    Three steps, and the first of them is the one whose absence lost the
    write entirely:

    1. **Drop every NUL.** See `_NUL` above. This is a storability fix, not
       a disclosure one -- without it psycopg2 raises before the statement
       is built and the job is stranded `running` with nothing recorded.
    2. **Redact.** See `_TOKEN_PATTERN`.
    3. **Truncate** to `MAX_ERROR_LENGTH`.

    ⚠ STEP 1 COMES BEFORE STEP 2, and that ordering IS load-bearing:
    `ghs_ABC\\x00DEF` redacted first leaves `DEF` visible, because a NUL is
    in none of the character classes and ends the match. Stripping first
    hands the pattern one contiguous token.

    REDACTING BEFORE TRUNCATING IS *NOT* A SECURITY GUARD, and the first
    version of this comment claimed it was ("truncating first can cut a
    token in half and leave most of it in the column"). MEASURED, that is
    false, and PR #41's review re-measured it independently over eighteen
    constructed inputs with zero leaks in either order. The reason is
    structural: truncation removes a SUFFIX and every pattern here anchors
    on a PREFIX, so whatever survives the cut still begins with the prefix
    and still matches. Recorded as mutation X, a deliberate survivor.

    What redacting before truncating does buy is that the OUTPUT LENGTH is
    computed over the text a reader will actually get, so a message made
    entirely of tokens collapses to a few markers instead of being trimmed
    to 2,000 characters of `[REDACTED]`. That is tidiness, and it is stated
    as tidiness.
    """
    redacted = _TOKEN_PATTERN.sub(_REDACTED, text.replace(_NUL, ""))
    if len(redacted) <= MAX_ERROR_LENGTH:
        return redacted
    keep = MAX_ERROR_LENGTH - len(_TRUNCATION_MARKER)
    return redacted[:keep] + _TRUNCATION_MARKER


def sanitize_error(error: BaseException) -> str:
    """Render an exception for `last_error`: typed, redacted and truncated.

    The class name is kept because the message alone is often useless --
    `psycopg2.errors.UniqueViolation` and a bare `Exception` with the same
    text mean very different things to whoever reads the admin endpoint.
    """
    return _sanitize_text(f"{type(error).__name__}: {error}")


# =====================================================================
# Connection modes
# =====================================================================


@contextmanager
def _unscoped(conn: Any, cursor_factory: Optional[Any] = None) -> Iterator[Any]:
    """Yield a cursor in a transaction with NO tenant set.

    The counterpart of `require_tenant`, for the two statements that must
    NOT have one: the claim and the sweep. It is deliberately the same
    shape -- same idle precondition, same autocommit save/restore, same
    commit-on-success -- so that a reader comparing them sees one
    difference, which is the `SET LOCAL` that is not there.

    ⚠ Only `ingestion_jobs` may be touched in here. It is the one table in
    this phase with no row-level security, so an unscoped statement against
    it is deterministic; against any other table it would be ISS-013's
    coin flip (silently empty, or 22P02 on a connection that has committed
    a `SET LOCAL`).
    """
    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE:
        raise RuntimeError(
            "an unscoped job transaction must be entered on an idle "
            "connection; psycopg2's `with conn:` idiom does not nest, so "
            "the caller's in-progress transaction would be silently "
            "committed at this scope's boundary. Commit or roll back "
            "first."
        )
    try:
        prev_autocommit = conn.autocommit
        conn.autocommit = False
        with conn:  # begins tx, commits on clean exit, rolls back on raise
            if cursor_factory is None:
                with conn.cursor() as cur:
                    yield cur
            else:
                with conn.cursor(cursor_factory=cursor_factory) as cur:
                    yield cur
    finally:
        try:
            conn.autocommit = prev_autocommit
        except NameError:  # pragma: no cover - signal between save and set
            pass


# =====================================================================
# Logging
# =====================================================================


def _log(
    level: int,
    transition: str,
    job: Job,
    worker_id: str,
    from_state: str,
    to_state: str,
    **extra: Any,
) -> None:
    """One line per transition, in a shape `grep` and a human both read.

    ⚠ NO PAYLOAD AND NO `last_error`. `payload` is producer-supplied and
    `last_error` is an exception message; both are exactly where a token
    would be if the redaction above ever missed one, and a log line is a
    second place it would then live.
    """
    fields = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.log(
        level,
        "job %s: %s %s->%s job_type=%s org=%s repo=%s attempt=%d/%d worker=%s%s",
        job.id,
        transition,
        from_state,
        to_state,
        job.job_type,
        job.organization_id,
        job.repository_id,
        job.attempts,
        job.max_attempts,
        worker_id,
        (" " + fields) if fields else "",
    )


# =====================================================================
# The transitions
# =====================================================================


def claim(conn: Any, worker_id: str, lease: timedelta) -> Optional[Job]:
    """Take the next claimable job and its lease. UNSCOPED.

    One short transaction. The work itself happens OUTSIDE any transaction
    (cloning and embedding are network calls; holding a Postgres
    transaction open for minutes causes bloat and pins a connection), and
    the results are written by `complete` in a second one.

    ⚠ IT WRITES NO PROJECTION. `sync_state` stays whatever the producer
    left until the installation has been checked (21-06): a job under an
    uninstalled App must be abandoned to `never_synced` without ever having
    claimed to be `syncing`.

    ⚠ RECLAIM IS A RETRY. The statement increments `attempts` on BOTH
    branches, so a job that repeatedly kills its worker walks towards
    `dead` instead of looping forever.

    Args:
        conn: an idle psycopg2 connection with NO tenant scope.
        worker_id: this worker's `lease_owner`. A UUID4 generated at
            worker start -- hostname plus PID repeats across container
            restarts that reuse both, and two workers sharing a
            `lease_owner` fence each other's writes in.
        lease: how long the claim is good for before another worker may
            reclaim it. 5 minutes in production (L3), extended by 21-06's
            heartbeat.

    Returns:
        The claimed `Job`, or None if nothing was claimable.
    """
    with _unscoped(conn, cursor_factory=RealDictCursor) as cur:
        cur.execute(CLAIM_SQL, (worker_id, _interval(lease)))
        row = cur.fetchone()

    if row is None:
        logger.debug("claim: nothing claimable (worker=%s)", worker_id)
        return None

    job = Job(
        id=UUID(str(row["id"])),
        organization_id=UUID(str(row["organization_id"])),
        repository_id=UUID(str(row["repository_id"])),
        job_type=row["job_type"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        needs_rerun=row["needs_rerun"],
        payload=row["payload"],
    )
    _log(logging.INFO, "claim", job, worker_id, "queued", "running")
    return job


def mark_started(conn: Any, job: Job, worker_id: str) -> None:
    """Project `sync_state = 'syncing'`. TENANT-SCOPED.

    Fenced THROUGH the job (PROJECT_SYNCING_SQL's `EXISTS`), so a worker
    that has lost its lease writes nothing rather than telling the UI that
    a run it no longer owns is under way.

    Silent on a lost lease, deliberately: unlike the terminal writes this
    one has nothing to roll back and nothing the caller must stop doing --
    21-06 discovers the loss at its next heartbeat either way. It logs at
    WARNING so the case is still visible.
    """
    with require_tenant(conn, job.organization_id) as cur:
        cur.execute(
            PROJECT_SYNCING_SQL,
            (str(job.repository_id), str(job.id), worker_id),
        )
        projected = cur.rowcount

    if projected == 0:
        logger.warning(
            "job %s: mark_started wrote nothing -- the lease is not ours "
            "(reclaimed or superseded) worker=%s repo=%s",
            job.id,
            worker_id,
            job.repository_id,
        )
        return
    _log(logging.INFO, "mark_started", job, worker_id, "running", "running",
         sync_state="syncing")


def complete(
    conn: Any,
    job: Job,
    worker_id: str,
    write_results: Optional[Callable[[Any], None]] = None,
) -> bool:
    """Finish a job successfully. TENANT-SCOPED, one transaction.

    ⚠ THE ORDER IS THE POINT, and getting it wrong raises nothing at all:

      1. `write_results(cur)`, if given -- Phase 22's chunk write, so that
         chunks and completion commit together or not at all. That single
         property is most of the argument for putting the queue in Postgres
         (L1): there is no state where a job is done and its chunks are
         missing.
      2. CLEAR_RERUN_SQL, fenced. A row back means a push arrived while
         this job was running.
      3. COMPLETE_SQL, fenced. ZERO ROWS RAISES `LeaseLost`, which rolls
         the whole transaction back -- including step 1's results, which is
         the whole reason step 1 is inside this transaction rather than
         before it.
      4. The projection: `synced` and `last_synced_at`.
      5. The rerun's follow-up job, if step 2 found a flag. AFTER step 3,
         never before: the enqueue is an upsert, and against a job still in
         the live set it takes the `ON CONFLICT` branch, flags `needs_rerun`
         on the row about to become `completed`, and creates NOTHING. No
         error is raised. The repository is left with no live job and a
         terminal row carrying a flag nothing will read.

    Args:
        write_results: a callback taking this transaction's cursor. Phase
            22 passes the chunk write here. It must not commit, roll back,
            or open a transaction of its own.

    Returns:
        True if a rerun was flagged and a follow-up job was enqueued.

    Raises:
        LeaseLost: the job was reclaimed or superseded. Nothing is
            committed.
    """
    enqueued_rerun = False

    with require_tenant(conn, job.organization_id) as cur:
        if write_results is not None:
            write_results(cur)

        cur.execute(CLEAR_RERUN_SQL, (str(job.id), worker_id))
        had_rerun = cur.fetchone() is not None

        cur.execute(COMPLETE_SQL, (str(job.id), worker_id))
        if cur.rowcount == 0:
            raise LeaseLost(
                f"job {job.id}: completion matched no row -- the lease is "
                f"not ours (reclaimed or superseded); worker={worker_id}"
            )

        cur.execute(PROJECT_SYNCED_SQL, (str(job.repository_id),))

        if had_rerun:
            # A push arrived mid-run. The repository has no live job as of
            # the statement above, so this INSERTs rather than flagging --
            # nothing else can hold a live job for it, because the partial
            # unique index made ours the only one and our row is still
            # locked by this transaction.
            #
            # `incremental`, not `full_ingest`: a rerun covers what changed
            # since the run that just finished, which is the same thing a
            # push asks for.
            cur.execute(
                ENQUEUE_UPSERT_SQL,
                (str(job.organization_id), str(job.repository_id), "incremental"),
            )
            followup = cur.fetchone()
            enqueued_rerun = True
            logger.info(
                "job %s: rerun follow-up enqueued job_id=%s was_existing=%s "
                "org=%s repo=%s",
                job.id,
                followup[0],
                followup[1],
                job.organization_id,
                job.repository_id,
            )

    _log(logging.INFO, "complete", job, worker_id, "running", "completed",
         sync_state="synced", rerun_enqueued=enqueued_rerun)
    return enqueued_rerun


def fail(conn: Any, job: Job, worker_id: str, error: BaseException) -> str:
    """Record a failed attempt. TENANT-SCOPED, one transaction.

    FAIL_SQL decides between `queued` and `dead` in SQL, from the row's own
    `attempts`, and reports which it chose. Writing `dead` DIRECTLY on the
    final attempt rather than leaving it to the sweeper is belt and braces:
    the sweeper is the backstop for a worker that dies before it can write
    anything, and a clean failure should not have to wait for a sweep.

    `last_error` goes through `sanitize_error`, which redacts installation
    tokens and API keys before they reach a column 21-07 hands back over
    HTTP.

    Returns:
        "queued" (the job will be retried after the backoff) or "dead".

    Raises:
        LeaseLost: the job was reclaimed or superseded; nothing is written.
    """
    delay = next_run_after_delay(job.attempts)
    message = sanitize_error(error)

    with require_tenant(conn, job.organization_id) as cur:
        cur.execute(
            FAIL_SQL,
            (_interval(delay), message, str(job.id), worker_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LeaseLost(
                f"job {job.id}: failure write matched no row -- the lease "
                f"is not ours (reclaimed or superseded); worker={worker_id}"
            )
        new_state = row[0]

        # Both shapes project `failed`; the job's own state is what says
        # whether it will be retried. See the module docstring.
        cur.execute(PROJECT_FAILED_SQL, (str(job.repository_id),))

    _log(logging.WARNING, "fail", job, worker_id, "running", new_state,
         sync_state="failed", retry_in=f"{delay.total_seconds():.0f}s",
         error_class=type(error).__name__)
    return new_state


def defer(
    conn: Any,
    job: Job,
    worker_id: str,
    delay: timedelta,
    reason: str,
) -> None:
    """Put a job back WITHOUT using up an attempt. TENANT-SCOPED.

    For a condition that heals on its own and that nothing will send a
    webhook about -- today, a SUSPENDED installation. 21-04 deliberately
    does not cancel a suspended installation's jobs, on the argument that
    cancelling burns the work for a condition that heals; this is the other
    half of that argument.

    The claim incremented `attempts`, so DEFER_SQL decrements it back. A
    repository whose App is suspended for a week is therefore still
    `queued` at the end of it, not `dead`.

    ⚠ The projection is deliberately UNCHANGED. A deferral is not something
    the UI should see, and rewriting `sync_state` here would either flap
    between `syncing` and `pending` every hour or lie about a failure.

    Raises:
        LeaseLost: the job was reclaimed or superseded; nothing is written.
    """
    # Sanitized ONCE, and used for both the column and the log line. The
    # reason is caller-supplied, and `_log`'s docstring says a log line
    # must not become the second place a token lives; passing the raw value
    # to one of them and the clean value to the other is how that rule gets
    # broken without anyone noticing. Found by PR #41's review.
    detail = _sanitize_text(reason)

    with require_tenant(conn, job.organization_id) as cur:
        cur.execute(
            DEFER_SQL,
            (_interval(delay), detail, str(job.id), worker_id),
        )
        if cur.rowcount == 0:
            raise LeaseLost(
                f"job {job.id}: deferral matched no row -- the lease is not "
                f"ours (reclaimed or superseded); worker={worker_id}"
            )

    _log(logging.INFO, "defer", job, worker_id, "running", "queued",
         sync_state="unchanged", attempts_returned=1,
         retry_in=f"{delay.total_seconds():.0f}s", reason=detail)


def abandon(conn: Any, job: Job, worker_id: str, reason: str) -> None:
    """Take a job that can NEVER run out of the live set. TENANT-SCOPED.

    The ISS-033 path: a `push` racing an `installation.deleted` leaves a
    live job under a dead installation, because neither producer checks
    `uninstalled_at` and neither takes a lock the other waits on. 21-06
    calls this when the claim-time installation read finds
    `installation_id IS NULL` or an installation carrying `uninstalled_at`.

    ⚠ IT IS NOT A FAILURE, and the distinction is the whole issue. Nothing
    went wrong, so:
      - the job goes to `superseded`, not `dead` -- it is out of the live
        set, so the next connect or reinstall can enqueue freely;
      - no attempt is consumed in the sense that matters: this path cannot
        walk a repository towards `dead`, because the job never returns to
        `queued`;
      - the repository projects `never_synced`, matching the webhook
        stand-down exactly (`github_webhook_events.go`: "'failed' is
        deliberately not used -- nothing failed, and the queue must not
        retry these").

    If this were left to the retry path instead, the repository would end
    at `failed` after five attempts -- the retry-looking terminal state
    that comment exists to forbid -- and ISS-033's priority would rise with
    it.

    Raises:
        LeaseLost: the job was reclaimed or superseded; nothing is written.
    """
    # Sanitized once; see `defer` for why the log line gets the same value
    # the column does.
    detail = _sanitize_text(reason)

    with require_tenant(conn, job.organization_id) as cur:
        cur.execute(
            ABANDON_SQL,
            (detail, str(job.id), worker_id),
        )
        if cur.rowcount == 0:
            raise LeaseLost(
                f"job {job.id}: abandonment matched no row -- the lease is "
                f"not ours (reclaimed or superseded); worker={worker_id}"
            )

        cur.execute(PROJECT_NEVER_SYNCED_SQL, (str(job.repository_id),))

    _log(logging.INFO, "abandon", job, worker_id, "running", "superseded",
         sync_state="never_synced", reason=detail)


def sweep(conn: Any) -> int:
    """Dead-letter every exhausted job. UNSCOPED. Returns how many moved.

    ⚠ QUEUE-WIDE AND CROSS-TENANT BY CONSTRUCTION, like the claim: it
    carries no organization filter, and the table has no row-level security
    to supply one. It must never run inside a request handler.

    It reaches two shapes the claim cannot:
      - `queued` at max attempts -- the CLEAN-failure path. A worker that
        fails on its last attempt writes `queued` unless FAIL_SQL's `CASE`
        catches it first, and the claim then skips the row on
        `attempts < max_attempts`, leaving it holding the partial unique
        index forever.
      - `running` with an expired or null lease at max attempts -- the
        CRASH path, for a worker that died before it could write anything.

    A `running` job with a LIVE lease is left alone: its worker is still
    working, and its heartbeat is what says so.
    """
    with _unscoped(conn) as cur:
        cur.execute(SWEEP_SQL)
        swept = cur.fetchall()

    for job_id, repository_id, organization_id, attempts in swept:
        logger.warning(
            "job %s: swept to dead (exhausted) org=%s repo=%s attempts=%d",
            job_id,
            organization_id,
            repository_id,
            attempts,
        )
    return len(swept)


def resolve_ingestion_run(
    cur: Any,
    repository_id: Any,
    commit_sha: str,
    branch: str,
) -> UUID:
    """Find or create the job's `ingestion_runs` row (W6). TENANT-SCOPED.

    Takes a CURSOR, not a connection, because it belongs inside whatever
    transaction the caller is already running -- normally the one that also
    calls `attach_ingestion_run`.

    ⚠ IT RESOLVES RATHER THAN INSERTS because `ingestion_runs` carries
    `UNIQUE (repository_id, commit_sha)` (000002), so attempt 2 of the same
    commit raises 23505 -- a determinate error on this phase's core path,
    raised in two reviews before it was addressed. A retry reuses its run;
    a superseded run's replacement reopens the same one; a job for a
    different commit gets its own row, which is the normal case.

    ⚠ IT DOES NOT REPLACE `PostgresWriter.create_ingestion_run`, which has
    no `ON CONFLICT` and still raises 23505 on a repeat commit. Phase 22
    owns wiring the pipeline through this helper.
    """
    cur.execute(RESOLVE_RUN_SQL, (str(repository_id), commit_sha, branch))
    return UUID(str(cur.fetchone()[0]))


def attach_ingestion_run(cur: Any, job: Job, worker_id: str, run_id: UUID) -> None:
    """Point the job at its run, fenced. TENANT-SCOPED, caller's cursor.

    Fenced for the same reason every other write here is: a reclaimed
    worker repointing the row at ITS run would overwrite the new attempt's,
    and `ingestion_run_id` is what 21-07 and Phase 22's progress endpoint
    join through.

    Raises:
        LeaseLost: the job was reclaimed or superseded. Raised inside the
            caller's transaction, so the caller's `require_tenant` rolls
            back the run resolution with it.
    """
    cur.execute(ATTACH_RUN_SQL, (str(run_id), str(job.id), worker_id))
    if cur.rowcount == 0:
        raise LeaseLost(
            f"job {job.id}: attaching run {run_id} matched no row -- the "
            f"lease is not ours (reclaimed or superseded); worker={worker_id}"
        )


def _interval(delta: timedelta) -> str:
    """Render a timedelta for a `%s::interval` parameter.

    Seconds, so that the value is unambiguous whatever the server's
    `IntervalStyle` is, and so that sub-second test timings survive.
    """
    return f"{delta.total_seconds()} seconds"
