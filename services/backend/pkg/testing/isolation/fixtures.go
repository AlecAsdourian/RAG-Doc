package isolation

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"
)

// TestOrg is a test tenant with three role members and a starter repository.
// WithTwoOrgs produces two of them for cross-tenant tests.
//
// Tests that need a signed JWT for one of the users call
// testjwt.Sign(org.OwnerID, org.ID, "owner") directly — the signer lives in
// pkg/testing/isolation/testjwt to keep the signing secret out of this
// package's public API. See package doc on testjwt for rationale.
type TestOrg struct {
	ID   string
	Slug string

	OwnerID  string
	AdminID  string
	MemberID string

	ProjectID string
	RepoID    string
}

// WithTwoOrgs creates two isolated test tenants, each with owner/admin/member
// users and a starter repository, invokes fn with them, and cleans up on
// t.Cleanup. Any additional per-test rows (chunks, queries, ingestion runs)
// are the caller's responsibility to insert inside fn.
//
// v2 breadcrumb: this fixture deliberately does NOT populate every
// tenant-scoped table. Future v2 graph tables or memory records are added by
// test-specific setup inside the fn closure, and the fixture stays stable.
func WithTwoOrgs(t *testing.T, pool *pgxpool.Pool, fn func(orgA, orgB *TestOrg)) {
	t.Helper()
	ctx := context.Background()

	tag := sanitizeTag(t.Name())
	orgA := createOrg(t, pool, fmt.Sprintf("iso-a-%s-%s", tag, shortToken()))
	orgB := createOrg(t, pool, fmt.Sprintf("iso-b-%s-%s", tag, shortToken()))

	t.Cleanup(func() {
		cleanupOrg(ctx, pool, orgA)
		cleanupOrg(ctx, pool, orgB)
	})

	fn(orgA, orgB)
}

func createOrg(t *testing.T, pool *pgxpool.Pool, slug string) *TestOrg {
	t.Helper()
	ctx := context.Background()

	org := &TestOrg{Slug: slug}
	require.NoError(t, pool.QueryRow(ctx,
		`INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id`,
		slug, slug,
	).Scan(&org.ID))

	org.OwnerID = createUserWithMembership(t, pool, org.ID, "owner", slug)
	org.AdminID = createUserWithMembership(t, pool, org.ID, "admin", slug)
	org.MemberID = createUserWithMembership(t, pool, org.ID, "member", slug)

	require.NoError(t, pool.QueryRow(ctx,
		`INSERT INTO projects (organization_id, name, slug)
		 VALUES ($1, $2, $3) RETURNING id`,
		org.ID, slug+"-proj", slug+"-proj",
	).Scan(&org.ProjectID))

	// repositories has RLS; must set tenant context on the transaction.
	tx, err := pool.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", org.ID))
	require.NoError(t, err)
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url)
		 VALUES ($1, $2, $3) RETURNING id`,
		org.ProjectID, slug+"-repo", "https://example.test/"+slug+".git",
	).Scan(&org.RepoID))
	require.NoError(t, tx.Commit(ctx))

	return org
}

func createUserWithMembership(t *testing.T, pool *pgxpool.Pool, orgID, role, slug string) string {
	t.Helper()
	ctx := context.Background()

	var userID string
	email := fmt.Sprintf("%s-%s-%s@iso-test.local", role, slug, shortToken())
	require.NoError(t, pool.QueryRow(ctx,
		`INSERT INTO users (supabase_user_id, email, full_name)
		 VALUES ($1, $2, $3) RETURNING id`,
		uuid.New(), email, role+" of "+slug,
	).Scan(&userID))

	_, err := pool.Exec(ctx,
		`INSERT INTO organization_memberships (user_id, organization_id, role)
		 VALUES ($1, $2, $3)`,
		userID, orgID, role,
	)
	require.NoError(t, err)
	return userID
}

// cleanupOrg deletes every row a test fixture created for org. It runs inside
// a transaction that sets app.current_tenant, so RLS allows the deletes on
// tenant-scoped tables (chunks, ingestion_runs, repositories, etc.). It is
// best-effort: errors from a single DELETE are logged-and-skipped so a
// failing test's cleanup does not overwrite the original failure.
//
// Each DELETE is wrapped in a SAVEPOINT so that one statement's failure
// (e.g., a future FK the fixture doesn't know about) does not abort the
// transaction and silently skip every subsequent DELETE — the bug the
// naive `continue`-on-error version had.
func cleanupOrg(ctx context.Context, pool *pgxpool.Pool, org *TestOrg) {
	if org == nil {
		return
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", org.ID)); err != nil {
		return
	}

	// Order matters: leaves first, then trunks.
	stmts := []struct {
		sql  string
		args []any
	}{
		{`DELETE FROM feedback WHERE retrieval_id IN (
			SELECT r.id FROM retrievals r
			JOIN queries q ON r.query_id = q.id
			WHERE q.project_id = $1)`, []any{org.ProjectID}},
		{`DELETE FROM retrievals WHERE query_id IN (SELECT id FROM queries WHERE project_id = $1)`, []any{org.ProjectID}},
		{`DELETE FROM queries WHERE project_id = $1`, []any{org.ProjectID}},
		{`DELETE FROM chunks WHERE repository_id IN (SELECT id FROM repositories WHERE project_id = $1)`, []any{org.ProjectID}},
		{`DELETE FROM ingestion_runs WHERE repository_id IN (SELECT id FROM repositories WHERE project_id = $1)`, []any{org.ProjectID}},
		{`DELETE FROM repositories WHERE project_id = $1`, []any{org.ProjectID}},
		{`DELETE FROM projects WHERE id = $1`, []any{org.ProjectID}},
		{`DELETE FROM organization_memberships WHERE organization_id = $1`, []any{org.ID}},
		{`DELETE FROM users WHERE id = ANY($1)`, []any{[]string{org.OwnerID, org.AdminID, org.MemberID}}},
		{`DELETE FROM organizations WHERE id = $1`, []any{org.ID}},
	}
	for _, s := range stmts {
		if _, err := tx.Exec(ctx, "SAVEPOINT cleanup_stmt"); err != nil {
			// Transaction is unusable; nothing more we can do.
			return
		}
		if _, err := tx.Exec(ctx, s.sql, s.args...); err != nil {
			// Un-poison the tx and keep going with the next stmt.
			_, _ = tx.Exec(ctx, "ROLLBACK TO SAVEPOINT cleanup_stmt")
			continue
		}
		_, _ = tx.Exec(ctx, "RELEASE SAVEPOINT cleanup_stmt")
	}
	_ = tx.Commit(ctx)
}

// sanitizeTag turns t.Name() into a slug fragment that satisfies the
// organizations.slug CHECK (lowercase [a-z0-9-]+).
func sanitizeTag(name string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(name) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '_', r == '/', r == ' ', r == '.':
			b.WriteByte('-')
		}
	}
	tag := b.String()
	tag = strings.Trim(tag, "-")
	if len(tag) > 30 {
		tag = tag[:30]
	}
	if tag == "" {
		tag = "test"
	}
	return tag
}

func shortToken() string {
	return strings.ReplaceAll(uuid.New().String(), "-", "")[:8]
}
