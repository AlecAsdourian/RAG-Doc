// Package db provides the database access primitives that carry tenant
// scope.
//
// The one type here, TenantScoper, is how a Go handler reads or writes a
// tenant-scoped table. See docs/isolation.md for where it sits in the
// three walls, and 20-01-DESIGN.md for why it is shaped this way rather
// than as middleware.
package db

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
)

// ErrNoTenant is returned when the request context carries no organization.
//
// This is a wiring bug, not a user error: JWTAuthMiddleware and
// TenantMiddleware populate the context, and a request that reached a
// tenant-scoped handler without them means the route is mounted in the
// wrong group. Handlers should surface it as a 500, not a 403 — a caller
// with no organization claim is already refused by TenantMiddleware and
// never gets this far.
var ErrNoTenant = errors.New("db: no organization on request context; " +
	"route is not behind TenantMiddleware")

// TenantScoper opens transactions with `app.current_tenant` set, which is
// what makes the RLS policies in migration 000008 and the
// assert_tenant_scoped trigger in 000009 resolve to the caller's own rows.
//
// It deliberately exposes ONE method, and that method always sets the
// tenant. There is no unscoped path through this type.
//
// That matters because of how the two failure modes differ. An unscoped
// WRITE fails loudly — the 000009 trigger refuses it with SQLSTATE 42501.
// An unscoped READ does not: RLS answers it with zero rows and no error,
// so the endpoint returns an empty list and looks like it works. A handler
// that holds a *pgxpool.Pool can make that mistake silently; a handler
// that holds a *TenantScoper cannot make it at all.
//
// So tenant-scoped handlers are constructed with a *TenantScoper and NOT
// a pool. Giving one a pool is then a visible act in router.go rather than
// an omission buried in a handler.
type TenantScoper struct {
	pool *pgxpool.Pool
}

// NewTenantScoper wraps a pool.
func NewTenantScoper(pool *pgxpool.Pool) *TenantScoper {
	if pool == nil {
		panic("db.NewTenantScoper: pool is nil")
	}
	return &TenantScoper{pool: pool}
}

// InTenantTx runs fn inside a transaction scoped to the caller's
// organization, committing if fn returns nil and rolling back otherwise.
//
// The tenant comes from ctx (populated by auth.TenantMiddleware from a
// Supabase-signed JWT claim) and from nowhere else. There is deliberately
// no parameter through which a caller can name a tenant: that is the
// X-Organization-ID vulnerability 19-03 removed, wearing a different hat.
//
// fn must use the supplied tx for every query. A query issued against the
// pool inside fn runs on a different connection with no tenant set, which
// is the silent-empty-read failure this type exists to prevent.
func (s *TenantScoper) InTenantTx(ctx context.Context, fn func(pgx.Tx) error) error {
	if fn == nil {
		return errors.New("db: InTenantTx requires a non-nil function")
	}

	tenantID, err := tenantFromContext(ctx)
	if err != nil {
		return err
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("db: begin tenant tx: %w", err)
	}
	// Safe after Commit — rolling back a committed transaction is a no-op.
	defer func() { _ = tx.Rollback(ctx) }()

	// SET LOCAL cannot take a bind parameter (pgx's extended protocol
	// rejects a parameterized SET, and Postgres would not accept one
	// anyway), so the id is concatenated. tenantFromContext has already
	// proven it is a UUID — see the comment there for why that check is
	// repeated rather than trusted from upstream.
	//
	// LOCAL, not SESSION: the setting must die with the transaction. A
	// session-scoped value would outlive this request on a pooled
	// connection and silently scope the NEXT request to this tenant.
	if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", tenantID)); err != nil {
		return fmt.Errorf("db: set app.current_tenant: %w", err)
	}

	if err := fn(tx); err != nil {
		return err
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("db: commit tenant tx: %w", err)
	}
	return nil
}

// tenantFromContext extracts and re-validates the caller's organization.
//
// The UUID check duplicates one auth.ExtractOrganizationID already
// performs. That is deliberate: the value is about to be concatenated
// into SQL, and this is the only place in the codebase where that
// happens. It should be defensible reading this function alone, rather
// than by tracing three layers up to a guarantee a future change could
// weaken without anyone noticing the connection.
func tenantFromContext(ctx context.Context) (string, error) {
	raw, ok := ctx.Value(auth.OrgIDKey).(string)
	if !ok || raw == "" {
		return "", ErrNoTenant
	}
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("db: organization id on context is not a valid UUID: %w", err)
	}
	return parsed.String(), nil
}
