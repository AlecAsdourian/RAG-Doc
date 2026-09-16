package jobs_test

// Migration 000014 (21-02): `ingestion_jobs`, and every SQL statement the
// rest of Phase 21 is built on, executed against the PostgreSQL version we
// deploy (`postgres:16-alpine`). 21-CONTEXT's statements were transcribed
// and run on PostgreSQL 17; this file re-runs them on 16, so 21-03 through
// 21-06 build on SQL that has run rather than SQL that reads well.
//
// The shared statements are named constants at the top. 21-03 (Go producer)
// and 21-05 (Python consumer) lift them verbatim; they are constants rather
// than inline strings so that a change to one of them fails a test here
// before it reaches a producer.
//
// ONE BEHAVIOUR PER TEST. Writes run as the app role inside a tenant
// transaction, as production does, except where the test is ABOUT the
// unscoped case. The few steps that need more privilege take a dedicated
// superuser connection through isolation.WithSuperuserConn.
//
// NEVER `session_replication_role = replica` in this file, although
// pkg/auth/testing.go uses it for cleanup. It switches off foreign-key
// enforcement, and W1 exists to prove a foreign key.
//
// Tenant ids are interpolated into SET LOCAL (which cannot bind a
// parameter). They come from WithTwoOrgs, which reads them back from the
// database as UUIDs, so the interpolation cannot carry anything else.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

// =====================================================================
// The shared statements
// =====================================================================

// enqueueConflictClause is the half of the enqueue upsert that every
// producer shares, split out so the bulk form below is structurally the
// same statement rather than a copy that has to be kept in step.
//
// ⚠ THE INFERENCE CLAUSE IS NOT OPTIONAL and two shorter forms both fail.
// Arbiter inference will not select a PARTIAL index unless the predicate is
// repeated, so `ON CONFLICT (repository_id)` raises 42P10, and
// `ON CONFLICT DO UPDATE` with no target at all raises 42601. Both measured
// on PostgreSQL 16 by TestIngestionJobs_EnqueueUpsertParsesAndReports.
//
// `xmax <> 0` distinguishes an insert from an update: on a freshly inserted
// tuple xmax is 0, on one the upsert updated it is the locking transaction.
const enqueueConflictClause = `
ON CONFLICT (repository_id) WHERE state IN ('queued','running')
DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()
RETURNING id, (xmax <> 0) AS was_existing`

// enqueueUpsertSQL is 21-CONTEXT L7's single enqueue statement, used by
// every producer — push, relink and bulk `installation_repositories.added`
// alike. $1 organization_id, $2 repository_id, $3 job_type.
//
// It never raises 23505: each row either enqueues or flags `needs_rerun` on
// the live job, and `was_existing` tells the caller which. That is what
// makes a bulk enqueue racing a relink safe (L8) — the earlier
// catch-23505-and-return-success design lost two repositories of three
// while reporting success.
const enqueueUpsertSQL = `
INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
VALUES ($1, $2, $3, 'queued')` + enqueueConflictClause

// supersedeLiveSQL takes a repository's live job out of the live set (L4).
// It runs BEFORE the enqueue of its replacement, in the same transaction.
//
// ⚠ IT DELIBERATELY LEAVES `lease_owner` AND `lease_expires_at` ATTACHED.
// A supersede is written by a different actor from the worker that holds the
// lease, and the pair is the only record of which worker was running the job
// when it was taken away — which 21-07's admin endpoint wants. The
// consequence is that a lease-fenced statement CANNOT rely on the lease
// alone to mean "still mine": every one of them also carries
// `AND state = 'running'`. See clearRerunSQL, where that predicate was
// missing.
const supersedeLiveSQL = `
UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW()
WHERE repository_id = $1 AND state IN ('queued','running')`

// completeSQL is the terminal success write, FENCED ON THE LEASE (L3).
// $1 id, $2 lease_owner. A reclaimed or superseded worker matches zero rows
// and exits instead of clobbering the new attempt's result.
const completeSQL = `
UPDATE ingestion_jobs
SET state = 'completed', lease_owner = NULL, lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = $1 AND lease_owner = $2 AND state = 'running'`

// claimSQL is 21-RESEARCH's claim query. $1 lease_owner, $2 lease interval.
//
// Two clauses in it are load-bearing and both were missing in a first
// revision:
//
//   - `attempts < max_attempts` applies to BOTH branches. Without it a job
//     that reliably kills its worker is reclaimed forever and never reaches
//     `dead`, because the transition to `dead` was to be written by the
//     worker — which is the thing that does not survive.
//   - `lease_expires_at IS NULL`. `NULL < NOW()` is NULL, not true, so a
//     `running` row with a null lease matched neither branch: invisible to
//     every claim while still occupying the partial unique index, and so
//     blocking every future job for that repository, silently and forever.
//
// The parentheses are load-bearing too. `AND` binds tighter than `OR`, so
// the intended grouping happens to be the default — and relying on that is
// how the next person introduces a bug.
//
// It touches neither organization_id nor repository_id, so
// trg_ingestion_jobs_tenant does not fire and the claim stays genuinely
// pre-tenant. That is the whole reason this table has no row-level
// security.
const claimSQL = `
UPDATE ingestion_jobs SET
  state             = 'running',
  lease_owner       = $1,
  lease_expires_at  = NOW() + $2::interval,
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
RETURNING *`

// sweepSQL dead-letters exhausted jobs. It runs on the heartbeat schedule.
//
// ⚠ The `state = 'queued'` branch is the CLEAN-failure path and its absence
// recreated the bug two other fixes had just closed: a worker that fails
// cleanly on its last attempt writes `state='queued'` (decision O2), the
// claim query then skips it on `attempts < max_attempts`, and it sits in
// `queued` holding the partial unique index forever.
//
// The `running` branch is the crash path. `failSQL` below writes `dead`
// directly on the final attempt, so this is the backstop for workers that
// die before they can.
const sweepSQL = `
UPDATE ingestion_jobs
SET state = 'dead', updated_at = NOW()
WHERE attempts >= max_attempts
  AND (
        state = 'queued'
     OR (state = 'running'
         AND (lease_expires_at IS NULL OR lease_expires_at < NOW()))
  )`

// failSQL is the terminal failure write, FENCED ON THE LEASE (L3).
// $1 id, $2 lease_owner, $3 backoff interval, $4 last_error.
const failSQL = `
UPDATE ingestion_jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
    run_after = NOW() + $3::interval,
    last_error = $4,
    lease_owner = NULL, lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = $1 AND lease_owner = $2 AND state = 'running'`

// clearRerunSQL clears `needs_rerun` and reports whether there was one to
// clear, fenced on the lease. $1 id, $2 lease_owner.
//
// ⚠ THE FENCE IN THE `WHERE` IS WHAT CARRIES THE ANSWER. The naive
// `... SET needs_rerun = FALSE RETURNING needs_rerun` returns the NEW
// value, so the worker reads `false` and drops the rerun. `RETURNING OLD.*`
// would say it directly and is PostgreSQL 18; we run 16.
//
// ⚠ `AND state = 'running'` IS PART OF THE FENCE, and it is not in
// 21-CONTEXT L7's transcription. Found in PR #38's review, measured:
// `supersedeLiveSQL` leaves `lease_owner` and `lease_expires_at` attached,
// so without this predicate a superseded worker's clear still matched —
// `UPDATE 1` — while its `completeSQL` correctly matched zero rows. The
// worker would consume the rerun flag and then be unable to act on it,
// losing the rerun WITHOUT leaving the `needs_rerun = true` breadcrumb that
// W5's silent-loss case at least leaves behind. Pinned by
// TestIngestionJobs_ClearingARerunIsFencedOnTheRunningState.
//
// The alternative fix — having `supersedeLiveSQL` null the lease pair —
// was rejected: it removes the matching value rather than stating the
// invariant, it would leave any FUTURE fenced statement exposed, and the
// lease pair on a superseded row is the only record of which worker was
// running it, which 21-07's admin endpoint wants.
const clearRerunSQL = `
UPDATE ingestion_jobs SET needs_rerun = FALSE, updated_at = NOW()
WHERE id = $1 AND lease_owner = $2 AND state = 'running' AND needs_rerun
RETURNING id`

