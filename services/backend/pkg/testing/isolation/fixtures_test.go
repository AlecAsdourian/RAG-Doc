package isolation

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// TestFixtures_WithTwoOrgs_DistinctIDs is self-test #1 — the two orgs the
// helper creates must have distinct ids, distinct slugs, and distinct user
// ids. It also proves testjwt.Sign works against fixture-produced ids —
// tests that need JWTs are expected to call testjwt.Sign directly rather
// than reading a field on TestOrg.
func TestFixtures_WithTwoOrgs_DistinctIDs(t *testing.T) {
	pool := SetupTestDB(t)

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		require.NotEmpty(t, orgA.ID)
		require.NotEmpty(t, orgB.ID)
		assert.NotEqual(t, orgA.ID, orgB.ID, "org IDs must differ")
		assert.NotEqual(t, orgA.Slug, orgB.Slug, "org slugs must differ")
		assert.NotEqual(t, orgA.OwnerID, orgB.OwnerID, "owner user IDs must differ")
		assert.NotEqual(t, orgA.RepoID, orgB.RepoID, "repo IDs must differ")

		// Fixture users mint valid tokens via the testjwt subpackage.
		token := testjwt.Sign(orgA.OwnerID, orgA.ID, "owner")
		assert.NotEmpty(t, token)
	})
}

// TestFixtures_TenantScope_SetsCurrentTenant is self-test #2 — TenantScope
// must actually set app.current_tenant to the given tenant id.
func TestFixtures_TenantScope_SetsCurrentTenant(t *testing.T) {
	pool := SetupTestDB(t)

	WithTwoOrgs(t, pool, func(orgA, _ *TestOrg) {
		ctx := context.Background()
		tx, err := TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		var current string
		require.NoError(t, tx.QueryRow(ctx,
			"SELECT current_setting('app.current_tenant', true)",
		).Scan(&current))
		assert.Equal(t, orgA.ID, current, "app.current_tenant should equal orgA.ID")
	})

	// Invalid tenant id must be rejected — this is the guard rail against
	// injecting arbitrary SQL through the tenant string.
	_, err := TenantScope(context.Background(), pool, "not-a-uuid")
	require.Error(t, err)
}

// TestFixtures_AssertNoCrossTenantLeak_CatchesLeak is self-test #3 — when a
// real leak exists, the assertion primitive detects it. We synthesize a leak
// by using RESET ROLE inside observe to escalate to the superuser session,
// which bypasses RLS, so tenantB "sees" tenantA's data.
func TestFixtures_AssertNoCrossTenantLeak_CatchesLeak(t *testing.T) {
	pool := SetupTestDB(t)

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		ctx := context.Background()

		err := CheckCrossTenantLeak(ctx, pool, orgA.ID, orgB.ID,
			func(tx pgx.Tx) error {
				return insertOneChunk(ctx, tx, orgA.RepoID, "leak-A")
			},
			func(tx pgx.Tx) (bool, error) {
				// Escalate to the superuser session so RLS no longer applies —
				// this simulates a leak that a broken RLS policy would allow.
				if _, err := tx.Exec(ctx, "RESET ROLE"); err != nil {
					return false, err
				}
				var count int
				if err := tx.QueryRow(ctx,
					`SELECT COUNT(*) FROM chunks WHERE repository_id = $1`,
					orgA.RepoID,
				).Scan(&count); err != nil {
					return false, err
				}
				return count > 0, nil
			},
		)
		require.Error(t, err, "CheckCrossTenantLeak must catch a synthetic leak")
		assert.Contains(t, err.Error(), "cross-tenant leak",
			"error message must identify the failure mode")
	})
}

// TestFixtures_AssertNoCrossTenantLeak_PassesOnIsolation is self-test #4 —
// when isolation is actually enforced by RLS, the assertion primitive
// reports no leak.
func TestFixtures_AssertNoCrossTenantLeak_PassesOnIsolation(t *testing.T) {
	pool := SetupTestDB(t)

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		ctx := context.Background()

		err := CheckCrossTenantLeak(ctx, pool, orgA.ID, orgB.ID,
			func(tx pgx.Tx) error {
				return insertOneChunk(ctx, tx, orgA.RepoID, "no-leak-A")
			},
			func(tx pgx.Tx) (bool, error) {
				var count int
				if err := tx.QueryRow(ctx,
					`SELECT COUNT(*) FROM chunks WHERE repository_id = $1`,
					orgA.RepoID,
				).Scan(&count); err != nil {
					return false, err
				}
				return count > 0, nil
			},
		)
		require.NoError(t, err, "properly isolated action must not report a leak")
	})
}

