package isolation

import (
	"context"
	"fmt"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// TenantScope begins a transaction on pool and sets app.current_tenant to
// tenantID for the duration of that transaction. The caller is responsible
// for committing or rolling back.
//
// tenantID must be a valid UUID. Because Postgres' SET LOCAL does not accept
// bind parameters for GUC values, the id is interpolated into the statement
// after validation.
func TenantScope(ctx context.Context, pool *pgxpool.Pool, tenantID string) (pgx.Tx, error) {
	if _, err := uuid.Parse(tenantID); err != nil {
		return nil, fmt.Errorf("tenant id must be a valid uuid: %w", err)
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", tenantID)); err != nil {
		_ = tx.Rollback(ctx)
		return nil, fmt.Errorf("set app.current_tenant: %w", err)
	}
	return tx, nil
}

// CheckCrossTenantLeak runs `action` inside a transaction scoped to tenantA
// (committing on success), then runs `observe` in a fresh transaction scoped
// to tenantB. It returns a non-nil error if observe reports that tenantB was
// able to see the effect of tenantA's action — a cross-tenant leak.
//
// This is the pure-Go core of AssertNoCrossTenantLeak; expose it directly so
// the harness's own self-tests can verify both the positive and negative case
// without needing to intercept t.Error.
func CheckCrossTenantLeak(
	ctx context.Context,
	pool *pgxpool.Pool,
	tenantA, tenantB string,
	action func(tx pgx.Tx) error,
	observe func(tx pgx.Tx) (leaked bool, err error),
) error {
	if action == nil || observe == nil {
		return fmt.Errorf("action and observe must be non-nil")
	}

	txA, err := TenantScope(ctx, pool, tenantA)
	if err != nil {
		return fmt.Errorf("setup tenantA scope: %w", err)
	}
	if err := action(txA); err != nil {
		_ = txA.Rollback(ctx)
		return fmt.Errorf("action as tenantA=%s: %w", tenantA, err)
	}
	if err := txA.Commit(ctx); err != nil {
		return fmt.Errorf("commit tenantA: %w", err)
	}

	txB, err := TenantScope(ctx, pool, tenantB)
	if err != nil {
		return fmt.Errorf("setup tenantB scope: %w", err)
	}
	defer func() { _ = txB.Rollback(ctx) }()

	leaked, err := observe(txB)
	if err != nil {
		return fmt.Errorf("observe as tenantB=%s: %w", tenantB, err)
	}
	if leaked {
		return fmt.Errorf(
			"cross-tenant leak: tenantB (%s) observed an effect written by tenantA (%s)",
			tenantB, tenantA,
		)
	}
	return nil
}

// AssertNoCrossTenantLeak is the testing.T-flavored wrapper around
// CheckCrossTenantLeak. It marks itself as a test helper and calls t.Error
// with a detailed message when a leak is detected. Use it in any test that
// mutates or reads tenant-scoped data and needs to prove isolation.
func AssertNoCrossTenantLeak(
	t *testing.T,
	pool *pgxpool.Pool,
	tenantA, tenantB string,
	action func(tx pgx.Tx) error,
	observe func(tx pgx.Tx) (leaked bool, err error),
) {
	t.Helper()
	if err := CheckCrossTenantLeak(context.Background(), pool, tenantA, tenantB, action, observe); err != nil {
		t.Errorf("AssertNoCrossTenantLeak failed: %v", err)
	}
}
