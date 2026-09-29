---
phase: 22-repository-clone-ingestion
plan: 01
subsystem: backend
tags: [migrations, pgvector, tenancy, rls, ci-gate, mutation-testing, test-harness]

requires:
  - phase: 21-01
    provides: "migration 000013, its per-organization backfill loop, and the deployment shape (a NOSUPERUSER NOBYPASSRLS owner) as the way to verify a migration"
  - phase: 21-02
    provides: "migration 000014 and `ingestion_jobs_repo_tenant_fk`, the key ISS-031 failed on"
  - phase: 21-04
    provides: "migration 000015 and pkg/jobs/backfill_migration_test.go's scratch-database pattern, lifted here"
  - phase: 17-01
    provides: "the testcontainers harness, its reuse-by-name container and the advisory-locked role setup (ISS-010)"
provides:
  - "migration 000016_enable_pgvector: `CREATE EXTENSION IF NOT EXISTS vector`, with the operator precondition documented and tested"
  - "pgvector/pgvector:pg16 pinned by digest in compose, the Go harness, the Python conftest and CI; the Go harness's reuse container renamed rag-doc-isolation-tests-pgv16"
  - "ISS-031's live failure fixed: 000014 declares ingestion_jobs_repo_tenant_fk inside CREATE TABLE, and 000013 adds repositories_project_org_fkey before its backfill loop; both with an identical resulting schema"
  - "TestMigrationsApplyToASeededDatabase: the seeded-migration gate, in CI from now on, with a tenant audit over every ALTER TABLE"
  - "TestMigration000016NeedsTheExtensionPreCreated, and TestMigrationSchemaMatchesBaseline (an env-gated proof tool)"
  - "isolation.ScratchDatabase(t, pool, owner), isolation.SuperuserRole, isolation.DeploymentOwnerRole"
  - "pkg/vectordb deleted, and github.com/qdrant/go-client with it"
affects: [22-02 (000017 runs through the gate and adds its assertions), 22-03, 24 (the operator creates the extension; pgvector >= 0.8.0; --shm-size; REINDEX an Alpine-initialised volume)]

tech-stack:
  added: ["pgvector 0.8.6 (the image, pinned by digest)"]
  removed: ["github.com/qdrant/go-client, and the indirect grpc, genproto, protobuf and x/net it pulled in"]
  patterns:
    - "Declare foreign keys on new tables INSIDE CREATE TABLE: ADD CONSTRAINT's validation reads the referenced table through its row-level-security policy, as the migrating owner"
    - "A key on an existing table goes BEFORE any tenant is set, while its column is still NULL; per-row checks then verify every backfilled value with RLS bypassed. A key over data already there needs FORCE lifted for the one statement: there is no tenant under which the owner's validation sees every row"
    - "Never rely on a migrating session's tenant; never make a validation pass with a sentinel tenant"
    - "Check the cause, not the effect, when the effect is unobservable: Postgres records nothing about what a validation read, so the gate records the session's tenant and the table's RLS state at every ALTER TABLE that validated a key"
    - "A migration gate asserts its own premises (who owns the database and every table, that FORCE is on, that the fixture's one mutating step changed exactly one row, that three tenants own rows), because a gate that silently ran as the superuser would pass the bug it exists to catch (M8b)"
    - "Change a reuse-by-name container's name whenever its image changes"
    - "Commit the failing gate before the fix, so the failure is recorded against unmodified code"

key-files:
  created:
    - services/backend/migrations/000016_enable_pgvector.up.sql
    - services/backend/migrations/000016_enable_pgvector.down.sql
    - services/backend/pkg/testing/isolation/scratch.go
    - services/backend/pkg/testing/isolation/migration_seeded_test.go
    - services/backend/pkg/testing/isolation/testdata/seed_at_000010.sql
  modified:
    - services/backend/migrations/000013_repositories_organization_id.up.sql
    - services/backend/migrations/000014_ingestion_jobs.up.sql
    - services/backend/pkg/testing/isolation/container.go
    - services/backend/pkg/testing/isolation/migrator.go
    - services/backend/pkg/jobs/backfill_migration_test.go
    - services/workers/tests/isolation/conftest.py
    - services/workers/workers/jobs/transitions.py
    - services/workers/scripts/test_ingestion.py
    - services/workers/scripts/test_query_engine.py
    - .github/workflows/backend-ci.yml
    - docker-compose.yml
    - docs/local-development.md
    - services/backend/go.mod
    - services/backend/go.sum
  deleted:
    - services/backend/pkg/vectordb/

key-decisions:
  - "ISS-031 fixed by moving one constraint in each of 000014 and 000013, not by lifting FORCE, not by NULLIF-tolerant policies and not by one session per migration (the plan's three rejected alternatives). For a key over data already there, lifting FORCE for that statement IS the rule's answer, because nothing else is correct."
  - "The gate runs in a scratch database inside the shared harness container, owned and migrated by rag_doc_owner, seeded as rag_doc_app under each organization's own tenant."
  - "The tenant audit flags a foreign key validated by ALTER TABLE under a non-empty tenant, or on a forced-RLS table holding rows the key applies to. No version is exempt: the one exemption the review suggested (13) would have hidden the instance it was named for."
  - "ScratchDatabase's owner is a parameter, restricted to the two roles whose password the harness knows. pkg/jobs/backfill_migration_test.go passes SuperuserRole and keeps exactly its old behaviour."
  - "The schema-identity proof is committed as an env-gated test, so a reviewer can re-run it."

