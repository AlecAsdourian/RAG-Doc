// Package jobs is the Go side of the ingestion queue: enqueue, and
// supersede-on-relink.
//
// THE PUBLIC SURFACE IS TWO FUNCTIONS, both in producer.go and both taking
// the CALLER'S transaction, because the caller owns atomicity and tenant
// scope:
//
//   - Enqueue puts one job on the queue per repository, in one statement,
//     and writes the `sync_state` projection for the repositories that got
//     a new job.
//   - SupersedeLive takes each repository's live job out of the live set,
//     and runs BEFORE the Enqueue that replaces it.
//
// Nothing else in `services/backend` may write `ingestion_jobs`, and
// nothing may write `repositories.sync_state` as a way of asking for work:
// that is what ISS-016 was.
//
// 21-02 shipped migration 000014 — the `ingestion_jobs` table, its two
// partial indexes, the composite foreign key onto `repositories (id,
// organization_id)` and the tenant trigger — together with schema_test.go,
// which runs every SQL statement the rest of the phase is built on against
// the PostgreSQL version we deploy. 21-03 added the producer above and
// moved the three statements it uses out of the test file and into
// producer.go, verbatim. 21-05 is the Python consumer, which lifts the
// claim, completion, failure, sweeper and run-resolution statements from
// the test file.
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
//   - EVERY LEASE-FENCED STATEMENT ALSO CARRIES `AND state = 'running'`,
//     not just `lease_owner = $2`. `supersedeLiveSQL` leaves the lease
//     attached — deliberately, so the row records which worker was running
//     when it was superseded — so the owner alone does not mean "still
//     mine". PR #38's review measured the one statement that was missing
//     the predicate letting a superseded worker consume a rerun flag it
//     could then do nothing with.
//
//   - `ingestion_jobs` HAS NO ROW-LEVEL SECURITY, so organization_id on it
//     is an authorization input that nothing in the database will apply for
//     you. Any handler reading this table filters by it explicitly (21-07).
//     THE SAME APPLIES TO SupersedeLive: its statement touches neither
//     organization_id nor repository_id, so the tenant trigger does not
//     fire and a repository id from another organization would be
//     superseded just as readily. Pass only ids the same transaction has
//     already read out of `repositories`, which IS scoped. Enqueue is safe
//     by contrast, because inserting a row DOES fire the trigger.
//
//   - ⚠ `claimSQL` AND `sweepSQL` ARE QUEUE-WIDE AND CROSS-TENANT BY
//     CONSTRUCTION. They carry no organization filter and, because the table
//     has no row-level security, an unscoped session running either one
//     reaches every tenant's rows — measured in PR #38's review, where a
//     claim returned another organization's job and its organization_id.
//     That is the design: a worker learns the tenant FROM the row it
//     claimed. It also means NEITHER STATEMENT MAY EVER RUN INSIDE A REQUEST
//     HANDLER, whatever tenant scope that handler holds. The table comment
//     warns about 21-07's GET; this is the warning for the two statements
//     themselves.
package jobs
