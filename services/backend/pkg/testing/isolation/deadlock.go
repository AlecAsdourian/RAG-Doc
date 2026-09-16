package isolation

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// deadlockDetected is SQLSTATE 40P01. Postgres raises it on ONE of the
// transactions in a lock cycle — the deadlock victim — and rolls that one
// back. The other side proceeds normally.
const deadlockDetected = "40P01"

// DeadlockRetries is how many attempts RetryOnDeadlock makes before giving
// up. Three, because a retry only helps if the other side of the cycle has
// moved on, and if it has not after two backoffs the problem is not
// transient.
const DeadlockRetries = 3

// IsDeadlock reports whether err is, or wraps, a PostgreSQL deadlock
// (40P01).
func IsDeadlock(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == deadlockDetected
}

// RetryOnDeadlock runs fn until it returns something other than a deadlock,
// up to DeadlockRetries attempts, with a short backoff between them. Any
// other error, and the last deadlock, are returned unchanged.
//
// WHY THIS EXISTS (ISS-032). A test that takes strong locks on tables other
// packages are also writing can be chosen as the deadlock victim through no
// fault of its own. `TestRepositoriesOrganizationID_DriftCheckDetectsDrift`
// manufactures drift by dropping a foreign key, and `ALTER TABLE
// repositories DROP CONSTRAINT repositories_project_org_fkey` takes
// AccessExclusiveLock on `repositories` AND on `projects` — the referenced
// table, whose RI triggers it must remove — while every other package's
// fixture cleanup is deleting from those two tables in the other order.
// That is a lock-order cycle with no author at fault, and it fired once in
// CI's package-parallelism step.
//
// Retrying weakens nothing the test proves: fn re-runs its own setup, and
// the assertion is made on the attempt that completed. It does NOT make a
// genuine, repeatable deadlock pass — three attempts all failing still
// fails, and the error names 40P01.
//
// fn must be self-contained: it owns its transaction and must leave nothing
// behind when it returns an error, or attempt two starts from somewhere
// attempt one did not intend.
func RetryOnDeadlock(ctx context.Context, fn func() error) error {
	var err error
	for attempt := 1; attempt <= DeadlockRetries; attempt++ {
		err = fn()
		if !IsDeadlock(err) {
			return err
		}
		if attempt == DeadlockRetries {
			break
		}
		// Linear backoff, with the context honoured so a cancelled test
		// does not sit here.
		select {
		case <-ctx.Done():
			return fmt.Errorf("deadlock retry abandoned: %w (last: %w)", ctx.Err(), err)
		case <-time.After(time.Duration(attempt) * 100 * time.Millisecond):
		}
	}
	return fmt.Errorf("still deadlocking after %d attempts: %w", DeadlockRetries, err)
}
