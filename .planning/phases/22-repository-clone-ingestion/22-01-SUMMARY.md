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
  - "ISS-031's live failure fixed: 000014 declares ingestion_jobs_repo_tenant_fk inside CREATE TABLE, with an identical resulting schema"
  - "TestMigrationsApplyToASeededDatabase: the seeded-migration gate, in CI from now on"
  - "TestMigration000016NeedsTheExtensionPreCreated, and TestMigrationSchemaMatchesBaseline (an env-gated proof tool)"
  - "isolation.ScratchDatabase(t, pool, owner), isolation.SuperuserRole, isolation.DeploymentOwnerRole"
  - "pkg/vectordb deleted, and github.com/qdrant/go-client with it"
affects: [22-02 (000017 runs through the gate and adds its assertions), 22-03, 24 (the operator creates the extension; pgvector >= 0.8.0; --shm-size)]

tech-stack:
  added: ["pgvector 0.8.6 (the image, pinned by digest)"]
  removed: ["github.com/qdrant/go-client, and the indirect grpc, genproto, protobuf and x/net it pulled in"]
  patterns:
    - "Declare foreign keys on new tables INSIDE CREATE TABLE: ADD CONSTRAINT's validation reads the referenced table through its row-level-security policy, as the migrating owner"
    - "Never rely on a migrating session's tenant; never make a validation pass with a sentinel tenant"
    - "A migration gate asserts its own premises (who owns the database and every table, that FORCE is on, that the fixture's one mutating step changed exactly one row), because a gate that silently ran as the superuser would pass the bug it exists to catch (M8b)"
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
    - services/backend/migrations/000014_ingestion_jobs.up.sql
    - services/backend/pkg/testing/isolation/container.go
    - services/backend/pkg/testing/isolation/migrator.go
    - services/backend/pkg/jobs/backfill_migration_test.go
    - services/workers/tests/isolation/conftest.py
    - .github/workflows/backend-ci.yml
    - docker-compose.yml
    - docs/local-development.md
    - services/backend/go.mod
    - services/backend/go.sum
  deleted:
    - services/backend/pkg/vectordb/

key-decisions:
  - "ISS-031 fixed by moving one constraint, not by lifting FORCE, not by NULLIF-tolerant policies and not by one session per migration (the plan's three rejected alternatives)."
  - "The gate runs in a scratch database inside the shared harness container, owned and migrated by rag_doc_owner, seeded as rag_doc_app under each organization's own tenant."
  - "ScratchDatabase's owner is a parameter, restricted to the two roles whose password the harness knows. pkg/jobs/backfill_migration_test.go passes SuperuserRole and keeps exactly its old behaviour."
  - "The schema-identity proof is committed as an env-gated test, so a reviewer can re-run it."

issues-closed: []
issues-updated: [ISS-031, ISS-035]
duration: ~5h
completed: 2026-09-17
---

# Phase 22 Plan 01: pgvector everywhere, and ISS-031 fixed behind a gate that saw it fail

**Every Postgres this project starts is now `pgvector/pgvector:pg16`, pinned
by digest, and migration 000016 fails on any image without the extension.**
The Go harness's reuse container is renamed, so a developer's leftover Alpine
container is never picked up.

**ISS-031 is fixed.** A database owned by a non-superuser, seeded, and migrated
from version 10 in one session failed at 000014 with `22P02` and was left dirty.
That is the shape the first production deploy will have. 000014 now declares
its foreign key inside `CREATE TABLE`, and the resulting schema is identical.

**The gate that proves it now runs in CI.** It failed on the commit that added
it (`4a33b08`, before the fix) and passes on every commit after `5677cc1`.

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

**`docs/local-development.md`** has a new section, "The Postgres image". It
covers:
- the four places, and the renamed container;
- the Alpine (musl) to Debian (glibc) volume question, marked
  **[not verified]**. The choice is recreate or `REINDEX DATABASE`, and it is
  the user's;
- the operator step, with the exact error and the `force 15` recovery;
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
operator again.

**`TestMigration000016NeedsTheExtensionPreCreated`** pins all three rows.
- **Premise:** the image has pgvector available, and the scratch database has
  not created it. So the failure can only be the privilege, not a wrong image,
  which would fail with `0A000` "is not available".
- **Without the extension:** migrate to 15 as `rag_doc_owner`, then to 16. The
  migration fails with `42501`, and `schema_migrations` reads 16, dirty.
