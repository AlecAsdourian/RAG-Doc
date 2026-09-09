package db_test

// Tests for the primitive every Phase 20+ handler stands on.
//
// Two halves, and the second is the one that earns its keep:
//
//  1. TenantScoper scopes correctly — a caller sees and writes only their
//     own tenant's rows.
//  2. BYPASSING it fails. Those tests document, in executable form, what
//     happens to a handler that queries the pool directly. An unscoped
//     write is refused loudly by the 000009 trigger; an unscoped READ
//     returns zero rows with no error, which is the failure mode that
//     looks like a working endpoint returning an empty list.
//
// The second half is why TenantScoper does not expose a pool. Someone will
// eventually wonder why they cannot just query directly; these tests are
// the answer.

import (
	"context"
	"errors"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

// ctxForTenant builds the request context a handler would see: the
// organization id under auth.OrgIDKey, exactly as auth.TenantMiddleware
// puts it there from a verified JWT claim.
func ctxForTenant(orgID string) context.Context {
	return context.WithValue(context.Background(), auth.OrgIDKey, orgID)
}

func TestTenantScoper_ScopesReadsToTheCallersTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)

		// WithTwoOrgs gives each org one repository. Under RLS, a scoped
		// read must see exactly its own.
		var seenByA []string
		require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
			rows, err := tx.Query(context.Background(), `SELECT id::text FROM repositories`)
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				var id string
				if err := rows.Scan(&id); err != nil {
					return err
				}
				seenByA = append(seenByA, id)
			}
			return rows.Err()
		}))

		require.Equal(t, []string{orgA.RepoID}, seenByA,
			"a scoped read must return exactly the caller's own repositories")
		require.NotContains(t, seenByA, orgB.RepoID,
			"cross-tenant leak: orgA saw orgB's repository")
	})
}

func TestTenantScoper_ScopesWritesToTheCallersTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)

		// A write inside orgA's scope lands, and orgB cannot see it.
		require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
			_, err := tx.Exec(context.Background(),
				`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
				 VALUES ($1, $2, $3, $4)`,
				orgA.RepoID, "abc123", "main", "completed")
			return err
		}))

		var visibleToB int
		require.NoError(t, scoper.InTenantTx(ctxForTenant(orgB.ID), func(tx pgx.Tx) error {
			return tx.QueryRow(context.Background(),
				`SELECT count(*) FROM ingestion_runs`).Scan(&visibleToB)
		}))
		require.Zero(t, visibleToB,
			"cross-tenant leak: orgB saw an ingestion_run written under orgA's scope")
	})
}

// TestTenantScoper_RollsBackOnError guards the property that makes the
// callback shape safe: a handler returning an error must not leave a
// partial write behind.
func TestTenantScoper_RollsBackOnError(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)
		sentinel := errors.New("handler failed after writing")

		err := scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
			if _, err := tx.Exec(context.Background(),
				`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
				 VALUES ($1, $2, $3, $4)`,
				orgA.RepoID, "rollback-me", "main", "processing"); err != nil {
				return err
			}
			return sentinel
		})
		require.ErrorIs(t, err, sentinel, "the handler's error must reach the caller unwrapped")

		var count int
		require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
			return tx.QueryRow(context.Background(),
				`SELECT count(*) FROM ingestion_runs WHERE commit_sha = 'rollback-me'`).Scan(&count)
		}))
		require.Zero(t, count, "a failed handler must not leave a committed row")
	})
}

// TestTenantScoper_RefusesRequestWithoutTenant covers the wiring-bug case:
// a route mounted outside TenantMiddleware.
func TestTenantScoper_RefusesRequestWithoutTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	scoper := db.NewTenantScoper(pool)

	called := false
	err := scoper.InTenantTx(context.Background(), func(pgx.Tx) error {
		called = true
		return nil
	})

	require.ErrorIs(t, err, db.ErrNoTenant)
	require.False(t, called,
		"the callback must not run without a tenant — it would query unscoped")
}

// TestTenantScoper_RefusesNonUUIDTenant pins the guard at the interpolation
// site. The value is concatenated into SQL because SET LOCAL cannot take a
// bind parameter, so this is the one place in the codebase where that
// happens and it must be defensible on its own.
func TestTenantScoper_RefusesNonUUIDTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	scoper := db.NewTenantScoper(pool)

	for _, bad := range []string{
		"not-a-uuid",
		"' OR '1'='1",
		"11111111-1111-1111-1111-111111111111'; DROP TABLE repositories; --",
	} {
		t.Run(bad, func(t *testing.T) {
			called := false
			err := scoper.InTenantTx(ctxForTenant(bad), func(pgx.Tx) error {
				called = true
				return nil
			})
			require.Error(t, err)
			require.False(t, called, "the callback must not run for a malformed tenant id")
		})
	}
}

// --- The other half: what happens when you DON'T use the scoper ---------

