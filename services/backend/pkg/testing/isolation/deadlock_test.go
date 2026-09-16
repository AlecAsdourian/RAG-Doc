package isolation_test

// Self-tests for the ISS-032 retry (isolation.RetryOnDeadlock).
//
// A retry nobody has watched retry is a `for` loop with a comment on it.
// These drive it with synthetic errors, because a REAL deadlock is exactly
// the thing that cannot be produced on demand — that is the whole reason
// ISS-032 was filed rather than reproduced: sixteen local runs of the
// failing command produced none.
//
// What that leaves untested here is the classification of a genuine
// PostgreSQL deadlock, and `TestRetryOnDeadlock_RecognisesARealPostgresDeadlock`
// below closes it by causing one.

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

func deadlockErr() error {
	return &pgconn.PgError{Code: "40P01", Message: "deadlock detected"}
}

func TestRetryOnDeadlock_RetriesUntilItSucceeds(t *testing.T) {
	ctx := context.Background()

	calls := 0
	err := isolation.RetryOnDeadlock(ctx, func() error {
		calls++
		if calls < isolation.DeadlockRetries {
			return deadlockErr()
		}
		return nil
	})
	require.NoError(t, err)
	require.Equal(t, isolation.DeadlockRetries, calls)
}

func TestRetryOnDeadlock_GivesUpAndSaysWhy(t *testing.T) {
	ctx := context.Background()

	calls := 0
	err := isolation.RetryOnDeadlock(ctx, func() error {
		calls++
		return deadlockErr()
	})
	require.Error(t, err, "a repeatable deadlock must still fail the test")
	require.ErrorContains(t, err, "deadlock detected")
	require.ErrorContains(t, err, "after 3 attempts")
	require.Equal(t, isolation.DeadlockRetries, calls)

	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "the SQLSTATE must survive wrapping")
	require.Equal(t, "40P01", pgErr.Code)
}

// Anything that is not a deadlock is returned on the FIRST attempt.
// Retrying a real failure would turn a broken test into a slow broken test
// and, worse, could hide a defect that only shows on some attempts.
func TestRetryOnDeadlock_DoesNotRetryAnythingElse(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		err  error
	}{
		{"a plain error", errors.New("assertion failed")},
		{"a serialization failure", &pgconn.PgError{Code: "40001", Message: "could not serialize"}},
		{"a lock-not-available", &pgconn.PgError{Code: "55P03", Message: "lock not available"}},
		{"a wrapped non-deadlock", fmt.Errorf("context: %w", &pgconn.PgError{Code: "23505"})},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			err := isolation.RetryOnDeadlock(ctx, func() error {
				calls++
				return tc.err
			})
			require.ErrorIs(t, err, tc.err, "the error must come back unchanged")
			require.Equal(t, 1, calls, "no retry for anything but 40P01")
		})
	}
}

// A wrapped deadlock is still a deadlock — pgx returns them wrapped, and
// `manufactureDriftAndCheck` passes them up through its own layers.
func TestIsDeadlock_SeesThroughWrapping(t *testing.T) {
	require.True(t, isolation.IsDeadlock(deadlockErr()))
	require.True(t, isolation.IsDeadlock(fmt.Errorf("manufacturing drift: %w", deadlockErr())))
	require.False(t, isolation.IsDeadlock(nil))
	require.False(t, isolation.IsDeadlock(errors.New("deadlock detected")),
		"the MESSAGE is not the test; the SQLSTATE is")
	require.False(t, isolation.IsDeadlock(&pgconn.PgError{Code: "40001"}))
}

// The one thing the synthetic errors above cannot prove: that a deadlock
// PostgreSQL actually raises is classified as one. Two transactions taking
// two rows in opposite orders is the textbook cycle, and the server picks
// one of them as the victim.
func TestRetryOnDeadlock_RecognisesARealPostgresDeadlock(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// `organizations` has no row-level security and no tenant trigger,
		// so two plain transactions can lock its rows in opposite orders
		// without any of this test's machinery interfering. The updates are
		// no-ops in value terms.
		lock := func(tx pgx.Tx, orgID string) error {
			_, err := tx.Exec(ctx,
				`UPDATE organizations SET name = name WHERE id = $1`, orgID)
			return err
		}

		tx1, err := pool.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = tx1.Rollback(ctx) }()
		tx2, err := pool.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = tx2.Rollback(ctx) }()

		require.NoError(t, lock(tx1, orgA.ID))
		require.NoError(t, lock(tx2, orgB.ID))

		// Now cross them. One of the two is chosen as the victim and gets
		// 40P01; the other waits for the victim's rollback and succeeds.
		var wg sync.WaitGroup
		errs := make([]error, 2)
		wg.Add(2)
		go func() { defer wg.Done(); errs[0] = lock(tx1, orgB.ID) }()
		go func() { defer wg.Done(); errs[1] = lock(tx2, orgA.ID) }()

		waited := make(chan struct{})
		go func() { wg.Wait(); close(waited) }()
		select {
		case <-waited:
		case <-time.After(30 * time.Second):
			t.Fatal("the two transactions neither deadlocked nor completed")
		}

		var victims int
		for _, err := range errs {
			if isolation.IsDeadlock(err) {
				victims++
			}
		}
		require.Equal(t, 1, victims,
			"exactly one of the two must be the deadlock victim; got %v and %v", errs[0], errs[1])

		require.NoError(t, tx1.Rollback(ctx))
		require.NoError(t, tx2.Rollback(ctx))
	})
}
