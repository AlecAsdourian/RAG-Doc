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
  - cmd/backfill-org-claims — the ONLY repair path for a failed org-context push, and the migration step for users provisioned before the claim existed
  - Org-context push is replay-safe, but NOTHING replays it — the webhook delivers once per user for all time
  - Working local development environment + runbook
  - Supabase project no longer exposes application tables to the anon key
affects: [19-04 (select-organization rewrites the same claim through AdminClient), 20+ (every new endpoint inherits a forgery-proof tenant)]

tech-stack:
  added: []
  patterns:
    - "Org context is written to Supabase from the backend (raw_app_meta_data via admin API) rather than computed by a Supabase Auth Hook — the hook cannot reach the app's database"
    - "The claim is read nested at app_metadata.organization_id, verified by round-trip against the live project rather than assumed from docs"
    - "JWTAuthMiddleware stashes the whole validated jwt.Token on the request context so downstream middleware reads claims without re-verifying a signature"
    - "Write side effects to be replay-safe (check actual end state, not an isNewUser-style flag) — but never assume anything replays them. Ship the replay driver, or the safety is theoretical. This plan shipped the safety without the driver and it took a reviewer to notice."
    - "A test asserting a security property must assert a POSITIVE outcome and be shown to FAIL under a mutation that breaks the property"

key-files:
  created:
    - services/backend/pkg/auth/supabase_admin.go
    - services/backend/pkg/auth/supabase_admin_test.go
    - services/backend/pkg/auth/jwt_test.go
    - services/backend/pkg/auth/backfill_isolation_test.go
    - services/backend/cmd/backfill-org-claims/main.go
    - docs/local-development.md
    - scripts/supabase/001-remove-app-schema-from-supabase.sql
    - .planning/phases/19-auth-wiring-org-provisioning/19-03-SUMMARY.md
  modified:
    - services/backend/pkg/auth/jwt.go (nested claim extraction, ExtractOrganizationID/Role)
    - services/backend/pkg/auth/middleware.go (header read deleted; TokenKey added)
    - services/backend/pkg/auth/webhook.go (pushOrgContext after provisioning; detached from the request context)
    - services/backend/pkg/auth/provisioning.go (UserOwnerOrgID, ListOwnerOrgAssignments)
    - services/backend/pkg/api/router.go (admin client wiring, CORS header removal)
    - services/backend/pkg/testing/isolation/testjwt/testjwt.go (nested shape, SignWithoutOrg, SignWithNoAppMetadata)
    - services/backend/pkg/api/handlers/search_isolation_test.go (scenarios 4/5 rewritten, 6 and 7 added)
    - services/backend/pkg/api/handlers/chat_isolation_test.go (scenario 4 rewritten)
    - services/backend/.env.example, docker-compose.yml
    - .planning/phases/19-auth-wiring-org-provisioning/19-03-PLAN.md (REVISION NOTICE)

key-decisions:
  - "No Supabase Auth Hook. The hook is a Postgres function running inside Supabase's database; the application's organization_memberships table lives in a different Postgres instance. The hook physically cannot make the membership decision. Verified, not assumed."
  - "No per-request membership re-check. The claim is trusted wholesale. Membership is validated where the claim is WRITTEN, and minting a token at all requires Supabase's signing key."
  - "The header path is deleted rather than deprecated behind a flag. A fallback that can be re-enabled is a fallback an operator can re-enable by accident."
  - "The org-context push is non-fatal, and there is NO automatic retry — Supabase webhooks fire once. cmd/backfill-org-claims is the repair path. (This entry originally claimed convergence on every delivery; that was wrong. See Deviation 7.)"
  - "AdminClient is optional at router construction. Missing credentials logs a loud warning and runs degraded rather than refusing to boot, so tests and offline dev work."
  - "The organization claim must parse as a UUID. Closes the injection window before ISS-008 moves tenant interpolation into the middleware."