// clearRerunNaiveSQL is the form that looks right and silently drops the
// rerun. It exists only so W4 can measure the difference.
const clearRerunNaiveSQL = `
UPDATE ingestion_jobs SET needs_rerun = FALSE, updated_at = NOW()
WHERE id = $1 AND lease_owner = $2
RETURNING needs_rerun`

// resolveRunSQL resolves the job's `ingestion_runs` row instead of
// inserting it. $1 repository_id, $2 commit_sha, $3 branch.
//
// ⚠ A RETRY REUSES THE ROW. `ingestion_runs` carries
// `UNIQUE (repository_id, commit_sha)` (000002), so attempt 2 inserting a
// fresh run for the same commit raises 23505 — a determinate error on this
// phase's core path, raised in two reviews before it was addressed.
//
// `ingestion_runs` has row-level security AND trg_assert_tenant, so this
// one runs under the job's tenant scope, unlike the claim.
const resolveRunSQL = `
INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
VALUES ($1, $2, $3, 'pending')
ON CONFLICT (repository_id, commit_sha) DO UPDATE
  SET started_at = NOW()
RETURNING id`

const (
	liveJobIndex     = "idx_ingestion_jobs_one_live_per_repo"
	repoTenantFK     = "ingestion_jobs_repo_tenant_fk"
	tenantTrigger    = "trg_ingestion_jobs_tenant"
	leaseInterval    = "5 minutes"
	tenantSQLState   = "42501"
	uniqueSQLState   = "23505"
	fkSQLState       = "23503"
	badUUIDSQLState  = "22P02"
	syntaxSQLState   = "42601"
	noArbiterSQLCode = "42P10"
)

// =====================================================================
// W1 — the composite foreign key, alone
// =====================================================================

// The foreign key is the GUARANTEE: with the tenant trigger disabled and
// row-level security bypassed, a job whose organization_id is not its
// repository's is still refused.
func TestIngestionJobs_CompositeForeignKeyMakesATenantMismatchUnrepresentable(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		isolation.WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			// DISABLE TRIGGER is durable catalog state once committed, and
			// this container is reused across runs. It is never committed
			// here: the deferred rollback undoes it, and the server aborts
			// the transaction if this process dies first.
			_, err = tx.Exec(ctx, `ALTER TABLE ingestion_jobs DISABLE TRIGGER `+tenantTrigger)
			require.NoError(t, err)

			// A superuser bypasses row-level security, and ingestion_jobs
			// has neither RLS nor trg_assert_tenant, so nothing here needs
			// a tenant and nothing but the key can refuse the row.
			_, err = tx.Exec(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'full_ingest', 'queued')`,
				orgB.ID, orgA.RepoID,
			)
			pgErr := requirePgError(t, err)
			require.Equal(t, fkSQLState, pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, repoTenantFK, pgErr.ConstraintName,
				"the composite key, not one of the single-column ones, must be what refuses it")
		})

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// The trigger is the MESSAGE. The same mismatch, trigger enabled, under the
// owning tenant, names the problem rather than a constraint.
func TestIngestionJobs_TenantTriggerNamesTheMismatch(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		_, err = tx.Exec(ctx,
			`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
			 VALUES ($1, $2, 'full_ingest', 'queued')`,
			orgB.ID, orgA.RepoID,
		)
		pgErr := requirePgError(t, err)
		require.Equal(t, tenantSQLState, pgErr.Code, "message: %s", pgErr.Message)
		require.Equal(t,
			fmt.Sprintf("organization_id %s does not match repository %s (owner %s)",
				orgB.ID, orgA.RepoID, orgA.ID),
			pgErr.Message)
		// The owner it names is the CALLER's own organization, which is the
		// only one this branch can be reached with under row-level
		// security. The test below is what pins that.
		require.NoError(t, tx.Rollback(ctx))
	})
}

// The trigger is not an existence oracle. A repository in ANOTHER
// organization must fail exactly like one that exists nowhere: under
// row-level security the read returns no row either way, so the mismatch
// branch — the one that names an owner — is unreachable from outside the
// tenant.
//
// This is the same hazard PR #37's review measured in 000013's trigger,
// which reads `projects` and so had no such protection. Here the read goes
// through `repositories`, which has FORCE ROW LEVEL SECURITY; this test
// exists because that is a property of the table read, not of the code, and
// nothing else would notice if the read moved.
func TestIngestionJobs_TheTenantTriggerIsNotAnExistenceOracle(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	absentRepoID := uuid.NewString()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// Each insert gets its own transaction: the first failure aborts it.
		insertAs := func(repoID string) *pgconn.PgError {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = tx.Exec(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'full_ingest', 'queued')`,
				orgA.ID, repoID,
			)
			return requirePgError(t, err)
		}

		otherTenant := insertAs(orgB.RepoID)
		absent := insertAs(absentRepoID)

		require.Equal(t, tenantSQLState, otherTenant.Code, "message: %s", otherTenant.Message)
		require.Equal(t, absent.Code, otherTenant.Code,
			"a repository in another organization must fail with the same SQLSTATE as one that does not exist")
		require.Equal(t,
			fmt.Sprintf("repository %s does not exist", orgB.RepoID), otherTenant.Message)
		require.Equal(t,
			fmt.Sprintf("repository %s does not exist", absentRepoID), absent.Message)
		require.NotContains(t, otherTenant.Message, "does not match",
			"the mismatch branch must be unreachable from outside the tenant")
		require.NotContains(t, otherTenant.Message, orgB.ID,
			"the error must not name the owning organization")
	})
}

// =====================================================================
// The ISS-016 guard: one live job per repository
// =====================================================================

func TestIngestionJobs_OneLiveJobPerRepository(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		first := seedJob(t, pool, orgA, orgA.RepoID, "queued", jobOpts{})

		t.Run("a second live job is refused", func(t *testing.T) {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = tx.Exec(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'full_ingest', 'queued')`,
				orgA.ID, orgA.RepoID,
			)
			pgErr := requirePgError(t, err)
			require.Equal(t, uniqueSQLState, pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, liveJobIndex, pgErr.ConstraintName)
		})

		t.Run("once the first is completed, a new one inserts", func(t *testing.T) {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			tag, err := tx.Exec(ctx,
				`UPDATE ingestion_jobs SET state = 'completed', updated_at = NOW() WHERE id = $1`, first)
			require.NoError(t, err)
			require.EqualValues(t, 1, tag.RowsAffected())

			var second string
			require.NoError(t, tx.QueryRow(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'full_ingest', 'queued') RETURNING id::text`,
				orgA.ID, orgA.RepoID,
			).Scan(&second))
			require.NotEqual(t, first, second)
			require.NoError(t, tx.Commit(ctx))

			require.Equal(t, []string{second}, liveJobIDs(t, pool, orgA.RepoID))
		})
	})
}

// =====================================================================
// W2 — the enqueue upsert parses and reports correctly
// =====================================================================

