package isolation

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// The two SQLSTATEs a transaction gets when it loses a fight over table
// locks rather than doing anything wrong.
const (
	// deadlockDetected: Postgres found a lock cycle and rolled THIS
	// transaction back as the victim. The other side proceeds.
	deadlockDetected = "40P01"
	// lockNotAvailable: `lock_timeout` elapsed while waiting. Nothing was
	// rolled back by anyone else; we gave up.
	lockNotAvailable = "55P03"
)

// LockContentionRetries is how many attempts RetryOnLockContention makes.
//
// SIX, NOT THREE, AND THE DIFFERENCE IS MEASURED. Three attempts with a
// 100ms linear backoff was the first cut, and it failed: running
// `./pkg/api/... ./pkg/auth/... ./pkg/db/... ./pkg/jobs/... ./pkg/testing/...`
// at default parallelism, `-count=3`, the drift self-test deadlocked three
// times in a row and reported "still deadlocking after 3 attempts". Under
// four packages' worth of sustained traffic against `repositories` and
// `projects`, a fixed short backoff just lands in the next burst.
const LockContentionRetries = 6

// LockWaitTimeout is what a caller sets as `lock_timeout` on the
// transaction it is about to retry.
//
// ⚠ IT IS DELIBERATELY BELOW PostgreSQL's DEFAULT `deadlock_timeout` OF ONE
// SECOND. The deadlock detector does not run until a transaction has been
// waiting for `deadlock_timeout`; a transaction that gives up before then
// is never anybody's deadlock victim, and — more useful — it stops being
// one side of a cycle before a cycle can be reported at all. Waiting
// LONGER, which is the instinct, is what makes a deadlock the likely
// outcome instead of a timeout.
//
// It does not make deadlocks impossible: the OTHER side's detector can fire
// first and pick us. RetryOnLockContention therefore retries both codes.
const LockWaitTimeout = 750 * time.Millisecond

// IsDeadlock reports whether err is, or wraps, a PostgreSQL deadlock
// (40P01).
func IsDeadlock(err error) bool { return hasSQLState(err, deadlockDetected) }

// IsLockTimeout reports whether err is, or wraps, a `lock_timeout` expiry
// (55P03).
func IsLockTimeout(err error) bool { return hasSQLState(err, lockNotAvailable) }

func hasSQLState(err error, code string) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == code
}

// RetryOnLockContention runs fn until it returns something that is not a
// lock-contention failure, up to LockContentionRetries attempts, backing
// off with jitter between them. Any other error, and the last contention
// failure, are returned unchanged — with the SQLSTATE still reachable
// through errors.As, so a reader is told which of the two it was.
//
// WHY THIS EXISTS (ISS-032). A test that takes strong locks on tables other
// packages are also writing can lose a race it did not start.
// `TestRepositoriesOrganizationID_DriftCheckDetectsDrift` manufactures
// drift by dropping a foreign key, and `ALTER TABLE repositories DROP
// CONSTRAINT repositories_project_org_fkey` takes AccessExclusiveLock on
// `repositories` AND on `projects` — the referenced table, whose RI
// triggers it must remove. Meanwhile other packages hold AccessShare on
// `projects` while taking RowExclusive on `repositories` (every connect
// does: it reads the default project, then writes the repository), and
// others take them in the reverse order. That is a lock cycle with no
// author at fault. It fired once in CI's package-parallelism step, and
// reproduces locally once `pkg/jobs` is added to that set.
//
// Retrying weakens nothing: fn rebuilds its own transaction each time, the
// caller asserts on the attempt that completed, and six failures still
// fail. It does NOT make a genuine, repeatable deadlock pass.
//
// fn must be self-contained: it owns its transaction and must leave nothing
// behind when it returns an error, or attempt two starts from somewhere
// attempt one did not intend. A caller whose fn takes table locks should
// also set `SET LOCAL lock_timeout` to LockWaitTimeout — see that
// constant.
func RetryOnLockContention(ctx context.Context, fn func() error) error {
	var err error
	for attempt := 1; attempt <= LockContentionRetries; attempt++ {
		err = fn()
		if !IsDeadlock(err) && !IsLockTimeout(err) {
			return err
		}
		if attempt == LockContentionRetries {
			break
		}
		// Linear backoff with jitter — about 2.3s across all six attempts,
		// on top of up to LockWaitTimeout of waiting inside each. Without
		// the jitter, N retriers released by the same burst of contention
		// collide again on the same schedule.
		backoff := time.Duration(attempt) * 150 * time.Millisecond
		backoff += time.Duration(rand.Int63n(int64(100 * time.Millisecond)))
		select {
		case <-ctx.Done():
			return fmt.Errorf("lock-contention retry abandoned: %w (last: %w)", ctx.Err(), err)
		case <-time.After(backoff):
		}
	}
	return fmt.Errorf("still losing the lock race after %d attempts: %w",
		LockContentionRetries, err)
}
