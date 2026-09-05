---
phase: 17-tenant-isolation-foundation
plan: 01
subsystem: testing

requires:
  - phase: v0.9-database-setup
    provides: 8 migrations (000001-000008) including RLS policies on tenant-scoped tables
provides:
  - SetupTestDB (testcontainers-go Postgres + migrator, cross-process reusable)
  - TenantScope (transaction scoped to app.current_tenant)
  - WithTwoOrgs (two-tenant fixture with owner/admin/member users and a starter repo)
  - AssertNoCrossTenantLeak (test-facing assertion) and CheckCrossTenantLeak (pure-Go core)
  - TestJWT (HS256 test token signer)
affects: [17-02, 17-03, 17-04, 17-05, and every future v1.0 phase that adds a mutation endpoint]

tech-stack:
  added:
    - github.com/testcontainers/testcontainers-go v0.44.0
    - github.com/testcontainers/testcontainers-go/modules/postgres v0.44.0
    - github.com/golang-migrate/migrate/v4 (programmatic migrator)
  patterns:
    - Ephemeral, reused Postgres container per test package
    - Non-superuser `rag_doc_app` role so RLS actually enforces (superusers bypass it)
    - Test transactions with SET LOCAL app.current_tenant for tenant scoping

key-files:
  created:
    - services/backend/pkg/testing/isolation/container.go
    - services/backend/pkg/testing/isolation/migrator.go
    - services/backend/pkg/testing/isolation/tenants.go
    - services/backend/pkg/testing/isolation/fixtures.go
    - services/backend/pkg/testing/isolation/doc.go
    - services/backend/pkg/testing/isolation/container_test.go
    - services/backend/pkg/testing/isolation/fixtures_test.go
  modified:
    - services/backend/go.mod
    - services/backend/go.sum
    - .planning/ISSUES.md (ISS-006 → closed)
    - .planning/STATE.md (removed ISS-006 from deferred list)

key-decisions:
  - "testcontainers-go over docker-compose: fully self-managed, no host-port-binding fragility (resolves ISS-006)"
  - "Shared container per test package + Ryuk disabled: subsequent go test invocations start in ~0.5s"
  - "Non-superuser `rag_doc_app` role created after migrations, applied via pgxpool AfterConnect: without it, superuser bypasses RLS and every isolation test would silently pass"
  - "CheckCrossTenantLeak split from AssertNoCrossTenantLeak so the harness's own self-tests can verify both catch-a-leak and no-leak paths without intercepting t.Error"

patterns-established:
  - "`SetupTestDB(t)` is the single entry point for any future Go integration test that touches Postgres"
  - "`WithTwoOrgs(t, pool, fn)` is the canonical fixture for any test that exercises tenant boundaries"
  - "`AssertNoCrossTenantLeak(t, pool, tenantA, tenantB, action, observe)` is the canonical primitive for cross-tenant assertions — action commits as A, observe verifies B cannot see the effect"
  - "Test-only helpers live under `pkg/testing/` and never ship in production binaries"

issues-created: []

duration: ~15 min
completed: 2026-09-05
---

# Phase 17 Plan 01: Go Test Harness Foundation Summary

**Testcontainers-go Postgres harness with WithTwoOrgs fixture and AssertNoCrossTenantLeak primitive — the shared foundation every future Go isolation test uses.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-09-05 (worker session bootstrap)
- **Completed:** 2026-09-05
- **Tasks:** 2 (both from plan)
- **Files created:** 7 (5 source, 2 test)
- **Files modified:** 4 (go.mod, go.sum, ISSUES.md, STATE.md)

## Accomplishments

- Ephemeral Postgres via testcontainers-go with cross-process container reuse; second `go test` run completes in ~0.5s
- `SetupTestDB` applies all 8 migrations programmatically (golang-migrate) and connects the pool as a non-superuser role so RLS actually enforces
- `WithTwoOrgs` produces two fully populated tenants (owner/admin/member users, project, repo) with slugs seeded from the test name for debuggability
- `AssertNoCrossTenantLeak` primitive proven both ways: catches synthetic leaks (via RESET ROLE bypass) and passes on real isolation
- ISS-006 closed — original host-connectivity problem is gone because testcontainers manages its own port binding

## Task Commits

Each task committed atomically per the plan's convention:

1. **Task 1: Test container + migration runner** — `c429592` (feat)
2. **Task 2: Fixtures and cross-tenant assertion primitive** — `74b16be` (feat)

_This SUMMARY and the ISS-006 status change land in a follow-up docs commit._

## Files Created/Modified

- `services/backend/pkg/testing/isolation/container.go` — SetupTestDB, container reuse via `WithReuseByName`, non-superuser role setup
- `services/backend/pkg/testing/isolation/migrator.go` — programmatic golang-migrate runner, idempotent (safe under container reuse)
- `services/backend/pkg/testing/isolation/tenants.go` — TenantScope, CheckCrossTenantLeak (pure-Go), AssertNoCrossTenantLeak (testing.T wrapper)
- `services/backend/pkg/testing/isolation/fixtures.go` — TestOrg, WithTwoOrgs, TestJWT, RLS-safe cleanupOrg
- `services/backend/pkg/testing/isolation/doc.go` — package overview
- `services/backend/pkg/testing/isolation/container_test.go` — TestSetupTestDB verifies migrations and RLS applied
- `services/backend/pkg/testing/isolation/fixtures_test.go` — four fixture self-tests (distinct IDs, TenantScope, leak-caught, leak-not-flagged)
- `services/backend/go.mod`, `go.sum` — testcontainers-go + golang-migrate deps
- `.planning/ISSUES.md` — ISS-006 moved from open to closed with resolution notes
- `.planning/STATE.md` — ISS-006 removed from deferred issue list