func TestIngestionJobs_EnqueueUpsertParsesAndReports(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		firstID, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "full_ingest")
		require.False(t, wasExisting, "the first enqueue inserts")

		secondID, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "incremental")
		require.True(t, wasExisting, "the second enqueue flags the live job")
		require.Equal(t, firstID, secondID, "and reports the live job's id, not a new one")

		var needsRerun bool
		require.NoError(t, tx.QueryRow(ctx,
			`SELECT needs_rerun FROM ingestion_jobs WHERE id = $1`, firstID).Scan(&needsRerun))
		require.True(t, needsRerun, "the conflict must set needs_rerun")
		require.NoError(t, tx.Rollback(ctx))

		// The two shorter forms, each in its own transaction because a
		// failure aborts one.
		shorter := []struct {
			name, sql, sqlstate string
		}{
			{
				"ON CONFLICT DO UPDATE with no target",
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, $3, 'queued')
				 ON CONFLICT DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()
				 RETURNING id, (xmax <> 0) AS was_existing`,
				syntaxSQLState,
			},
			{
				"ON CONFLICT (repository_id) without the index predicate",
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, $3, 'queued')
				 ON CONFLICT (repository_id) DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()
				 RETURNING id, (xmax <> 0) AS was_existing`,
				noArbiterSQLCode,
			},
		}
		for _, form := range shorter {
			t.Run(form.name, func(t *testing.T) {
				tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()

				_, err = tx.Exec(ctx, form.sql, orgA.ID, orgA.RepoID, "full_ingest")
				pgErr := requirePgError(t, err)
				require.Equal(t, form.sqlstate, pgErr.Code, "message: %s", pgErr.Message)
			})
		}
	})
}

// =====================================================================
// W3 — a bulk enqueue racing a live job handles every row
// =====================================================================
//
// The case that silently lost two repositories of three while the handler
// reported success, under the withdrawn catch-23505 design (L8).