patterns-established:
  - "When a plan's approach turns out to be impossible against the real system, revise the PLAN file with a REVISION NOTICE recording what was empirically verified, then execute the revised version."
  - "Test-token helpers must emit the identity provider's actual claim shape, pinned by a drift guard in both directions — a harness that disagrees with the IdP proves nothing."

issues-created:
  - ISS-009 (pkg/vectordb does not compile against its pinned Qdrant client)
  - ISS-010 (isolation harness setup races when test packages run in parallel)
  - ISS-011 (OAuth callback routes are live, broken, and bypass the org-context push)

issues-closed:
  - ISS-007 (fully)
  - ISS-004 (security half; switching UX remains for 19-04)

patterns-established-addendum:
  - "A test asserting a security property must assert a POSITIVE outcome and be shown to fail under a mutation that breaks the property. Two scenarios in this plan passed against a deliberately broken middleware before this was applied."

duration: ~7 hours (roughly half of it unplanned environment and Supabase-project work, plus a round of reviewer fixes)
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
- The claim must parse as a UUID before it becomes a tenant id — a trust-boundary check, not formatting hygiene (see Deviation 8)
- The header is removed from `Access-Control-Allow-Headers`, so a browser client cannot even send it
- `cmd/backfill-org-claims` repairs users whose org-context push failed, and doubles as the migration step for users provisioned before the claim existed
- 11 isolation scenarios (7 search, 4 chat) on the JWT path, plus unit coverage for claim extraction. Two of them were confirmed able to fail by mutation testing (see Verification)

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

That part survived review. The characterization of the residual risk did not.

**Correction.** The first draft of this section said the exposure is "the token lifetime window — a user removed from an organization keeps access until their current token expires… the standard, accepted cost of stateless claims," with short TTLs as the mitigation.

That is wrong, and wrong in the direction that matters. It assumes the claim is recomputed each time a token is minted. It is not: `raw_app_meta_data` is written once, by the webhook, and nothing else in the codebase writes it. Supabase re-reads that same column at every mint, so a **refreshed token carries the same stale organization**. Removing a user from an organization does not expire their claim after an hour; it does not expire it at all. Short TTLs mitigate nothing here.

Nothing removes memberships today, so this is currently theoretical — but 19-04 is precisely where memberships start changing, and it is the plan that would have inherited the bad reasoning. What actually bounds the exposure is not token expiry but the rule that **every membership change must rewrite the claim**. 19-04 owns that.

### 8. Unplanned: the claim was not validated as a UUID

Not in the plan, added in review. `ExtractOrganizationID` accepted any non-empty string, so `"x'; SET ROLE postgres; --"` was a valid tenant id as far as the middleware was concerned.

Not exploitable today: every current consumer parses the UUID before interpolating it, so the outcome is a 500. But Postgres cannot bind a parameter into `SET LOCAL app.current_tenant`, so the established in-repo pattern for applying a tenant is string interpolation — and `middleware.go` carries a `_ = db // TODO(17-03/ISS-008)` reserving exactly that spot. ISS-008 would have turned an unchecked claim into SQL injection at the front door.

One `uuid.Parse` closes it now, before the phase that would open it. Rejecting is safe because our own writer only ever pushes a `uuid.UUID`.

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

## The blocker review caught

Worth its own section, because the failure mode is the one this plan spent three paragraphs congratulating itself for finding elsewhere.

**The claim:** "the org-context push runs on every webhook delivery, so a failed push self-heals on the next one." It appeared in the code comments, in `jwt.go`'s user-facing error string, in ISS-007, and in the PR body.

**The reality:** there is no next delivery. The trigger is `AFTER INSERT ON auth.users` — one `auth_user_events` row per user, ever — and Supabase database webhooks fire once with no retry. That second fact was already written down in this repo, in `04-06-SUMMARY.md`, in Phase 4.

