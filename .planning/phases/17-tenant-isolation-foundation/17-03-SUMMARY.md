---
phase: 17-tenant-isolation-foundation
plan: 03
subsystem: database

requires:
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs, TenantScope
  - migration: 000008 (RLS policies)
provides:
  - Migration 000009 — assert_tenant_scoped() PL/pgSQL function + BEFORE INSERT/UPDATE/DELETE trigger on 6 tenant-scoped tables
  - Table-driven test suite pinning trigger coverage
affects: [17-04 (Python harness — cross-language firing verified), 17-05 (docs), every future phase adding tenant-scoped tables must extend both trigger and test list]

tech-stack:
  added: []
  patterns:
    - "PL/pgSQL trigger with SQLSTATE 42501 and HINT pointing to docs/isolation.md"
    - "Trigger scope matches migration 000008 RLS coverage (6 tables, not the plan's 9)"

key-files:
  created:
    - services/backend/migrations/000009_tenant_assertion.up.sql
    - services/backend/migrations/000009_tenant_assertion.down.sql
    - services/backend/pkg/testing/isolation/db_assertion_test.go
    - .planning/phases/17-tenant-isolation-foundation/17-03-SUMMARY.md

key-decisions:
  - "Option A trigger coverage (6 tables) over plan's Option B (9 tables). Plan's list included organizations, projects, organization_memberships — attaching the trigger there breaks signup (the org must be creatable before any tenant exists to scope the write to) and every fixture that seeds data. Extending coverage to those tables would drag pkg/auth/provisioning.go and every fixture's insert into 17-03's scope. Option A caps the migration at exactly the tables migration 000008 already covers with RLS — same layered-defense unit."
  - "UPDATE/DELETE scenario assertions loosened from 'trigger fires 42501' to 'write is refused OR affects zero rows'. Postgres quirk: once SET LOCAL app.current_tenant runs and commits on a connection, the custom GUC becomes an empty string on that session, not NULL. Migration 000008's RLS ::uuid cast on '' raises 22P02 during row identification, before the trigger's per-row hook can fire. That's still a refusal — the row is not written — but with a different SQLSTATE than the trigger raises for INSERT. Test asserts the safety property (write does not land) rather than the specific error code."

patterns-established:
  - "assert_tenant_scoped() is generic — new tenant-scoped tables attach the trigger with one ALTER TABLE line and add themselves to protectedTables in db_assertion_test.go."
  - "SQLSTATE 42501 is the canonical isolation-refusal code; migrations and tests use it consistently."

issues-created: []

duration: ~40 min
completed: 2026-09-06
---

# Phase 17 Plan 03: DB-level tenant assertion trigger

**Second wall of tenant isolation. Migration 000009 adds a BEFORE-INSERT/UPDATE/DELETE trigger on the 6 tables migration 000008 already protects with RLS. Any raw mutation without app.current_tenant set — from a debug script, a new worker, a fixture that forgot — is refused loudly with SQLSTATE 42501 and a HINT pointing to the isolation docs.**

## Accomplishments

- `assert_tenant_scoped()` PL/pgSQL function raises 42501 with clear message identifying operation and table
- Trigger attached to 6 tables: repositories, ingestion_runs, chunks, queries, retrievals, feedback
- Down migration cleanly removes triggers then function
- 11 subtests across 5 top-level tests pin: INSERT fires trigger (per table); UPDATE/DELETE refused (via trigger or RLS); happy-path mutations under TenantScope succeed; exempt tables unaffected; WithTwoOrgs still works end-to-end
- Existing 17-02 endpoint tests still pass — no regression from adding the trigger

## Task Commits

Two atomic commits:

1. `8ec1f62` — **feat(17-03):** migration 000009 with `assert_tenant_scoped` trigger on 6 tenant-scoped tables
2. `dd3e0b9` — **test(17-03):** verify trigger fires on INSERT and refuses UPDATE/DELETE across 6 tenant-scoped tables

_This SUMMARY commits separately as `docs(17-03):`._

## Deviations from Plan

### 1. Trigger coverage: 6 tables (Option A) instead of the plan's 9

Plan Task 1 listed 9 tables: repositories, ingestion_runs, chunks, queries, retrievals, feedback, **organizations, projects, organization_memberships**. The three additions break the workflow in two independent ways:

- **Signup chicken-and-egg.** When a new user signs up, `pkg/auth/provisioning.go` creates their organization. There is no tenant scope to set — the org IS the tenant, and it doesn't exist yet. This is exactly the reason the plan already exempts `users`. Organizations has the same shape.
- **Every fixture insert.** `WithTwoOrgs` (from 17-01) inserts organizations, projects, and memberships to build the two-tenant scaffold. None of those go through TenantScope today. Attaching the trigger without fixing every seed path would break Task 2 scenario 6 (`WithTwoOrgs still works`) — which the plan explicitly asserts must still hold.