// TestCleanupOrg_SavepointIsolatesFailure documents the transaction primitive
// cleanupOrg relies on. After a per-statement failure inside a SAVEPOINT,
// ROLLBACK TO SAVEPOINT restores the transaction to a usable state so
// subsequent statements execute normally. Without this the current-transaction
// -is-aborted error propagates and every following statement silently no-ops
// — the class of bug the reviewer flagged in the original cleanupOrg.
func TestCleanupOrg_SavepointIsolatesFailure(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()
	tx, err := pool.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	_, err = tx.Exec(ctx, "SAVEPOINT sp1")
	require.NoError(t, err)

	// A statement that fails inside the savepoint.
	_, err = tx.Exec(ctx, "SELECT * FROM this_table_does_not_exist")
	require.Error(t, err)

	// Baseline: without ROLLBACK TO SAVEPOINT, the next Exec must fail
	// because the transaction is aborted. This is the trap.
	_, abortedErr := tx.Exec(ctx, "SELECT 1")
	require.Error(t, abortedErr, "sanity: aborted tx must refuse further work")

	// Fix: rolling back to the savepoint restores the transaction.
	_, err = tx.Exec(ctx, "ROLLBACK TO SAVEPOINT sp1")
	require.NoError(t, err)

	var one int
	require.NoError(t, tx.QueryRow(ctx, "SELECT 1").Scan(&one))
	assert.Equal(t, 1, one, "tx must be usable after ROLLBACK TO SAVEPOINT")
}

// TestCleanupOrg_RemovesAllRows exercises cleanupOrg on an org with rows in
// every table it targets. Regression guard: if the SAVEPOINT loop ever
// regresses to per-statement transaction-abort, phantom rows would leak
// between test runs against the reused container.
func TestCleanupOrg_RemovesAllRows(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	org := createOrg(t, pool, "iso-cleanup-e2e-"+shortToken())

	// Populate leaf tables so cleanupOrg has real DELETEs to run.
	tx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	var runID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, 'abc', 'main', 'completed') RETURNING id`,
		org.RepoID,
	).Scan(&runID))
	_, err = tx.Exec(ctx,
		`INSERT INTO chunks
		   (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
		 VALUES ($1, $2, 'x.txt', 1, 1, 'x', 'xh')`,
		runID, org.RepoID,
	)
	require.NoError(t, err)
	require.NoError(t, tx.Commit(ctx))

	cleanupOrg(ctx, pool, org)

	// Every row created for this org must be gone.
	var orgCount, projCount, repoCount, userCount int
	require.NoError(t, pool.QueryRow(ctx,
		"SELECT COUNT(*) FROM organizations WHERE id = $1", org.ID).Scan(&orgCount))
	require.NoError(t, pool.QueryRow(ctx,
		"SELECT COUNT(*) FROM projects WHERE id = $1", org.ProjectID).Scan(&projCount))
	require.NoError(t, pool.QueryRow(ctx,
		"SELECT COUNT(*) FROM users WHERE id = ANY($1)",
		[]string{org.OwnerID, org.AdminID, org.MemberID},
	).Scan(&userCount))

	// repositories requires tenant scope to be visible under RLS. After
	// cleanupOrg deletes the row and commits, a fresh unscoped SELECT
	// returns 0 (RLS blocks) — check via a scoped tx to observe truthfully.
	sTx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = sTx.Rollback(ctx) }()
	require.NoError(t, sTx.QueryRow(ctx,
		"SELECT COUNT(*) FROM repositories WHERE id = $1", org.RepoID).Scan(&repoCount))

	assert.Equal(t, 0, orgCount, "organization row must be deleted")
	assert.Equal(t, 0, projCount, "project row must be deleted")
	assert.Equal(t, 0, repoCount, "repository row must be deleted")
	assert.Equal(t, 0, userCount, "user rows must be deleted")
}

// insertOneChunk writes a single chunk row into the repo, creating a scaffold
// ingestion_run beforehand. It is used by the leak self-tests.
func insertOneChunk(ctx context.Context, tx pgx.Tx, repoID, marker string) error {
	var runID string
	if err := tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, $2, 'main', 'completed') RETURNING id`,
		repoID, marker,
	).Scan(&runID); err != nil {
		return err
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO chunks
		   (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
		 VALUES ($1, $2, 'x.txt', 1, 1, $3, $4)`,
		runID, repoID, marker, marker+"-hash",
	)
	return err
}
