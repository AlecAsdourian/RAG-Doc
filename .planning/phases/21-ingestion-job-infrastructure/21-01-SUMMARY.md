---
phase: 21-ingestion-job-infrastructure
plan: 01
subsystem: database
tags: [postgres, rls, tenancy, migrations, foreign-keys, triggers]

requires:
  - phase: 17-03
    provides: trg_assert_tenant on repositories, which is why the backfill runs one organization at a time
  - phase: 20-02
    provides: every organization has a default project, so every repository has a project to copy from
  - phase: 20-05
    provides: 000012's backfill pattern, and the lesson of why it does not transfer here
provides:
  - "Migration 000013 — repositories.organization_id NOT NULL, projects_id_org_key, repositories_project_org_fkey (composite, the guarantee), repositories_id_org_key (what 21-02 references), the fill-and-guard trigger, and D5's reject_cross_org_reparent verbatim"
  - "isolation.AssertNoRepositoryTenantDrift / CheckRepositoryTenantDrift — D5's drift-detection check, reusable from any package"
  - "isolation.WithSuperuserConn — a hijacked, closed-after-use superuser connection for tests that need one"
affects: [21-02 (composite FK onto repositories_id_org_key), 22 (chunks.organization_id under D5)]

tech-stack:
  added: []
  patterns:
    - "Guarantee a denormalised tenant column with a COMPOSITE FOREIGN KEY to the parent's (id, organization_id), not only a trigger: FK checks bypass RLS, so the guarantee holds whatever tenant context the writer has"
    - "Prove a backfill with ALTER COLUMN ... SET NOT NULL, never with a SELECT: the validation scan ignores row-level security, a SELECT under FORCE RLS sees one tenant or nothing"
    - "Verify a migration's backfill in the DEPLOYMENT shape (tables owned by a NOSUPERUSER NOBYPASSRLS role under FORCE RLS), not only as the harness superuser, who bypasses the thing being tested"
    - "A drift check must refuse to run under RLS, or it passes without reading the table"
    - "Mutation-test a migration against a FRESH container every time: a reused container at version N never re-applies a changed migration N"

key-files:
  created:
    - services/backend/migrations/000013_repositories_organization_id.up.sql
    - services/backend/migrations/000013_repositories_organization_id.down.sql
    - services/backend/pkg/testing/isolation/drift.go
    - services/backend/pkg/testing/isolation/repositories_organization_id_test.go
  modified:
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "The guarantee is the composite foreign key repositories (project_id, organization_id) -> projects (id, organization_id), which goes beyond D5's wording (a trigger that keeps the copy in step). The trigger remains, to fill the column on insert and to give readable errors."
  - "The backfill sets app.current_tenant one organization at a time; no guard (trg_assert_tenant, FORCE RLS) is lifted. 000012's lift-FORCE pattern raises 42501 here on any database with a repository, measured."
  - "A missing project on insert is left to the database rather than reported by the trigger: the trigger raises nothing, so it is no oracle for which project ids exist in other tenants."
  - "The mismatch branch is gated on the caller's tenant (added after PR #37's review): it names a project only when that project belongs to the caller's organization. Outside it, the trigger falls through and row-level security refuses the row with the same error a non-existent project gets. The error also no longer names the owning organization."
  - "The trigger's two refusals use SQLSTATE 42501, matching assert_installation_matches_repository_tenant on the same table; tests assert the message, not only the code."
  - "The drift helper lives in a non-test file, because 21-02 calls it from pkg/jobs."

issues-created: []

duration: ~40 min to the docs commit (branch created 12:39 PDT per reflog; docs committed ~13:17)
completed: 2026-09-14
---

# Phase 21 Plan 01: `repositories.organization_id`

**`repositories` now stores its tenant, and a composite foreign key to `projects (id, organization_id)` makes that copy impossible to drift — proven on seeded data under row-level security, in the shape we deploy.**

## The final migration

`000013_repositories_organization_id`, in this order:

