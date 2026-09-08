---
phase: 19-auth-wiring-org-provisioning
plan: 03
subsystem: auth

requires:
  - phase: 19-02
    provides: provisioning persists the real Supabase user id (without this the org-context push has no id to target)
  - phase: 19-01
    provides: signature-verified webhook that fires on every user creation
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs, testjwt
  - phase: 17-02
    provides: the 10 endpoint isolation scenarios this plan re-points at the JWT
provides:
  - Tenant identity sourced exclusively from the Supabase-signed app_metadata.organization_id claim
  - X-Organization-ID header path deleted, including from CORS Access-Control-Allow-Headers
  - auth.AdminClient — writes organization context onto a Supabase user (reused by 19-04)
  - Convergent org-context push (retried on every webhook delivery, not just fresh provisions)
  - Working local development environment + runbook
  - Supabase project no longer exposes application tables to the anon key
affects: [19-04 (select-organization rewrites the same claim through AdminClient), 20+ (every new endpoint inherits a forgery-proof tenant)]

tech-stack:
  added: []
  patterns:
    - "Org context is written to Supabase from the backend (raw_app_meta_data via admin API) rather than computed by a Supabase Auth Hook — the hook cannot reach the app's database"
    - "The claim is read nested at app_metadata.organization_id, verified by round-trip against the live project rather than assumed from docs"
    - "JWTAuthMiddleware stashes the whole validated jwt.Token on the request context so downstream middleware reads claims without re-verifying a signature"
    - "Non-fatal side effects that must converge are retried on every delivery, not gated on an isNewUser-style flag"

key-files:
  created:
    - services/backend/pkg/auth/supabase_admin.go
    - services/backend/pkg/auth/supabase_admin_test.go
    - docs/local-development.md
    - scripts/supabase/001-remove-app-schema-from-supabase.sql
    - .planning/phases/19-auth-wiring-org-provisioning/19-03-SUMMARY.md
  modified:
    - services/backend/pkg/auth/jwt.go (nested claim extraction, ExtractOrganizationID/Role)
    - services/backend/pkg/auth/middleware.go (header read deleted; TokenKey added)
    - services/backend/pkg/auth/webhook.go (pushOrgContext on every delivery)
    - services/backend/pkg/auth/provisioning.go (UserOwnerOrgID)
    - services/backend/pkg/api/router.go (admin client wiring, CORS header removal)
    - services/backend/pkg/testing/isolation/testjwt/testjwt.go (nested shape + SignWithoutOrg)
    - services/backend/pkg/api/handlers/search_isolation_test.go (scenarios 4/5 rewritten, 6 added)
    - services/backend/pkg/api/handlers/chat_isolation_test.go (scenario 4 rewritten)
    - services/backend/.env.example, docker-compose.yml
    - .planning/phases/19-auth-wiring-org-provisioning/19-03-PLAN.md (REVISION NOTICE)

key-decisions:
  - "No Supabase Auth Hook. The hook is a Postgres function running inside Supabase's database; the application's organization_memberships table lives in a different Postgres instance. The hook physically cannot make the membership decision. Verified, not assumed."
  - "No per-request membership re-check. The claim is trusted wholesale. Membership is validated where the claim is WRITTEN, and minting a token at all requires Supabase's signing key."
  - "The header path is deleted rather than deprecated behind a flag. A fallback that can be re-enabled is a fallback an operator can re-enable by accident."
  - "The org-context push is non-fatal but convergent: a failed push does not fail the webhook, and every subsequent delivery retries it."
  - "AdminClient is optional at router construction. Missing credentials logs a loud warning and runs degraded rather than refusing to boot, so tests and offline dev work."

patterns-established:
  - "When a plan's approach turns out to be impossible against the real system, revise the PLAN file with a REVISION NOTICE recording what was empirically verified, then execute the revised version."
  - "Test-token helpers must emit the identity provider's actual claim shape, pinned by a drift guard in both directions — a harness that disagrees with the IdP proves nothing."

issues-created:
  - ISS-009 (pkg/vectordb does not compile against its pinned Qdrant client)

issues-closed:
  - ISS-007 (fully)
  - ISS-004 (security half; switching UX remains for 19-04)

duration: ~5 hours (roughly half of it unplanned environment and Supabase-project work)
completed: 2026-09-08
---

# Phase 19 Plan 03: JWT-carried organization claim

**`TenantMiddleware` no longer trusts anything the client sends. Tenant identity comes from `app_metadata.organization_id` — a Supabase-signed claim — and the `X-Organization-ID` header is gone from the middleware, the CORS allowlist, and every test. This closes ISS-007, the last known cross-tenant hole in the request path.**

## The hole this closes

Since Phase 4, any authenticated user could set `X-Organization-ID` to any organization's UUID and the middleware forwarded it downstream unchecked. A valid login for org A could read org B's data. 17-02 declined to quietly fix it mid-phase and instead *pinned* the behavior in a test named `Scenario5_HeaderTamper_HeaderCurrentlyTrusted_TODO_1903`, so the gap stayed visible and a silent change in either direction would fail the build.

