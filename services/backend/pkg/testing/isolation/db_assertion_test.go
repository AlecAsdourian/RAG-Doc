package isolation_test

// TestDBAssertion exercises migration 000009's assert_tenant_scoped
// trigger — the second wall of tenant isolation. The trigger fires
// BEFORE INSERT/UPDATE/DELETE on the 6 tables migration 000008 already
// puts RLS on: repositories, ingestion_runs, chunks, queries, retrievals,
// feedback. Any mutation on those tables outside a TenantScope (i.e.
// without app.current_tenant set) is refused with SQLSTATE 42501.
//
// Not covered by the trigger (by design — see 000009 up-migration
// header): users, organizations, projects, organization_memberships.
// These are the "root" tables created during signup before any tenant
// exists to scope the write to; they carry their own FK-integrity
// checks.
//
// v2 breadcrumb: when v2 adds a new tenant-scoped table (graph nodes,
// memory records, etc.), the developer must (a) attach the trigger in a
// new migration and (b) add the table to `protectedTables` below. If the
// migration is added without the test entry, the trigger might miss the
// table and no one would notice. This test is the ratchet.

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

const tenantIsolationSQLState = "42501"

// protectedTables lists every table migration 000009 attaches the trigger
// to, plus a minimal INSERT statement per table. FKs may reference
// non-existent parents; that's fine — BEFORE triggers run before FK
// checks, so the trigger's 42501 fires first.
var protectedTables = []struct {
	name       string
	insertSQL  string
	insertArgs []any
}{
	{
		"repositories",
		`INSERT INTO repositories (project_id, name, git_url) VALUES ($1, $2, $3)`,
		[]any{uuid.NewString(), "test-repo", "https://example.test/x.git"},
	},
	{
		"ingestion_runs",
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status) VALUES ($1, $2, $3, $4)`,
		[]any{uuid.NewString(), strings.Repeat("a", 40), "main", "completed"},
	},
	{
		"chunks",
		`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)`,
		[]any{uuid.NewString(), uuid.NewString(), "x.md", 1, 10, "content", "hash"},
	},
	{
		"queries",
		`INSERT INTO queries (project_id, query_text) VALUES ($1, $2)`,
		[]any{uuid.NewString(), "test query"},
	},
	{
		"retrievals",
		`INSERT INTO retrievals (query_id, chunk_id, rank) VALUES ($1, $2, $3)`,
		[]any{uuid.NewString(), uuid.NewString(), 1},
	},
	{
		"feedback",
		`INSERT INTO feedback (retrieval_id, feedback_type) VALUES ($1, $2)`,
		[]any{uuid.NewString(), "positive"},
	},
}

func TestDBAssertion_TriggerFiresOnInsertWithoutTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	for _, tc := range protectedTables {
		t.Run(tc.name, func(t *testing.T) {
			_, err := pool.Exec(ctx, tc.insertSQL, tc.insertArgs...)
			requireTenantViolation(t, err, "INSERT", tc.name)
		})
	}
}

// UPDATE/DELETE without tenant: layered semantics.
//
// The trigger's INSERT protection (tested above) is the strong assertion
// — raw INSERTs on tenant-scoped tables cannot happen without a tenant.
// For UPDATE/DELETE the picture is subtler because two Postgres quirks
// interact:
//
//   1. RLS runs BEFORE the row-level trigger during UPDATE/DELETE row
//      identification. If RLS returns zero rows, the trigger doesn't fire.
//   2. Once any transaction on a connection runs `SET LOCAL
//      app.current_tenant = '<uuid>'` and commits, Postgres registers the
//      custom GUC as an empty string on that session (NOT unset). A
//      subsequent `current_setting('app.current_tenant', true)` returns
//      `''`, not NULL. Migration 000008's RLS policies cast that value to
//      uuid — `''::uuid` raises SQLSTATE 22P02.
//
// Net effect on a pool-reused connection: an UPDATE/DELETE without
// tenant is refused, but via 22P02 (RLS cast) rather than 42501 (our
// trigger). Either way the write does not land — the isolation guarantee
// holds. On a genuinely fresh connection the same UPDATE/DELETE would
// silently target zero rows (RLS filters, no error). Both outcomes are
// safe; neither exercises the trigger's UPDATE/DELETE branch under
// normal RLS.
//
// The trigger's UPDATE/DELETE branch matters as belt-and-suspenders if
// RLS is ever disabled on the table (superuser + FORCE bypass, or an
// explicit DISABLE ROW LEVEL SECURITY). Testing that path here would
// need superuser privilege the test role deliberately doesn't have.
//
// So scenarios 3 and 4 assert the safety property, not the specific
// error code: the write is refused, or affects zero rows — never lands.