func TestIngestionJobs_BulkEnqueueRacingALiveJobHandlesEveryRow(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		repos := []string{
			orgA.RepoID,
			insertRepository(t, pool, orgA, "bulk-2"),
			insertRepository(t, pool, orgA, "bulk-3"),
		}
		// The relink that got there first.
		live := seedJob(t, pool, orgA, repos[0], "running", jobOpts{LeaseOwner: "worker-1"})

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		args := make([]any, 0, len(repos)*2)
		for _, repo := range repos {
			args = append(args, orgA.ID, repo)
		}
		rows, err := tx.Query(ctx, bulkEnqueueSQL(len(repos)), args...)
		require.NoError(t, err)
		results, err := pgx.CollectRows(rows, pgx.RowToStructByPos[enqueueResult])
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		require.Len(t, results, len(repos), "every row of the statement must report")
		flagged, inserted := 0, 0
		for _, r := range results {
			if r.WasExisting {
				flagged++
				require.Equal(t, live, r.ID.String(), "the flagged row must be the live job")
			} else {
				inserted++
			}
		}
		require.Equal(t, 1, flagged)
		require.Equal(t, 2, inserted)

		for _, repo := range repos {
			require.Len(t, liveJobIDs(t, pool, repo), 1,
				"repository %s must end with exactly one live job", repo)
		}

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// W4 — the conditional clear reports a rerun; the naive form loses it
// =====================================================================

func TestIngestionJobs_ConditionalClearReportsARerun(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		const owner = "worker-1"
		fenced := seedJob(t, pool, orgA, orgA.RepoID, "running",
			jobOpts{LeaseOwner: owner, NeedsRerun: true})

		var clearedID string
		require.NoError(t, pool.QueryRow(ctx, clearRerunSQL, fenced, owner).Scan(&clearedID),
			"the fenced clear must return a row when there was a rerun to do")
		require.Equal(t, fenced, clearedID)

		// And nothing the second time: the flag is gone.
		err := pool.QueryRow(ctx, clearRerunSQL, fenced, owner).Scan(&clearedID)
		require.ErrorIs(t, err, pgx.ErrNoRows, "a cleared flag must not report a second rerun")

		// The naive form, on a job that genuinely has a rerun pending.
		naive := seedJob(t, pool, orgA, insertRepository(t, pool, orgA, "naive"), "running",
			jobOpts{LeaseOwner: owner, NeedsRerun: true})
		var reported bool
		require.NoError(t, pool.QueryRow(ctx, clearRerunNaiveSQL, naive, owner).Scan(&reported))
		require.False(t, reported,
			"RETURNING needs_rerun returns the NEW value, so the worker would drop the rerun")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// W5 — completion then re-enqueue, and what the reverse order really does
// =====================================================================

func TestIngestionJobs_CompletionThenReEnqueue(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const owner = "worker-1"

	t.Run("the right order leaves one new live job and no error", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			done := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: owner})

			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			tag, err := tx.Exec(ctx, completeSQL, done, owner)
			require.NoError(t, err)
			require.EqualValues(t, 1, tag.RowsAffected(), "the fence must match the owning worker")

			next, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "incremental")
			require.False(t, wasExisting, "the completed job is out of the live set, so this inserts")
			require.NoError(t, tx.Commit(ctx))

			require.Equal(t, []string{next.String()}, liveJobIDs(t, pool, orgA.RepoID))
			require.Equal(t, "completed", jobState(t, pool, done))
		})
	})

	// ⚠ THE CORRECTED FAILURE MODE. 21-CONTEXT L4 and L7 recorded 23505
	// here. That is what a plain INSERT does; through the upsert — the only
	// enqueue path — the reverse order raises NOTHING. The conflict flags
	// needs_rerun on the very job that is about to leave the live set, and
	// the repository ends with no live job and no error. Silent lost work,
	// which is worse than an error, and the reason the order is mandatory.
	t.Run("the wrong order through the upsert loses the rerun silently", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			done := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: owner})

			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			flagged, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "incremental")
			require.True(t, wasExisting, "the upsert flags the job it should have waited for")
			require.Equal(t, done, flagged.String())

			tag, err := tx.Exec(ctx, completeSQL, done, owner)
			require.NoError(t, err, "no error is raised — that is the whole problem")
			require.EqualValues(t, 1, tag.RowsAffected())
			require.NoError(t, tx.Commit(ctx))

			require.Empty(t, liveJobIDs(t, pool, orgA.RepoID),
				"the repository is left with NO live job: the rerun is silently lost")
			require.Equal(t, "completed", jobState(t, pool, done))
			require.True(t, needsRerun(t, pool, done),
				"the flag survives on a terminal row, where nothing will ever act on it")
		})
	})

	t.Run("the wrong order through a plain INSERT raises 23505", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			done := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: owner})

			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = tx.Exec(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'incremental', 'queued')`,
				orgA.ID, orgA.RepoID,
			)
			pgErr := requirePgError(t, err)
			require.Equal(t, uniqueSQLState, pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, liveJobIndex, pgErr.ConstraintName)
			require.NoError(t, tx.Rollback(ctx))

			require.Equal(t, []string{done}, liveJobIDs(t, pool, orgA.RepoID))
		})
	})
}

// =====================================================================
// W6 — a retry reuses its run
// =====================================================================

func TestIngestionJobs_RetryReusesItsIngestionRun(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		sha := strings.Repeat("a", 40)
		other := strings.Repeat("b", 40)

		resolve := func(commit string) string {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			var id string
			require.NoError(t, tx.QueryRow(ctx, resolveRunSQL, orgA.RepoID, commit, "main").Scan(&id))
			require.NoError(t, tx.Commit(ctx))
			return id
		}

		first := resolve(sha)
		second := resolve(sha)
		require.Equal(t, first, second,
			"attempt 2 for the same commit must reuse the run, not raise 23505 on ingestion_runs' unique key")

		require.NotEqual(t, first, resolve(other),
			"a job for a different commit gets its own run, which is the normal case")
	})
}

// =====================================================================
// L4 — supersede before enqueue (the ISS-016 fix)
// =====================================================================

func TestIngestionJobs_SupersedeBeforeEnqueue(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	t.Run("the right order supersedes the old job and queues a new one", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			old := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-1"})

			// Both statements in ONE transaction, so a crash between them
			// cannot leave a repository with its old job superseded and no
			// new one to replace it.
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			tag, err := tx.Exec(ctx, supersedeLiveSQL, orgA.RepoID)
			require.NoError(t, err)
			require.EqualValues(t, 1, tag.RowsAffected())

			replacement, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "full_ingest")
			require.False(t, wasExisting)
			require.NoError(t, tx.Commit(ctx))

			require.Equal(t, "superseded", jobState(t, pool, old))
			require.Equal(t, []string{replacement.String()}, liveJobIDs(t, pool, orgA.RepoID))
			require.Equal(t, "queued", jobState(t, pool, replacement.String()))
		})
	})

	// The same corrected failure mode as W5, on the relink path this
	// decision was written for.
	t.Run("the wrong order through the upsert leaves no live job and no error", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			old := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-1"})

			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			flagged, wasExisting := enqueue(t, ctx, tx, orgA.ID, orgA.RepoID, "full_ingest")
			require.True(t, wasExisting)
			require.Equal(t, old, flagged.String())

			tag, err := tx.Exec(ctx, supersedeLiveSQL, orgA.RepoID)
			require.NoError(t, err, "no error is raised — that is the whole problem")
			require.EqualValues(t, 1, tag.RowsAffected())
			require.NoError(t, tx.Commit(ctx))

			require.Empty(t, liveJobIDs(t, pool, orgA.RepoID),
				"the relink is left with NO job to run: silent loss on exactly the ISS-016 path")
			require.Equal(t, "superseded", jobState(t, pool, old))
		})
	})

	t.Run("the wrong order through a plain INSERT raises 23505", func(t *testing.T) {
		isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
			old := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-1"})

			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = tx.Exec(ctx,
				`INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
				 VALUES ($1, $2, 'full_ingest', 'queued')`,
				orgA.ID, orgA.RepoID,
			)
			pgErr := requirePgError(t, err)
			require.Equal(t, uniqueSQLState, pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, liveJobIndex, pgErr.ConstraintName)
			require.NoError(t, tx.Rollback(ctx))

			require.Equal(t, []string{old}, liveJobIDs(t, pool, orgA.RepoID))
		})
	})
}

// =====================================================================
// Enqueueing needs tenant scope — both ISS-013 shapes
// =====================================================================
//
// trg_ingestion_jobs_tenant reads `repositories`, which has FORCE ROW LEVEL
// SECURITY, so an unscoped enqueue cannot see the repository. WHICH error
// it gets depends on the connection's history, and both are pinned here so
// that nobody later "fixes" the trigger instead of scoping the enqueue.

func TestIngestionJobs_EnqueueingNeedsTenantScope(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		t.Run("a fresh connection gets the trigger's does-not-exist", func(t *testing.T) {
			// A connection that has never set app.current_tenant: the GUC
			// is unset, `current_setting(..., true)` yields NULL, the
			// policy filters every row, and the trigger's NOT FOUND branch
			// speaks.
			conn, err := pgx.Connect(ctx, pool.Config().ConnString())
			require.NoError(t, err)
			defer func() { _ = conn.Close(ctx) }()
			_, err = conn.Exec(ctx, "SET ROLE rag_doc_app")
			require.NoError(t, err)

			var unset bool
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT current_setting('app.current_tenant', true) IS NULL`).Scan(&unset))
			require.True(t, unset, "this case is only meaningful on a connection with the GUC unset")

			_, err = conn.Exec(ctx, enqueueUpsertSQL, orgA.ID, orgA.RepoID, "full_ingest")
			pgErr := requirePgError(t, err)
			require.Equal(t, tenantSQLState, pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, fmt.Sprintf("repository %s does not exist", orgA.RepoID), pgErr.Message)
		})

		t.Run("a connection that committed a SET LOCAL gets 22P02", func(t *testing.T) {
			// A committed SET LOCAL leaves the GUC as '' on that backend
			// permanently — RESET, SET TO DEFAULT, RESET ALL and DISCARD
			// ALL all leave it — and `''::uuid` raises inside the policy.
			pooled, err := pool.Acquire(ctx)
			require.NoError(t, err)
			// HIJACKED: this connection carries '' for the rest of its
			// life, so it must never go back to the pool.
			conn := pooled.Hijack()
			defer func() { _ = conn.Close(ctx) }()

			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
			require.NoError(t, err)
			require.NoError(t, tx.Commit(ctx))

			var empty bool
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT current_setting('app.current_tenant', true) = ''`).Scan(&empty))
			require.True(t, empty, "a committed SET LOCAL must leave the GUC as an empty string")

			_, err = conn.Exec(ctx, enqueueUpsertSQL, orgA.ID, orgA.RepoID, "full_ingest")
			pgErr := requirePgError(t, err)
			require.Equal(t, badUUIDSQLState, pgErr.Code, "message: %s", pgErr.Message)
		})
	})
}

// =====================================================================
// The claim query
// =====================================================================

func TestIngestionJobs_Claim(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	past := func(d time.Duration) *time.Time { t := time.Now().Add(-d); return &t }
	future := func(d time.Duration) *time.Time { t := time.Now().Add(d); return &t }

	cases := []struct {
		name    string
		state   string
		opts    jobOpts
		claimed bool
		why     string
	}{
		{
			name:    "a queued job whose run_after has passed",
			state:   "queued",
			opts:    jobOpts{RunAfter: past(time.Hour)},
			claimed: true,
		},
		{
			name:    "a running job whose lease has expired is reclaimed",
			state:   "running",
			opts:    jobOpts{Attempts: 1, LeaseOwner: "dead-worker", LeaseExpires: past(time.Hour)},
			claimed: true,
		},
		{
			// `NULL < NOW()` is NULL, not true. Without the explicit IS
			// NULL branch this row matches nothing, is invisible to every
			// claim, and blocks the repository forever while still holding
			// the partial unique index.
			name:    "a running job with a null lease is reclaimed",
			state:   "running",
			opts:    jobOpts{Attempts: 1, LeaseOwner: "dead-worker"},
			claimed: true,
		},
		{
			name:    "a job at max_attempts is not claimed",
			state:   "queued",
			opts:    jobOpts{Attempts: 5, MaxAttempts: 5, RunAfter: past(time.Hour)},
			claimed: false,
			why:     "without `attempts < max_attempts` a poison job loops forever and never reaches dead",
		},
		{
			name:    "a queued job whose backoff has not elapsed is not claimed",
			state:   "queued",
			opts:    jobOpts{Attempts: 1, RunAfter: future(time.Hour)},
			claimed: false,
		},
		{
			name:    "a running job with a live lease is not claimed",
			state:   "running",
			opts:    jobOpts{Attempts: 1, LeaseOwner: "worker-1", LeaseExpires: future(time.Hour)},
			claimed: false,
			why:     "its owner is alive and heartbeating",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
				before := tc.opts
				id := seedJob(t, pool, orgA, orgA.RepoID, tc.state, before)

				// The claim runs with NO tenant set, as a worker's does:
				// it touches neither organization_id nor repository_id, so
				// the tenant trigger never fires. That is what having no
				// row-level security on this table buys.
				withQueue(t, pool, []string{orgA.RepoID}, func(tx pgx.Tx) {
					rows, err := tx.Query(ctx, claimSQL, "claimer", leaseInterval)
					require.NoError(t, err)
					claimed, err := pgx.CollectRows(rows, pgx.RowToMap)
					require.NoError(t, err)

					if !tc.claimed {
						require.Empty(t, claimed, "must not be claimed: %s", tc.why)
						return
					}
					require.Len(t, claimed, 1, "must be claimed")
					require.Equal(t, id, uuidString(t, claimed[0]["id"]))
					require.Equal(t, "running", claimed[0]["state"])
					require.Equal(t, "claimer", claimed[0]["lease_owner"])
					require.EqualValues(t, before.Attempts+1, claimed[0]["attempts"],
						"reclaim is a retry: attempts must be incremented on both branches")
					require.NotNil(t, claimed[0]["lease_expires_at"], "the claim must set a lease")
				})
			})
		})
	}
}

// =====================================================================
// The sweeper
// =====================================================================

func TestIngestionJobs_Sweeper(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	past := func(d time.Duration) *time.Time { t := time.Now().Add(-d); return &t }
	future := func(d time.Duration) *time.Time { t := time.Now().Add(d); return &t }

	cases := []struct {
		name  string
		state string
		opts  jobOpts
		want  string
		why   string
	}{
		{
			// The CLEAN failure path: a worker that fails cleanly on its
			// last attempt writes state='queued' (decision O2). Without
			// this branch the row is invisible to the claim
			// (`attempts < max_attempts`) and sits in the live set forever.
			name:  "a queued job at max attempts is dead-lettered",
			state: "queued",
			opts:  jobOpts{Attempts: 5, MaxAttempts: 5},
			want:  "dead",
		},
		{
			name:  "a running, lease-expired job at max attempts is dead-lettered",
			state: "running",
			opts:  jobOpts{Attempts: 5, MaxAttempts: 5, LeaseOwner: "dead-worker", LeaseExpires: past(time.Hour)},
			want:  "dead",
		},
		{
			name:  "a running job with a live lease is left alone",
			state: "running",
			opts:  jobOpts{Attempts: 5, MaxAttempts: 5, LeaseOwner: "worker-1", LeaseExpires: future(time.Hour)},
			want:  "running",
			why:   "its worker is still heartbeating; the sweeper must not steal a live job's terminal write",
		},
		{
			name:  "a job below max attempts is left alone",
			state: "queued",
			opts:  jobOpts{Attempts: 1, MaxAttempts: 5},
			want:  "queued",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
				id := seedJob(t, pool, orgA, orgA.RepoID, tc.state, tc.opts)

				withQueue(t, pool, []string{orgA.RepoID}, func(tx pgx.Tx) {
					_, err := tx.Exec(ctx, sweepSQL)
					require.NoError(t, err)

					var got string
					require.NoError(t, tx.QueryRow(ctx,
						`SELECT state FROM ingestion_jobs WHERE id = $1`, id).Scan(&got))
					require.Equal(t, tc.want, got, tc.why)
				})
			})
		})
	}
}

// failSQL writes `dead` directly on the final attempt rather than writing
// `queued` and waiting for a sweep, and it is fenced on the lease. The
// sweeper above is the backstop for a worker that dies before running it.
func TestIngestionJobs_FailIsFencedAndDeadLettersOnTheFinalAttempt(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		const owner = "worker-1"

		retryable := seedJob(t, pool, orgA, orgA.RepoID, "running",
			jobOpts{Attempts: 1, MaxAttempts: 5, LeaseOwner: owner})
		tag, err := pool.Exec(ctx, failSQL, retryable, owner, "30 seconds", "boom")
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())
		require.Equal(t, "queued", jobState(t, pool, retryable))

		// A worker whose lease was reclaimed elsewhere writes nothing.
		tag, err = pool.Exec(ctx, failSQL, retryable, "some-other-worker", "30 seconds", "boom")
		require.NoError(t, err)
		require.EqualValues(t, 0, tag.RowsAffected(),
			"the lease fence must match zero rows for a worker that no longer owns the job")

		exhausted := seedJob(t, pool, orgA, insertRepository(t, pool, orgA, "exhausted"), "running",
			jobOpts{Attempts: 5, MaxAttempts: 5, LeaseOwner: owner})
		tag, err = pool.Exec(ctx, failSQL, exhausted, owner, "30 seconds", "boom")
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())
		require.Equal(t, "dead", jobState(t, pool, exhausted))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// The OTHER half of the lease fence: `AND state = 'running'`
// =====================================================================
//
// `lease_owner = $2` is exercised by the test above and by W5. The `state`
// half was not, and PR #38's review measured that deleting it from BOTH
// completeSQL and failSQL left the whole suite green.
//
// It is the half 21-CONTEXT L3 spends a paragraph on, because it is what
// stops the two bugs review found: a worker whose lease expired and was
// reclaimed elsewhere writing `completed` over the new attempt's result,
// and a SUPERSEDED worker writing `queued` back into the live set, where it
// collides with the replacement job — "on exactly the ISS-016 path".
//
// Every terminal state is tested WITH THE LEASE STILL ATTACHED, because
// that is the reachable shape: supersedeLiveSQL leaves it, and a job whose
// lease the claim query reassigned carries the new owner, not none.
func TestIngestionJobs_TerminalWritesAreFencedOnTheRunningState(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const owner = "worker-1"

	for _, state := range []string{"completed", "dead", "superseded"} {
		t.Run("a "+state+" job still holding its lease", func(t *testing.T) {
			isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
				id := seedJob(t, pool, orgA, orgA.RepoID, state,
					jobOpts{Attempts: 1, MaxAttempts: 5, LeaseOwner: owner})

				// The CORRECT owner, so only the state predicate can refuse
				// these.
				tag, err := pool.Exec(ctx, completeSQL, id, owner)
				require.NoError(t, err)
				require.EqualValues(t, 0, tag.RowsAffected(),
					"completeSQL must not overwrite a %s job", state)

				tag, err = pool.Exec(ctx, failSQL, id, owner, "30 seconds", "boom")
				require.NoError(t, err)
				require.EqualValues(t, 0, tag.RowsAffected(),
					"failSQL must not resurrect a %s job into the live set", state)

				require.Equal(t, state, jobState(t, pool, id), "the row must be untouched")
			})
		})
	}
}

// Clearing a rerun is fenced the same way, and was the one lease-fenced
// statement that was not.
//
// FOUND IN PR #38's REVIEW, and a real defect rather than a coverage gap:
// 21-CONTEXT L7's transcription fences the clear on id and lease_owner only.
// Because supersedeLiveSQL leaves the lease attached, a superseded worker's
// clear matched (`UPDATE 1`) while its completion did not (`UPDATE 0`) — so
// it consumed the rerun flag and then could not act on it. That loses the
// rerun WITHOUT leaving the `needs_rerun = true` breadcrumb W5's case does,
// which makes it worse than the failure mode this PR was written to pin.
func TestIngestionJobs_ClearingARerunIsFencedOnTheRunningState(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		const owner = "worker-1"
		id := seedJob(t, pool, orgA, orgA.RepoID, "running",
			jobOpts{LeaseOwner: owner, NeedsRerun: true})

		// The relink supersedes the running job. L4 step 3 is cooperative,
		// so the old worker is still going until its next heartbeat.
		tag, err := pool.Exec(ctx, supersedeLiveSQL, orgA.RepoID)
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())

		// The lease really is still attached — that is what makes the
		// missing predicate reachable, so assert it rather than assume it.
		var leaseOwner *string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT lease_owner FROM ingestion_jobs WHERE id = $1`, id).Scan(&leaseOwner))
		require.NotNil(t, leaseOwner, "supersedeLiveSQL leaves the lease attached, by design")
		require.Equal(t, owner, *leaseOwner)

		// The old worker finishes its stage and runs its clear.
		var clearedID string
		err = pool.QueryRow(ctx, clearRerunSQL, id, owner).Scan(&clearedID)
		require.ErrorIs(t, err, pgx.ErrNoRows,
			"a superseded worker must not consume the rerun flag it cannot act on")

		require.True(t, needsRerun(t, pool, id),
			"the flag must survive for the replacement job's producer to see")

		// And its completion is refused, as it already was.
		tag, err = pool.Exec(ctx, completeSQL, id, owner)
		require.NoError(t, err)
		require.EqualValues(t, 0, tag.RowsAffected())
	})
}

