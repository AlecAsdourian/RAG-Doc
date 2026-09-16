package isolation

import (
	"context"
	"fmt"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Querier is what CheckRepositoryTenantDrift reads through: a *pgx.Conn or a
// pgx.Tx. Accepting a transaction lets a self-test observe drift it has
// manufactured and will roll back.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// repositoryTenantDriftSQL is DECISIONS.md D5's drift-detection query for
// repositories: every row whose stored organization_id disagrees with the
// organization its project belongs to.
const repositoryTenantDriftSQL = `
SELECT r.id::text
FROM repositories r
JOIN projects p ON p.id = r.project_id
WHERE r.organization_id IS DISTINCT FROM p.organization_id
ORDER BY r.id`

// CheckRepositoryTenantDrift returns the id of every repository whose
// organization_id disagrees with its project's (migration 000013). An empty
// slice means no drift.
//
// It REFUSES to run under a role that row-level security applies to. There
// it would see one tenant's rows, or none, and report "no drift" about a
// table it never read, which is the same vacuous pass 21-01's migration
// comment warns about. AssertNoRepositoryTenantDrift arranges a superuser
// connection; a caller passing its own Querier must do the same.
func CheckRepositoryTenantDrift(ctx context.Context, q Querier) ([]string, error) {
	var bypassesRLS bool
	if err := q.QueryRow(ctx,
		`SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user`,
	).Scan(&bypassesRLS); err != nil {
		return nil, fmt.Errorf("read current role: %w", err)
	}
	if !bypassesRLS {
		return nil, fmt.Errorf("drift check must run as a role that bypasses row-level security: " +
			"under RLS it sees only the current tenant's repositories and passes without reading the rest")
	}

	rows, err := q.Query(ctx, repositoryTenantDriftSQL)
	if err != nil {
		return nil, fmt.Errorf("run drift query: %w", err)
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, fmt.Errorf("read drift rows: %w", err)
	}
	return ids, nil
}

// AssertNoRepositoryTenantDrift fails the test if any repository's stored
// organization_id disagrees with its project's, across the whole table.
// It is D5's drift-detection check, run in CI through every test that calls
// it. Later plans that write repositories, or tables keyed to them, call it
// at the end of their tests.
//
// The composite foreign key repositories_project_org_fkey makes drift
// unrepresentable, so this should never fire. It exists to catch the day
// that stops being true — a dropped constraint, a trigger-disabled load.
func AssertNoRepositoryTenantDrift(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
		t.Helper()
		ids, err := CheckRepositoryTenantDrift(context.Background(), conn)
		if err != nil {
			t.Fatalf("isolation: repository tenant drift check: %v", err)
		}
		if len(ids) > 0 {
			t.Errorf("repository tenant drift (D5): %d repositories whose organization_id "+
				"disagrees with their project's: %v", len(ids), ids)
		}
	})
}

// WithSuperuserConn runs fn on a dedicated connection taken out of pool and
// switched back to the session user, which in this harness is the
// `isolation` superuser (container.go connects as it, then SET ROLE).
//
// The connection is HIJACKED: it never returns to the pool, and is closed
// when fn returns, including when fn calls t.FailNow. A later test
// that expects every pooled connection to run as rag_doc_app therefore
// cannot be handed this one.
//
// Superusers bypass row-level security but not triggers: a write to a
// tenant-scoped table still needs app.current_tenant set (000009).
func WithSuperuserConn(t *testing.T, pool *pgxpool.Pool, fn func(conn *pgx.Conn)) {
	t.Helper()
	ctx := context.Background()

	pooled, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("isolation: acquire connection: %v", err)
	}
	conn := pooled.Hijack()
	defer func() { _ = conn.Close(ctx) }()

	if _, err := conn.Exec(ctx, "RESET ROLE"); err != nil {
		t.Fatalf("isolation: RESET ROLE: %v", err)
	}
	var superuser bool
	if err := conn.QueryRow(ctx,
		`SELECT rolsuper FROM pg_roles WHERE rolname = current_user`,
	).Scan(&superuser); err != nil {
		t.Fatalf("isolation: read role after RESET ROLE: %v", err)
	}
	if !superuser {
		t.Fatalf("isolation: RESET ROLE did not yield a superuser session")
	}

	fn(conn)
}