That scenario is now `Scenario5_TamperedOrgClaim_CannotReachOtherTenantsData`, and it asserts the opposite outcome.

## Accomplishments

- `auth.AdminClient` writes `organization_id` / `organization_role` into the Supabase user's `raw_app_meta_data`, which Supabase surfaces as the `app_metadata` JWT claim
- `ExtractOrganizationID` / `ExtractOrganizationRole` read the claim **nested**, with error messages that distinguish "never provisioned" from "provisioned but the push failed" — the two have different remediations
- `TenantMiddleware` reads the claim and returns **403** when it is absent, rather than defaulting the caller into anyone's organization
- The header is removed from `Access-Control-Allow-Headers`, so a browser client cannot even send it
- The org-context push runs on **every** webhook delivery, so a user whose first push failed converges on the next one instead of being permanently claim-less
- 10 isolation scenarios (6 search, 4 chat) now exercise the JWT path, including a tampered claim and a claim-less token

## Task Commits

Six commits on the branch, three of them the plan's actual tasks:

1. `b41410e` — **docs(19-03):** plan revision after discovering the two-Postgres split
2. `a79aceb` — **docs:** two-datastore documentation + local development runbook
3. `9223fb8` — **docs:** Supabase cleanup script
4. `49eb778` — **docs(19-03):** record the verified Supabase contract from the live round-trip
5. `93bed35` — **feat(19-03):** admin client + org-context push *(Task 1)*
6. `fa474a9` — **feat(19-03):** JWT claim read, header path removed *(Task 2)*
7. `81cc3d2` — **test(19-03):** isolation scenarios flipped onto the claim *(Task 3)*
8. `46f41db` — **test(auth):** unrelated webhook assertion fix (see Deviation 5)

## Deviations from Plan

### 1. No Supabase Auth Hook — it was structurally impossible

The plan (and ISS-004's original sketch, and 19-02's summary) all assumed a Supabase Auth Hook: a Postgres function that runs at token-mint time, looks up `organization_memberships`, and stamps the claim.

**Supabase's Postgres and the application's Postgres are two different database instances.** The hook runs inside Supabase's; `organization_memberships` lives in docker-compose's on port 5434. The function cannot see the table it needs to query. No amount of SQL fixes this.

19-02's summary states the hook's lookup "can now match" thanks to the identity fix. That was true about the *data* and wrong about the *topology* — the id is correct, the hook just can't reach the row. Recorded here so the claim doesn't get inherited again.

**What was done instead:** the backend writes `raw_app_meta_data` through the Supabase admin API after provisioning. Same resulting claim, no cross-instance dependency.

**Cost of the substitution, stated plainly:** the claim is baked at token-mint time, so a change to a user's organization does not take effect until their token refreshes. 19-04's select-organization endpoint has to force a refresh or accept the lag. A hook would have recomputed per mint. This is a real downside, not a wash.

### 2. No per-request membership re-check

ISS-007 called for the middleware to "cross-check against `organization_memberships`" on every request. It does not.

Membership is validated where the claim is **written** — at provisioning, and in 19-04 at organization switch. Re-validating per request would add a database round-trip to the hot path of every authenticated call, to defend against an attacker who by construction would already need Supabase's JWT signing key. Someone holding that key does not need to lie about `organization_id`; they can mint any `sub` they like.

The residual exposure is the token lifetime window: a user removed from an organization keeps access until their current token expires. That is the standard, accepted cost of stateless claims, and the right mitigation is short token TTLs plus a revocation path — not a per-request join. Flagged for the reviewer as a deliberate deviation.

### 3. Unplanned: the local development environment did not work

Half of this plan's wall-clock went to work that was not in it.

The docker-compose Postgres was down; its volume held a password from an older `.env`; `schema_migrations` was empty. Most consequentially: **migration 000009 — the tenant-scoping trigger from Phase 17-03 — had never been applied to the real database.** It existed only inside the testcontainers Postgres. Every isolation test that "verified" the trigger was verifying it in a container that the application never talks to.

