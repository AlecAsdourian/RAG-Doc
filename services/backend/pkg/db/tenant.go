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

// ErrCommitFailed wraps a failure to commit the tenant transaction.
//
// It is distinguishable from an error the handler's callback returned,
// and the distinction matters: a commit failure means the callback
// SUCCEEDED and its work was then discarded. A handler that has already
// decided to return 201 needs to know that the row it just wrote is not
// there.
//
// The temptation is to treat any non-nil error from InTenantTx the same
// way. That is fine for a handler that renders its response after
// InTenantTx returns — the pattern docs/isolation.md recommends — and
// wrong for one that renders inside the callback.
var ErrCommitFailed = errors.New("db: tenant transaction failed to commit")

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
	//
	// WithoutCancel because ctx is the request context. On a client
	// disconnect it is already cancelled, Rollback fails, and pgx marks the
	// connection dead and discards it — correct, but it means a burst of
	// disconnects churns the pool. Detaching lets the rollback complete and
	// the connection go back.
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()

	// SET LOCAL cannot take a bind parameter (pgx's extended protocol
	// rejects a parameterized SET, and Postgres would not accept one
	// anyway), so the id is concatenated. tenantFromContext guarantees it
	// is the canonical 36-character form — see the comment there, which
	// explains why "it parsed as a UUID" would NOT have been enough.
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
		// Wrapped in a sentinel so a caller can distinguish this from an
		// error its own callback returned. Reaching here means the callback
		// succeeded and its work was then thrown away — the one case where
		// "the handler failed" is the wrong thing to tell the user.
		return fmt.Errorf("%w: %w", ErrCommitFailed, err)
	}
	return nil
}

// tenantFromContext extracts the caller's organization and returns it in
// canonical form, or an error.
//
// It duplicates a check auth.ExtractOrganizationID already performs. That
// is deliberate: the value is about to be concatenated into SQL, and this
// is the only place in the codebase where that happens. It must be
// defensible reading this function alone, not by tracing three layers up
// to a guarantee a future change could weaken.
//
// REQUIRING THE CANONICAL FORM IS THE SAFETY PROPERTY, not the parse.
// `uuid.Parse` is a parser, not a validator — its own documentation says
// "Parse should not be used to validate strings as it parses non-standard
// encodings". For 38-character input it strips the first and last bytes
// without checking them, so all of these parse successfully:
//
//	'11111111-1111-1111-1111-111111111111;      (quote and semicolon!)
//	{11111111-1111-1111-1111-111111111111}
//	urn:uuid:11111111-1111-1111-1111-111111111111
//	11111111111111111111111111111111            (no dashes)
//	11111111-1111-1111-1111-11111111111A        (uppercase)
//
// Interpolating the RAW string after a successful parse would put those
// first two into SQL. Returning parsed.String() normalizes all of them to
// `[0-9a-f-]{36}`, which is what actually makes the concatenation safe.
//
// The equality check below makes that dependency explicit rather than
// implicit. Without it, "simplifying" this to `return raw, nil` — keeping
// the parse, so it still looks validated — would compile, pass every
// test, and reopen the hole. A reviewer measured exactly that mutation
// surviving the suite.
func tenantFromContext(ctx context.Context) (string, error) {
	raw, ok := auth.OrgIDFromContext(ctx)
	if !ok || raw == "" {
		return "", ErrNoTenant
	}
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("db: organization id on context is not a valid UUID: %w", err)
	}
	canonical := parsed.String()
	if canonical != raw {
		// Not merely pedantic. A non-canonical value here means something
		// upstream stopped canonicalizing (auth.ExtractOrganizationID does
		// today), and the difference between "parsed" and "canonical" is
		// exactly where injection would live.
		return "", fmt.Errorf(
			"db: organization id on context is not in canonical form (got %q, canonical %q)",
			raw, canonical)
	}
	return canonical, nil
}