issues-closed: []
issues-updated: [ISS-031, ISS-035]
review: "PR #48 — CHANGES REQUESTED, one important finding (I1: the gate could not see a validation run under a real tenant, and committed 000013 ran one), three minors, four nits. The reviewer re-ran every claim and they held. The user chose the fullest option: close the hole in the gate AND fix 000013. Applied 2026-09-29; the second section of this summary records it."
duration: ~5h, plus ~4h applying PR #48's review
completed: 2026-09-29
---

# Phase 22 Plan 01: pgvector everywhere, and ISS-031 fixed behind a gate that saw it fail

**Every Postgres this project starts is now `pgvector/pgvector:pg16`, pinned
by digest, and migration 000016 fails on any image without the extension.**
The Go harness's reuse container is renamed, so a developer's leftover Alpine
container is never picked up.

**ISS-031 is fixed, twice over.** A database owned by a non-superuser, seeded,
and migrated from version 10 in one session failed at 000014 with `22P02` and
was left dirty. That is the shape the first production deploy will have.
000014 now declares its foreign key inside `CREATE TABLE`. And PR #48's review
found that 000013 validated its own key after its tenant-setting loop, so that
the validation read one organization's rows; that key now precedes the loop.
Both edits leave the schema identical.

**The gate that proves it now runs in CI.** It failed on the commit that added
it (`4a33b08`, before the 000014 fix) and passes on every commit after
`5677cc1`. Its tenant audit, added for the review, failed on committed 000013
(`b01d623`) and passes after `a9b0982`.

## The image

**Digest:** `sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b`.
It is the multi-arch index, with linux/amd64 at `sha256:eac62140…` and
linux/arm64 at `sha256:3f0a8882…`. The image was created 2026-08-13.

It was re-checked on 2026-09-17 with:

```
docker buildx imagetools inspect pgvector/pgvector:pg16
```

The local image's `RepoDigests` agree. It runs PostgreSQL 16.15 with pgvector
0.8.6, and `vector 0.8.6` is read back by the gate.

**The four places** carry the same reference,
`pgvector/pgvector:pg16@sha256:ccc6e83d…`:

| Where | What changed |
|---|---|
| `pkg/testing/isolation/container.go` | `postgresImage`. `containerName` becomes `rag-doc-isolation-tests-pgv16`, with the measured reason and the rule **change the name whenever the image changes** beside it |
| `services/workers/tests/isolation/conftest.py` | `_POSTGRES_IMAGE`, new |
| `.github/workflows/backend-ci.yml` | `services.postgres.image`. CI's `migrate up` step now proves the image, because 000016 needs the extension |
| `docker-compose.yml` | the `postgres` image only. It was never started; the only compose command run was `docker compose config` |

**testcontainers-python pulls `name:tag@digest` correctly.** docker-py
splits at the `@` and pulls by digest. This was measured with
`hello-world:latest@sha256:…` through the same `images.get` → `ImageNotFound` →
`images.pull` path testcontainers takes. The image was removed afterwards.

**`docs/local-development.md`** has a section, "The Postgres image". It
covers:
- the four places, and the renamed container;
- the Alpine (musl) to Debian (glibc) volume hazard, **verified by PR #48's
  review** on a throwaway volume: a text index built on Alpine was out of
  order under the pgvector image, nothing warned, `bt_index_check` reported
  it, and `REINDEX DATABASE` fixed it. The docs give the amcheck command and
  say to reindex before starting the backend or the workers against the
  volume. The choice between recreating and reindexing is the user's;
- the operator step, with the exact error, and a table of the three dirty
  states 22-01 measured and their recoveries;
- `--shm-size` for HNSW builds.

**The compose volume was not touched.**

## 000016

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The header records what was measured on 2026-09-17, as a non-superuser table
owner:

| Case | Result |
|---|---|
| extension absent | `ERROR 42501: permission denied to create extension "vector"`, `HINT: Must be superuser to create this extension.` |
| extension present | `NOTICE 42710: extension "vector" already exists, skipping`, then success |
| `DROP EXTENSION IF EXISTS vector` (the down migration), extension created by the operator | `ERROR 42501: must be owner of extension vector` |

The header also records the production requirements: pgvector 0.8.0 or later,
because 22-03 needs iterative scans, and a pointer to `22-RESEARCH.md` Q2. The
down comment says that rolling back past 16 in the deployment shape needs the
operator again, what state the failure leaves (15, dirty, extension still
installed) and the two ways out.

**`TestMigration000016NeedsTheExtensionPreCreated`** pins all of it.
- **Premise:** the image has pgvector available, and the scratch database has
  not created it. So the failure can only be the privilege, not a wrong image,
  which would fail with `0A000` "is not available".
