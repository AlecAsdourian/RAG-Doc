package auth

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestCrossTenantIsolation verifies User A cannot access Org B's data
func TestCrossTenantIsolation(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Setup: Create two orgs with users
	orgA := CreateTestOrg(t, db, "Org A", "org-a")
	userA := CreateTestUser(t, db, "alice@orga.com", "Alice")
	AddUserToOrg(t, db, userA, orgA, "member")

	orgB := CreateTestOrg(t, db, "Org B", "org-b")
	userB := CreateTestUser(t, db, "bob@orgb.com", "Bob")
	AddUserToOrg(t, db, userB, orgB, "member")

	// Create projects in each org
	projectA := CreateTestProject(t, db, orgA, "Project A", "project-a")
	projectB := CreateTestProject(t, db, orgB, "Project B", "project-b")

	// Create repositories in each project
	repoA := CreateTestRepository(t, db, projectA, "Repo A", "https://github.com/orga/repo-a")
	repoB := CreateTestRepository(t, db, projectB, "Repo B", "https://github.com/orgb/repo-b")

	// Test 1: User A can access their own repository
	conn, err := db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	_, err = conn.Exec(context.Background(), "SET LOCAL app.current_tenant = $1", orgA.String())
	require.NoError(t, err)

	var count int
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM repositories WHERE id = $1", repoA).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 1, count, "User A should see their own repository")

	// Test 2: User A CANNOT access Org B's repository (RLS blocks it)
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM repositories WHERE id = $1", repoB).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 0, count, "User A should NOT see Org B's repository (RLS isolation)")

	// Test 3: Begin new transaction for tenant context switch
	conn.Release()
	conn, err = db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	// Switch tenant context to Org B
	_, err = conn.Exec(context.Background(), "SET LOCAL app.current_tenant = $1", orgB.String())
	require.NoError(t, err)

	// Now User B's org can see Repo B
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM repositories WHERE id = $1", repoB).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 1, count, "Org B context should see Repo B")

	// But cannot see Repo A
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM repositories WHERE id = $1", repoA).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 0, count, "Org B context should NOT see Repo A")
}

// TestRLSWithoutTenantContext verifies queries fail when tenant not set
func TestRLSWithoutTenantContext(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Create org and repository
	orgA := CreateTestOrg(t, db, "Org A", "org-a")
	projectA := CreateTestProject(t, db, orgA, "Project A", "project-a")
	repoA := CreateTestRepository(t, db, projectA, "Repo A", "https://github.com/orga/repo-a")

	// Query without setting app.current_tenant
	conn, err := db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	var count int
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM repositories WHERE id = $1", repoA).Scan(&count)
	require.NoError(t, err)

	// Should return 0 (RLS policy requires current_setting, which is NULL)
	// current_setting('app.current_tenant', true) with 'true' returns NULL instead of error
	assert.Equal(t, 0, count, "Query without tenant context should return no rows (RLS enforcement)")
}

// TestRoleBasedAccess verifies owner/admin vs member permissions
func TestRoleBasedAccess(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Create org with owner and member
	orgA := CreateTestOrg(t, db, "Org A", "org-a")
	owner := CreateTestUser(t, db, "owner@orga.com", "Owner")
	AddUserToOrg(t, db, owner, orgA, "owner")

	member := CreateTestUser(t, db, "member@orga.com", "Member")
	AddUserToOrg(t, db, member, orgA, "member")

	// Query role for each user
	conn, err := db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	var role string
	err = conn.QueryRow(context.Background(),
		`SELECT role FROM organization_memberships
         WHERE user_id = $1 AND organization_id = $2`,
		owner, orgA).Scan(&role)
	require.NoError(t, err)
	assert.Equal(t, "owner", role)

	err = conn.QueryRow(context.Background(),
		`SELECT role FROM organization_memberships
         WHERE user_id = $1 AND organization_id = $2`,
		member, orgA).Scan(&role)
	require.NoError(t, err)
	assert.Equal(t, "member", role)

	// Note: Role-based authorization (who can invite, delete org) is
	// enforced at application layer via RequireRole middleware
	// This test verifies role data is stored correctly
}

// TestMultipleOrganizationsPerUser verifies user can belong to multiple orgs
func TestMultipleOrganizationsPerUser(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Create user in two organizations
	orgA := CreateTestOrg(t, db, "Org A", "org-a")
	orgB := CreateTestOrg(t, db, "Org B", "org-b")
	user := CreateTestUser(t, db, "alice@example.com", "Alice")

	AddUserToOrg(t, db, user, orgA, "owner")
	AddUserToOrg(t, db, user, orgB, "member")

	// Query memberships
	conn, err := db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	var count int
	err = conn.QueryRow(context.Background(),
		`SELECT COUNT(*) FROM organization_memberships WHERE user_id = $1`,
		user).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 2, count, "User should have 2 organization memberships")
}

// TestChunksIsolation verifies RLS policies on chunks table
func TestChunksIsolation(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Setup: Create two orgs with repositories and chunks
	orgA := CreateTestOrg(t, db, "Org A", "org-a")
	projectA := CreateTestProject(t, db, orgA, "Project A", "project-a")
	repoA := CreateTestRepository(t, db, projectA, "Repo A", "https://github.com/orga/repo-a")

	orgB := CreateTestOrg(t, db, "Org B", "org-b")
	projectB := CreateTestProject(t, db, orgB, "Project B", "project-b")
	repoB := CreateTestRepository(t, db, projectB, "Repo B", "https://github.com/orgb/repo-b")

	// Create ingestion runs
	var runA, runB string
	err := db.QueryRow(context.Background(),
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
         VALUES ($1, 'abc123', 'main', 'completed') RETURNING id`,
		repoA).Scan(&runA)
	require.NoError(t, err)

	err = db.QueryRow(context.Background(),
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
         VALUES ($1, 'def456', 'main', 'completed') RETURNING id`,
		repoB).Scan(&runB)
	require.NoError(t, err)

	// Create chunks for each repo
	var chunkA, chunkB string
	err = db.QueryRow(context.Background(),
		`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
         VALUES ($1, $2, 'src/file.ts', 1, 10, 'console.log("A")', 'hash-a') RETURNING id`,
		runA, repoA).Scan(&chunkA)
	require.NoError(t, err)

	err = db.QueryRow(context.Background(),
		`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
         VALUES ($1, $2, 'src/file.ts', 1, 10, 'console.log("B")', 'hash-b') RETURNING id`,
		runB, repoB).Scan(&chunkB)
	require.NoError(t, err)

	// Test: Set tenant context to Org A
	conn, err := db.Acquire(context.Background())
	require.NoError(t, err)
	defer conn.Release()

	_, err = conn.Exec(context.Background(), "SET LOCAL app.current_tenant = $1", orgA.String())
	require.NoError(t, err)

	// Should see Org A's chunk
	var count int
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM chunks WHERE id = $1", chunkA).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 1, count, "Should see Org A's chunk")

	// Should NOT see Org B's chunk
	err = conn.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM chunks WHERE id = $1", chunkB).Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 0, count, "Should NOT see Org B's chunk (RLS isolation)")
}