// =====================================================================
// FOR UPDATE SKIP LOCKED
// =====================================================================
//
// Deleting the clause left the whole suite green (PR #38's review). Without
// it, `ORDER BY run_after ... FOR UPDATE LIMIT 1` makes every concurrent
// claim wait on the single oldest row, so a worker pool serialises on one
// job instead of spreading across the queue — a performance collapse with no
// error anywhere, and 21-05 ports this statement into Python.
//
// No barrier and no goroutine: one transaction claims and stays open, and a
// second connection must claim the OTHER job rather than block on the first.
// A short statement_timeout turns "blocks" into a failure instead of a hang.
func TestIngestionJobs_ClaimSkipsRowsLockedByAnotherWorker(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		// Two claimable jobs, backdated far enough that they sort ahead of
		// anything any other test in this package creates (the next oldest
		// is one hour). `claimSQL` has no repository filter, so ordering is
		// how this test names the rows it means.
		oldest := time.Now().Add(-365 * 24 * time.Hour)
		nextOldest := time.Now().Add(-364 * 24 * time.Hour)
		jobA := seedJob(t, pool, orgA, orgA.RepoID, "queued", jobOpts{RunAfter: &oldest})
		jobB := seedJob(t, pool, orgA, insertRepository(t, pool, orgA, "skiplocked"), "queued",
			jobOpts{RunAfter: &nextOldest})

		// Self-diagnosis rather than a confusing failure: if a crashed run
		// ever leaves a job older than these, say so plainly.
		var older int
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT count(*) FROM ingestion_jobs
			 WHERE id <> ALL($1::uuid[]) AND run_after < $2`,
			[]string{jobA, jobB}, nextOldest,
		).Scan(&older))
		require.Zero(t, older, "a leaked job older than this test's fixtures would break its ordering")

		// tx1 claims the oldest and holds the row lock open.
		tx1, err := pool.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = tx1.Rollback(ctx) }()

		_, err = tx1.Exec(ctx,
			`DELETE FROM ingestion_jobs WHERE id <> ALL($1::uuid[])`, []string{jobA, jobB})
		require.NoError(t, err)

		rows, err := tx1.Query(ctx, claimSQL, "worker-1", leaseInterval)
		require.NoError(t, err)
		claimed, err := pgx.CollectRows(rows, pgx.RowToMap)
		require.NoError(t, err)
		require.Len(t, claimed, 1)
		require.Equal(t, jobA, uuidString(t, claimed[0]["id"]), "tx1 must take the oldest job")

		// tx2, on its own connection, must skip jobA's lock and take jobB.
		conn2, err := pool.Acquire(ctx)
		require.NoError(t, err)
		defer conn2.Release()

		tx2, err := conn2.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = tx2.Rollback(ctx) }()

		// Without SKIP LOCKED this waits on tx1 forever; with it, this
		// timeout is never reached. 3s is far above the statement's real
		// cost and far below the package timeout.
		_, err = tx2.Exec(ctx, "SET LOCAL statement_timeout = '3s'")
		require.NoError(t, err)

		rows, err = tx2.Query(ctx, claimSQL, "worker-2", leaseInterval)
		require.NoError(t, err)
		claimed2, err := pgx.CollectRows(rows, pgx.RowToMap)
		require.NoError(t, err, "a blocked claim fails here with 57014 query_canceled")
		require.Len(t, claimed2, 1, "tx2 must claim a job rather than find none")
		require.Equal(t, jobB, uuidString(t, claimed2[0]["id"]),
			"tx2 must SKIP the row tx1 locked and take the next one")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// `updated_at = NOW()`
// =====================================================================
//
// Deleting it from claimSQL, sweepSQL and completeSQL left the whole suite
// green (PR #38's review). The table comment advertises Phase 24's pruning
// as `... AND updated_at < NOW() - INTERVAL '30 days'`, so a statement that
// stops touching the column leaves the row on its INSERT-time default and a
// job that ran for a week is pruned on its creation date. Silent, and only
// visible a month later.
//
// Every statement that claims to write it is covered, not only the three the
// review deleted it from. `NOW()` is transaction start time and each of
// these runs in a later transaction than the seed, so the comparison is
// strict.
func TestIngestionJobs_EveryStatementAdvancesUpdatedAt(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const owner = "worker-1"
	past := func(d time.Duration) *time.Time { at := time.Now().Add(-d); return &at }

	cases := []struct {
		name  string
		state string
		opts  jobOpts
		run   func(t *testing.T, org *isolation.TestOrg, id string)
	}{
		{
			name: "claimSQL", state: "queued", opts: jobOpts{RunAfter: past(time.Hour)},
			run: func(t *testing.T, org *isolation.TestOrg, id string) {
				withQueue(t, pool, []string{org.RepoID}, func(tx pgx.Tx) {
					rows, err := tx.Query(ctx, claimSQL, owner, leaseInterval)
					require.NoError(t, err)
					claimed, err := pgx.CollectRows(rows, pgx.RowToMap)
					require.NoError(t, err)
					require.Len(t, claimed, 1)
					requireAdvanced(t, tx, id)
				})
			},
		},
		{
			name: "sweepSQL", state: "queued", opts: jobOpts{Attempts: 5, MaxAttempts: 5},
			run: func(t *testing.T, org *isolation.TestOrg, id string) {
				withQueue(t, pool, []string{org.RepoID}, func(tx pgx.Tx) {
					_, err := tx.Exec(ctx, sweepSQL)
					require.NoError(t, err)
					requireAdvanced(t, tx, id)
				})
			},
		},
		{
			name: "completeSQL", state: "running", opts: jobOpts{LeaseOwner: owner},
			run: func(t *testing.T, _ *isolation.TestOrg, id string) {
				tag, err := pool.Exec(ctx, completeSQL, id, owner)
				require.NoError(t, err)
				require.EqualValues(t, 1, tag.RowsAffected())
				requireAdvanced(t, pool, id)
			},
		},
		{
			name: "failSQL", state: "running", opts: jobOpts{Attempts: 1, LeaseOwner: owner},
			run: func(t *testing.T, _ *isolation.TestOrg, id string) {
				tag, err := pool.Exec(ctx, failSQL, id, owner, "30 seconds", "boom")
				require.NoError(t, err)
				require.EqualValues(t, 1, tag.RowsAffected())
				requireAdvanced(t, pool, id)
			},
		},
		{
			name: "supersedeLiveSQL", state: "running", opts: jobOpts{LeaseOwner: owner},
			run: func(t *testing.T, org *isolation.TestOrg, id string) {
				tag, err := pool.Exec(ctx, supersedeLiveSQL, org.RepoID)
				require.NoError(t, err)
				require.EqualValues(t, 1, tag.RowsAffected())
				requireAdvanced(t, pool, id)
			},
		},
		{
			name: "clearRerunSQL", state: "running",
			opts: jobOpts{LeaseOwner: owner, NeedsRerun: true},
			run: func(t *testing.T, _ *isolation.TestOrg, id string) {
				var cleared string
				require.NoError(t, pool.QueryRow(ctx, clearRerunSQL, id, owner).Scan(&cleared))
				requireAdvanced(t, pool, id)
			},
		},
		{
			// The upsert's DO UPDATE branch, which is what a push against a
			// live job runs. Its row is the one most likely to sit in the
			// table longest, so its timestamp matters most to pruning.
			name: "the enqueue upsert's DO UPDATE branch", state: "running",
			opts: jobOpts{LeaseOwner: owner},
			run: func(t *testing.T, org *isolation.TestOrg, id string) {
				tx, err := isolation.TenantScope(ctx, pool, org.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()

				flagged, wasExisting := enqueue(t, ctx, tx, org.ID, org.RepoID, "incremental")
				require.True(t, wasExisting)
				require.Equal(t, id, flagged.String())
				requireAdvanced(t, tx, id)
				require.NoError(t, tx.Rollback(ctx))
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
				id := seedJob(t, pool, orgA, orgA.RepoID, tc.state, tc.opts)
				tc.run(t, orgA, id)
			})
		})
	}
}

// requireAdvanced asserts the job's updated_at is strictly later than its
// created_at, which seedJob left equal.
func requireAdvanced(t *testing.T, q querier, id string) {
	t.Helper()
	var createdAt, updatedAt time.Time
	require.NoError(t, q.QueryRow(context.Background(),
		`SELECT created_at, updated_at FROM ingestion_jobs WHERE id = $1`, id,
	).Scan(&createdAt, &updatedAt))
	require.True(t, updatedAt.After(createdAt),
		"updated_at (%s) must have advanced past created_at (%s); Phase 24's pruning reads it",
		updatedAt, createdAt)
}

// =====================================================================
// The schema's shape
// =====================================================================

func TestIngestionJobs_SchemaShape(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	t.Run("no row-level security, by decision", func(t *testing.T) {
		// 21-CONTEXT L5: a worker claims a job BEFORE it knows the tenant,
		// so scoping the claim by the answer is circular. This is asserted
		// rather than left implicit because "RLS is missing" and "RLS was
		// forgotten" look identical in a catalog.
		var rowSecurity, forced bool
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT relrowsecurity, relforcerowsecurity FROM pg_class
			 WHERE oid = 'ingestion_jobs'::regclass`).Scan(&rowSecurity, &forced))
		require.False(t, rowSecurity, "ingestion_jobs must have no row-level security (L5)")
		require.False(t, forced)

		var comment string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT obj_description('ingestion_jobs'::regclass, 'pg_class')`).Scan(&comment))
		require.Contains(t, comment, "NO ROW-LEVEL SECURITY, DELIBERATELY",
			"the table comment must say RLS was a decision, not an oversight")
		require.Contains(t, comment, "AUTHORIZATION INPUT",
			"and that organization_id here is one, which 21-07 depends on")
		require.Contains(t, comment, "DELETE FROM ingestion_jobs WHERE state IN",
			"and carry the pruning statement Phase 24 will schedule")
	})

	t.Run("the indexes", func(t *testing.T) {
		// Pinned verbatim. `pg_get_indexdef` normalises `IN (...)` to
		// `= ANY (ARRAY[...])`, and PostgreSQL may reword that across major
		// versions — these strings are PostgreSQL 16's, the version we
		// deploy and the version CI runs.
		indexes := map[string]string{
			"idx_ingestion_jobs_claimable": "CREATE INDEX idx_ingestion_jobs_claimable ON public.ingestion_jobs " +
				"USING btree (run_after) WHERE (state = ANY (ARRAY['queued'::text, 'running'::text]))",
			liveJobIndex: "CREATE UNIQUE INDEX " + liveJobIndex + " ON public.ingestion_jobs " +
				"USING btree (repository_id) WHERE (state = ANY (ARRAY['queued'::text, 'running'::text]))",
		}
		for name, want := range indexes {
			var def string
			require.NoError(t, pool.QueryRow(ctx,
				`SELECT indexdef FROM pg_indexes WHERE tablename = 'ingestion_jobs' AND indexname = $1`,
				name).Scan(&def), "index %s must exist", name)
			require.Equal(t, want, def, "index %s", name)
		}
	})

	t.Run("the composite foreign key", func(t *testing.T) {
		var def string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_constraintdef(oid) FROM pg_constraint
			 WHERE conrelid = 'ingestion_jobs'::regclass AND conname = $1`,
			repoTenantFK).Scan(&def))
		require.Equal(t,
			"FOREIGN KEY (repository_id, organization_id) REFERENCES repositories(id, organization_id) ON DELETE CASCADE",
			def)
	})

	t.Run("the tenant trigger", func(t *testing.T) {
		var def, enabled string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_triggerdef(oid), tgenabled::text FROM pg_trigger
			 WHERE tgrelid = 'ingestion_jobs'::regclass AND tgname = $1`,
			tenantTrigger).Scan(&def, &enabled))
		// The attachment is load-bearing in BOTH directions. It has to
		// cover the columns the value arrives on, and it must NOT cover the
		// ones the claim, heartbeat, completion, failure and sweeper write —
		// those run pre-tenant and the trigger would refuse them.
		require.Contains(t, def, "BEFORE INSERT OR UPDATE OF organization_id, repository_id ON")
		require.Contains(t, def, "EXECUTE FUNCTION ingestion_jobs_fix_tenant()")
		// W1 disables this trigger inside a transaction it rolls back. "O"
		// is enabled; anything else means one of those leaked into the
		// reused container.
		require.Equal(t, "O", enabled, "the tenant trigger must be enabled")
	})

	t.Run("the five states and two job types", func(t *testing.T) {
		// `failed` was removed after review (decision O2): it had no edge
		// back to the claimable set and no place in the partial unique
		// index, so a failed job could neither be retried nor prevent a
		// second live job.
		for _, bad := range []string{"failed", "pending", "cancelled"} {
			var ok bool
			require.NoError(t, pool.QueryRow(ctx,
				`SELECT $1 = ANY (ARRAY['queued','running','completed','dead','superseded'])`,
				bad).Scan(&ok))
			require.False(t, ok)
		}
		var def string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_constraintdef(oid) FROM pg_constraint
			 WHERE conrelid = 'ingestion_jobs'::regclass AND conname = 'ingestion_jobs_state_check'`).Scan(&def))
		require.Equal(t,
			"CHECK ((state = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'dead'::text, 'superseded'::text])))",
			def)
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_constraintdef(oid) FROM pg_constraint
			 WHERE conrelid = 'ingestion_jobs'::regclass AND conname = 'ingestion_jobs_job_type_check'`).Scan(&def))
		require.Equal(t,
			"CHECK ((job_type = ANY (ARRAY['full_ingest'::text, 'incremental'::text])))",
			def)
	})

	t.Run("no updated_at trigger", func(t *testing.T) {
		// Every statement above writes it explicitly. A trigger here would
		// be a second writer of a column those statements already set.
		var count int
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT count(*) FROM pg_trigger
			 WHERE tgrelid = 'ingestion_jobs'::regclass AND NOT tgisinternal`).Scan(&count))
		require.Equal(t, 1, count, "trg_ingestion_jobs_tenant must be the only user trigger")
	})
}

