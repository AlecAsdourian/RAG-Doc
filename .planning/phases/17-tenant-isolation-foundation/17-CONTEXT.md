# Phase 17: Multi-tenant Isolation Foundation - Context

**Gathered:** 2026-09-03
**Status:** Ready for planning

<vision>
## How This Should Work

Isolation is not a thing we "check for" — it's a property the platform is built to have. When a future engineer (human or agent) writes a new endpoint, they cannot accidentally leak data across tenants because three independent layers refuse to let them:

1. **Middleware layer** — the request never reaches the handler without a valid tenant claim; missing claim = hard reject before any query runs.
2. **Database layer** — Postgres itself refuses tenant-scoped operations when `app.current_tenant` is not set. This is language-agnostic: whether the caller is Go, Python, or a psql shell, the DB enforces the same invariant.
3. **Test layer** — every mutation endpoint has a tenant-isolation test that spins up two orgs, mutates as one, verifies the other cannot see the change. CI blocks merge if a new mutation endpoint ships without this test.

Structured audit — every DB query emits a log line with the resolved tenant_id (part of the observability foundation in Phase 18) — so if any of the three layers fails silently, we can reconstruct what happened. Belt + suspenders + audit.

The goal is that after this phase, the answer to "does isolation work?" is "yes — here are the three walls, here are the tests that prove it, here is the CI gate that keeps it that way as the codebase grows."

</vision>

<essential>
## What Must Be Nailed

- **Runtime middleware refuses missing tenant claims.** No handler runs without `app.current_tenant` set. Go backend covers HTTP path; Python workers cover job path. Both call the same underlying primitive.
- **DB-level assertion as second wall.** A Postgres function or trigger asserts `current_setting('app.current_tenant', true)` is not null on tenant-scoped tables. This catches raw-query mistakes that bypass the middleware — including any future ad-hoc script or debugging session.
- **Integration test harness that actually runs.** Resolve ISS-006. Tests execute reliably from host and CI. Postgres test container per test run (or shared with per-test isolation via transactions/schemas — decided during planning).
- **Cross-tenant leak assertion helper.** A single `AssertNoCrossTenantLeak(t, tenantA, tenantB, action)` helper any future test can call. Setup once, use everywhere. Parallel helpers in Go (`testing.T`) and Python (`pytest`).
- **Full audit of existing surface.** `/api/search` and `/api/chat/stream` — the two live endpoints — get tested against multi-org fixtures. Any leak found gets fixed as part of this phase, not deferred.
- **CI gate that blocks merge.** A CI job scans PRs for new/modified mutation endpoints and fails if the corresponding tenant-isolation test is missing. Only an explicit `@skip-isolation-test: <justification>` tag with reviewer sign-off can waive.
- **Isolation pattern documented.** `pkg/auth/README.md` (or a dedicated `docs/isolation.md`) documents the middleware primitive, the DB assertion, and the test pattern. Reviewer session uses this as its review checklist.
- **Coverage: Go AND Python.** Both languages talk to Postgres directly; both need the same guarantees.

</essential>

<boundaries>
## What's Out of Scope

- **Encryption at rest per tenant.** Postgres-level encryption at rest (via the deployment host in Phase 24) is sufficient for v1.0. Per-tenant encryption keys are overkill.
- **Cross-org sharing / public repos.** No sharing feature exists in v1.0 and none is planned. The RLS policies remain strict "one tenant, one view."
- **Rate limiting per tenant.** Handled in Phase 24 (Production Deployment & Cost Controls). Not an isolation concern; a resource-consumption concern.
- **Full RBAC build-out (member/admin/owner permission enforcement).** The isolation harness verifies *tenant-level* boundaries. Verifying that a member cannot delete a repo when only an owner should be able to is a role-permission concern layered on top of tenant isolation — belongs in Phase 19 (auth wiring) or later. This phase will include a *lightweight check* that role claims round-trip through the middleware and land in `app.current_tenant_role`, but not enforce specific role permissions at every endpoint.
- **Row-level permissions beyond tenant.** No "user A can see this repo but user B cannot" within the same org for v1.0. All members of an org see everything the org has access to.

</boundaries>

<specifics>
## Specific Ideas

- **Test isolation strategy: DB per test class or shared with transaction rollback.** Both are reasonable; the plan phase picks based on speed vs correctness trade-off after quick benchmark. Preference: shared DB + `BEGIN...ROLLBACK` per test for speed, provided we can also run "committed" scenarios (needed for testing triggers).
- **Two-org fixture standardized.** Every test that verifies isolation uses the same helper: `withTwoOrgs(t, func(orgA, orgB) { ... })`. Consistency reduces cognitive load and makes the pattern easy to enforce.
- **DB-level assertion mechanism.** Preference: a `PL/pgSQL` trigger on `INSERT`/`UPDATE`/`DELETE` for tenant-scoped tables that raises exception if `app.current_tenant` is not set. Or a `BEFORE` policy hook. Plan phase picks the exact mechanism after evaluating trigger overhead vs a stricter set of RLS policies that check `current_setting`.
- **CI gate implementation.** Custom GitHub Action or a script called from an existing action. Scans the diff for `router.HandleFunc`/`chi.Route` additions in Go and route decorators in Python, cross-references against tests in the same PR. Fails with a comment naming the missing test.
- **Escape hatch.** Some endpoints are legitimately not tenant-scoped (e.g., `/healthz`, `/webhooks/supabase`). These get an explicit annotation (Go struct tag or Python decorator) declaring "not tenant-scoped" so the CI gate ignores them.
- **Naming.** Test helper: `IsolationTest` (Go) / `isolation_test` (Python). Middleware primitive: `RequireTenant` (Go) / `require_tenant` (Python). DB assertion function: `assert_tenant_scoped()`.

</specifics>

<notes>
## Additional Context

**Why this phase is first in v1.0.** Every subsequent phase adds endpoints that mutate tenant-scoped data (repos, jobs, org settings). If the isolation pattern is not established first, we retrofit later — which means auditing everything after the fact and probably missing something. By establishing the middleware + DB assertion + CI-enforced test pattern now, every new endpoint in Phases 19-25 inherits isolation by default. The reviewer session uses this pattern as first-check on every PR.

**Reviewer session criterion.** After this phase ships, the reviewer session prompt at `.planning/fleet/reviewer-session-prompt.md` gets updated to include "check isolation test coverage on any mutation endpoint" as a hard rule.

**v2 door-keeping.** No specific concerns. Future graph tables (v2 GraphRAG) will be tenant-scoped like any other data and will inherit the same isolation primitives automatically. The DB-level assertion in particular is helpful because it means when v2 adds new tables, they get isolation enforcement without additional wiring — just add the trigger to the new table.

**Known blocker.** ISS-006 (test DB connectivity from host) must be resolved as part of 17-01. This is not a codebase issue; it's an infrastructure/setup issue. Options include running tests inside Docker, configuring pg_hba.conf, or using testcontainers-go/testcontainers-python for ephemeral DBs. The plan phase decides.

**Reference implementation.** `services/backend/pkg/auth/isolation_test.go` already exists — it just fails to run. Its structure is a reasonable starting point; we don't rewrite from scratch, we extend and fix.

</notes>

---

*Phase: 17-tenant-isolation-foundation*
*Context gathered: 2026-09-03*