- **Without the extension:** migrate to 15 as `rag_doc_owner`, then to 16. The
  migration fails with `42501`, `schema_migrations` reads 16, dirty, and
  nothing was installed.
- **The operator's recovery,** as the docs give it: create the extension as the
  superuser, `Force(15)`, and migrate to 16 again. It reaches 16, clean.
- **The down,** as the owner, fails with `42501 must be owner of extension
  vector`, and leaves **15, dirty, with the extension still installed**.
- **Both recoveries from that:** `Force(16)` gives 16, clean, extension
  present; then dropping the extension as the superuser and `Force(15)` gives
  15, clean, extension absent.

## ISS-031, the first instance: 000014

### The failure on `main`, recorded on the commit that added the gate

`4a33b08`, with `000014` byte-identical to `RAG-Doc/main`'s:

```
=== RUN   TestMigrationsApplyToASeededDatabase
    migration_seeded_test.go:170: ISS-031 CLASS: migrating a seeded database from 12 to 16 as rag_doc_owner, in one session, failed and left schema_migrations at 14 (dirty=true): SQLSTATE 22P02: migration failed: invalid input syntax for type uuid: ""

        A migration validated a constraint or evaluated a row-level-security policy on a session whose app.current_tenant an earlier migration left at ''. Declare foreign keys inside CREATE TABLE, and never rely on the session's tenant. See this file's header and ISS-031.
--- FAIL: TestMigrationsApplyToASeededDatabase (0.38s)
FAIL
```

The server log names the statement, which golang-migrate cannot: it sends a
whole file as one statement, and an internal query's error has no position.
It is the foreign key's validation query:

```
ERROR:  invalid input syntax for type uuid: ""
CONTEXT:  SQL statement "SELECT fk."repository_id", fk."organization_id" FROM ONLY "public"."ingestion_jobs" fk
          LEFT OUTER JOIN ONLY "public"."repositories" pk ON ( pk."id" OPERATOR(pg_catalog.=) fk."repository_id"
          AND pk."organization_id" OPERATOR(pg_catalog.=) fk."organization_id") WHERE pk."id" IS NULL
          AND (fk."repository_id" IS NOT NULL AND fk."organization_id" IS NOT NULL)"
STATEMENT:  -- Phase 21-02: `ingestion_jobs`, the durable queue. …
```

### The fix

**`ingestion_jobs_repo_tenant_fk` is declared inside `CREATE TABLE
ingestion_jobs`**, with the same name and definition. The `ALTER TABLE … ADD
CONSTRAINT` block is gone. With comments stripped, the SQL diff against `main`
is exactly those two hunks.

The comments now say:
- **per-row** foreign-key checks bypass row-level security, and
  `ADD CONSTRAINT`'s **validation** does not (section 4 and the header);
- why the key is inline, with the measured failure;
- why editing a shipped migration is acceptable **only** because nothing is
  deployed, what happens to databases already at 14, and how to recover one
  the old form left dirty at 14 (`force 13`, then `up`; never `force 14`);
- the rule, with its answer for keys on existing tables;
- what the gate can and cannot see of the rule.

### The gate passes with the fix

Three runs, each on its own scratch database, none left behind. Re-run on
2026-09-29 after the review's changes: the one-session `up` from 12 to 16 took
97, 108 and 100 ms; the whole test 518, 475 and 402 ms.

## ISS-031, the second instance: 000013 (PR #48's review, I1)

### What the review found

A migration file is one transaction, so the tenant 000013's loop sets is still
in force when the same file later runs `ADD CONSTRAINT
repositories_project_org_fkey`. The key's validation read `repositories`
through its policy under the **last** organization's tenant. In the gate that
is delta, which owns no repositories, so it read nothing and passed. The gate
was green, and three places said the gate enforced the rule.

### The audit sees it, on the commit that added the audit

`b01d623`, with 000013 byte-identical to `main`'s:

```
=== RUN   TestMigrationsApplyToASeededDatabase/no_foreign_key_was_validated_under_a_tenant_or_through_row-level_security
        	Error:      	Should be empty, but was [13: repositories_project_org_fkey on public.repositories validated under a tenant (tenant=10000000-0000-4000-8000-00000000000d, force_rls=t, checkable_rows=8)]
        	Messages:   	an ALTER TABLE validated a foreign key in a shape the rule forbids; see this file's header, 000014's section 4 and ISS-031
--- FAIL: TestMigrationsApplyToASeededDatabase (0.66s)
```

Every other subtest passed: the up reached 16 clean, which is the point.

### MX13, before and after the fix

