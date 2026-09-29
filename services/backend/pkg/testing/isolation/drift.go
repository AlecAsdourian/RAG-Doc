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
	if err := requireRLSBypass(ctx, q); err != nil {
		return nil, err
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

// chunkTenantDriftSQL is D5's drift-detection query for the two tables
// 000017 (22-02) keys to repositories. Four arms, each row tagged
// "<table>:<id>:<reason>":
//
//   - chunks:<id>:tenant and symbols:<id>:tenant. The row's organization_id
//     disagrees with its repository's, or the repository is gone (a row
//     with no repository has no truth to agree with). Unrepresentable
//     while chunks_repo_tenant_fk and symbols_repo_tenant_fk are enforced,
//     and those are foreign-key triggers: `session_replication_role =
//     replica` and `DISABLE TRIGGER` switch them off, and a misfiled row
//     then inserts cleanly (22-CONTEXT P3's correction, measured).
//   - chunks:<id>:run. The chunk's run belongs to another repository, or
//     is gone. The single-column key ingestion_run_id carries NO TENANCY:
//     per-row foreign-key checks bypass row-level security, so as tenant A
//     a chunk of A's repository citing B's run is ACCEPTED, no bypass
//     needed (PR #49's review, measured in the deployment shape). Not a
//     breach, since A can only mis-file its own row and B cannot read it,
//     but B deleting its run then cascades into A's partition, and
//     neither key sees it. ISS-036 carries the composite keys that close
//     it when 22.1-01 gives symbols a writer.
//   - chunks:<id>:symbol. The same for symbol_id, where it is set.
//
// `chunks` is read without ONLY, so every partition's rows are checked.
// Ordered in the "C" collation, byte by byte, so the result matches Go's
// string order whatever the database's default collation makes of the
// hyphens in a uuid. (`ORDER BY 1 COLLATE "C"` is not that: the ordinal
// is an integer there, and integers take no collation, 42804.)
const chunkTenantDriftSQL = `
SELECT drift FROM (
  SELECT 'chunks:' || c.id::text || ':tenant' AS drift
  FROM chunks c
  LEFT JOIN repositories r ON r.id = c.repository_id
  WHERE r.id IS NULL OR c.organization_id IS DISTINCT FROM r.organization_id
  UNION ALL
  SELECT 'symbols:' || s.id::text || ':tenant'
  FROM symbols s
  LEFT JOIN repositories r ON r.id = s.repository_id
  WHERE r.id IS NULL OR s.organization_id IS DISTINCT FROM r.organization_id
  UNION ALL
  SELECT 'chunks:' || c.id::text || ':run'
  FROM chunks c
  LEFT JOIN ingestion_runs ir ON ir.id = c.ingestion_run_id
  WHERE ir.id IS NULL OR ir.repository_id IS DISTINCT FROM c.repository_id
  UNION ALL
  SELECT 'chunks:' || c.id::text || ':symbol'
  FROM chunks c
  LEFT JOIN symbols s ON s.id = c.symbol_id
  WHERE c.symbol_id IS NOT NULL
    AND (s.id IS NULL OR s.repository_id IS DISTINCT FROM c.repository_id)
) d
ORDER BY drift COLLATE "C"`

// CheckChunkTenantDrift returns every chunk and symbol that is out of step
// with the rows it points at, as "<table>:<id>:<reason>" (see
// chunkTenantDriftSQL for the four reasons; migration 000017). An empty
// slice means no drift.
//
// Like CheckRepositoryTenantDrift it REFUSES to run under a role that
// row-level security applies to: every partition of chunks, and symbols,
// forces the tenant policy, so such a role would read one tenant's rows, or
// none, and report "no drift" about tables it never read.
func CheckChunkTenantDrift(ctx context.Context, q Querier) ([]string, error) {
	if err := requireRLSBypass(ctx, q); err != nil {
		return nil, err
	}
	rows, err := q.Query(ctx, chunkTenantDriftSQL)
	if err != nil {
		return nil, fmt.Errorf("run chunk drift query: %w", err)
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, fmt.Errorf("read chunk drift rows: %w", err)
	}
	return ids, nil
}

// AssertNoChunkTenantDrift fails the test if any chunk or symbol carries an
// organization_id that is not its repository's, or cites a run or symbol of
// another repository, across every partition. It is the CI half of
// 22-CONTEXT P3: the composite keys make tenant drift unrepresentable only
// while foreign-key triggers are enabled, and the run and symbol keys carry
// no tenancy at all (ISS-036), so tests that write chunks or symbols call
// this at the end.
func AssertNoChunkTenantDrift(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
		t.Helper()
		ids, err := CheckChunkTenantDrift(context.Background(), conn)
		if err != nil {
			t.Fatalf("isolation: chunk tenant drift check: %v", err)
		}
		if len(ids) > 0 {
			t.Errorf("chunk tenant drift (D5, P3, ISS-036): %d chunks or symbols out of step with "+
				"their repository, run or symbol: %v", len(ids), ids)
		}
	})
}

// requireRLSBypass returns an error unless the current role bypasses
// row-level security. A drift check under RLS is the vacuous pass 21-01's
// migration comment warns about.
func requireRLSBypass(ctx context.Context, q Querier) error {
	var bypassesRLS bool
	if err := q.QueryRow(ctx,
		`SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user`,
	).Scan(&bypassesRLS); err != nil {
		return fmt.Errorf("read current role: %w", err)
	}
	if !bypassesRLS {
		return fmt.Errorf("drift check must run as a role that bypasses row-level security: " +
			"under RLS it sees only the current tenant's rows and passes without reading the rest")
	}
	return nil
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