1. `ADD COLUMN organization_id UUID`, nullable for now.
2. `projects_id_org_key UNIQUE (id, organization_id)` — the key the composite FK references.
3. **Backfill, one organization at a time,** in one `DO` block that sets `app.current_tenant` per organization. It satisfies `trg_assert_tenant` and `FORCE ROW LEVEL SECURITY` without lifting either.
4. **`SET NOT NULL` — the proof** that the backfill reached every row.
5. **`repositories_project_org_fkey`** `FOREIGN KEY (project_id, organization_id) REFERENCES projects (id, organization_id) ON DELETE CASCADE` — **the guarantee.** The original `project_id` FK is kept.
6. `repositories_id_org_key UNIQUE (id, organization_id)` — what 21-02's `ingestion_jobs` FK references.
7. `repositories_organization_id_guard()` on `BEFORE INSERT OR UPDATE OF organization_id`: fills the column from the project on insert, refuses a mismatched value (42501, readable message), refuses any rewrite (42501). Branches on `TG_OP`; `search_path` pinned; body schema-qualified.
8. D5's `reject_cross_org_reparent()` and `trg_reject_cross_org_reparent`, **verbatim**, UPDATE-only.
9. `COMMENT ON COLUMN`: a copy of `projects.organization_id`, guaranteed by the FK, filled by trigger, an **authorization input**, never written by application code.

The down migration drops all of it in reverse order.

### How it departs from D5's wording

D5 asks for "a trigger on the parent that keeps it in step". **The guarantee here is a composite foreign key instead, which is stronger:**

- **Foreign-key checks bypass row-level security,** so the key holds whatever tenant context the writer has, and survives the trigger being disabled, dropped or wrong. Test 3 shows it holding with the trigger off and RLS bypassed.
- **It fixes a project's organization while the project has repositories.** `UPDATE projects SET organization_id` now fails with 23503 (test 7). Nothing in the codebase does that today; the only writer of `projects.organization_id` was 000010's one-time backfill.

The triggers stay, for what a key cannot do: fill the column so no writer has to name it, and word the error.

### L5's re-parent item: closed at the repository level

21-CONTEXT L5 left "drift on re-parent" open, deliberately. At the repository level it is now closed by the schema rather than by convention:

- cross-organization re-parent → D5's message (test 6), and behind it RLS's `WITH CHECK`, and behind that the composite FK
- moving a project that has repositories to another organization → 23503 (test 7)
- rewriting the column → refused (test 4)

**Once 21-02 lands, jobs → repositories → projects is enforced by foreign keys end to end:** `ingestion_jobs (repository_id, organization_id) → repositories (id, organization_id)`, and `repositories (project_id, organization_id) → projects (id, organization_id)`.

## Facts the plan rested on, checked

| Claim | Result |
|---|---|
| `trg_assert_tenant` refuses an unscoped write to `repositories` even from a superuser | **True.** Superuser, no tenant: `42501 tenant isolation violated`. Same with `FORCE` lifted. |
| `projects` has no RLS | **True.** `relrowsecurity = f`, and no user triggers on `projects` or `organizations`. |
| `SET NOT NULL`'s validation scan is not subject to RLS | **True, measured in the deployment shape.** Run by a `NOSUPERUSER NOBYPASSRLS` table owner under `FORCE RLS`, with a backfill that skipped one organization: a `SELECT count(*) … WHERE organization_id IS NULL` in the same transaction returned **0**, and `SET NOT NULL` then failed with **23502**. |
| After the `DO` block, `app.current_tenant` is `''` | **Half true.** `set_config(..., true)` lasts to the end of the transaction, so for the rest of the file the tenant is the **last organization's id** (DML would silently see one tenant). After commit it is `''`, and a same-session read of `repositories` raised **22P02** — measured. The migration comment says both. |

## Seeded-backfill evidence

Scratch `postgres:16-alpine`, golang-migrate v4.19.1 (the version CI pins). Seed: four organizations — A with two projects and three repositories, B with two, C with one, D with none — every repository inserted under its own tenant. Each run on a fresh copy of the seeded database. The SQL for every run was **cut out of the committed migration file** by a script, not retyped, and the final runs were repeated after the last comment edit.

