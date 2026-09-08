---
phase: 19-auth-wiring-org-provisioning
plan: 04
subsystem: auth

requires:
  - phase: 19-02
    provides: every user has at least one owner membership, committed atomically
  - phase: 19-03
    provides: auth.AdminClient, the app_metadata claim contract, and TokenKey on the request context
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs
provides:
  - GET /api/user/organizations — the caller's memberships, with the active one flagged
  - POST /api/user/select-organization — membership-validated switch that rewrites the claim
  - A user-scoped route group (JWT auth WITHOUT TenantMiddleware) for endpoints a claim-less user must still reach
  - api.NewRouterWithValidatorAndAdmin — admin-client seam so tests can observe what gets written to Supabase
  - isolation.AddMembership and Supabase ids on TestOrg — fixtures for genuinely multi-org users
  - docs/auth-frontend-contract.md — the wire contract Phase 23 implements against
affects: [23 (frontend org switcher), 20+ (invitations and membership removal build on these endpoints and inherit ISS-012)]

tech-stack:
  added: []
  patterns:
    - "User-scoped endpoints mount under JWT auth but OUTSIDE TenantMiddleware — an endpoint that repairs a bad tenant state cannot be gated on being in a good one"
    - "Any write to app_metadata sends every key it owns, because Supabase merges and an omitted key silently keeps its old value"
    - "Authorization failures and not-found are deliberately indistinguishable on tenant-addressable endpoints, so they cannot be used to enumerate other tenants"
    - "A test asserting a security property must be shown to FAIL under a mutation that breaks it"

key-files:
  created:
    - services/backend/pkg/api/handlers/user_orgs.go
    - services/backend/pkg/api/handlers/user_orgs_isolation_test.go
    - docs/auth-frontend-contract.md
    - .planning/phases/19-auth-wiring-org-provisioning/19-04-SUMMARY.md
  modified:
    - services/backend/pkg/api/router.go (user-scoped group; admin-client seam)
    - services/backend/pkg/testing/isolation/fixtures.go (Supabase ids, AddMembership, cross-org cleanup)
    - docs/isolation.md (wall 1 now names the claim; cross-link)
    - services/backend/README.md (doc index; -p 1 and ISS-009 warnings)
    - .planning/phases/19-auth-wiring-org-provisioning/19-04-PLAN.md (REVISION NOTICE)

key-decisions:
  - "Select writes BOTH organization_id and organization_role. Supabase merges app_metadata, so writing only the id leaves the previous role in place — verified against the live project. The plan's sketch wrote only the id."
  - "The membership check in Select is the ONLY authorization on that path, not defense-in-depth as the plan described. There is no Auth Hook to fall back on."
  - "List and Select sit outside TenantMiddleware. They are how a claim-less user recovers, so they cannot require a claim."
  - "403 for both 'not a member' and 'no such organization', so the endpoint is not a tenant enumeration oracle."
  - "Degraded router (no admin client) returns 503 from Select rather than 202. A 202 would have the client refresh into an unchanged organization and believe it switched."

issues-created:
  - ISS-012 (a revoked membership does not revoke the organization claim)

issues-closed:
  - ISS-004 (fully — backend contract complete; frontend picker is Phase 23 implementing the documented contract)

duration: ~2 hours
completed: 2026-09-08
---

# Phase 19 Plan 04: Multi-org endpoints

**A user can now see every organization they belong to and switch between them, with membership validated server-side. Closes ISS-004, open since Phase 4, and completes Phase 19.**

## What shipped

`GET /api/user/organizations` returns the caller's memberships with the active one flagged. `POST /api/user/select-organization` validates that the caller actually belongs to the target, then rewrites their Supabase `app_metadata` so the next token they mint carries the new organization. The frontend calls `refreshSession()` and reloads.

Both are **user-scoped**: mounted under JWT authentication but deliberately outside `TenantMiddleware`.

## Two things the plan got wrong

This plan was written before 19-03 executed, and assumed the Supabase Auth Hook that 19-03 proved impossible. A `REVISION NOTICE` at the top of the plan file records the full correction. Two of its consequences were security-relevant.

### 1. The membership check is the only authorization, not defense-in-depth

The plan says, verbatim: *"Avoid: pre-validating the org id via a synchronous membership check inside the JWT hook — the hook already handles missing/mismatched memberships. The `Select` endpoint's membership check is defense-in-depth in case the hook is misconfigured."*

There is no hook, and nothing else validates the target. Had the check been treated as optional belt-and-braces and skipped, any authenticated user could have placed themselves in any organization by POSTing its id — handing back, through a new door, exactly the capability 19-03 removed by deleting the `X-Organization-ID` header.

It is load-bearing. Scenario 1 fails without it.

### 2. Writing only `organization_id` is a privilege escalation

The plan's code sketch writes a single key:

```go
h.supabaseAdmin.UpdateUserAppMetadata(ctx, userID, map[string]any{
    "organization_id": req.OrganizationID,
})
```

Supabase **merges** `app_metadata` — 19-03 verified that, and relied on it so our keys wouldn't clobber `provider`/`providers`. The same property means an omitted key keeps its old value. Verified directly against the live project before writing any code:

```
PUT {app_metadata:{organization_id:X, organization_role:"member"}} → refresh → role=member
PUT {app_metadata:{organization_id:Y}}          (role omitted)     → refresh → role=member   ← STALE
```