func TestDBAssertion_UpdateWithoutTenantIsRefusedOrNoOp(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		chunkID := insertOneChunk(t, pool, orgA)
		originalPath := "z.md"

		tag, err := pool.Exec(ctx,
			`UPDATE chunks SET file_path = $1 WHERE id = $2`,
			"changed.md", chunkID,
		)
		if err != nil {
			// Accepted: RLS cast (22P02) or trigger raise (42501) — both
			// refuse the write.
			requireIsolationRefusal(t, err)
		} else {
			// Otherwise: zero rows affected — RLS filtered silently.
			require.Zero(t, tag.RowsAffected(), "UPDATE must affect zero rows without tenant")
		}

		// Regardless of path, the row must be unchanged. Verify under a
		// fresh TenantScope so RLS lets us read it.
		requireChunkPathUnchanged(t, pool, orgA, chunkID, originalPath)
	})
}

func TestDBAssertion_DeleteWithoutTenantIsRefusedOrNoOp(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		chunkID := insertOneChunk(t, pool, orgA)

		tag, err := pool.Exec(ctx,
			`DELETE FROM chunks WHERE id = $1`, chunkID,
		)
		if err != nil {
			requireIsolationRefusal(t, err)
		} else {
			require.Zero(t, tag.RowsAffected(), "DELETE must affect zero rows without tenant")
		}

		// Row must still be present.
		requireChunkExists(t, pool, orgA, chunkID)
	})
}

func TestDBAssertion_TriggerAllowsMutationsWhenTenantSet(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	// Under a valid TenantScope with proper parents, every protected
	// table should accept a well-formed INSERT.
	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		var runID string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
			 VALUES ($1, $2, $3, $4) RETURNING id`,
			orgA.RepoID, strings.Repeat("b", 40), "main", "completed",
		).Scan(&runID))

		var chunkID string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
			 VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
			runID, orgA.RepoID, "y.md", 1, 5, "body", "h-1",
		).Scan(&chunkID))

		var queryID string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO queries (project_id, query_text) VALUES ($1, $2) RETURNING id`,
			orgA.ProjectID, "what does foo do?",
		).Scan(&queryID))

		var retrievalID string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO retrievals (query_id, chunk_id, rank) VALUES ($1, $2, $3) RETURNING id`,
			queryID, chunkID, 1,
		).Scan(&retrievalID))

		_, err = tx.Exec(ctx,
			`INSERT INTO feedback (retrieval_id, feedback_type) VALUES ($1, $2)`,
			retrievalID, "positive",
		)
		require.NoError(t, err)
	})
}

func TestDBAssertion_ExemptTablesUnaffected(t *testing.T) {
	// Signup-flow tables stay writable without app.current_tenant.
	// If the coverage decision ever changes, this test loudly fails
	// and forces the change to be intentional.
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	t.Run("users", func(t *testing.T) {
		var id string
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO users (supabase_user_id, email, full_name)
			 VALUES ($1, $2, $3) RETURNING id`,
			uuid.New(), "exempt-user@iso-test.local", "exempt user",
		).Scan(&id))
		t.Cleanup(func() { _, _ = pool.Exec(ctx, `DELETE FROM users WHERE id = $1`, id) })
	})

	t.Run("organizations", func(t *testing.T) {
		slug := "exempt-org-" + shortHex()
		var id string
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id`,
			slug, slug,
		).Scan(&id))
		t.Cleanup(func() { _, _ = pool.Exec(ctx, `DELETE FROM organizations WHERE id = $1`, id) })
	})

	t.Run("projects", func(t *testing.T) {
		// projects.organization_id FK requires a real org; use a
		// throwaway one and delete both on cleanup.
		slug := "exempt-proj-" + shortHex()
		var orgID string
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id`,
			slug, slug,
		).Scan(&orgID))
		var projID string
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO projects (organization_id, name, slug) VALUES ($1, $2, $3) RETURNING id`,
			orgID, slug+"-p", slug+"-p",
		).Scan(&projID))
		t.Cleanup(func() {
			_, _ = pool.Exec(ctx, `DELETE FROM projects WHERE id = $1`, projID)
			_, _ = pool.Exec(ctx, `DELETE FROM organizations WHERE id = $1`, orgID)
		})
	})

	t.Run("organization_memberships", func(t *testing.T) {
		// Membership needs a user and an org — build both throwaway.
		slug := "exempt-mem-" + shortHex()
		var orgID, userID string
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id`,
			slug, slug,
		).Scan(&orgID))
		require.NoError(t, pool.QueryRow(ctx,
			`INSERT INTO users (supabase_user_id, email, full_name)
			 VALUES ($1, $2, $3) RETURNING id`,
			uuid.New(), "exempt-"+slug+"@iso-test.local", "exempt mem",
		).Scan(&userID))
		_, err := pool.Exec(ctx,
			`INSERT INTO organization_memberships (user_id, organization_id, role)
			 VALUES ($1, $2, $3)`,
			userID, orgID, "member",
		)
		require.NoError(t, err)
		t.Cleanup(func() {
			_, _ = pool.Exec(ctx, `DELETE FROM organization_memberships WHERE user_id = $1`, userID)
			_, _ = pool.Exec(ctx, `DELETE FROM users WHERE id = $1`, userID)
			_, _ = pool.Exec(ctx, `DELETE FROM organizations WHERE id = $1`, orgID)
		})
	})
}