**Harness shape — the plan's check.** Tables owned by the superuser; steps 1-2 as owner, the `DO` block under `SET ROLE rag_doc_app` (`rolsuper = f, rolbypassrls = f`, role and grants as `container.go` creates them), the rest as owner:

| org | repositories | carries its project's org | NULL |
|---|---|---|---|
| org-a | 3 | 3 | 0 |
| org-b | 2 | 2 | 0 |
| org-c | 1 | 1 | 0 |

`drifted_rows = 0`, `organization_id` NOT NULL, all three constraints and both triggers present and enabled. Committed.

**Deployment shape.** A `rag_doc_owner NOSUPERUSER NOBYPASSRLS` role owns every table (migrations 1-12 applied by golang-migrate connecting as it; `repositories` `forced = t`). The whole of 000013 applied as that owner — once through `psql` in one transaction, once through `migrate up` — gave the identical table above, `schema_migrations = 13, dirty = false`.

**Harness shape, full file, `migrate up` as superuser on seeded data:** same table, committed.

**Up, down, up** on an empty CI-shaped database: clean. `pg_dump --schema-only` at version 12 and after `up` → `down 1` are identical, as are the two version-13 dumps — excluding one line pg_dump regenerates on every run (`\restrict <random token>`).

## Mutation results

### Task 1 — the backfill, on seeded data

| Mutation | Plan predicted | DO block under `rag_doc_app` | As superuser | Deployment shape (RLS-subject owner) |
|---|---|---|---|---|
| Drop `set_config` from the loop | 42501 | **no 42501:** `UPDATE`s match 0 rows under RLS, so no row trigger fires; `SET NOT NULL` fails **23502**; nothing committed | **42501** from `trg_assert_tenant`; nothing committed | 0 rows; **23502**; nothing committed |
| Loop skips organization B | `SET NOT NULL` fails | **23502** | **23502** | **23502** — while the `SELECT` check under RLS saw 0 NULL rows |
| One unscoped `UPDATE` instead of the loop | 42501 | **no 42501:** `UPDATE 0`; **23502** | **42501** | `UPDATE 0`; **23502** |
| 000012's pattern (lift `FORCE`, unscoped `UPDATE`) | — | — | — | **42501** from `trg_assert_tenant` |

Every mutation is refused and nothing commits. **The plan's 42501 prediction holds only for an RLS-bypassing role.** Under a role RLS applies to, the unscoped `UPDATE` is filtered to zero rows before a row trigger can fire (the same layering `db_assertion_test.go` documents for UPDATE/DELETE), and `SET NOT NULL` is what catches it — which is the plan's own argument for making `SET NOT NULL` the proof.

### Task 2 — the invariants

Each on a copy of the file, each against a freshly built harness container. "Package" = all of `pkg/testing/isolation`; otherwise `-run OrganizationID`.

| Mutation | Plan predicted | Result |
|---|---|---|
| Remove the NULL-fill | test 1 and every fixture-based test fail | **Killed** (package): 17 of 24 top-level tests — test 1, every `WithTwoOrgs`-based test old and new — with 23502 |
| Remove the mismatch `RAISE` | test 2 gets an FK error instead of the message | **Killed**: test 2 only, 23503 instead of 42501 |
| Drop `repositories_project_org_fkey` | tests 3 and 7 fail | **Killed**: tests 3, 7, 9 and the drift self-test |
| Remove D5's UPDATE-only guard | every insert fails | **Survived** (package, 24/24 pass) — see below |
| Widen D5's attachment to `INSERT OR UPDATE`, guard kept | — | **Killed**: test 9 only (the attachment is pinned) |
| Widen the attachment **and** remove the guard | — | **Killed** (package): 18 of 24; every insert raises `cannot move repository <NULL> across organisations` |
| Remove the rewrite `RAISE` *(added)* | — | **Killed**: test 4, 23503 from the composite FK instead |
| Remove D5's cross-organization `RAISE` *(added)* | — | **Killed**: test 6, RLS's `WITH CHECK` message instead |
| Drift query never matches *(added)* | — | **Killed**: the drift self-test only |
| Drift check skips its RLS-bypass refusal *(added)* | — | **Killed**: the refuses-under-RLS self-test only |