- **The operator's recovery,** as the docs give it: create the extension as the
  superuser, `Force(15)`, and migrate to 16 again. It reaches 16, clean.
- **The down,** as the owner, fails with `42501 must be owner of extension
  vector`.

## ISS-031

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
  deployed, and what happens to databases already at 14;
- the rule.

### The gate passes with the fix, three runs, each on its own scratch database

```
go test ./pkg/testing/isolation -run 'TestMigrationsApplyToASeededDatabase|TestMigration000016NeedsTheExtensionPreCreated' -count=3
```

- **Three passes of each test.** The gate took 3.26 s cold (the container
  start included), then 0.34 s and 0.35 s.
- **The one-session `up`** from 12 to 16 took 85 to 87 ms.
- **`pg_database` afterwards** holds `isolation`, `postgres`, `template0` and
  `template1`. No scratch database was left behind.

The shared harness container was removed (`docker rm -f
rag-doc-isolation-tests-pgv16`) before these runs, so it rebuilt from the edited
000014.

### The schema is identical

`TestMigrationSchemaMatchesBaseline`, run with
`RAG_DOC_SCHEMA_BASELINE_MIGRATIONS` pointing at a scratch copy of the
migrations whose `000014` came from
`git show RAG-Doc/main:services/backend/migrations/000014_ingestion_jobs.up.sql`:

```
migration_seeded_test.go:360: identical: 550 catalog lines
--- PASS: TestMigrationSchemaMatchesBaseline (0.64s)
```

(Re-run on the final commit, `f766ce6`.)

**How it compares.**
- Both databases are migrated **empty**, in the deployment shape. The original
  000014 cannot migrate a seeded one.
- The dump covers:
  - `pg_constraint`, with `convalidated`, `condeferrable` and `condeferred`;
  - `pg_indexes` and `information_schema.columns`;
  - `pg_trigger`, `WHERE NOT tgisinternal`;
  - internal triggers **by the constraint they enforce**, not by their OID-based
    names;
  - `pg_policies`;
  - every relation's kind, RLS and FORCE flags, owner and comment;
  - column comments, functions (hash of the definition) and extensions.
- `ingestion_jobs_repo_tenant_fk` reads `convalidated = true` both ways.

**The tool is proven to see what it claims to.** A baseline with the key added
`NOT VALID` fails it on exactly one line:

```
-constraint | ingestion_jobs | ingestion_jobs_repo_tenant_fk | f | FOREIGN KEY (…) REFERENCES repositories(id, organization_id) ON DELETE CASCADE NOT VALID | false | false | false
+constraint | ingestion_jobs | ingestion_jobs_repo_tenant_fk | f | FOREIGN KEY (…) REFERENCES repositories(id, organization_id) ON DELETE CASCADE | true | false | false
```

**One artifact, found and handled.** The first comparison differed on one line,
the hash of `ingestion_jobs_fix_tenant`'s definition. That was line endings, not
schema. A function body keeps its file's line endings, and a Windows checkout is
CRLF where `git show` output is LF. The dump now strips `\r` before hashing, and
says why.

### The rule

It is written in three places:
- 000014's section 4;
- the gate's header;
- ISS-031.

It reads:
- declare foreign keys on new tables **inside `CREATE TABLE`**;
- **never rely on the session's tenant.** A migration that needs one sets it
  itself, per organization, as 000015 does. **000013 and 000015 both leave it
  at `''`**; M9 below measures the second;
- **never** make a validation pass with a sentinel tenant. The gate cannot tell
  that apart from a real fix (M3), so review has to catch it.

## The gate: `TestMigrationsApplyToASeededDatabase`

**The deployment shape.**
- `ScratchDatabase(t, pool, DeploymentOwnerRole)` creates a database owned by
  `rag_doc_owner`, a `NOSUPERUSER NOBYPASSRLS LOGIN` role.
- The role is created idempotently under the same `pg_advisory_xact_lock` as
  `ensureAppRole` (ISS-010).
- Its attributes are then **checked on every call**, because the role outlives
  any one run.

**The steps.**
1. The superuser runs `CREATE EXTENSION vector`, the operator's step.
2. `applyMigrationsTo(OwnerDSN, dir, 10)` migrates as `rag_doc_owner`, in its
   own session.
