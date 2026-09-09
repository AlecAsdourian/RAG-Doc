package db_test

// Properties of the transaction itself, as opposed to what it scopes.
//
// Every test here uses a MaxConns=1 pool. That is not incidental — it is
// what makes these deterministic. On a normal pool you cannot say which
// connection a query lands on, so "the tenant leaked onto the connection"
// and "the transaction was never returned" both become "sometimes fails,
// depending on ordering". With exactly one connection, both are decidable.
//
// A review found each of the three properties below untested: mutating
// SET LOCAL to SET, or deleting the rollback defer, produced either no
// failure at all or a failure in an unrelated assertion whose message
// pointed somewhere else entirely.

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

// singleConnPool returns a pool with exactly one connection to the shared
// test container, so a leaked connection or a leaked GUC is observable
// rather than probabilistic.
func singleConnPool(t *testing.T) *pgxpool.Pool {
	t.Helper()

	cfg := isolation.SetupTestDB(t).Config().Copy()
	cfg.MaxConns = 1
	cfg.MinConns = 0

	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	require.NoError(t, err)
	t.Cleanup(pool.Close)
	return pool
}

// TestInTenantTx_TenantDoesNotOutliveTheTransaction is the guard on the
// word LOCAL.
//
// `SET LOCAL` reverts at the end of the transaction; a plain `SET` would
// persist for the life of the pooled connection. With the latter, request
// N's tenant stays set, and any later query on that connection outside a
// scope reads request N's organization — a silent cross-tenant read, which
// is the precise failure this whole type exists to prevent.
//
// Before this test existed, changing LOCAL to SET was caught only
// incidentally, by an assertion about a Postgres quirk whose failure
// message said nothing about tenants outliving transactions.
func TestInTenantTx_TenantDoesNotOutliveTheTransaction(t *testing.T) {
	pool := singleConnPool(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)

		require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgA.ID),
			func(tx pgx.Tx) error {
				var inside string
				return tx.QueryRow(ctx,
					`SELECT current_setting('app.current_tenant', true)`).Scan(&inside)
			}))

		// Same pool, one connection, so necessarily the same backend.
		var after *string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT current_setting('app.current_tenant', true)`).Scan(&after))

		require.NotNil(t, after,
			"expected the empty-string remnant of a committed SET LOCAL, got NULL")
		require.Equalf(t, "", *after,
			"app.current_tenant OUTLIVED InTenantTx (still %q). The SET is not LOCAL, so "+
				"the next request on this pooled connection inherits organization %s.",
			derefOr(after, ""), orgA.ID)
	})
}

// TestInTenantTx_ReturnsTheConnectionOnEveryPath covers the rollback
// defer.
//
// Deleting it does not fail an assertion anywhere — it leaves an "idle in
// transaction" backend holding row locks, so the NEXT thing to want that
// connection blocks. On a normal pool that surfaces as the suite hanging
// until the 15-minute CI timeout and reporting "test timed out", which
// reads as flaky infrastructure rather than a missing rollback.
//
// With one connection and a short deadline it is a clean, named failure.
func TestInTenantTx_ReturnsTheConnectionOnEveryPath(t *testing.T) {
	isolation.WithTwoOrgs(t, singleConnPool(t), func(orgA, _ *isolation.TestOrg) {
		// WithTwoOrgs needs its own pool for fixtures/cleanup; take a
		// separate single-conn pool for the scoper so the assertion below
		// is about the scoper's connection and nothing else.
		pool := singleConnPool(t)
		scoper := db.NewTenantScoper(pool)
		ctx := context.Background()
		tenantCtx := auth.ContextWithOrgID(ctx, orgA.ID)

		for _, tc := range []struct {
			name string
			fn   func(pgx.Tx) error
		}{
			{"callback succeeds", func(pgx.Tx) error { return nil }},
			{"callback fails", func(pgx.Tx) error { return errors.New("boom") }},
			{"callback writes then fails", func(tx pgx.Tx) error {
				_, _ = tx.Exec(ctx,
					`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
					 VALUES ($1, $2, $3, $4)`,
					orgA.RepoID, "leak-check", "main", "processing")
				return errors.New("boom after write")
			}},
			{"callback panics", func(pgx.Tx) error { panic("handler panicked") }},
		} {
			t.Run(tc.name, func(t *testing.T) {
				func() {
					defer func() { _ = recover() }()
					_ = scoper.InTenantTx(tenantCtx, tc.fn)
				}()

				// If the connection was not returned, this blocks.
				acquireCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				defer cancel()

				conn, err := pool.Acquire(acquireCtx)
				require.NoErrorf(t, err,
					"the only pooled connection was never returned after %q: "+
						"InTenantTx leaked the transaction", tc.name)
				conn.Release()
			})
		}
	})
}

// TestInTenantTx_CommitFailureIsDistinguishable pins that a caller can
// tell "your write did not land" apart from "your callback failed".
//
// Without a sentinel, a handler that renders 201 inside the callback and
// then hits a failed commit reports success for a write that was rolled
// back. Swallowing the commit error entirely — `_ = tx.Commit(ctx)` —
// previously survived the whole suite.
func TestInTenantTx_CommitFailureIsDistinguishable(t *testing.T) {
	pool := singleConnPool(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)

		// Commit fails when the callback has already ended the transaction.
		err := scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgA.ID), func(tx pgx.Tx) error {
			return tx.Rollback(ctx)
		})

		require.Error(t, err, "a failed commit must not be reported as success")
		require.ErrorIs(t, err, db.ErrCommitFailed,
			"the caller must be able to tell a commit failure from a handler error, "+
				"or it will report 201 for a write that did not land")
	})
}

func derefOr(s *string, fallback string) string {
	if s == nil {
		return fallback
	}
	return *s
}
