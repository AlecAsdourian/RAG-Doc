package isolation

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSetupTestDB verifies the harness starts Postgres, applies every migration,
// and returns a working pgx pool. It is the single entry-point verification for
// Task 1: if this passes, ISS-006 is resolved.
func TestSetupTestDB(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	var one int
	require.NoError(t, pool.QueryRow(ctx, "SELECT 1").Scan(&one))
	assert.Equal(t, 1, one, "pool should execute a simple query")

	// Sanity-check that the connection is running under the app role, not the
	// superuser. If it were the superuser, every isolation test would silently
	// bypass RLS.
	var currentRole string
	require.NoError(t, pool.QueryRow(ctx, "SELECT current_user").Scan(&currentRole))
	assert.Equal(t, appRole, currentRole, "pool must run as the non-superuser app role")

	// All 8 migration up-files created 10 tables between them (organizations,
	// projects, repositories, ingestion_runs, chunks, queries, retrievals,
	// feedback, users, organization_memberships), plus golang-migrate's
	// schema_migrations bookkeeping table.
	var tableCount int
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT COUNT(*) FROM information_schema.tables
		 WHERE table_schema = 'public' AND table_type = 'BASE TABLE'`,
	).Scan(&tableCount))
	assert.GreaterOrEqual(t, tableCount, 10, "at least 10 domain tables should exist after migrations")

	// RLS on repositories proves migration 000008 ran.
	var rlsEnabled bool
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT relrowsecurity FROM pg_class WHERE relname = 'repositories'`,
	).Scan(&rlsEnabled))
	assert.True(t, rlsEnabled, "RLS should be enabled on repositories (migration 000008)")
}
