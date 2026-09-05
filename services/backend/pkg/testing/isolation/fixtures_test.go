package isolation

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestFixtures_WithTwoOrgs_DistinctIDs is self-test #1 — the two orgs the
// helper creates must have distinct ids, distinct slugs, and their users must
// have distinct ids.
func TestFixtures_WithTwoOrgs_DistinctIDs(t *testing.T) {
	pool := SetupTestDB(t)

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		require.NotEmpty(t, orgA.ID)
		require.NotEmpty(t, orgB.ID)
		assert.NotEqual(t, orgA.ID, orgB.ID, "org IDs must differ")
		assert.NotEqual(t, orgA.Slug, orgB.Slug, "org slugs must differ")
		assert.NotEqual(t, orgA.OwnerID, orgB.OwnerID, "owner user IDs must differ")
		assert.NotEqual(t, orgA.RepoID, orgB.RepoID, "repo IDs must differ")

		assert.NotEmpty(t, orgA.OwnerJWT)
		assert.NotEmpty(t, orgA.AdminJWT)
		assert.NotEmpty(t, orgA.MemberJWT)
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
