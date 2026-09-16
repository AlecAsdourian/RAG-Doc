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
