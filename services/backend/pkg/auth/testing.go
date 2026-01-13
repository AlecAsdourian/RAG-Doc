package auth

import (
	"context"
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

// CleanupTestDB closes connection and cleans up test data
func CleanupTestDB(t *testing.T, db *pgxpool.Pool) {
	// Clean up test data (delete in reverse FK order)
	db.Exec(context.Background(), "DELETE FROM feedback")
	db.Exec(context.Background(), "DELETE FROM retrievals")
	db.Exec(context.Background(), "DELETE FROM queries")
	db.Exec(context.Background(), "DELETE FROM chunks")
	db.Exec(context.Background(), "DELETE FROM ingestion_runs")
	db.Exec(context.Background(), "DELETE FROM repositories")
	db.Exec(context.Background(), "DELETE FROM projects")
	db.Exec(context.Background(), "DELETE FROM organization_memberships")
	db.Exec(context.Background(), "DELETE FROM users")
	db.Exec(context.Background(), "DELETE FROM organizations")

	db.Close()
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

// CreateTestRepository creates test repository
func CreateTestRepository(t *testing.T, db *pgxpool.Pool, projectID uuid.UUID, name, gitURL string) uuid.UUID {
	var repoID uuid.UUID
	err := db.QueryRow(context.Background(),
		`INSERT INTO repositories (project_id, name, git_url)
         VALUES ($1, $2, $3) RETURNING id`,
		projectID, name, gitURL,
	).Scan(&repoID)
	require.NoError(t, err)
	return repoID
}
