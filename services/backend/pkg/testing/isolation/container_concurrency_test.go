package isolation

import (
	"context"
	"sync"
	"testing"
)

// TestEnsureAppRoleIsConcurrencySafe is the regression guard for ISS-010.
//
// It drives the contention directly instead of hoping Go's package
// scheduler produces it, and that distinction is the whole point.
//
// The first attempt at guarding this was a CI step running the harness
// packages at default parallelism, on the theory that two packages
// calling setupContainer at once would reproduce the race. It does — but
// only sometimes. Measured against the pre-fix code across sixteen runs,
// that approach detected the missing advisory lock roughly **one time in
// eight**, with cold and warm containers performing identically. A guard
// that passes 7 times out of 8 on genuinely broken code is not a guard;
// it is a coin flip that teaches people to trust a green check.
//
// Releasing N callers through a barrier reproduces it every time:
//
//	pre-fix code:  8/8 runs detected the race
//	current code:  0/6 runs false-positived
//
// It also runs in milliseconds once the container is up, needs no cold
// container, and is caught by the ordinary `go test ./...` gate — so it
// survives someone later deleting the CI step, which the CI-step-only
// approach could not.
//
// If this fails with `tuple concurrently updated (SQLSTATE XX000)` or
// `role "rag_doc_app" already exists (SQLSTATE 42710)`, the advisory lock
// in ensureAppRole is gone or no longer covers every statement.
func TestEnsureAppRoleIsConcurrencySafe(t *testing.T) {
	// Populates sharedDSN and guarantees the container is up, so the
	// goroutines below contend on role setup rather than on container
	// creation.
	_ = SetupTestDB(t)

	const callers = 16

	var (
		start sync.WaitGroup
		done  sync.WaitGroup
		mu    sync.Mutex
		errs  []error
	)
	start.Add(1)
	done.Add(callers)

	for i := 0; i < callers; i++ {
		go func() {
			defer done.Done()
			start.Wait() // release them together

			// ensureAppRole is idempotent by design; running it 16 times
			// against the shared container is safe and is exactly what
			// concurrent packages do.
			if err := ensureAppRole(context.Background(), sharedDSN); err != nil {
				mu.Lock()
				errs = append(errs, err)
				mu.Unlock()
			}
		}()
	}

	start.Done()
	done.Wait()

	if len(errs) > 0 {
		t.Fatalf("ensureAppRole failed for %d of %d concurrent callers; first: %v",
			len(errs), callers, errs[0])
	}
}