The review's mutation: 000013's backfill files alpha's four repositories under
bravo's organization (`SET organization_id = CASE WHEN org.id = alpha THEN
bravo ELSE p.organization_id END`), on a scratch copy of the migrations.

| 000013 | As `rag_doc_owner` (the deployment shape) | As the superuser |
|---|---|---|
| `main`'s: key **after** the loop | the `up` reaches **16, clean**; the key is marked valid over four rows that violate it. The audit flags `13: repositories_project_org_fkey … tenant=…000d, checkable_rows=8`; the drift check finds `…0a1 …0a2 …0a3 …0a4`; 000015's assertion fails on `a-pending` | **13, dirty, `23503`** |
| committed: key **before** the loop (`a9b0982`) | **13, dirty, `23503`**: `insert or update on table "repositories" violates foreign key constraint "repositories_project_org_fkey"` | **13, dirty, `23503`** |

### The fix

`repositories_project_org_fkey` is added in a new section 3, between
`projects_id_org_key` and the loop. With comments stripped, the SQL diff
against `main` is exactly that move.
- Every `organization_id` is NULL there, so the validation has nothing to
  check (MATCH SIMPLE skips a row with a NULL key column), and passes for that
  reason, not because a policy hid the rows.
- Every value the loop writes is then checked per row, which bypasses
  row-level security, against `projects`, which has no policy at all.
- **What still runs after the loop, and why each is safe there:** `SET NOT
  NULL` and the UNIQUE constraint validate by scanning the heap, which
  row-level security does not filter (measured below); the trigger and
  function definitions read no rows.
- The comment carries the justification for a second edit to an applied
  migration, on the same nothing-is-deployed grounds as 000014.

**Re-verified after the move:**
- **The seeded backfill in both shapes** (21-01's evidence): the gate as
  committed is the deployment shape, and passes; the gate with a
  superuser-owned database and the ownership premise neutered (M8b's setup)
  passes every subtest on the committed migrations.
- **The schema is identical** to `main`'s: 550 catalog lines match against
  `main`'s 000013 and 000014 together, against `main`'s 000013 alone, and
  against `main`'s 000014 alone.
- **Up, down, up** with the `migrate` CLI on a fresh database: 16 clean; after
  `down -all` no tables, functions or extension; 16 clean again with both keys
  validated.

### The tenant audit

`installTenantAudit` installs a schema `gate_audit`, owned by the superuser,
with two `SECURITY DEFINER` event-trigger functions, before the first
migration runs:
- `gate_audit.snapshot`, on `ddl_command_start` for `ALTER TABLE`, records
  every foreign key and whether it is validated;
- `gate_audit.record`, on `ddl_command_end` for `ALTER TABLE`, finds each
  foreign key the command left validated that was not validated before it,
  counts the rows of the key's table (without `ONLY`, so partitions count)
  where every key column is non-NULL, and flags the key when either

  **(a)** the session's `app.current_tenant` was non-empty, or
  **(b)** the key's table has `relrowsecurity AND relforcerowsecurity` and
  that count is above zero.

  The row records the migration version (golang-migrate writes `version = N,
  dirty` before it runs file N), the relation, the constraint, the tenant,
  the FORCE flag, the row count and the reason.

**Threshold: none.** Every version from 1 on is audited; the gate asserts the
audit is empty. The review suggested exempting 13 with a comment naming the
instance; with the key moved, 13 no longer needs it, and an exemption is
exactly where the next instance would hide.

**Why (b) exists.** The review's minor 1: the `''` half of the gate is loud
only because 13 and 15 run in its one session, and the same `ALTER TABLE` key
in a fresh session reached 17 clean. (a) alone would not see a fresh session
(tenant NULL). (b) does not depend on the session at all.

**Why SET NOT NULL, CHECK and UNIQUE are not its business.** Their
validations scan the heap directly and are not subject to row-level security.
Measured on the poisoned session (after 000015, tenant `''`), where a
policy-filtered read raises `22P02`: a violating CHECK failed with `23514
check constraint "probe_name_chk" … is violated by some row`; a violating
UNIQUE failed with `23505 could not create unique index … Duplicate keys
exist`; a satisfied CHECK and UNIQUE pair reached 17 clean. 21-01 measured
SET NOT NULL the same way.

### The rule, revised

Written in 000014's section 4, the gate's header and ISS-031:
- A key on a **new** table goes inside `CREATE TABLE`.
- A key on an **existing** table, for a column the migration **adds**: add
  the key before any tenant is set in the file, while the column is still
  NULL, then fill the column under each organization's own tenant. Never after
  a tenant-setting loop. 000013 is the worked example.
- A key over data **already there**, on a forced-RLS table: the owner's
  validation is filtered whatever the tenant. **Measured** (below, M11): NULL
  sees no rows and marks the key valid over eight violating rows; `''` fails
  with `22P02`; an organization's id sees one organization's rows. There is
  no tenant that makes it correct. Lift FORCE for that one statement, as
  000012 does, before any tenant is set. **Measured:** the same key over the
  same eight rows then fails with `23503`, and the audit does not flag it,
  because FORCE is off at that moment.
- Never rely on the tenant an earlier loop left behind; never set a sentinel
  tenant.

### The second review (APPROVE WITH NITS): three minors on the FORCE-lift clause and the audit's edges, applied 2026-09-29

The reviewer confirmed I1 closed, tried to evade the audit with `NOT VALID`
plus `VALIDATE CONSTRAINT` and with a key added through `DO`/`EXECUTE` (both
caught), and confirmed `SECURITY DEFINER` is contained. Three minors remained;
the user chose to fix them before merging.

**Minor 1: the validation reads the referenced table through its policy
too.** The rule's new clause named only the key's own table. The validation
is one `LEFT OUTER JOIN` of the key's table to the referenced table, and as
the owner each side is read through its own policy: that referenced side is
ISS-031's original mechanism (`ingestion_jobs` had no policy, `repositories`
did). M11c held only because its probe referenced `organizations`, which has
no RLS. The clause now says "every table the validation reads that has it,
the key's table and the referenced table", in 000014 §4, the gate header,
ISS-031 and the `22P02` hint (which gained "or validated a foreign key
against a forced-RLS table with FORCE still on").

The reviewer's E4/E4b are pinned as a committed test,
`TestForeignKeyValidationReadsTheReferencedTable`, against the table 22-02's
keys will reference. A plain table holding every seeded repository's
**correct** `(id, organization_id)` pair (filled under each organization's
tenant, so correct by construction, and checked as the superuser), keyed to
`repositories` by `ALTER TABLE` in a **fresh** session:
- **nothing lifted:** `23503 … violates foreign key constraint
  "probe_children_repo_tenant_fk", Key (repository_id, organization_id)=(…)`,
  18 dirty. `repositories` shows the owner no rows, so every correct pair is
  reported missing;
- **FORCE lifted on `repositories` for the statement:** 18 clean, the key
  validated, FORCE back on, no key left `NOT VALID`, nothing flagged.

The same probe through the gate's one session: nothing lifted → 18 dirty,
`22P02`, with the corrected hint; lifted → 6 migrations, clean, unflagged,
FORCE intact.

**Minor 2: nothing checked that a lifted FORCE came back.** A probe 000017
of just `ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY;` passed the gate
unflagged (the reviewer's E5). After the up, the gate now asserts that no
table has row-level security enabled without FORCE, and that every table that
forced it before the up still does (which also catches a table whose RLS was
switched off altogether). **Measured:** E5 is killed: `tables with row-level
security enabled but no longer FORCEd: [chunks]`.

**Minor 3: a key left `NOT VALID` passed.** A key added `NOT VALID` and never
validated runs no validation, so the audit had nothing to record (E2). After
the up, the gate asserts no public foreign key is `NOT convalidated`.
**Measured:** E2 (a `NOT VALID` key on a new table at 17) is killed:
`[probe_nv.probe_nv_repo_tenant_fk]`.

**Nits, chosen:**
- (b)'s one theoretical false positive, a FORCE table whose policy shows the
  owner every row (`USING (true)`): stated in the gate header and ISS-031 as a
  limit; none exists, every policy is tenant-scoped.
- (b) encodes the owner-migrates premise, which `assertDeploymentShape` pins:
  stated in the header and, for Phase 24's grant model, in ISS-031 (a
  non-owner migrating role is subject to RLS on any enabled table, FORCE or
  not, so (b) should then test `relrowsecurity` alone).
- `checkable_rows` follows MATCH SIMPLE's rule: a comment on the audit says a
  MATCH FULL key would also reject partly-NULL rows the count skips. None here.
- M13/M14: this summary's numbering is the record (M13 disables (b), M14
  disables (a)); the PR comment's summary line had them the other way round.

**Re-run on the final code:** the gate, the extension test and the new test
`-count=3`, all passes, no scratch database left behind; M1, M3, M10 and MX13
on the committed 000013 killed as before.

### What the gate guards, corrected in the three places the review named

000014:202, the gate's header and ISS-031 all said the gate enforced the rule.
They now say:
1. **the `''` leftover**, through the one-session run, loud only because 13
   and 15 run inside it. They must stay there; the header says so;
2. **any foreign key validated by `ALTER TABLE` under a tenant or through
   forced RLS with rows to check, in any version**, by the audit, independent
   of the session;
3. **what it cannot see:** DML that inherits a tenant, beyond what 000013's
   and 000015's outcome assertions cover, and a validating trigger's reads.

## The gate: `TestMigrationsApplyToASeededDatabase`

**The deployment shape.**
- `ScratchDatabase(t, pool, DeploymentOwnerRole)` creates a database owned by
  `rag_doc_owner`, a `NOSUPERUSER NOBYPASSRLS LOGIN` role.
- The role is created idempotently under the same `pg_advisory_xact_lock` as
  `ensureAppRole` (ISS-010).
- Its attributes are then **checked on every call**, because the role outlives
  any one run.

**The steps** (`seedDeploymentShape`, shared with the review's probes).
1. The superuser runs `CREATE EXTENSION vector`, the operator's step, and
   installs the audit.
2. `applyMigrationsTo(OwnerDSN, dir, 10)` migrates as `rag_doc_owner`, in its
   own session.
3. The harness's grants are given to `rag_doc_app` in this database. They are
   one shared list, `appRoleGrants`, used by `ensureAppRole` too.
4. `testdata/seed_at_000010.sql` is loaded as `rag_doc_app`, with each
   organization in its own transaction under its own
   `set_config('app.current_tenant', …, true)`.
5. `applyMigrationsTo(…, 12)`, a **re-grant**, and then, as `rag_doc_app` under
   alpha's tenant, `UPDATE github_installations SET uninstalled_at = NOW()`.
   `RowsAffected` must be 1: a filtered update matches nothing and reports
   success.
6. **Premises**, checked before the `up`:
   - the database, and every table in it including `schema_migrations`, belongs
     to `rag_doc_owner`;
   - `repositories` has RLS and FORCE on;
   - every seeded table is non-empty, and there are three chunks;
   - **at least three organizations own repositories** (new; the review's
     nit 4). A per-organization backfill that reached one tenant and not
     another can only be caught if several tenants have rows to backfill.
7. **One `m.Up()`**, from 12 to the newest version: one migrate instance, one
   pinned connection. golang-migrate's postgres driver holds a single
   `*sql.Conn` for the instance's life (`postgres.go:139`).
8. **Assertions, read as the superuser:**
   - `schema_migrations` is at the newest version, not dirty;
   - still the deployment shape;
   - **the audit is empty**;
   - **000013:** `CheckRepositoryTenantDrift` is empty, no `organization_id` is
     NULL, and every repository is still there;
   - **000015:** the eight named outcomes below, and the seed and the list agree
     on which repositories exist. There are exactly four jobs, no job's
     organization differs from its repository's, and "`pending` means a live job
     exists" holds over the whole table;
   - **000016:** `vector` is installed, and reads 0.8.6;
   - every seeded row outside `chunks` survived, by key, across eleven tables.
     22-02 adds 000017's assertions, including the dangling retrievals.

### The seed

| Organization | Rows | Repositories → expected after 000015 |
|---|---|---|
| alpha | a user and an owner membership; installations a1 (live) and a2 (**uninstalled at 12**); one run, two chunks, `queries` → `retrievals` → `feedback` | `a-pending` (a1) → 1 job, `pending`; `a-pending-no-install` (NULL) → none, `never_synced`; `a-pending-uninstalled` (a2) → none, `never_synced`; `a-synced` → none, `synced` |
| bravo | installation b1; one run, one chunk | `b-pending` → 1 job, `pending`; `b-syncing` → 1 job, `pending`; `b-synced` → none, `synced` |
| charlie | installation c1 | `c-pending` → 1 job, `pending` |
| delta | a default project, inserted **last** | none. In a fresh database the loops read `organizations` in insertion order, so a loop that handled only the last organization would backfill nothing |

**Only columns that exist at 10 are seeded.** `uninstalled_at` arrives in
000012, which is why a2 is uninstalled at step 5 (fact-check a2).

### Runtime

- The `up` from 12 to 16: **85 to 108 ms**, the audit included.
- The whole test: **about 0.4 to 0.5 s** on a warm container, and 3.2 s
  including a cold container start.

## Mutations

Committed code was never edited to run a mutation.
- **Migration mutations** ran from scratch copies of the migrations directory,
  passed through `RAG_DOC_SEEDED_GATE_MIGRATIONS`. A script built every copy
  and printed, for each, the files that differ from the committed set
  (`diff -rq --strip-trailing-cr`: exactly the intended ones) and the mutated
  text's presence.
- **Test and seed mutations** were made in the working tree after everything
  was committed, by a script that applied one `sed`, printed the mutated
  text's line count, ran the test, restored the file with `git checkout --`
  and printed whether the tree was clean. Every restore was clean.
- **The review's probes** (case B, CHECK/UNIQUE, the dirty-at-14 recovery)
  ran from a throwaway test file that reused the gate's helpers, deleted
  before the full suites ran.

### The first pass (2026-09-17)

| # | Mutation | Expected | Result |
|---|---|---|---|
| T1 | `conftest.py` → `postgres:16-alpine` | the Python suite fails on 000016 | **killed**: 87 errors, all at the session fixture, `FeatureNotSupported: extension "vector" is not available … vector.control`; 197 non-database tests passed. Restored; diff clean |
| T2 | a stale `rag-doc-isolation-tests` container from `postgres:16-alpine` | the renamed harness still passes | **passed**. The developer's own old container, exited, was present throughout, so none was created and none removed |
| M1 | restore the `ALTER TABLE … ADD CONSTRAINT` form of 000014's key | the gate fails with `22P02` | **killed**: 14, dirty, `SQLSTATE 22P02` |
| S1 | schema-tool baseline with the key `NOT VALID` | the comparison fails | **fails on the one `convalidated` line** |

### After the review (2026-09-29), every mutation re-run on the final code

| # | Mutation | Result |
|---|---|---|
| M1 | `main`'s 000014 | **killed**: 14, dirty, `22P02`, with the ISS-031 hint |
| M1' | `main`'s 000013 and 000014 together (the `main` state) | **killed**: 14, dirty, `22P02`. The `''` half fires before the audit is read |
| M2a | remove the seed's second organization (bravo) | **killed by the chunk-count premise**: 2 chunks, 3 expected |
| M2b | as M2a, with that premise relaxed | **killed by the new premise**: `"2" is not greater than or equal to "3"`, "the seed is multi-tenant where the backfills act" |
| M2c | as M2a, with both premises relaxed | **recorded survivor, as the plan predicted:** the 000013 subtest passes; 000015's fails only because the seed and the outcome list disagree (5 repositories, 8 named). The fixture alone cannot catch a last-tenant-only backfill; M4 is what does |
| M3 | a **sentinel tenant** (`…00dead`) set before the `ALTER TABLE` form of 000014's key | **KILLED, by the audit** (it was the recorded survivor before the review): `14: ingestion_jobs_repo_tenant_fk on public.ingestion_jobs validated under a tenant (tenant=00000000-0000-4000-8000-00000000dead, force_rls=f, checkable_rows=0)`. Every other subtest passes, which is why nothing else could see it |
| M4 | 000013's loop visits only the last organization (`ORDER BY ctid DESC LIMIT 1`) | **killed**: 13, dirty, `23502 column "organization_id" … contains null values` |
| M5 | 000015's loop visits only the last organization | **killed**: `a-pending: exactly one queued full_ingest job` |
| M6 | 000015's `INSERT` ignores `uninstalled_at` (neutered to `AND TRUE`) | **killed**: `a-pending-uninstalled: no job` |
| M7 | drop the re-grant at 12 | **killed**: `permission denied for table github_installation_tenants (SQLSTATE 42501)` |
| M8a | the gate's database owned by `SuperuserRole` | **killed by the premise**: owner `isolation`, `rag_doc_owner` expected |
| M8b | as M8a, with the premise neutered, committed migrations | **passes**: the superuser shape of the seeded backfill, 21-01's other half, with the fixed 000013 |
| M8b' | as M8b, `main`'s 000014 | **passes on the unfixed 000014**: the vacuous pass the premise exists to prevent |
| M9 | a probe `000017` adding a composite tenant key by `ALTER TABLE` | **killed**: 17, dirty, `22P02` |
| M9b | the same probe key declared inside `CREATE TABLE` | passes |
| M10 | `main`'s 000013 alone (its key after the loop), fixed 000014 | **killed by the audit**: `13: repositories_project_org_fkey on public.repositories validated under a tenant (tenant=…000d, force_rls=t, checkable_rows=8)` |
| MX13 | the backfill files alpha's repositories under bravo, on `main`'s 000013 | the `up` reaches **16, clean**; the audit flags 13; the drift check finds `…0a1 …0a2 …0a3 …0a4`; 000015's assertion fails. As the superuser: 13, dirty, `23503` |
| MX13' | the same, on the committed 000013 | **killed at 13, dirty, `23503`** in the deployment shape, and the same as the superuser |
| M11a | a probe `000017` fills a new `repositories.probe_org` with an id no organization has, per organization; a probe `000018` adds the key by `ALTER TABLE`; both in the gate's one session | **killed**: 18, dirty, `22P02` (the `''` half) |
| M11b | the same 000018 in a **fresh session** (its own migrate instance) after 000017 | the `up` reaches **18, clean**, `convalidated = true` over **8 rows that violate the key**. **Flagged by clause (b):** `18: probe_org_fk on public.repositories validated through forced row-level security with rows to check (tenant=NULL, force_rls=t, checkable_rows=8)` |
| M11c | the same, with FORCE lifted around the one statement | **fails with `23503`** on those rows (`Key (probe_org)=(…dead) is not present in table "organizations"`), 18 dirty; FORCE is back on after the rollback; **nothing flagged** |
| M12 | a violating CHECK at 17, on the poisoned session | **`23514`**: the validation saw the rows. A violating UNIQUE: **`23505`**. A satisfied pair: 17, clean |
| M13 | clause (b) of the audit disabled (`OR false`) | M11b is **no longer flagged** (`"[]" should have 1 item(s)`): (b) is load-bearing |
| M14 | clause (a) of the audit disabled (`IF false OR …`) | M3 **survives again**: (a) is load-bearing |
| E2 | a key added `NOT VALID` on a new table at 17, never validated (second review) | **killed by the NOT VALID assertion**: `[probe_nv.probe_nv_repo_tenant_fk]`. Before it: 17 clean, `convalidated = false`, audit empty |
| E4 | a plain table of eight **correct** pairs keyed to `repositories`, nothing lifted (second review) | in the gate's one session: 18 dirty, `22P02`, with the corrected hint. In a fresh session (the committed test): `23503` on correct data |
| E4b | the same, FORCE lifted on `repositories` for the statement | passes in both shapes, FORCE restored, unflagged, key validated |
| E5 | `ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY;` alone at 17 (second review) | **killed by the FORCE assertion**: `[chunks]`. Before it: 5 migrations, clean, unflagged |
| R1 | the dirty-at-14 recovery: `main`'s 000014 on the seeded shape, then `force 14`, then `up` | 14, dirty, **no `ingestion_jobs` table**; after `force 14`, `up` fails at 15 with `42P01 relation "public.ingestion_jobs" does not exist`, 15 dirty |
| R2 | then `force 13`, `up` | **16, clean**, the key validated, 4 jobs, audit empty |
| S2 | schema tool against `main`'s 000013 and 000014 together, against `main`'s 000013 alone, and against `main`'s 000014 alone | **identical, 550 catalog lines**, all three |

**The one recorded survivor is M2c**, the fixture's own limit. M3 no longer
survives. M8b' is not a survivor of the committed gate: M8a kills it.

## Verification

Run on 2026-09-29, on the final code, in the same environment shape as the
first pass: `DATABASE_TEST_URL` on a scratch pgvector container on port 55481
(echoed before every run), `REDIS_URL` on a scratch Redis, port 5434 never
touched, the compose volume never touched.

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `go mod tidy -diff`, gofmt on every changed Go file | clean |
| `go test ./... -count=1 -p 1` | every package `ok` except `pkg/api/handlers`, whose only failure is `TestSignatureComparisonIsConstantTime`, the known CRLF artefact. After the second review: **485 tests and subtests passed, 1 failed (that one), 4 skipped**: three pre-existing `pkg/auth` supersessions, and the schema tool with no baseline set. The gate: `4 migrations from 12 in one session in 92ms` |
| CI's package-parallelism step | same |
| `-race` in `golang:1.25` (go1.25.14) with the Docker socket and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`, CI's package list | same, 0 data races (run after the first review; the second review's changes are two gate assertions and one test, which CI's race step covers) |
| `pytest tests/ workers/ -q` (`REDIS_URL` on db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no reachable `.env`, fresh venv from `requirements.txt`) | **284 passed** after the first review; nothing under `services/workers` changed in the second, so it was not re-run |
| up, down, up with the `migrate` CLI as the superuser on a fresh `pgvector/pgvector:pg16` database | up to 16, clean; `down -all` leaves no tables, functions or extension; up again to 16, clean, both keys validated |
| `docker compose config` | the pgvector image; `docker ps` identical before and after |
| gate and extension test, `-count=3` | six passes; no scratch database left behind |