**The guard mutation survives, and it should.** `trg_reject_cross_org_reparent` is attached `BEFORE UPDATE OF project_id`, so its `TG_OP <> 'UPDATE'` branch is unreachable; removing it changes nothing. D5 says "the guard and the attachment below are both load-bearing", and the three rows above measure exactly that: the attachment alone is caught by test 9, the guard alone is dead code, and the two together break every insert. Test 9 pins the attachment so the combination cannot happen one half at a time.

**A harness hazard found on the way.** A drift-helper mutation, run on the container the previous migration mutation had built, failed an unrelated test: that container still held the mutated schema at version 13, and golang-migrate never re-applies a recorded version. Re-run on a fresh container, the result above is clean. Mutation-testing a migration needs a fresh container per run — worth knowing for 21-02.

## Deviations from the plan

**1. Facts corrected (implementation matches reality; recorded in the migration comments).**
- **M1/M3 do not raise 42501 under `rag_doc_app`** (table above). The migration still fails, at `SET NOT NULL`.
- **"Remove D5's UPDATE-only guard → every insert fails" is false for that mutation alone** (table above).
- **A missing project on insert is not worded by the `project_id` FK,** as the plan's step 7 says. Measured: under a role RLS applies to, RLS's `WITH CHECK` refuses it (42501) — unchanged from before this migration.

  **⚠ This entry originally claimed a project in another organization "gets the identical error, so there is no existence oracle here". PR #37's review measured that false, and it is now fixed.** The claim held for the `NOT FOUND` branch it was written about, but not for the mismatch branch twelve lines below: a caller naming **their own** organization id alongside a guessed project id got `organization_id … does not match project …, which belongs to organization <the other org's UUID>`. That answered "does this project exist, and who owns it?" from inside another tenant. No writer names the column, so it was never reachable over HTTP — latent, like `assert_installation_matches_repository_tenant` (000010), which discloses the same way and is left alone.

  **The fix** (`9d69e30`, `fef62f0`): the message no longer names the owning organization, and the mismatch branch is gated on `current_setting('app.current_tenant', true)` — outside the caller's tenant it falls through, and RLS refuses the row with the message a non-existent project gets. An unset or empty tenant (a superuser, a migration) keeps the readable error, since such a caller already reads every project.

  **Pinned by test 2b** (`TheTriggerIsNotAnExistenceOracle`), which asserts the two errors are identical in SQLSTATE and message. **Its first version was wrong and the mutation caught it:** it named org B's project together with org B's id, which agree, so the mismatch branch never ran and the test passed against the unfixed trigger. Corrected to name org A's own id with org B's project — the only combination that reaches the branch. With the gate replaced by `IF TRUE`, on a fresh container, test 2b fails on the differing message; with the gate, all 25 tests in the package pass. Under an RLS-bypassing role, NOT NULL refuses it (23502), where before it was the FK (23503): NOT NULL is checked as the row is written, foreign keys at statement end. The behaviour the plan specified (return `NEW`, raise nothing) is kept; only the comment's reason changed. No application code branches on 23502, 23503 or 42501.
- **D5's prose, not its code:** it says a move to a non-existent project "yields NULL on both sides of the comparison", leaving the FK to reject it. Only the new side is NULL, so D5's own trigger raises its "across organisations" message first — measured. The move is refused either way; the function is left verbatim, with a comment.
- **`app.current_tenant` after the `DO` block** is the last organization's id until commit, then `''` (facts table).

**2. The drift helper is in `drift.go`, not the test file.** 21-02 calls `AssertNoRepositoryTenantDrift` from `pkg/jobs`, which cannot import a `_test.go` file. `CheckRepositoryTenantDrift` (the pure core, taking a `pgx.Tx` or `*pgx.Conn`) and `WithSuperuserConn` are exported beside it.

**3. Additions.**
- Two self-tests for the drift check: it **detects** manufactured drift (both guards removed inside a rolled-back transaction), and it **refuses to run under RLS**, where it would pass without reading the table. A check never seen to fail proves nothing.
- Test 9 also pins both triggers' attachments and that both are enabled — the latter guards against a test's transactional `DISABLE TRIGGER` ever leaking into the reused container.
- The trigger's SQLSTATEs (42501), which the plan did not specify.

