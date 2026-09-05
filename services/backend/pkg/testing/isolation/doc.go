// Package isolation is the shared harness for tenant-isolation integration tests
// in the Go backend. It spins up an ephemeral Postgres via testcontainers-go,
// applies all migrations, and exposes fixtures and primitives that any future
// test can use to prove cross-tenant boundaries hold.
//
// Typical use:
//
//	pool := isolation.SetupTestDB(t)
//	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
//	    // ... exercise an endpoint or query as orgA and prove orgB cannot see it.
//	})
//
// The four primitives are:
//
//   - SetupTestDB — ephemeral Postgres container, migrations applied, pgx pool
//   - WithTwoOrgs — two fully populated tenants for cross-tenant testing
//   - TenantScope — begin a transaction scoped to a specific tenant
//   - AssertNoCrossTenantLeak — act as tenantA, prove tenantB cannot observe it
//
// Container reuse: the Postgres container is reused across test packages via a
// stable name so subsequent runs start in <2s. Migrations are idempotent so
// reuse is safe. Each returned pool is closed on t.Cleanup; the container
// itself persists for the developer's session.
package isolation