So a user who is `owner` of orgA and `member` of orgB would switch to orgB and carry `organization_role: "owner"`.

Nothing reads `organization_role` today — 19-03's review established that it is written to the request context and read nowhere. That is the only reason this is not currently exploitable, and precisely why it would have shipped unnoticed and waited for the first handler that gated on role.

`Select` writes both keys, sourced from the membership row it just validated. Pinned by scenario 2.

## The harness was signing tokens production never issues

`WithTwoOrgs` exposes `OwnerID` as the internal `users.id`, and every isolation test since 17-01 signs its JWT with that value as `sub`. In production `sub` is the **Supabase** user id — a different UUID, stored separately in `users.supabase_user_id`.

Harmless until now: `auth.UserIDKey` was written by the middleware and read by *nothing*. `GET /api/user/organizations` is the first code in the project to resolve `sub` back to a user row, and against the old fixture it would have found zero memberships for every user — a test suite that passes while the endpoint returns an empty list in production.

Same shape as the flat-vs-nested claim bug 19-03 fixed, in a different field. `TestOrg` now carries `OwnerSupabaseID` / `AdminSupabaseID` / `MemberSupabaseID`, and these tests sign with those.

Also fixed while there: `cleanupOrg` deleted memberships by organization only. Once `AddMembership` puts one org's user into another org, cleaning up the first org would fail to delete its user (still referenced by the second org's membership row) and — because each statement runs in its own savepoint — skip silently, leaking an orphaned row into the reused container. Cleanup now deletes memberships by org **or** user.

## Verification

| Check | Result |
|---|---|
| `go build ./pkg/... ./cmd/... .` (less `vectordb`) | clean |
| `go vet ./pkg/api/... ./pkg/auth/... ./pkg/testing/...` | clean |
| `TestUserOrgsIsolation` | 6/6 |
| `go test -p 1 ./pkg/...` | all pass |
| CI isolation scanner | PASS |
| Live Supabase: `refreshSession()` re-reads metadata | confirmed |
| Live Supabase: partial write leaves role stale | confirmed (this is the bug above) |

### Mutation testing

Both security assertions were shown to fail when the property they guard is broken:

| Mutation | Result |
|---|---|
| Remove the membership check from `Select` | scenario 1 fails, and is the **only** failure |
| Write only `organization_id` (the plan's original sketch) | scenario 2 fails, and is the **only** failure |

Each mutation produces exactly one targeted failure — the tests are neither vacuous nor over-broad.

## Deviations from plan

**Scenario 6 added** (not in the plan): a malformed `organization_id` must be a clean 4xx. The value reaches a UUID comparison in Postgres, and an unvalidated one surfaces as a driver error rendered as 500 — which reads as our bug rather than a bad request. Covered by validator plus an explicit `uuid.Parse` in `callerRoleIn`.

**`is_active` is computed from the token, not `auth.OrgIDKey`.** The plan's Sub-step A reads `OrgIDKey`; its Sub-step B moves these routes out of the group that sets it. Both cannot hold. Reading the claim off `TokenKey` also makes scenario 4 reachable — a claim-less user gets an empty `active_organization_id` and a populated list, rather than the 403 `TenantMiddleware` would have returned before the handler ran.

**Response is an object, not a bare array.** The plan specified `[{...}]`. An envelope lets `active_organization_id` be reported explicitly, which distinguishes "belongs to nothing" from "belongs to organizations, none active" — different states needing different UI.

**`NewRouterWithValidatorAndAdmin` added.** Tests need to observe what gets written to Supabase; that write *is* the security property. Without the seam, the role-carryover escalation would have been unobservable from a test.

## Issues

**ISS-012 filed — a revoked membership does not revoke the claim.** The claim is written at provisioning and at switch, and nowhere else; Supabase re-reads the same column at every mint, so refreshing preserves it. Removing a user from an organization does not end their access to it.

Not exploitable today — nothing removes memberships — but filed because the intuitive model ("stateless claims expire, so exposure is bounded by token lifetime") is *wrong here*, and it is the model the next author will bring. 19-03's summary asserted exactly that before a reviewer caught it. Whatever ships membership removal must rewrite the affected user's claim; short TTLs do not help. The warning is also carried in `docs/auth-frontend-contract.md` so it reaches Phase 20+ without going through the planning files.

## Next Phase Readiness

- **Phase 19 is complete** once this merges. All four plans executed, ISS-004 and ISS-007 closed.
- **Phase 23** has a spec to build against rather than code to reverse-engineer: `docs/auth-frontend-contract.md` covers sign-in, reading the active org from the token, listing, switching, sign-out, and the UX conventions — including the non-obvious requirement to hard-reload after a switch so cached responses from the previous tenant don't linger on screen.
- **Phase 20** is next and needs planning. The ROADMAP flags a user action for 20-01: registering the GitHub App is a manual step in GitHub's UI.
- **Carried follow-ups (unchanged):** delete the three superseded tests in `pkg/auth/isolation_test.go`; migrate `pkg/auth/testing.go` onto the 17-01 harness; ISS-009 (`vectordb` build), ISS-010 (`-p 1` race), ISS-011 (OAuth callbacks 500), and the unsettled webhook-retry question from 19-03.

---
*Phase: 19-auth-wiring-org-provisioning*
*Completed: 2026-09-08*
