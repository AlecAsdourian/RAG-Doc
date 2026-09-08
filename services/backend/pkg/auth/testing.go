package auth

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"
)

// setupStart records when each pool's test began, so CleanupTestDB can
// scope its deletes to rows created during THIS test's window instead of
// truncating shared tables.
//
// Keyed by pool pointer rather than *testing.T because CleanupTestDB
// receives the pool and the two are 1:1 per test.
var setupStart sync.Map // *pgxpool.Pool -> time.Time

// SetupTestDB creates test database connection
// Uses DATABASE_TEST_URL env var or defaults to test DB
func SetupTestDB(t *testing.T) *pgxpool.Pool {
	dbURL := os.Getenv("DATABASE_TEST_URL")
	if dbURL == "" {
		dbURL = "postgres://coderag:coderag@localhost:5434/coderag?sslmode=disable"
	}

	pool, err := pgxpool.New(context.Background(), dbURL)
	require.NoError(t, err, "Failed to connect to test database")

	// Read the cutoff from the database clock, not the Go process clock —
	// the two can differ and the comparison happens server-side.
	var now time.Time
	require.NoError(t,
		pool.QueryRow(context.Background(), "SELECT NOW()").Scan(&now),
		"Failed to read database clock for cleanup watermark",
	)
	setupStart.Store(pool, now)

	return pool
}

// CleanupTestDB deletes the rows this test created, then closes the pool.
//
// Two properties this function must have, both learned the hard way:
//
//  1. It must not leave the migration-000009 tenant trigger disarmed.
//     An earlier version used `ALTER TABLE ... DISABLE TRIGGER`, which
//     writes `pg_trigger.tgenabled='D'` — durable catalog state that
//     survives session close AND process exit. Because the 17-01
//     testcontainers Postgres is reused across `go test` invocations
//     (Ryuk disabled), a panic or timeout between disable and re-enable
//     would permanently disarm tenant isolation for every later run on
//     that container, turning real failures into silent passes.
//     `SET LOCAL session_replication_role = replica` achieves the same
//     trigger suppression but is transaction-scoped: it cannot outlive
//     the transaction, however the process dies.
//
//  2. It must not delete other packages' rows. An earlier version issued
//     unfiltered `DELETE FROM users` etc. Those previously no-opped
//     (every error was discarded), so making them work turned them into
//     a hazard: `go test ./...` runs packages in parallel against the
//     same container, so an unscoped truncate can wipe another package's
//     in-flight fixtures. Deletes are now scoped to rows created after
//     this pool's SetupTestDB watermark.
//
// Residual caveat: the watermark bounds deletes to this test's time
// window, not to this test's rows, so a package running concurrently
// *within that same window* could still lose fixtures. The real fix is
// migrating this helper onto the 17-01 harness (which scopes cleanup by
// tenant id) — tracked as follow-up 2 in 19-02-SUMMARY.md.
func CleanupTestDB(t *testing.T, db *pgxpool.Pool) {
	ctx := context.Background()
	defer db.Close()
	defer setupStart.Delete(db)

	since, ok := setupStart.Load(db)
	if !ok {
		t.Logf("CleanupTestDB: no setup watermark for this pool; skipping cleanup to avoid an unscoped delete")
		return
	}
	watermark := since.(time.Time)

	tx, err := db.Begin(ctx)
	if err != nil {
		t.Logf("CleanupTestDB: begin: %v", err)
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// Suppress user triggers (including assert_tenant_scoped) for this
	// transaction only. Cleanup legitimately spans every tenant, so it
	// cannot satisfy the trigger with a single app.current_tenant.
	if _, err := tx.Exec(ctx, "SET LOCAL session_replication_role = replica"); err != nil {
		t.Logf("CleanupTestDB: could not suppress triggers: %v", err)
	}

	// Delete in reverse FK order, scoped by creation time. Surface
	// failures via t.Logf rather than discarding them — a silent cleanup
	// failure is what let the 17-03 breakage rot undetected for two
	// phases.
	//
	// retrievals and feedback have no created_at watermark of their own
	// in the FK chain, so they are scoped through their parent query.
	for _, stmt := range []string{
		`DELETE FROM feedback WHERE retrieval_id IN (
			SELECT r.id FROM retrievals r JOIN queries q ON r.query_id = q.id
			WHERE q.created_at >= $1)`,
		`DELETE FROM retrievals WHERE query_id IN (
			SELECT id FROM queries WHERE created_at >= $1)`,
		`DELETE FROM queries WHERE created_at >= $1`,
		`DELETE FROM chunks WHERE created_at >= $1`,
		`DELETE FROM ingestion_runs WHERE created_at >= $1`,
		`DELETE FROM repositories WHERE created_at >= $1`,
		`DELETE FROM projects WHERE created_at >= $1`,
		`DELETE FROM organization_memberships WHERE created_at >= $1`,
		`DELETE FROM users WHERE created_at >= $1`,
		`DELETE FROM organizations WHERE created_at >= $1`,
	} {
		if _, err := tx.Exec(ctx, stmt, watermark); err != nil {
			t.Logf("CleanupTestDB: %q failed: %v", firstLineOf(stmt), err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		t.Logf("CleanupTestDB: commit: %v", err)
	}
}

// firstLineOf trims a multi-line SQL statement to its first line for
// readable log output.
func firstLineOf(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i] + " ..."
	}
	return s
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