Fixed: volume reset, `schema_migrations` bootstrapped, migrations applied through 000009 against the real database for the first time. Written up in `docs/local-development.md`, including the two gotchas that cost the most time (Git Bash mangling container paths without `MSYS_NO_PATHCONV=1`, and the fact that **the Go backend does not read `.env` at all** — only docker-compose does, which makes `.env` look authoritative when it isn't).

### 4. Unplanned: a live security exposure in the Supabase project

While looking for the app's tables inside Supabase, found them — a stale duplicate of the entire application schema sitting in Supabase's `public` schema with RLS off, readable by anyone holding the **anon** key, which is public by design and ships in the frontend bundle.

Most were vestigial (empty, from a pre-restructure deploy) and were dropped. One was not: `auth_user_events` is the load-bearing signup bridge — a trigger on `auth.users` writes to it and the webhook reads it — and it carried real user PII. Kept, with anon/authenticated grants revoked.

`scripts/supabase/001-remove-app-schema-from-supabase.sql` records exactly what was dropped and what was deliberately kept.

**A mistake worth recording:** the first version of the FK-safety check I handed over queried `tgt.relname IN ('users', ...)` without schema qualification, so it matched Supabase's own `auth.users` and reported false positives that looked like "dropping this will break authentication." Caught because the step was run separately and the output inspected. Schema-qualify catalog queries in a database that has a schema named `auth`.

### 5. Unplanned: a false test failure the environment fix exposed

`TestWebhookHandler_UserCreatedEvent` asserted `SELECT COUNT(*) FROM organizations WHERE name LIKE 'Test%'` and started failing with `expected 1, actual 2`.

Not a regression. The local dev database ships a seed row named `Test Org`, and the prefix match caught it alongside the provisioned `Test User's Organization`. The assertion has been wrong since it was written; it was simply never reached, because before this plan `SetupTestDB` could not connect to that database at all.

Rewritten to join through `organization_memberships` to the test's own user — which is the property the test actually means. Committed separately (`46f41db`) since it has nothing to do with auth wiring.

### 6. The claim shape was verified, not assumed — and the assumption would have been wrong

Sub-step D ran a full round-trip against the live Supabase project: admin write → sign in → decode the access token. Three findings, all now recorded as facts in the plan file:

- The admin API **merges** `app_metadata` rather than replacing it, so writing our two keys does not clobber Supabase's `provider` / `providers`
- The claim arrives **nested** under `app_metadata` — there is **no** top-level `organization_id`
- The direct database host is IPv6-only

The third finding matters most for the test harness. `testjwt.Sign` previously emitted a **flat** `organization_id` claim. Every 17-02 isolation test passed against it — while a real Supabase token would have been rejected by the same middleware, because the claim it reads simply is not there. The harness agreed with itself and disagreed with the identity provider, which is the failure mode where a green suite means nothing. `testjwt` now emits the nested shape, with `TestSign_DoesNotEmitFlatClaims` guarding the reverse direction.

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/api/... ./pkg/auth/... ./pkg/testing/...` | clean |
| `TestSearchIsolation` | 6/6 pass (scenarios 4 and 5 rewritten, 6 new) |
| `TestChatIsolation` | 4/4 pass (scenario 4 rewritten) |
| `go test ./pkg/auth/...` | all pass |
| `go test ./pkg/client/... ./pkg/testing/...` | all pass |
| `go test ./...` | **red** — `pkg/vectordb` build failure, pre-existing and unrelated (ISS-009) |
| Live Supabase round-trip | admin write → sign in → decode confirms nested claim |
| Migrations against the real DB | applied through 000009 (first time) |

## Issues Encountered

- **GoTrue admin *read* endpoints return 500 on this Supabase project.** Both `GET /auth/v1/admin/users` ("Database error finding users") and `GET /auth/v1/admin/users/{id}` ("Database error loading user"). Writes, signup, sign-in, settings, and JWKS all work. Dropping the duplicated `public.users` table was my hypothesis for the cause; the error persisted afterward, so **that hypothesis was wrong** and the root cause is still unknown. Does not block 19-03 — nothing in the request path reads those endpoints — but it will block any future admin console work. User has the `error_id`s for Supabase support.
- **`pkg/vectordb` has never compiled against its pinned dependency** (ISS-009). Filed rather than fixed: a dependency bump does not belong inside a security PR.
- **A stale test user** (`tes***@example.com`, created February 2026) is still in the Supabase project. Harmless, but it should be cleaned up before any real signup traffic.

## Next Phase Readiness

- **19-04** — unblocked and cheaper than planned. `auth.AdminClient` already exists and is the exact mechanism `POST /api/user/select-organization` needs; the endpoint validates membership then rewrites the same claim. The one thing 19-04 must solve that the plan did not anticipate: forcing a token refresh so the new claim takes effect immediately (see Deviation 1).
- **Phase 20+** — every new endpoint inherits a tenant identity the client cannot forge. Combined with ISS-008 still open, the remaining work before a Go handler reads a tenant-scoped table directly is the request-scoped transaction, not the identity.
- **Recommended follow-ups (not blocking):**
  1. Delete the three genuinely-superseded tests in `pkg/auth/isolation_test.go` (carried over from 19-02).
  2. Migrate `pkg/auth/testing.go` onto the 17-01 testcontainers harness (carried over from 19-02 — and Deviation 5 is a fresh example of what that gap costs).
  3. Fix ISS-009 so whole-module `go build ./...` can become a CI gate.
  4. Decide on a token-refresh strategy for claim changes before 19-04 ships.

---
*Phase: 19-auth-wiring-org-provisioning*
*Completed: 2026-09-08*