## Decisions Made

- **Non-superuser test role.** Postgres superusers bypass RLS regardless of `FORCE ROW LEVEL SECURITY`. Without this the whole harness would be theatre — tests would pass even under a broken RLS policy. Solution: after migrations, create `rag_doc_app NOSUPERUSER NOBYPASSRLS`, grant CRUD + sequences on public schema, and `SET ROLE rag_doc_app` in `pgxpool.Config.AfterConnect`.
- **Ryuk disabled.** testcontainers-go's Ryuk reaper kills labelled containers when the test session ends, defeating cross-process reuse. Set `TESTCONTAINERS_RYUK_DISABLED=true` inside `setupContainer` before `postgres.Run`. Consequence: the container named `rag-doc-isolation-tests` persists on the developer's Docker daemon; migrations are idempotent so accumulated state is safe, but a developer can `docker rm -f rag-doc-isolation-tests` for a clean slate.
- **`CheckCrossTenantLeak` factored out of `AssertNoCrossTenantLeak`.** Made the pure-Go core testable: self-test #3 asserts an error is returned when a leak is synthesized, without needing to intercept `t.Error`. The `testing.T`-flavored wrapper is what all real tests will use.
- **Leak synthesis via `RESET ROLE` in the observe fn.** The plan called for `SET ROLE postgres bypass` in the self-test. Implemented via `RESET ROLE` which resets to the session user (the container's superuser) inside the observe transaction — same effect, one fewer role assumption.

## Deviations from Plan

### Auto-fixed / additions beyond declared `<files>`

**1. Added `doc.go` and `container_test.go`**
- **Reason:** Task 1's `<verify>` step runs `go test -run TestSetupTestDB` — that test has to live somewhere; `container_test.go` is the natural home. `doc.go` gives the package a docstring so `go doc` and reviewers can see the intent at a glance. Neither adds runtime code.

**2. Added `rag_doc_app` role and Ryuk-disable env var (Task 1 scope creep)**
- **Reason:** Without them the harness does not actually verify tenant isolation (superusers bypass RLS) and does not achieve cross-process reuse (Ryuk kills the container). Both are load-bearing for the plan's stated verify conditions ("Container reuse verified: two consecutive go test runs — second run <5s" and "RLS is enabled on repositories" implying enforceable).

**3. `container_test.go` also asserts `current_user = rag_doc_app`**
- **Reason:** Guardrail against a future edit accidentally reverting the AfterConnect hook and silently disabling RLS enforcement across every isolation test.

### Not fixed (deferred to a later plan)

**Full-project `go build ./...` failure in `pkg/vectordb`**
- **Observed during:** Task 1 verification checklist item "cd services/backend && go build ./..."
- **Cause:** `pkg/vectordb/client.go` uses an older `qdrant/go-client` API (`qdrant.Client`, `qdrant.NewClient`, etc.) that no longer resolves. Reproduced against `main` with my changes stashed — this is pre-existing, not introduced by this plan.
- **Action:** Left the plan's build-project verify step failing and noted it here. The isolation package itself builds and vets cleanly (`go build ./pkg/testing/isolation/...` and `go vet ./pkg/testing/isolation/...` both pass). Suggest planner opens a separate issue or folds a `qdrant/go-client` upgrade into whatever phase touches vector search next (post-17).

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/testing/isolation/...` | clean |
| `go test -count=1 -v ./pkg/testing/isolation/...` | 5/5 pass in ~0.5s |
| `go build ./pkg/testing/isolation/...` | success |
| `go build ./...` | pre-existing failure in `pkg/vectordb` (see deviations) |
| Container reuse: cold run vs warm run | cold ~5s, warm ~1.4s wall / 0.5s test — well under the 5s bar |
| Works from Windows host without docker-compose | yes — testcontainers-go manages its own port mapping |

## Issues Encountered

- **First-time diagnosis:** cross-process container reuse was silently not working because Ryuk was tearing down the container on session end. Resolved by disabling Ryuk (see decisions above); alternative would have been a longer-lived "keep alive" label but Ryuk-disable is the simpler, well-documented path.
- **RLS enforcement gap:** discovered while designing the leak-synthesis self-test that a superuser connection would defeat every assertion. Fixed by introducing the `rag_doc_app` role.

## Next Phase Readiness

- Plan 17-02 (audit `/api/search` and `/api/chat/stream`) can import `pkg/testing/isolation` and write endpoint-level cross-tenant tests immediately.
- Plan 17-03 (PL/pgSQL trigger migration) can use the harness to verify the trigger fires for language-agnostic callers.
- Plan 17-04 (Python harness) has the Go patterns to mirror: two-tenant fixture, tenant-scope helper, no-leak assertion.
- Plan 17-05 (CI gate + docs) will document this API in `docs/isolation.md`.

**v2 breadcrumb (from Task 2):** `WithTwoOrgs` deliberately does not populate every tenant-scoped table. Future v2 graph/memory tables get populated inside the `fn` closure, and the fixture stays stable. No refactor needed when v2 arrives.

---
*Phase: 17-tenant-isolation-foundation*
*Completed: 2026-09-05*