The code is genuinely replay-convergent; the reviewer verified that by calling the handler twice and watching the push retry. Nothing calls it twice. A single failed push — one transient 5xx from an admin API this very summary records as flaky — left that user with no organization claim **permanently**, 403 on every route, with no repair path anywhere in the codebase and a 202 returned to Supabase.

Compounding it: the push used the webhook request's context, so a client hangup cancelled the user's only chance at a claim.

**Fixed by** `cmd/backfill-org-claims`, which reads org ownership from our database and re-pushes; `context.WithoutCancel` on the outbound call; an alert-shaped log line naming the repair command; and correcting every place that promised a retry. Returning 500 instead was considered and rejected — with no retry it would only make the log noisier.

There was also no backfill for users provisioned before this PR. The same command covers them.

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/api/... ./pkg/auth/... ./pkg/testing/... ./cmd/...` | clean |
| `TestSearchIsolation` | 7/7 pass (4 and 5 rewritten, 6 and 7 new) |
| `TestChatIsolation` | 4/4 pass (scenario 4 rewritten) |
| `TestExtractOrganizationID_*` | 23 subtests, new — malformed shapes, UUID enforcement, error distinction |
| `go test ./pkg/auth/... ./pkg/client/... ./pkg/testing/...` | all pass |
| `go test -p 1 ./pkg/...` | all pass — **`-p 1` required**, see ISS-010 |
| `go test ./...` | **red** — `pkg/vectordb` build failure, pre-existing and unrelated (ISS-009) |
| CI isolation scanner | PASS |
| Live Supabase round-trip | admin write → sign in → decode confirms nested claim |
| Migrations against the real DB | applied through 000009 (first time) |

### Mutation testing

Two scenarios were reported by the reviewer as unable to fail. Both were rewritten and then verified by breaking the middleware on purpose:

| Mutation | Before | After |
|---|---|---|
| Re-add `X-Organization-ID` fallback to `TenantMiddleware` | all 10 scenarios passed | scenario 7 fails, and is the **only** failure |
| Replace the claim read with a hardcoded nonexistent org | scenario 5 passed | scenario 5 fails |

The same technique confirmed the two new admin-client security tests: reverting the redaction and the redirect refusal turns both red.

This is now the standard for any test claiming to cover a security property in this repo — assert the positive outcome, then prove the test can fail.

## Issues Encountered

- **GoTrue admin *read* endpoints return 500 on this Supabase project.** Both `GET /auth/v1/admin/users` ("Database error finding users") and `GET /auth/v1/admin/users/{id}` ("Database error loading user"). Writes, signup, sign-in, settings, and JWKS all work. Dropping the duplicated `public.users` table was my hypothesis for the cause; the error persisted afterward, so **that hypothesis was wrong** and the root cause is still unknown. Does not block 19-03 — nothing in the request path reads those endpoints — but it will block any future admin console work. User has the `error_id`s for Supabase support.
- **`pkg/vectordb` has never compiled against its pinned dependency** (ISS-009). Filed rather than fixed: a dependency bump does not belong inside a security PR.
- **A stale test user** (`tes***@example.com`, created February 2026) is still in the Supabase project. Harmless, but it should be cleaned up before any real signup traffic.

## Reviewer follow-ups (post-review, same branch)

Reviewer returned one blocker, six mediums and eight nits, verified empirically rather than reasoned from docs — including running the isolation suite against a deliberately re-broken middleware. Option 1 applied per user decision: everything.

**H1 — the "convergent" push has no trigger.** Covered in its own section above. Fixed with `cmd/backfill-org-claims`, `context.WithoutCancel`, an alert-shaped log line, and corrections to every place that promised a retry.

**M1 — scenario 5 could not fail.** Its only assertion was that no result contained `"marmalade"`, which an empty result set satisfies — so every broken middleware passed it. Proved by mutation. Rewritten with positive assertions (`TotalResults == 1`, content contains `"velvet"`) and renamed: it was called `TamperedOrgClaim_CannotReachOtherTenantsData` while asserting behavior where the token *does* reach orgB's data, correctly and by design. Only Supabase's key can mint it; "tampered" was never the right word.

**M2 — the suite could not detect re-introduction of the hole.** With a header-with-claim-fallback added back to `TenantMiddleware`, all ten scenarios still passed, because no test sent the header. Added scenario 7: orgA's token, orgA's repo, `X-Organization-ID: orgB`. Under that mutation it is now the only failure.

**M3 — the service-role key could reach the log.** Non-2xx errors embedded 300 bytes of response body. A proxy or WAF error page that echoes request headers puts the key in plaintext in the application log; reviewer demonstrated it. Now scrubbed via `redactSecret`. The existing test named `NeverLeaksServiceKey` did not catch it — it split the error on the first colon and inspected only the prefix, testing the format string while discarding the only part that could carry the key. Now asserts on the whole string.

**M4 — the key was forwarded on cross-host redirects.** Go strips `Authorization` across hosts but knows nothing about GoTrue's custom `apikey` header, which carries the same secret. Reviewer demonstrated a redirector harvesting it. Client now refuses to follow redirects at all.

**M5 — `SignWithoutOrg` emitted a shape Supabase never issues.** It omitted `app_metadata` entirely, while a real un-provisioned user has it present with `provider`/`providers` and our key missing — a different branch, different error, different remediation. Exactly the harness-disagrees-with-the-IdP failure this plan fixed elsewhere. Corrected, with `SignWithNoAppMetadata` added for the defensive case and drift guards for both.

**M6 — any non-empty string was a valid tenant id.** See Deviation 8.

**Nits, all applied:** dead `meta == nil` branch and wrong-type error text in `jwt.go` (L1, L2); the schema-unqualified FK check still sitting in the committed cleanup script despite the summary describing that bug as fixed (L3); ROADMAP still specifying the Auth Hook (L4); `local-development.md` claiming Supabase holds `auth.users` only, contradicted by the bridge table the cleanup script deliberately keeps (L5); `Sign`'s doc comment still describing flat claims (L6); STATE.md contradicting itself about the reviewer session (L7). L8 (the live-but-broken OAuth callback routes) is filed as ISS-011 rather than fixed — it is pre-existing and out of scope for an auth-claim PR.

**Also found while fixing:** running these packages in parallel races on the harness's `GRANT` statement (ISS-010). Use `go test -p 1`.

## Reviewer round 2 (verification pass)

Reviewer re-tested every round-1 fix rather than re-reading it — drove the backfill query against a genuinely stranded user, re-ran both mutations, and reverted each security fix to confirm its test fails. H1 and all six mediums confirmed closed. Nothing found at blocker level. Three new mediums, all documentation and operator ergonomics; all applied.

**M-A — the correction didn't reach the fields future plans read.** The prose in this summary was fixed; the YAML frontmatter still said "retried on every webhook delivery" in `provides:`, `key-files:`, and worst, `tech-stack.patterns` — a *reuse this* field encoding the exact false model that caused H1. Three more stale copies survived in `19-03-PLAN.md` (Constraining Decisions, plus the code sketch showing `r.Context()`) and in `19-02-SUMMARY.md`, whose H3 and H4 justifications both rested on retries existing. All corrected, with the retractions left visible rather than quietly rewritten — the wrong reasoning is the useful artifact.

**M-B — the "no retry" claim was load-bearing and under-evidenced.** It reached ~8 places sourced from `04-06-SUMMARY.md:282`, and that same Phase 4 document says at `:306` to "return 500 for provisioning failures (trigger Supabase retry)". It contradicts itself, and I cited one half of it as settled — in a plan whose whole methodological point was verifying the Supabase contract by round-trip instead of trusting docs.

Two things done about it. The `:306` line is struck with an explanation. And the code no longer *depends* on the claim: `cmd/backfill-org-claims` now carries a doc section separating what is verified (the `AFTER INSERT` trigger writes exactly one event per user — checked in this repo) from what is inferred (that Supabase itself does not retry — consistent with pg_net being fire-and-forget, but not measured against this project). The operative rule is "do not rely on webhook retry", which holds either way: retries are finite, a user who exhausts them is stranded identically, and pre-existing users need the backfill regardless.

**Still open, and it needs the live project:** actually measuring whether a 500 from `/webhooks/supabase` produces a second delivery. That requires a publicly reachable backend (ngrok) plus a real signup, so it is a deliberate exercise, not something to slip into a code change. It does not gate this PR — no behavior depends on the answer — but it should be settled before anyone designs around delivery semantics again.

**M-C — the backfill loop ground past its own deadline.** Once the context expired, every remaining call failed instantly and the loop kept logging `FAILED`, so a timeout at user 400 of 10,000 reported 9,600 failures. That count is what an operator acts on, and it lied about how much was broken. Now breaks at the deadline and reports how far it got. Separately, the fixed 5-minute default was the real scaling cliff: runtime here is linear in user count, so any constant is both too long for a dev database and too short for a real one, and "too short" failed silently mid-repair. The budget is now derived from the number of users found, with `-timeout` as an explicit override.

**L-a — the entire H1 remediation had no tests.** `ListOwnerOrgAssignments` and `cmd/backfill-org-claims` were verified once by hand and never again. Added `backfill_isolation_test.go`: the stranded-user case built through the real webhook with a failing admin client; a `DISTINCT ON` guard asserting backfill and the webhook resolve the *same* organization (if they disagreed, a backfill run would silently move a user between tenants); and a non-owner exclusion test, because the push writes `organization_role: "owner"` unconditionally, so a member leaking into that query would be privilege-escalated by the repair tool. Mutation-checked: removing the `role = 'owner'` filter turns the third red.

**L-b, L-c** — leftover duplicate paragraph in `SignWithoutOrg`; `key-files` and `provides` missing scenario 7, `backfill_isolation_test.go`, and the backfill command.

## Next Phase Readiness

- **19-04** — unblocked and cheaper than planned. `auth.AdminClient` already exists and is the exact mechanism `POST /api/user/select-organization` needs; the endpoint validates membership then rewrites the same claim. Two things 19-04 must solve that the original plan did not anticipate: forcing a token refresh so the new claim takes effect (Deviation 1), and rewriting the claim on **every** membership change, since nothing else expires a stale one (Deviation 2).
- **Phase 20+** — every new endpoint inherits a tenant identity the client cannot forge. Combined with ISS-008 still open, the remaining work before a Go handler reads a tenant-scoped table directly is the request-scoped transaction, not the identity.
- **Recommended follow-ups (not blocking):**
  1. Delete the three genuinely-superseded tests in `pkg/auth/isolation_test.go` (carried over from 19-02).
  2. Migrate `pkg/auth/testing.go` onto the 17-01 testcontainers harness (carried over from 19-02 — and Deviation 5 is a fresh example of what that gap costs).
  3. Fix ISS-009 so whole-module `go build ./...` can become a CI gate, and ISS-010 so it can run in parallel.
  4. Decide on a token-refresh strategy for claim changes before 19-04 ships.
  5. Schedule `backfill-org-claims` (cron or equivalent). It exists and is idempotent; running it periodically is what turns "one shot per user" into an eventually-consistent system rather than a manual incident response. When user count makes a full re-push wasteful, add a `WHERE u.created_at > now() - interval '...'` filter to the query — not a work queue.
  6. Resolve ISS-011 — the OAuth callback routes are mounted and returning 500.
  7. Settle the webhook-retry question against the live project (see reviewer round 2, M-B). Nothing depends on the answer today; the point is that the assertion should meet the same evidence bar as the rest of this phase.

---
*Phase: 19-auth-wiring-org-provisioning*
*Completed: 2026-09-08*