// TestUnscopedAccess_BehaviourDependsOnConnectionHistory is the reason
// TenantScoper does not hand out a pool — and the result is stranger than
// the design note originally assumed.
//
// The RLS policies compare against
// current_setting('app.current_tenant', true)::uuid. The `true` means
// "missing is OK", but Postgres has two kinds of missing on a pooled
// connection:
//
//	never scoped ................. NULL -> NULL::uuid -> 0 rows, NO ERROR
//	scoped once, then committed .. ""   -> ""::uuid   -> ERROR 22P02
//
// A committed SET LOCAL leaves the GUC as an empty string on that backend
// for the rest of its life. RESET and SET TO DEFAULT do not clear it —
// established in 17-02 and re-confirmed here.
//
// So an unscoped query on a tenant-scoped table is silently empty OR a
// 500, depending on which pooled connection it gets and what that
// connection did earlier. That is worse than either outcome alone: it
// passes in a fresh test process and fails in production under load.
//
// This test pins BOTH halves by holding one connection, so the
// nondeterminism is demonstrated rather than described.
//
// Filed as ISS-013, deliberately not fixed here: making the policies
// deterministic means a migration across all six tenant-scoped tables, and
// choosing between "always silent" and "always loud" deserves its own
// decision rather than being made inside a plan about something else.
func TestUnscopedAccess_BehaviourDependsOnConnectionHistory(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		// The row demonstrably exists when read through a scope.
		scoper := db.NewTenantScoper(pool)
		var scopedCount int
		require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
			return tx.QueryRow(ctx, `SELECT count(*) FROM repositories`).Scan(&scopedCount)
		}))
		require.Positive(t, scopedCount, "precondition: orgA has repositories")

		t.Run("never scoped: silently empty, no error", func(t *testing.T) {
			// A SECOND pool, because `pool` cannot be relied on to hand out
			// a connection that has never been scoped — the fixtures and
			// the InTenantTx above have already used several of its
			// connections, and which one you get back is not yours to
			// choose. That unpredictability is precisely the finding this
			// test records; here it just has to be worked around.
			//
			// SetupTestDB opens a fresh pool against the same container, so
			// its backends have no GUC history.
			virgin := isolation.SetupTestDB(t)

			conn, err := virgin.Acquire(ctx)
			require.NoError(t, err)
			defer conn.Release()

			var v *string
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT current_setting('app.current_tenant', true)`).Scan(&v))
			require.Nil(t, v, "precondition: this connection has never been scoped")

			var n int
			err = conn.QueryRow(ctx, `SELECT count(*) FROM repositories`).Scan(&n)
			require.NoError(t, err,
				"THIS is the hazard: an unscoped read does not error on a virgin connection")
			require.Zero(t, n,
				"it returns zero rows — indistinguishable from a tenant that owns nothing")
		})

		t.Run("previously scoped: 22P02, an unexplained 500", func(t *testing.T) {
			// Pin one connection and give it a history.
			conn, err := pool.Acquire(ctx)
			require.NoError(t, err)
			defer conn.Release()

			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			_, err = tx.Exec(ctx, `SET LOCAL app.current_tenant = '`+orgA.ID+`'`)
			require.NoError(t, err)
			require.NoError(t, tx.Commit(ctx))

			var v *string
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT current_setting('app.current_tenant', true)`).Scan(&v))
			require.NotNil(t, v)
			require.Equal(t, "", *v,
				"a committed SET LOCAL leaves an empty string, not NULL — the 17-02 quirk")

			var n int
			err = conn.QueryRow(ctx, `SELECT count(*) FROM repositories`).Scan(&n)
			require.Error(t, err, "the SAME query now fails on the SAME connection")

			var pgErr *pgconn.PgError
			require.True(t, errors.As(err, &pgErr), "expected a Postgres error, got %T", err)
			require.Equal(t, "22P02", pgErr.Code,
				"an empty string is an invalid text representation for uuid")
		})
	})
}

// TestUnscopedWrite_IsRefusedNotSilent complements the read case.
//
// A write never passes quietly. It is refused either by RLS's WITH CHECK
// (22P02 on a previously-scoped connection) or by migration 000009's
// assert_tenant_scoped trigger (42501 on a virgin one). Which one depends
// on the same connection history as above — but unlike a read, BOTH
// outcomes are loud, so a handler that forgets the scope on a write finds
// out immediately.
func TestUnscopedWrite_IsRefusedNotSilent(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		_, err := pool.Exec(context.Background(),
			`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
			 VALUES ($1, $2, $3, $4)`,
			orgA.RepoID, "unscoped", "main", "processing")

		require.Error(t, err, "an unscoped write must never succeed")

		var pgErr *pgconn.PgError
		require.True(t, errors.As(err, &pgErr), "expected a Postgres error, got %T", err)
		require.Containsf(t, []string{"42501", "22P02"}, pgErr.Code,
			"expected the 000009 trigger (42501) or the RLS uuid cast (22P02), got %s: %v",
			pgErr.Code, err)
	})
}
