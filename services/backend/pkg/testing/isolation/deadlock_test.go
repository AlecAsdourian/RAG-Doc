package isolation_test

// Self-tests for the ISS-032 retry (isolation.RetryOnLockContention).
//
// A retry nobody has watched retry is a `for` loop with a comment on it.
// Most of these drive it with synthetic errors, because the interesting
// cases (five deadlocks then a success) cannot be arranged on demand
// against a real server. `RecognisesARealPostgresDeadlock` below closes the
// one gap that matters — that an error PostgreSQL actually raises is
// classified as one — by causing a genuine cycle.

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

func lockTimeoutErr() error {
	return &pgconn.PgError{Code: "55P03", Message: "canceling statement due to lock timeout"}
}

func TestRetryOnLockContention_RetriesUntilItSucceeds(t *testing.T) {
	ctx := context.Background()

	// Both codes, so neither is retried only by accident. The deadlock case
	// goes all the way to the last attempt (the property that matters); the
	// lock-timeout case stops at two, because every extra exhausted run
	// costs the whole backoff schedule on every `go test ./...`.
	for _, tc := range []struct {
		name       string
		err        error
		succeedsAt int
	}{
		{"a deadlock, right up to the last attempt", deadlockErr(), isolation.LockContentionRetries},
		{"a lock timeout", lockTimeoutErr(), 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			err := isolation.RetryOnLockContention(ctx, func() error {
				calls++
				if calls < tc.succeedsAt {
					return tc.err
				}
				return nil
			})
			require.NoError(t, err)
			require.Equal(t, tc.succeedsAt, calls)
		})
	}
}

func TestRetryOnLockContention_GivesUpAndSaysWhy(t *testing.T) {
	ctx := context.Background()

	calls := 0
	err := isolation.RetryOnLockContention(ctx, func() error {
		calls++
		return deadlockErr()
	})
	require.Error(t, err, "a repeatable deadlock must still fail the test")
	require.ErrorContains(t, err, "deadlock detected")
	require.ErrorContains(t, err, "after 6 attempts")
	require.Equal(t, isolation.LockContentionRetries, calls)

	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "the SQLSTATE must survive wrapping")
	require.Equal(t, "40P01", pgErr.Code)
}

// Anything that is not lock contention is returned on the FIRST attempt.
// Retrying a real failure would turn a broken test into a slow broken test
// and, worse, could hide a defect that only shows on some attempts.
func TestRetryOnLockContention_DoesNotRetryAnythingElse(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		err  error
	}{
		{"a plain error", errors.New("assertion failed")},
		// 40001 is the OTHER transaction-rollback code and is deliberately
		// NOT retried: a serialization failure means this transaction read
		// something that changed, which is a statement about the data, not
		// about who won a lock race.
		{"a serialization failure", &pgconn.PgError{Code: "40001", Message: "could not serialize"}},
		{"a unique violation", &pgconn.PgError{Code: "23505"}},
		{"a wrapped non-contention error", fmt.Errorf("context: %w", &pgconn.PgError{Code: "42501"})},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			err := isolation.RetryOnLockContention(ctx, func() error {
				calls++
				return tc.err
			})
			require.ErrorIs(t, err, tc.err, "the error must come back unchanged")
			require.Equal(t, 1, calls, "only 40P01 and 55P03 are retried")
		})
	}
}

// Wrapped is still wrapped — pgx returns these wrapped, and
// `manufactureDriftAndCheck` passes them up through its own layers.
func TestIsDeadlock_SeesThroughWrapping(t *testing.T) {
	require.True(t, isolation.IsDeadlock(deadlockErr()))
	require.True(t, isolation.IsDeadlock(fmt.Errorf("manufacturing drift: %w", deadlockErr())))
	require.False(t, isolation.IsDeadlock(nil))
	require.False(t, isolation.IsDeadlock(errors.New("deadlock detected")),
		"the MESSAGE is not the test; the SQLSTATE is")
	require.False(t, isolation.IsDeadlock(&pgconn.PgError{Code: "40001"}))
	require.False(t, isolation.IsDeadlock(lockTimeoutErr()))

	require.True(t, isolation.IsLockTimeout(lockTimeoutErr()))
	require.True(t, isolation.IsLockTimeout(fmt.Errorf("wrapped: %w", lockTimeoutErr())))
	require.False(t, isolation.IsLockTimeout(deadlockErr()))
	require.False(t, isolation.IsLockTimeout(nil))
}

// The one thing the synthetic errors above cannot prove: that a deadlock
// PostgreSQL actually raises is classified as one. Two transactions taking
// two rows in opposite orders is the textbook cycle, and the server picks
// one of them as the victim.
func TestRetryOnLockContention_RecognisesARealPostgresDeadlock(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// `organizations` has no row-level security and no tenant trigger,
		// so two plain transactions can lock its rows in opposite orders
		// without any of this harness's machinery interfering. The updates
		// are no-ops in value terms, and both transactions are rolled back.
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

// `lock_timeout` below `deadlock_timeout` is the half of the ISS-032 fix
// that the retry cannot supply, so the SQLSTATE it produces is measured
// here rather than assumed: a transaction that gives up waiting gets 55P03,
// and 55P03 is what RetryOnLockContention treats as retryable.
func TestLockWaitTimeout_ProducesARetryableLockTimeout(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	require.Less(t, isolation.LockWaitTimeout, time.Second,
		"LockWaitTimeout must stay below PostgreSQL's default deadlock_timeout, "+
			"or the transaction waits long enough to be reported as a deadlock instead")

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		// Holder takes a row lock and keeps it.
		holder, err := pool.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = holder.Rollback(ctx) }()
		_, err = holder.Exec(ctx,
			`UPDATE organizations SET name = name WHERE id = $1`, orgA.ID)
		require.NoError(t, err)

		waiter, err := pool.Begin(ctx)
		require.NoError(t, err)
		defer func() { _ = waiter.Rollback(ctx) }()
		_, err = waiter.Exec(ctx, fmt.Sprintf("SET LOCAL lock_timeout = '%dms'",
			isolation.LockWaitTimeout.Milliseconds()))
		require.NoError(t, err)

		start := time.Now()
		_, err = waiter.Exec(ctx,
			`UPDATE organizations SET name = name WHERE id = $1`, orgA.ID)
		elapsed := time.Since(start)

		require.True(t, isolation.IsLockTimeout(err),
			"a contended write must give up with 55P03, not wait; got %v", err)
		require.Less(t, elapsed, 5*time.Second, "and it must give up promptly")

		require.NoError(t, waiter.Rollback(ctx))
		require.NoError(t, holder.Rollback(ctx))
	})
}