## Deviations from the plan

1. **`ScratchDatabase` returns a `ScratchDB`** (`Name`, `OwnerDSN`,
   `SuperuserDSN`) and accepts only `SuperuserRole` or `DeploymentOwnerRole`,
   because it has to know the owner's password to build a DSN. The plan fixed
   the parameters, not the return.
2. **`appRoleGrants` is extracted in `container.go`**, so the gate re-grants
   with exactly the harness's list rather than a copy.
   `TestEnsureAppRoleIsConcurrencySafe` still covers `ensureAppRole`.
3. **The schema-identity helper is committed** as
   `TestMigrationSchemaMatchesBaseline`. It skips unless
   `RAG_DOC_SCHEMA_BASELINE_MIGRATIONS` is set; the plan left its form open. It
   found a CRLF artifact in function-body hashes, which is normalised.
4. **The extension test goes further than the plan.** It pins the operator's
   recovery (`force 15`), the down's `must be owner`, the state a failed down
   leaves, and both recoveries from it.
5. **The gate asserts premises the plan did not list:** ownership before and
   after, FORCE on `repositories`, `RowsAffected = 1` on the uninstall,
   non-empty seeded tables and the chunk count, three tenants with
   repositories, and the seed and the outcome list agreeing. M2a, M2b and M8a
   show them working.