// =====================================================================
// D5's drift check
// =====================================================================
//
// ⚠ THIS TEST DOES NOT DEPEND ON RUNNING LAST, and an earlier revision's
// `ZZ_` name said it did (PR #38's review). `go test` runs a package's tests
// in source order, but `-shuffle=on` and `-run` do not, and a check that is
// only meaningful in one ordering is a check whose green result nobody can
// read.
//
// What makes it order-independent: the tests that create repositories call
// AssertNoRepositoryTenantDrift themselves, while their rows still exist.
// This one is the whole-table sweep — it runs with row-level security
// bypassed and reads EVERY repository in the database, including rows left
// behind by a crashed earlier run that no fixture is still tracking. That is
// worth having whenever it runs, and worth nothing more for running last.
func TestIngestionJobs_NoRepositoryTenantDrift(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	isolation.AssertNoRepositoryTenantDrift(t, pool)
}

// =====================================================================
// Helpers
// =====================================================================

// enqueueResult is what enqueueUpsertSQL returns per row.
type enqueueResult struct {
	ID          uuid.UUID
	WasExisting bool
}

// querier is satisfied by *pgxpool.Pool, pgx.Tx and *pgx.Conn alike.
type querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// enqueue runs the one enqueue statement and returns what it reports.
func enqueue(t *testing.T, ctx context.Context, q querier, orgID, repoID, jobType string) (uuid.UUID, bool) {
	t.Helper()
	var out enqueueResult
	require.NoError(t, q.QueryRow(ctx, enqueueUpsertSQL, orgID, repoID, jobType).
		Scan(&out.ID, &out.WasExisting))
	return out.ID, out.WasExisting
}