Extending trigger coverage to those three tables would drag production provisioning code and every isolation fixture into 17-03's scope. The chosen resolution (Option A, out of three presented to the planner) caps coverage at exactly the tables migration 000008 already protects with RLS — 6 tables. Layered defense on the same tables in both directions.

If a future phase wants to expand coverage — e.g., after 19-03 lands and provisioning starts consciously setting tenant scope during signup — extending the trigger is one ALTER TABLE per new table plus one line in `protectedTables` in `db_assertion_test.go`. The migration header documents the pattern.

### 2. UPDATE/DELETE assertions weakened from "trigger fires 42501" to "write is refused OR affects zero rows"

The plan's scenarios 3 (UPDATE without tenant) and 4 (DELETE without tenant) both expected the trigger to fire with 42501. That expectation is impossible in practice for a subtle reason unearthed during test development:

> Postgres registers a custom `app.*` GUC as an empty string on the session the first time any `SET LOCAL app.foo = 'x'` transaction commits — not as unset. So `current_setting('app.current_tenant', true)` returns `''`, not NULL, on any connection that ever entered a TenantScope. Migration 000008's RLS policies cast that string to uuid — `''::uuid` raises SQLSTATE 22P02 during row identification, before the BEFORE-UPDATE/DELETE trigger's per-row hook can fire.

Net effect: on a pool-reused connection, UPDATE/DELETE without tenant IS refused, but by RLS (22P02) rather than the trigger (42501). On a truly fresh connection that never touched TenantScope, the same operations would target zero rows silently (RLS filter, no error). Both outcomes are safe — the row does not change — but neither exercises the trigger's UPDATE/DELETE branch under normal RLS.

The trigger's UPDATE/DELETE branch is real defense-in-depth for the case where RLS is disabled (superuser, or an explicit DISABLE ROW LEVEL SECURITY). Testing that path would require superuser privileges the `rag_doc_app` test role deliberately doesn't have.

Scenarios 3 and 4 in `db_assertion_test.go` therefore assert the safety property (`err != nil` with acceptable SQLSTATE, OR `RowsAffected() == 0`) plus a follow-up `SELECT` confirming the row is unchanged / still present. Comments in the test file document why this shape.

**Not a bug.** The RLS-first 22P02 behavior is actually the more defensive outcome — a caller who forgot to set tenant on a reused connection gets a loud error rather than a silent zero-row no-op. If we ever want it to be quieter we'd wrap `current_setting(...)` in `NULLIF(..., '')` in a follow-up migration. Not doing that here — the loudness is a feature.

### 3. Not fixed (pre-existing, unrelated)

- `pkg/auth/webhook_test.go::TestGenerateOrgSlugFromEmail` still fails on `main` (`expected "my-org", actual "-org"`). Documented in 17-02 SUMMARY; not in scope.
- `pkg/vectordb` still has pre-existing qdrant API drift. All 17-03 touched packages build and vet cleanly.

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/testing/isolation/...` | clean |
| `go test -count=1 -run TestDBAssertion ./pkg/testing/isolation/...` | 11/11 pass (~0.5s warm) |
| `go test -count=1 ./pkg/testing/isolation/...` (17-01 regression) | all pass |
| `go test -count=1 ./pkg/api/handlers/... ./pkg/client/...` (17-02 regression) | all pass |
| `migrate up` (via harness) | idempotent, applies 000009 on fresh container |
| `migrate down 1` | reverses cleanly (trigger removed before function) |

## Issues Encountered

- **Empty-string GUC behavior after SET LOCAL commit.** Discovered while writing scenario 3. Postgres tracks a custom `app.*` parameter as empty string on the session forever after any `SET LOCAL` transaction commits on that connection. Neither `RESET` nor `SET TO DEFAULT` clears it. Rewrote UPDATE/DELETE assertions to match reality (see deviation 2).

## Next Phase Readiness

- **17-04 (Python isolation harness):** the trigger fires from any language — Python writes go through the same pgx/asyncpg → Postgres path. 17-04's Python tests should mirror db_assertion_test.go's INSERT scenarios and confirm cross-language enforcement.
- **17-05 (CI gate + docs):** `docs/isolation.md` should call out (a) the trigger's coverage list and how to extend it, (b) SQLSTATE 42501 as the canonical isolation-refusal code, (c) the empty-string GUC quirk so future contributors don't re-discover it.
- **Phase 20+ handlers reading tenant-scoped tables:** paired with ISS-008 (17-02) — a request-scoped tenant transaction pattern must be in place before any handler runs a direct DB read on the trigger-protected tables. Without SET LOCAL, every write would 42501 and every UPDATE/DELETE would 22P02.

---
*Phase: 17-tenant-isolation-foundation*
*Completed: 2026-09-06*