func TestDBAssertion_WithTwoOrgsStillWorks(t *testing.T) {
	// End-to-end golden path: WithTwoOrgs already routes its own
	// tenant-scoped inserts through TenantScope. Prove it hasn't
	// regressed under migration 000009.
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		_ = insertOneChunk(t, pool, orgA)
		_ = insertOneChunk(t, pool, orgB)
	})
}

// insertOneChunk creates an ingestion_run and a chunk for org under a
// committed TenantScope tx and returns the chunk id.
func insertOneChunk(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg) string {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var runID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, $2, $3, $4) RETURNING id`,
		org.RepoID, strings.Repeat("c", 40), "main", "completed",
	).Scan(&runID))

	var chunkID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
		 VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
		runID, org.RepoID, "z.md", 1, 3, "committed", "h-committed",
	).Scan(&chunkID))
	require.NoError(t, tx.Commit(ctx))
	return chunkID
}

// requireTenantViolation asserts that err is the trigger's 42501 error
// with a message identifying the operation and table. Used for INSERT
// scenarios where the trigger fires before RLS row identification.
func requireTenantViolation(t *testing.T, err error, op, table string) {
	t.Helper()
	require.Error(t, err, "trigger should have refused %s on %s without app.current_tenant", op, table)
	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "expected *pgconn.PgError, got %T: %v", err, err)
	require.Equal(t, tenantIsolationSQLState, pgErr.Code,
		"expected SQLSTATE %s, got %s (message: %s)", tenantIsolationSQLState, pgErr.Code, pgErr.Message)
	require.Contains(t, pgErr.Message, "tenant isolation violated")
	require.Contains(t, pgErr.Message, table, "error should name the table")
	require.Contains(t, pgErr.Message, op, "error should name the operation")
}

// requireIsolationRefusal asserts that err is either the trigger's 42501
// or the RLS uuid-cast 22P02 — both are "the write is refused." See the
// long comment above TestDBAssertion_UpdateWithoutTenantIsRefusedOrNoOp
// for why both are acceptable on UPDATE/DELETE.
func requireIsolationRefusal(t *testing.T, err error) {
	t.Helper()
	require.Error(t, err)
	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "expected *pgconn.PgError, got %T: %v", err, err)
	if pgErr.Code != tenantIsolationSQLState && pgErr.Code != "22P02" {
		t.Fatalf("expected SQLSTATE 42501 (trigger) or 22P02 (RLS uuid cast), got %s: %s", pgErr.Code, pgErr.Message)
	}
}

// requireChunkPathUnchanged reads the chunk under org's TenantScope and
// asserts its file_path is still the value passed in.
func requireChunkPathUnchanged(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, chunkID, wantPath string) {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var got string
	require.NoError(t, tx.QueryRow(ctx,
		`SELECT file_path FROM chunks WHERE id = $1`, chunkID,
	).Scan(&got))
	require.Equal(t, wantPath, got, "chunk file_path must be unchanged")
}

// requireChunkExists reads the chunk under org's TenantScope and asserts
// the row is still present.
func requireChunkExists(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, chunkID string) {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var count int
	require.NoError(t, tx.QueryRow(ctx,
		`SELECT COUNT(*) FROM chunks WHERE id = $1`, chunkID,
	).Scan(&count))
	require.Equal(t, 1, count, "chunk must still exist")
}

func shortHex() string {
	return strings.ReplaceAll(uuid.NewString(), "-", "")[:8]
}