**4. Verification environment.**
- `go test ./...` includes `pkg/auth`, whose helpers default to `localhost:5434` — the docker-compose Postgres. It was pointed at a scratch CI-shaped Postgres instead (`DATABASE_TEST_URL`, migrated with the CLI as CI does), and `REDIS_URL` at database 15.
- Development and mutation runs used a temporarily renamed harness container (never committed), so the shared `rag-doc-isolation-tests` container was not left holding a mutated schema.

None of these changes what the migration guarantees or departs further from D5.

## Verification

| Check | Command | Result |
|---|---|---|
| Backend, whole module | `DATABASE_TEST_URL=<scratch CI-shaped Postgres> REDIS_URL=redis://localhost:6379/14 go test ./... -count=1 -p 1 -v` (from `services/backend` of a clean `git archive` of the code commit, on a freshly created harness container) | **135 top-level tests pass (376 with subtests), 3 skip, 1 fails.** The failure is `TestSignatureComparisonIsConstantTime` ("could not find the end of verifySignature"), the known failure on a Windows CRLF checkout (`git archive` on this machine writes CRLF); this PR does not touch the file it reads. The skips are `pkg/auth`'s pre-existing "superseded by pkg/testing/isolation (Phase 17-01)". All 10 `TestRepositoriesOrganizationID_*` pass. |
| New tests | `go test ./pkg/testing/isolation/ -run OrganizationID -count=1 -v` | 10/10 pass |
| Workers, CI environment | `REDIS_URL=redis://localhost:6379/15 OPENAI_API_KEY=sk-test-dummy pytest tests/ workers/ -q`, no `DATABASE_URL`, no `.env`, fresh venv from `requirements.txt`, run from a clean `git archive` of the code commit | **181 passed**, 0 skipped, 0 failed (19 `utcnow` deprecation warnings, pre-existing) — including the isolation tests, which apply 000013 through psycopg2 |
| Up, down, up | `migrate goto 12` → `up` → `down 1` → `up` (v4.19.1) | clean; schema dumps identical |
| Seeded backfill under `rag_doc_app` | above | every row carries its project's organization |
| Seeded, deployment shape | above | same; `SET NOT NULL` proven RLS-independent |
| CI isolation scanner | `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD --json` | nothing missing (no endpoint changed) |
| Commit trailers | `git log --format=%B RAG-Doc/main..HEAD` | none |

## Follow-ups

- **Simplify `repositories`' RLS policy to scalar equality** on the new column (`organization_id = current_setting('app.current_tenant', true)::uuid`). D5 names it as a consequence worth having; it is deliberately out of scope, because changing an RLS policy deserves its own isolation review. The same applies to `ingestion_runs` and `chunks`, whose policies join through `repositories`.
- **ISS-013 across migrations — now filed as ISS-031.** After 000013 commits, the migrating session holds `''`. A later migration applied **in the same run** that reads a row-level-security table must set a tenant first, or it fails with 22P02 on any database with rows — and passes on CI's empty one. 21-02's 000014 is DDL only, so it is unaffected. PR #37's review made the point that a comment is the only guard: ISS-031 carries the two candidate fixes it measured, and recommends a CI check that applies migrations to a **seeded** database, before a later plan in this phase adds a migration with DML.
- **Three minor items from PR #37's review,** none blocking, all deliberately not fixed here:
  - `CheckRepositoryTenantDrift` joins `projects` inner, so a repository whose project vanished is dropped by the very check meant to survive it. Unreachable while the `project_id` foreign key stands; a `LEFT JOIN` would be strictly better.
  - `SchemaShape` pins `pg_get_constraintdef` output verbatim, which PostgreSQL may reword across major versions.
  - The backfill is O(organizations × repositories). Fine at this size; worth a note if either grows large.

## Next Phase Readiness

21-02 can reference `repositories_id_org_key` and call `isolation.AssertNoRepositoryTenantDrift` and `isolation.WithSuperuserConn`.

---
*Phase: 21-ingestion-job-infrastructure — 1 of 7 plans*
*Completed: 2026-09-14*
