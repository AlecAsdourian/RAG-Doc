// Package jobs is the Go side of the ingestion queue: enqueue, and
// supersede-on-relink.
//
// It is empty at 21-02. This plan ships migration 000014 — the
// `ingestion_jobs` table, its two partial indexes, the composite foreign
// key onto `repositories (id, organization_id)` and the tenant trigger —
// together with schema_test.go, which runs every SQL statement the rest of
// the phase is built on against the PostgreSQL version we deploy. 21-03
// fills this file with Enqueue and SupersedeLive; 21-05 is the Python
// consumer, which lifts the claim, completion, failure, sweeper and
// run-resolution statements from the same test file.
//
// The rules a caller has to know, all of them measured rather than
// asserted (see .planning/phases/21-ingestion-job-infrastructure/21-CONTEXT.md):
//
//   - THERE IS ONE ENQUEUE STATEMENT, the per-row upsert in L7, used by
//     push, relink and bulk `installation_repositories.added` alike. Its
//     `ON CONFLICT (repository_id) WHERE state IN ('queued','running')`
//     inference clause is not optional: without the predicate the
//     statement raises 42P10, and without a target at all, 42601.
//
//   - ORDER MATTERS, AND GETTING IT WRONG IS SILENT. Supersede or complete
//     the live job BEFORE enqueueing its replacement, in one transaction.
//     The reverse order through the upsert raises nothing — it flags
//     needs_rerun on the job that is about to leave the live set, and the
//     repository ends with no live job at all. (Through a plain INSERT it
//     raises 23505, which is what 21-CONTEXT L4 and L7 originally recorded;
//     the correction is dated 2026-09-14 and pinned by the tests.)
//
//   - EVERY TERMINAL WRITE IS FENCED ON THE LEASE:
//     `WHERE id = $1 AND lease_owner = $2 AND state = 'running'`. A
//     reclaimed or superseded worker's write then matches zero rows instead
//     of clobbering the new attempt's result or colliding with its
//     replacement.
//
//   - WRITES THAT TOUCH organization_id OR repository_id MUST BE
//     TENANT-SCOPED, because trg_ingestion_jobs_tenant reads `repositories`,
//     which carries FORCE ROW LEVEL SECURITY. The claim, heartbeat,
//     completion, failure and sweeper statements touch neither column, so
//     the claim stays genuinely pre-tenant — which is the whole reason the
//     table has no row-level security of its own.
//
//   - `ingestion_jobs` HAS NO ROW-LEVEL SECURITY, so organization_id on it
//     is an authorization input that nothing in the database will apply for
//     you. Any handler reading this table filters by it explicitly (21-07).
package jobs