// bulkEnqueueSQL is the same statement with n rows in its VALUES list —
// what an `installation_repositories.added` event for n repositories sends.
// The ON CONFLICT clause is the shared constant, not a copy.
func bulkEnqueueSQL(n int) string {
	values := make([]string, n)
	for i := range values {
		values[i] = fmt.Sprintf("($%d, $%d, 'full_ingest', 'queued')", i*2+1, i*2+2)
	}
	return `INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
VALUES ` + strings.Join(values, ", ") + enqueueConflictClause
}

// jobOpts arranges the state a statement under test acts on. Production
// reaches these states only through the statements above; this is for
// setting up the row they are pointed at.
type jobOpts struct {
	JobType      string // "" -> full_ingest
	Attempts     int
	MaxAttempts  int // 0 -> the column default, 5
	LeaseOwner   string
	LeaseExpires *time.Time
	RunAfter     *time.Time // nil -> NOW()
	NeedsRerun   bool
}

// seedJob inserts and COMMITS a job under org's tenant scope. Committing
// matters: the claim and sweeper tests then run in their own unscoped
// transaction, the way a worker does.
func seedJob(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, repoID, state string, opts jobOpts) string {
	t.Helper()
	ctx := context.Background()

	jobType := opts.JobType
	if jobType == "" {
		jobType = "full_ingest"
	}
	maxAttempts := opts.MaxAttempts
	if maxAttempts == 0 {
		maxAttempts = 5
	}
	var leaseOwner *string
	if opts.LeaseOwner != "" {
		leaseOwner = &opts.LeaseOwner
	}

	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var id string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_jobs
		   (organization_id, repository_id, job_type, state, attempts, max_attempts,
		    lease_owner, lease_expires_at, run_after, needs_rerun)
		 VALUES ($1, $2, $3, $4, $5, $6, $7::text, $8::timestamptz,
		         COALESCE($9::timestamptz, NOW()), $10)
		 RETURNING id::text`,
		org.ID, repoID, jobType, state, opts.Attempts, maxAttempts,
		leaseOwner, opts.LeaseExpires, opts.RunAfter, opts.NeedsRerun,
	).Scan(&id))
	require.NoError(t, tx.Commit(ctx))
	return id
}

// withQueue runs fn in an UNSCOPED transaction — no app.current_tenant, as
// a worker has none when it claims — over a queue holding only the given
// repositories' jobs, and rolls back.
//
// The delete is what makes the verbatim statements testable. `claimSQL` and
// `sweepSQL` deliberately have no repository filter: production has one
// queue. In a container shared across packages and reused across runs, a
// stray row from anywhere would make "nothing was claimed" untestable. The
// transaction is never committed, so nothing is actually removed.
//
// ⚠ THIS IS ONLY SAFE WHILE `pkg/jobs` IS THE SOLE WRITER of the table, and
// today it is — nothing else in `services/backend` mentions
// `ingestion_jobs`, and CI's default-parallelism step ("Harness under
// package parallelism" in backend-ci.yml) does not include `./pkg/jobs/...`.
// 21-03 and 21-04 put producers in `pkg/api/handlers`, which IS in that
// step. From then on this blanket delete holds row locks on every other
// package's `ingestion_jobs` rows for the length of the test. Not a deadlock
// — it takes no `repositories` locks, so there is no lock-order cycle — but
// a source of confusing waits under `go test ./...`. Narrow it to this
// test's rows, or serialise the package, before that lands.
func withQueue(t *testing.T, pool *pgxpool.Pool, repoIDs []string, fn func(tx pgx.Tx)) {
	t.Helper()
	ctx := context.Background()

	tx, err := pool.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	_, err = tx.Exec(ctx,
		`DELETE FROM ingestion_jobs WHERE repository_id <> ALL($1::uuid[])`, repoIDs)
	require.NoError(t, err)

	fn(tx)
}

// liveJobIDs returns the repository's jobs in the live set — the set the
// partial unique index allows at most one of.
func liveJobIDs(t *testing.T, q querier, repoID string) []string {
	t.Helper()
	rows, err := q.Query(context.Background(),
		`SELECT id::text FROM ingestion_jobs
		 WHERE repository_id = $1 AND state IN ('queued','running') ORDER BY id`, repoID)
	require.NoError(t, err)
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	require.NoError(t, err)
	return ids
}

// uuidString renders whatever pgx.RowToMap produced for a uuid column.
// `claimSQL` ends in `RETURNING *` — kept verbatim, because 21-05's Python
// consumer wants the whole row — so its columns come back untyped, and pgx
// decodes a uuid into a [16]byte rather than a string.
func uuidString(t *testing.T, v any) string {
	t.Helper()
	switch typed := v.(type) {
	case [16]byte:
		return uuid.UUID(typed).String()
	case string:
		return typed
	default:
		t.Fatalf("unexpected uuid representation %T: %v", v, v)
		return ""
	}
}

func jobState(t *testing.T, q querier, id string) string {
	t.Helper()
	var state string
	require.NoError(t, q.QueryRow(context.Background(),
		`SELECT state FROM ingestion_jobs WHERE id = $1`, id).Scan(&state))
	return state
}

func needsRerun(t *testing.T, q querier, id string) bool {
	t.Helper()
	var flag bool
	require.NoError(t, q.QueryRow(context.Background(),
		`SELECT needs_rerun FROM ingestion_jobs WHERE id = $1`, id).Scan(&flag))
	return flag
}

// insertRepository adds a repository to org's fixture project and commits.
// WithTwoOrgs' cleanup deletes every repository under that project, and the
// delete cascades to this table through ingestion_jobs_repository_id_fkey.
func insertRepository(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, name string) string {
	t.Helper()
	ctx := context.Background()

	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	suffix := strings.ReplaceAll(uuid.NewString(), "-", "")[:8]
	var id string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url)
		 VALUES ($1, $2, $3) RETURNING id::text`,
		org.ProjectID, name+"-"+suffix, "https://example.test/"+name+"-"+suffix+".git",
	).Scan(&id))
	require.NoError(t, tx.Commit(ctx))
	return id
}

// requirePgError asserts err is a *pgconn.PgError and returns it. It logs
// the SQLSTATE and message unconditionally: under `-v` that is what tells
// you WHICH error a test saw, which is how a mutation that a test appears
// to survive gets diagnosed rather than guessed at.
func requirePgError(t *testing.T, err error) *pgconn.PgError {
	t.Helper()
	require.Error(t, err)
	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "expected *pgconn.PgError, got %T: %v", err, err)
	t.Logf("SQLSTATE %s: %s (constraint=%q)", pgErr.Code, pgErr.Message, pgErr.ConstraintName)
	return pgErr
}