6. **A second applied migration is edited, 000013,** at the review's finding
   and the user's decision. Its section 3 carries the justification.
7. **The tenant audit** is the review's prototype re-done with a precise
   predicate (foreign keys only; two clauses; no version exempt) rather than
   every `ALTER TABLE` from 14 on.
8. **Fourteen extra mutations** beyond the plan's three, and the review's own
   MX13 re-run before and after.
9. **000014's header** carried the same "foreign-key checks run with row-level
   security bypassed" claim as section 4, so it says "per-row" too. 000013's
   header likewise.
10. **The seed includes more than the plan listed:** `users`,
    `organization_memberships`, a `syncing` repository, and an empty fourth
    organization inserted last.
11. **The gate's failure message names the ISS-031 class only when the
    SQLSTATE is `22P02`**, because M4's `23502` was being labelled ISS-031.
12. **Outside the plan's files, at the review's nits:**
    `services/workers/workers/jobs/transitions.py:10` no longer names
    `postgres:16-alpine` as the image we deploy;
    `services/workers/scripts/test_ingestion.py` and `test_query_engine.py`
    use `pkg/github/client.go` as their Go sample instead of the deleted
    `pkg/vectordb/client.go`.

## What 22-02 inherits

- **000017 runs through the gate** in one session after 000013 and 000015, and
  through the audit. Any foreign key it adds with `ALTER TABLE` fails there:
  with `22P02` in the one session, and by the audit in any session.
- **Add 000017's assertions** to the gate:
  - `chunks` is empty;
  - `retrievals` and `feedback` survive with a dangling `chunk_id`;
  - `symbols` exists;
  - there are 64 partitions, each with RLS.
- **The seed is at version 10**, so it holds `chunks` rows and a
  `retrievals` → `feedback` chain for 000017 to drop out from under.
- **The drift self-test belongs in a scratch database:**
  `isolation.ScratchDatabase(t, pool, …)`.
- **The audit counts a partitioned parent's rows without `ONLY`**, so a key
  on the new `chunks` parent is judged by the rows in its partitions.