3. The harness's grants are given to `rag_doc_app` in this database. They are
   now one shared list, `appRoleGrants`, used by `ensureAppRole` too.
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
   - every seeded table is non-empty;
   - there are three chunks.
7. **One `m.Up()`**, from 12 to the newest version: one migrate instance, one
   pinned connection. golang-migrate's postgres driver holds a single `*sql.Conn`
   for the instance's life (`postgres.go:139`).
8. **Assertions, read as the superuser:**
   - `schema_migrations` is at the newest version, not dirty;
   - still the deployment shape;
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

- The `up` from 12 to 16: **85 to 92 ms**.
- The whole test: **about 0.35 s** on a warm container, and 3.2 s including a
  cold container start.

## Mutations

Committed code was never edited to run a mutation.
- **Migration mutations** ran from scratch copies of the migrations directory,
  passed through `RAG_DOC_SEEDED_GATE_MIGRATIONS`. Each was proven present and
  the original absent, and `diff -rq --strip-trailing-cr` shows exactly one file
  changed per copy.
- **Test and seed mutations** were made in the working tree after everything
  was committed. Each was restored with `git checkout --`, and `git diff --quiet`
  was confirmed after.

| # | Mutation | Expected | Result |
|---|---|---|---|
| T1 | `conftest.py` → `postgres:16-alpine` | the Python suite fails on 000016 | **killed**: 87 errors, all at the session fixture, `FeatureNotSupported: extension "vector" is not available … vector.control`; 197 non-database tests passed. Restored; diff clean |
| T2 | a stale `rag-doc-isolation-tests` container from `postgres:16-alpine` | the renamed harness still passes | **passed**. The developer's own old container, exited, was present throughout, so none was created and none removed. The harness made `rag-doc-isolation-tests-pgv16` from pgvector |
| M1 | restore the `ALTER TABLE … ADD CONSTRAINT` form | the gate fails with `22P02` | **killed**: 14, dirty, `SQLSTATE 22P02` |
| M2a | remove the seed's second organization (bravo) | — | **killed by the gate's premise**: 2 chunks, 3 expected |
| M2b | as M2a, with that premise relaxed | the 000013 assertion still passes | **survivor, as the plan predicted.** The 000013 subtest passes; 000015's fails only because the seed and the outcome list disagree. Recorded: the fixture alone cannot catch a last-tenant-only backfill. 000013's own `SET NOT NULL` is what does (M4) |
| M3 | a **sentinel tenant** (`…00dead`) set before the `ALTER TABLE` form | the gate passes: a recorded survivor | **survivor, as the plan predicted.** Every subtest passes. `ingestion_jobs` is empty at 14, so validation under the sentinel checks nothing. **Not shipped.** The rule forbids it, and review has to catch it |
| M4 | 000013's loop visits only the last organization (`ORDER BY ctid DESC LIMIT 1`) | — | **killed**: 13, dirty, `23502 column "organization_id" … contains null values` |
| M5 | 000015's loop visits only the last organization | — | **killed**: `a-pending: exactly one queued full_ingest job` |
| M6 | 000015's `INSERT` ignores `uninstalled_at` (neutered to `AND TRUE`) | — | **killed**: `a-pending-uninstalled: no job`. The step at 12 is load-bearing |
| M7 | drop the re-grant at 12 | `42501` (the fact-check's claim) | **killed**: `permission denied for table github_installation_tenants (SQLSTATE 42501)` |
| M8a | the gate's database owned by `SuperuserRole`, main's 000014 | — | **killed by the premise**: owner `isolation`, `rag_doc_owner` expected |
| M8b | as M8a, with the premise neutered | the plan's "a superuser owner passes" | **passes on the unfixed 000014.** This is the vacuous pass the premise exists to prevent |
| M9 | a probe `000017` adding a composite tenant key by `ALTER TABLE` | — | **killed**: 17, dirty, `22P02`. 000015 leaves `''` too, and the gate catches the class in later migrations |
| M9b | the same probe key declared inside `CREATE TABLE` | — | passes |
| S1 | schema-tool baseline with the key `NOT VALID` | the comparison fails | **fails on the one `convalidated` line** |

**The two recorded survivors are M2b and M3.** M8b is not a survivor of the
committed gate: M8a kills it.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `go mod tidy -diff` | clean. `pkg/vectordb` had no importer outside itself, and tidy dropped `github.com/qdrant/go-client` plus the indirect `grpc`, `genproto`, `protobuf` and `x/net` it pulled in |
| `go test ./... -count=1 -p 1` (`DATABASE_TEST_URL` on a scratch pgvector container on port 55481, `REDIS_URL` on a scratch Redis) | every package `ok` except `pkg/api/handlers`, whose only failure is `TestSignatureComparisonIsConstantTime`, the known CRLF artefact. **479 tests and subtests passed, 1 failed (that one), 4 skipped**: three pre-existing `pkg/auth` supersessions, and the schema tool with no baseline set |
| CI's package-parallelism step | same: all `ok` except the CRLF test |
| `-race` in `golang:1.25` (go1.25.14) with the Docker socket and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`, CI's package list | all `ok` except the CRLF test. **0 data races** |
| `go test ./pkg/jobs/... -count=1` | ok. The backfill test runs on the lifted helper with the superuser as owner, which is its old behaviour |
| `pytest tests/ workers/ -q` (`REDIS_URL` on db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no reachable `.env`, fresh venv from `requirements.txt`) | **284 passed** |
| up, down, up with the `migrate` CLI as the superuser on a fresh `pgvector/pgvector:pg16` database | up to 16, clean. `down -all` leaves no tables, functions, types or extension. Up again to 16, clean, and `ingestion_jobs_repo_tenant_fk` validated |
| `docker compose config` | the pgvector image. `docker ps` was identical before and after |
| gofmt | clean on the committed (LF) versions of every changed Go file |

**Port 5434 was never touched.** Every Go run had `DATABASE_TEST_URL` on the
scratch container, echoed before the run.

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
   found the CRLF artifact above, which is now normalised.
4. **The extension test goes further than the plan.** It pins the operator's
   recovery (`force 15`) and the down's `must be owner` as well, because the
   docs and 000016's down comment state both.
5. **The gate asserts premises the plan did not list:**
   - deployment-shape ownership, before and after;
   - FORCE on `repositories`;
   - `RowsAffected = 1` on the uninstall;
   - non-empty seeded tables and the chunk count;
   - the seed and the outcome list agreeing.

   M2a and M8a show two of them working.
6. **Seven extra mutations** (M4 to M9b, and S1) beyond the plan's three.
7. **000014's header** carried the same "foreign-key checks run with row-level
   security bypassed" claim as section 4, so it now says "per-row" too. This is
   a comment change only; the SQL diff is the constraint move alone.
8. **The seed includes more than the plan listed.** It adds `users`,
   `organization_memberships` and a `syncing` repository, for coverage the plan
   did not ask for, plus an empty fourth organization inserted last.
9. **Two commits refine the gate after it was committed.**
   - `9a3ef21` makes the schema tool readable and blind to line endings.
   - `f766ce6` names the ISS-031 class in the failure only when the SQLSTATE is
     `22P02`, because M4's `23502` was being labelled ISS-031.

   The recorded failure above is from `4a33b08`, before either. The same
   mutation (M1) under the final wording reads `… left schema_migrations at 14
   (dirty=true): SQLSTATE 22P02 …` followed by the `THE ISS-031 CLASS:` hint.

**Found and deliberately not changed,** because each is outside this plan's files:
- **`services/workers/workers/jobs/transitions.py:10`** says 21-02's statements
  ran on "`postgres:16-alpine`, the version we deploy". The first half is
  history; the second is now wrong. `pkg/jobs/schema_test.go:5` has the same
  history.
- **Two Qdrant-era manual scripts reference the deleted
  `pkg/vectordb/client.go` as sample content:**
  `services/workers/scripts/test_ingestion.py` and `test_query_engine.py`. The
  ingestion script skips a missing file with a warning. 22-03 retires both
  scripts' Qdrant paths.

## What 22-02 inherits

- **000017 runs through the gate** in one session after 000013 and 000015. Any
  foreign key it adds with `ALTER TABLE` fails there with `22P02`, as M9 shows.
- **Add 000017's assertions** to the gate:
  - `chunks` is empty;
  - `retrievals` and `feedback` survive with a dangling `chunk_id`;
  - `symbols` exists;
  - there are 64 partitions, each with RLS.
- **The seed is at version 10**, so it holds `chunks` rows and a
  `retrievals` → `feedback` chain for 000017 to drop out from under.
- **The drift self-test belongs in a scratch database:**
  `isolation.ScratchDatabase(t, pool, …)`.
