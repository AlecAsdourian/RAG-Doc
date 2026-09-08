package auth

import (
	"context"
	"fmt"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"
)

// SetupTestDB creates test database connection
// Uses DATABASE_TEST_URL env var or defaults to test DB
func SetupTestDB(t *testing.T) *pgxpool.Pool {
	dbURL := os.Getenv("DATABASE_TEST_URL")
	if dbURL == "" {
		dbURL = "postgres://coderag:coderag@localhost:5434/coderag?sslmode=disable"
	}

	pool, err := pgxpool.New(context.Background(), dbURL)
	require.NoError(t, err, "Failed to connect to test database")

	return pool
}

// tenantScopedTables carries the assert_tenant_scoped trigger from
// migration 000009. Cleanup spans every tenant at once, so it cannot
// satisfy the trigger by setting a single app.current_tenant — the
// trigger is disabled for the duration instead.
//
// Keep in sync with the trigger attachments in
// migrations/000009_tenant_assertion.up.sql.
var tenantScopedTables = []string{
	"feedback",
	"retrievals",
	"queries",
	"chunks",
	"ingestion_runs",
	"repositories",
}

// CleanupTestDB closes the connection and truncates test data.
//
// Migration 000009's trigger refuses DELETE on tenant-scoped tables
// without `app.current_tenant` set. The original version of this
// function issued bare cross-tenant DELETEs and — because it discards
// the error from every `db.Exec` — silently stopped deleting anything
// the moment Phase 17-03 landed. Rows leaked between tests, and the
// next test using a hardcoded slug hit a
// `organizations_slug_key` unique violation.
//
// The trigger is disabled and re-enabled around the deletes. RLS is not
// an obstacle here: the test connection is the container superuser,
// which bypasses RLS even under FORCE.
func CleanupTestDB(t *testing.T, db *pgxpool.Pool) {
	ctx := context.Background()

	for _, tbl := range tenantScopedTables {
		if _, err := db.Exec(ctx, fmt.Sprintf("ALTER TABLE %s DISABLE TRIGGER trg_assert_tenant", tbl)); err != nil {
			t.Logf("CleanupTestDB: could not disable trigger on %s: %v", tbl, err)
		}
	}
	defer func() {
		for _, tbl := range tenantScopedTables {
			if _, err := db.Exec(ctx, fmt.Sprintf("ALTER TABLE %s ENABLE TRIGGER trg_assert_tenant", tbl)); err != nil {
				t.Logf("CleanupTestDB: could not re-enable trigger on %s: %v", tbl, err)
			}
		}
		db.Close()
	}()

	// Delete in reverse FK order. Surface failures via t.Logf rather than
	// discarding them — a silent cleanup failure is what let this rot
	// undetected for two phases.
	for _, stmt := range []string{
		"DELETE FROM feedback",
		"DELETE FROM retrievals",
		"DELETE FROM queries",
		"DELETE FROM chunks",
		"DELETE FROM ingestion_runs",
		"DELETE FROM repositories",
		"DELETE FROM projects",
		"DELETE FROM organization_memberships",
		"DELETE FROM users",
		"DELETE FROM organizations",
	} {
		if _, err := db.Exec(ctx, stmt); err != nil {
			t.Logf("CleanupTestDB: %q failed: %v", stmt, err)
		}
	}
}

// CreateTestOrg creates test organization
func CreateTestOrg(t *testing.T, db *pgxpool.Pool, name, slug string) uuid.UUID {
	var orgID uuid.UUID
	err := db.QueryRow(context.Background(),
		`INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id`,
		name, slug,
	).Scan(&orgID)
	require.NoError(t, err)
	return orgID
}

// CreateTestUser creates test user
func CreateTestUser(t *testing.T, db *pgxpool.Pool, email, fullName string) uuid.UUID {
	var userID uuid.UUID
	supabaseUserID := uuid.New()
	err := db.QueryRow(context.Background(),
		`INSERT INTO users (supabase_user_id, email, full_name)
         VALUES ($1, $2, $3) RETURNING id`,
		supabaseUserID, email, fullName,
	).Scan(&userID)
	require.NoError(t, err)
	return userID
}

// AddUserToOrg adds user to organization with role
func AddUserToOrg(t *testing.T, db *pgxpool.Pool, userID, orgID uuid.UUID, role string) {
	_, err := db.Exec(context.Background(),
		`INSERT INTO organization_memberships (user_id, organization_id, role)
         VALUES ($1, $2, $3)`,
		userID, orgID, role,
	)
	require.NoError(t, err)
}

// CreateTestProject creates test project
func CreateTestProject(t *testing.T, db *pgxpool.Pool, orgID uuid.UUID, name, slug string) uuid.UUID {
	var projectID uuid.UUID
	err := db.QueryRow(context.Background(),
		`INSERT INTO projects (organization_id, name, slug)
         VALUES ($1, $2, $3) RETURNING id`,
		orgID, name, slug,
	).Scan(&projectID)
	require.NoError(t, err)
	return projectID
}

// CreateTestRepository creates a test repository under the given
// project's owning organization.
//
// `repositories` is tenant-scoped: migration 000008 puts RLS on it and
// migration 000009 attaches the assert_tenant_scoped trigger, which
// refuses any INSERT without `app.current_tenant` set. The original
// version of this helper did a bare INSERT and started failing with
// SQLSTATE 42501 the moment Phase 17-03 landed — the failure went
// unnoticed because the tests using it also need a live Redis and
// Postgres that CI wasn't providing.
//
// The tenant is derived from the project rather than taken as a
// parameter so existing call sites keep working unchanged.
func CreateTestRepository(t *testing.T, db *pgxpool.Pool, projectID uuid.UUID, name, gitURL string) uuid.UUID {
	ctx := context.Background()

	var orgID uuid.UUID
	require.NoError(t,
		db.QueryRow(ctx, `SELECT organization_id FROM projects WHERE id = $1`, projectID).Scan(&orgID),
		"CreateTestRepository: project %s not found (create it with CreateTestProject first)", projectID,
	)

	tx, err := db.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	// SET LOCAL does not accept bind parameters for GUC values; orgID is
	// a uuid.UUID from the DB so interpolation is safe here.
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgID))
	require.NoError(t, err)

	var repoID uuid.UUID
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url)
         VALUES ($1, $2, $3) RETURNING id`,
		projectID, name, gitURL,
	).Scan(&repoID))

	require.NoError(t, tx.Commit(ctx))
	return repoID
}
