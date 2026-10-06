package handlers

// Internals this package's EXTERNAL tests (`package handlers_test`) need to
// exercise production behaviour rather than a paraphrase of it.
//
// Keep this list short and keep each entry's reason with it. An export here
// is a piece of the package's shape that a test now depends on.

// ExistingRepositoryLookupSQL is Connect's org-wide adopt lookup.
//
// TestRepositoriesConnect_ConcurrentConnectsOfDifferentRepositoriesDoNotBlock
// runs it in a second transaction, standing in for another connect that is
// in flight. Running the REAL statement is what makes that test able to
// fail: with a bare `FOR UPDATE` the two calls contend on the shared
// `projects` row and the second one blocks, and with `FOR UPDATE OF r` they
// lock a row each. A hand-copied statement in the test file would keep
// passing after someone widened the production one.
const ExistingRepositoryLookupSQL = existingRepositoryLookupSQL

// CurrentJobJoinSQL, RepositoryWithCurrentJobSQL and
// RepositoryListWithCurrentJobSQL are the current-job fragment and the Get
// and List statements built from it (22.1-03).
//
// repositories_current_job_isolation_test.go's Scenario4 runs the two
// statements inside a transaction holding PLANTED job rows (another
// organization's, on the caller's repository), because the repository's own
// row-level security masks the organization filters from any request a test
// can make. Running the PRODUCTION statements is what lets that scenario
// fail when a filter is neutered; a copy in the test file would keep its own
// filters and pass. The fragment is exported so the test can assert that
// both statements really contain it.
const (
	CurrentJobJoinSQL               = currentJobJoinSQL
	RepositoryWithCurrentJobSQL     = repositoryWithCurrentJobSQL
	RepositoryListWithCurrentJobSQL = repositoryListWithCurrentJobSQL
)
