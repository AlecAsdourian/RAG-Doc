# Project Issues Log

Enhancements discovered during execution. Not critical - address in future phases.

## Open Enhancements

### ISS-001: Implement shared type definitions for cross-phase data contracts

- **Discovered:** Phase 12 Task 3 (2026-01-12)
- **Type:** Refactoring / Code Quality
- **Description:** Create shared TypedDict definitions for data passed between phases to prevent integration bugs. Currently, phases make assumptions about data structure from upstream phases (e.g., AnswerGenerator assumed QueryEngine returns 'content' field). This led to a bug where QueryEngine returned 'content_preview' but not 'content', causing LLM to receive insufficient context. Shared type definitions would:
  - Make contracts explicit and type-checkable
  - Prevent field name mismatches
  - Enable IDE autocomplete for cross-phase data
  - Document expected data structures in code
- **Impact:** Medium (prevents integration bugs, improves maintainability)
- **Effort:** Medium (create `workers/types/` module with contracts for retrieval results, chunk data, query results)
- **Suggested phase:** Before Phase 13 (Web UI) to establish contracts for API responses
- **Example:**
  ```python
  # workers/types/retrieval.py
  class ChunkResult(TypedDict):
      chunk_id: str
      file_path: str
      content: str  # ← Explicit requirement
      content_preview: str
      # ... all fields documented
  ```

### ISS-002: Add cross-phase verification pattern to planning workflow

- **Discovered:** Phase 12 Task 3 (2026-01-12)
- **Type:** Process Improvement
- **Description:** When a phase depends on output from a previous phase, add an explicit verification task to the plan that checks the upstream component's actual output before implementation begins. This would have caught the QueryEngine/AnswerGenerator contract mismatch earlier. Pattern: Before implementing integration, run upstream component and verify its output schema matches assumptions.
- **Impact:** Medium (prevents integration issues, improves plan quality)
- **Effort:** Low (documentation/template update to remind planners to add verification tasks)
- **Suggested phase:** Update planning templates after Phase 12 completion
- **Example task in PLAN.md:**
  ```markdown
  <task type="auto">
    <name>Verify QueryEngine output contract</name>
    <action>
      Before implementing AnswerGenerator, verify QueryEngine returns:
      - Full 'content' field (not just preview)
      - All required metadata fields

      Run test query and inspect output schema.
    </action>
  </task>
  ```


### ISS-012: A revoked membership does not revoke the organization claim

- **Discovered:** Phase 19-04 (2026-09-08)
- **Type:** Security / Authorization
- **Priority:** HIGH before any membership-removal feature ships; **not exploitable today** (nothing removes memberships)
- **Description:** The organization claim is written at two moments — provisioning, and `POST /api/user/select-organization` — and at no other time. Supabase re-reads the same `raw_app_meta_data` column at every token mint, so refreshing a token *preserves* the claim rather than recomputing it. Removing a user from an organization therefore does not end their access to it: their token still names that org, `TenantMiddleware` still honors it, and every refresh renews it indefinitely.
- **Why it is not a live bug:** no code path removes a membership. `git grep 'DELETE FROM organization_memberships'` finds only test cleanup.
- **Why it is filed anyway:** the natural mental model — "stateless claims expire, so exposure is bounded by token lifetime" — is **wrong here**, and it is the model a future author will bring. Short token TTLs do not mitigate this at all. This was written into 19-03's summary as fact before a reviewer caught it.
- **What closes it:** whatever ships membership removal must also rewrite the affected user's claim (and, if they were removed from their *active* org, decide what to put there — most likely another membership, or nothing plus a 403 that routes them to the org picker). `auth.AdminClient` is the mechanism; `cmd/backfill-org-claims` is the precedent.
- **Related:** ISS-004 (closed), and the "Not covered here" section of `docs/auth-frontend-contract.md`, which carries this warning forward to Phase 20+.

### ISS-005: Supabase Native OAuth webhook handler

- **Discovered:** Phase 4 Plan 3 (Architecture Decision)
- **Type:** Feature Implementation / Integration
- **Priority:** MEDIUM (architectural decision needs implementation)
- **Description:** Decided to use Supabase Native OAuth (Supabase handles OAuth flow) instead of custom OAuth implementation. This requires implementing webhook handler to receive `user.created` events from Supabase and provision users in our database. Current OAuth handlers serve as reference implementation only.
- **Impact:** Medium (can't use Supabase OAuth until webhook handler exists, currently relying on direct DB user creation)
- **Effort:** Medium (webhook endpoint, signature verification, user provisioning reuse)
- **Suggested phase:** Phase 5 or dedicated "Phase 04-05: Supabase Integration"
- **Blocked by:** Requires actual Supabase project setup with credentials
- **Current code:** `services/backend/pkg/auth/provisioning.go` (ProvisionOAuthUser, CreateOrganizationForUser - reusable)
- **Implementation:**
  - Create `POST /webhooks/supabase` endpoint
  - Verify webhook signature (HMAC with Supabase webhook secret)
  - Handle `user.created` event: call ProvisionOAuthUser, CreateOrganizationForUser
  - Configure webhook URL in Supabase dashboard
  - Update frontend to use Supabase JS client for OAuth


### ISS-014: `pkg/db` imports `pkg/auth`, which inverts the layering

- **Discovered:** Phase 20-01 review (2026-09-08)
- **Type:** Architecture / Maintainability
- **Priority:** LOW — no cycle today, and nothing is blocked
- **Description:** `db.tenantFromContext` reads the caller's organization via `auth.OrgIDFromContext`, so `pkg/db` depends on `pkg/auth`. `pkg/auth` currently imports no internal package, so there is no cycle.
- **Why it may bite:** `pkg/auth` already does its own database work with a raw pool (`provisioning.go`, `webhook.go`). The first time any of `users` / `organizations` / `organization_memberships` gains RLS, or any auth flow needs a tenant-scoped write, `pkg/auth` will want `db.TenantScoper` — and the import direction makes that a refactor rather than a line.
- **Fix:** move the context key and its accessors to a leaf package (`pkg/tenantctx`) that both can import. Mechanical: the key already has accessors as of 20-01, so the change is an import rewrite across five call sites.
- **Not done in 20-01** because the cycle does not exist, the benefit is speculative, and the refactor would have widened a plan that already grew a security fix.

### ISS-016: `sync_state` has no lease, so a relink can re-queue a run already in flight

- **Discovered:** Phase 20-03 second review (2026-09-09)
- **Type:** Correctness / Ingestion
- **Priority:** MEDIUM — must be settled **before Phase 21 builds the queue**, not after
- **Description:** `POST /api/repositories` sets `sync_state = 'pending'` when a repository's `installation_id` changes. If the row was `syncing` at that moment, it is re-queued while the original run is still going, and whichever finishes last writes the final state. `idx_repositories_sync_state` is a partial index on `sync_state <> 'synced'`, so the Phase 21 worker will pick the re-queued row straight up.
- **Why it was not simply avoided:** refusing to re-queue a `syncing` row is worse. The in-flight run holds an installation token for an App that was just uninstalled, so it will fail regardless — and leaving the row `syncing` strands it until that failure lands, with nothing to retry it.
- **The actual gap:** `sync_state` is a status column being used as a queue, with no lease, owner or attempt counter. Two writers can believe they own the same repository. 20-05's webhook writes go through the same upsert, so it inherits this.
- **What Phase 21 should do:** give the queue a lease (`sync_lease_owner`, `sync_lease_expires_at`) or move it out of `repositories` entirely. Then a relink can cancel or supersede a run rather than racing it.
- **Also carried:** a `failed` repository cannot be retried through this API at all — re-connecting deliberately does not reset the state. Documented in `docs/api-repositories.md`; Phase 21 owns retry.

### ISS-015: The isolation scanner's coverage match is method-blind

- **Discovered:** Phase 20-03 review (2026-09-08), while fixing the nested-`chi.Route` blind spot
- **Type:** Testing / CI
- **Priority:** LOW — the ratchet works; this is the last soft edge in it
- **Description:** `scripts/ci/check-isolation-tests.py` decides coverage by looking for the endpoint's path in an isolation-test file. It cannot see which HTTP method the test exercises, so an existing test that only does `GET /api/things` marks a newly added `POST /api/things` as covered.
- **Why it was not fixed with the rest of 20-03's scanner work:** every cheap way to add method-awareness reads a Go test for method tokens (`http.MethodPost`, `"POST"`, helper wrappers) and guesses. A gate that fails a PR because the author spelled the method differently gets disabled, and a disabled gate is worse than a loose one. Worth doing properly — resolve the handler symbol per route and check the test drives that handler — or not at all.
- **Correction to the first version of this entry.** It claimed "every mutation route in a nested `chi.Route` block is now detected with its full path". False when written, and found by review: `r.With(mw).Post(...)` and `r.Method("POST", …)` were both invisible, and a new route nested under an already-tested prefix was marked covered for nothing. Both fixed and pinned; this entry is narrowed to what actually remains.
- **What IS pinned now, each by its own test in `scripts/ci/test_check_isolation.py`:** nested `chi.Route` blocks; middleware-wrapped registrations; `Method`/`MethodFunc`; every static segment of a parameterised path having to appear, not just the leading one; a path resolving to `/` never matching; braces inside strings, raw strings and block comments; and a skip marker on a group opener not reaching the routes inside it.

### ISS-013: Unscoped access to an RLS table behaves differently depending on connection history

- **Discovered:** Phase 20-01 (2026-09-08), while writing the tests for `TenantScoper`
- **Type:** Correctness / Operability
- **Priority:** MEDIUM — no live code path hits it, but it is a heisenbug generator
- **Description:** The RLS policies in migration 000008 compare against `current_setting('app.current_tenant', true)::uuid`. The `missing_ok` flag makes an *unset* GUC return `NULL`, which filters every row and returns an empty result with no error. But a **committed `SET LOCAL` leaves the GUC as an empty string** on that backend permanently (`RESET` and `SET TO DEFAULT` do not clear it — established 17-02), and `''::uuid` raises **SQLSTATE 22P02**.
- **So the same unscoped query is silently empty OR a 500**, depending on which pooled connection it gets and what that connection did earlier. Verified by direct probe; both halves are pinned by `TestUnscopedAccess_BehaviourDependsOnConnectionHistory` in `pkg/db/tenant_isolation_test.go`.
- **Why it is not live today:** `db.TenantScoper` makes unscoped access unreachable from a correctly-constructed handler, and the only handler holding a raw pool (`user_orgs.go`) touches no RLS table.
- **Why it is filed anyway:** it fails in the direction that trains people badly. A fresh test process gets `NULL` and sees a clean empty result; production, once connections have been reused, gets intermittent 500s with a message about invalid uuid syntax that points nowhere near the actual cause.
- **Independently reproduced** on PG 16.11 during 20-01 review, including the case I had not tested. **Nothing clears it short of reconnecting:**

  | after a committed `SET LOCAL` | `current_setting('app.current_tenant', true)` |
  |---|---|
  | (as-is) | `''` |
  | `RESET app.current_tenant` | `''` |
  | `SET app.current_tenant TO DEFAULT` | `''` |
  | `RESET ALL` | `''` |
  | **`DISCARD ALL`** | **`''`** |

  And pgxpool never runs `DISCARD ALL` — it only destroys closed, busy, in-transaction, or expired connections, and this repo registers no `AfterRelease` hook. Corroborating evidence from another angle: `SetupTestDB` does `SET ROLE rag_doc_app` in `AfterConnect`; if pgxpool reset connections on release, that would revert to the superuser (`rolbypassrls=t`) and **every isolation test in the repo would silently pass**. It does not.

- **THE FIX IS CHEAPER THAN FIRST WRITTEN — no migration required.** The original entry said this needs a policy change across all six tables. It does not. A one-line `pgxpool.Config.AfterConnect` sentinel gives the recommended always-loud behaviour on every connection, measured:

  ```sql
  SET app.current_tenant = '';                      -- once, in AfterConnect
  SELECT count(*) FROM repositories;                -- ERROR 22P02, deterministically
  BEGIN; SET LOCAL app.current_tenant = '1111...';
    SELECT count(*) FROM repositories;              -- works
  COMMIT;                                            -- reverts to '', same after ROLLBACK
  ```

  Reversible, no schema change, and it makes the two "kinds of missing" into one.

- **Other options:** `NULLIF(current_setting('app.current_tenant', true), '')::uuid` in the policies makes it deterministically *silent*; dropping the `missing_ok` flag makes it deterministically *loud*. Both are migrations across six tables and neither is necessary given the above.
- **Recommendation:** the `AfterConnect` sentinel, giving deterministically **loud**. With `TenantScoper` in place an unscoped query is by definition a bug, and a bug that always throws is cheaper than one that sometimes returns `[]`. Still an operational-risk judgement — a 500 is worse than an empty list for a user who trips it — so it belongs to whoever owns that call, but it is now a pool-constructor line rather than a schema change.

## Closed Enhancements

### ISS-008: Request-scoped tenant transaction for DB-hitting endpoints ✅

- **Discovered:** Phase 17-02 (2026-09-06)
- **Closed:** 2026-09-08 (Phase 20-01)
- **Type:** Architecture / Correctness
- **Original problem:** `TenantMiddleware` once tried to `SET LOCAL app.current_tenant` on a pool-acquired connection and released it before the handler ran — broken twice over, since `SET LOCAL` outside a transaction is a no-op and pgx rejects a parameterized `SET`. It was removed in 17-02, leaving no way for a Go handler to query an RLS-scoped table.
- **Verified blocking, not theoretical:** the only Go handler touching the database before this phase was `user_orgs.go`, which reads `users` / `organizations` / `organization_memberships` — none RLS-scoped. **No Go handler had ever read an RLS-scoped table.** `GET /api/repositories` (20-03) is the first.
- **Resolution: option 2, hardened.** `db.TenantScoper` opens a transaction, sets `app.current_tenant` from the verified claim, and runs the handler's callback inside it. Option 1 (middleware-opens-transaction) was rejected: it would hold a pooled connection and an open transaction for the life of every authenticated request, including `/api/chat/stream`, and it couples commit to HTTP status. Full comparison in `20-01-DESIGN.md`.
- **The hardening is the part that matters.** Option 2's weakness is that a handler can forget. So tenant-scoped handlers are constructed with a `*db.TenantScoper` and **not** a `*pgxpool.Pool` — there is no unscoped path through the type, and giving a handler a pool becomes a visible act in `router.go` rather than an omission inside a handler. Same principle as 19-03 deleting the `X-Organization-ID` header instead of deprecating it.
- **Files:** `services/backend/pkg/db/tenant.go` (new), `pkg/db/tenant_isolation_test.go` (new), `pkg/auth/middleware.go` (the reserved `db *pgxpool.Pool` parameter is gone, not ignored), `pkg/api/router.go`, `docs/isolation.md`.
- **Surfaced ISS-013** — the failure mode for bypassing this turned out to be nondeterministic rather than merely silent, which is why the type refuses to hand out a pool rather than merely documenting that you shouldn't use one.
- **Mutation-verified:** removing the `SET LOCAL` turns four tests red.

### ISS-009: `pkg/vectordb` does not compile against its pinned Qdrant client ✅

- **Discovered:** Phase 19-03 (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work)
- **Resolution:** bumped `github.com/qdrant/go-client` v1.7.0 → v1.19.2 and fixed three points of API drift — `CreateFieldIndex` now returns `(*UpdateResult, error)`, and `NewIDString` became `NewIDUUID`. The package was written against the high-level client API introduced in v1.9, so the pin had *always* been wrong. Never a regression; it simply never compiled.
- **What fixing it exposed:** with the package building, its own unit tests ran for the first time and **panicked**. `TestUpsertVectorsValidation` builds a zero-value `Client{}`, and its "matching lengths" case — the one meant to prove validation *accepts* good input — necessarily proceeds past validation into the wire call, dereferencing a nil connection. `TestSearchSimilarValidation` had the identical latent bug and had never run at all, because the first panic killed the test binary.
- **Also fixed:** validation split into `validateUpsertInput` / `validateQueryVector` so the rules are testable without a live Qdrant, plus an `ErrNotConnected` guard so a clientless call returns a legible error rather than panicking several frames inside the SDK.
- **Root cause of the invisibility:** no CI job had ever built the Go code. Closed alongside the new `.github/workflows/backend-ci.yml`.

### ISS-010: Isolation harness setup races when test packages run in parallel ✅

- **Discovered:** Phase 19-03 (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work)
- **Resolution:** `ensureAppRole` now runs its whole statement sequence in one transaction holding `pg_advisory_xact_lock`. All five statements are covered, not just the GRANTs — the `DO $$ … CREATE ROLE` block is check-then-act and races the same way, it just failed less visibly (SQLSTATE 42710 instead of XX000).
- **Why transaction-scoped, not session-scoped:** `CREATE ROLE` and `GRANT` are both transactional in Postgres, so the sequence commits or rolls back as a unit, and the server releases an xact lock however the process dies. A session-level `pg_advisory_lock` leaks if a test binary panics between acquire and release.
- **Precedent:** the same mechanism golang-migrate already uses around migrations — which is exactly why migrations survived the concurrency that broke role setup.
- **Verified:** 5 consecutive **cold-container** parallel runs, zero failures. Cold is the case that matters: the race reproduced on roughly 40% of cold starts and effectively never on a warm container, so its failure profile was "green locally, red in CI".

### ISS-011: OAuth callback routes are live and broken, and bypass org-context push ✅

- **Discovered:** Phase 19-03 review (2026-09-08)
- **Closed:** 2026-09-08 (CI gate work) — **unmounted, not repaired**
- **Resolution:** `/auth/{github,gitlab}/{login,callback}` are no longer mounted. They were served whenever Redis happened to be reachable, and every completed callback returned 500 — GitHub's numeric user id fails the `uuid.Parse` provisioning has done since 19-02.
- **Why unmount rather than fix:** repairing the 500 alone would have been worse. These handlers are a second provisioning path that never calls `pushOrgContext`, so a user created through them would have no organization claim and be refused by every tenant-scoped route — turning a loud 500 into a quiet broken account.
- **Handlers kept, not deleted.** Deleting is a planner/user call, and they stay useful as reference if direct OAuth is ever wanted alongside Supabase-native. Reviving them needs a non-UUID identity column in provisioning plus routing through the same post-provision org-context push the webhook uses.
- **The StateStore probe is retained** — it reports a real configuration gap, and Phase 20's GitHub App flow will want it.

### ISS-004: Organization selection mechanism for multi-org users ✅

- **Discovered:** Phase 4 (Authentication System)
- **Closed:** 2026-09-08 (Phase 19-04; security half closed in 19-03)
- **Type:** User Experience / Authorization
- **Original problem:** Users belonging to multiple organizations had no way to choose which org context they operate in beyond the `X-Organization-ID` header. Needed: (1) an endpoint listing the user's organizations, (2) a frontend picker, (3) the selection stored in a JWT claim, (4) middleware reading the claim instead of the header.
- **Done in 19-03:** items (3) and (4). `app_metadata.organization_id` is written onto the Supabase user and read back off the verified JWT by `TenantMiddleware`; the header path deleted, not deprecated.
- **Done in 19-04:** item (1) plus the switching mechanism — `GET /api/user/organizations` and `POST /api/user/select-organization`, both user-scoped and deliberately outside `TenantMiddleware` so a user with no organization claim can still reach them. Item (2), the frontend picker, is Phase 23 work implementing `docs/auth-frontend-contract.md`; the backend contract it needs is complete and documented, so this issue is closed rather than left open on UI.
- **Note on the original implementation sketch:** it called for a Supabase Auth Hook to inject the claim at token-mint time. Impossible in this architecture — the hook is a Postgres function inside Supabase's instance and `organization_memberships` lives in a different one. The backend writes `raw_app_meta_data` instead.
- **Verified against the live project (2026-09-08):** `refreshSession()` genuinely re-reads `raw_app_meta_data`, so 202 → refresh → new claim works and a full sign-out/sign-in is not required.
- **Security note carried forward:** switching writes BOTH `organization_id` and `organization_role`, because Supabase merges `app_metadata` and omitting the role leaves the previous one in place. See 19-04's summary; the escalation this prevents is pinned by a test.
- **Successor issue:** ISS-012 — a *revoked* membership still does not revoke the claim. Out of scope here (nothing removes memberships yet).

### ISS-007: JWT-carried tenant claim (supersedes header trust) ✅

- **Discovered:** Phase 17-02 (2026-09-06)
- **Closed:** 2026-09-08 (Phase 19-03)
- **Type:** Security / Authorization
- **Priority:** HIGH before v1 public rollout
- **Original problem:** `TenantMiddleware` sourced the caller's tenant from the `X-Organization-ID` request header. Any authenticated user could set it to any org id and the middleware forwarded it downstream unchecked — a valid login for one organization could read another organization's data. 17-02 pinned the behavior in `search_isolation_test.go` scenario 5 rather than leaving it undetected.
- **Resolution:** Tenant identity now comes exclusively from `app_metadata.organization_id`, a Supabase-signed claim on the access token. The header path is **deleted**, not deprecated — including from the CORS `Access-Control-Allow-Headers` list, so a client cannot even send it. A token with no organization claim gets 403 rather than defaulting into anyone's org.
- **Files:**
  - `services/backend/pkg/auth/supabase_admin.go` (new) — writes `app_metadata` onto the Supabase user via the admin API
  - `services/backend/pkg/auth/jwt.go` — `ExtractOrganizationID` / `ExtractOrganizationRole` read the nested claim and require a UUID
  - `services/backend/pkg/auth/middleware.go` — header read replaced by claim read
  - `services/backend/pkg/auth/webhook.go` — pushes org context after provisioning
  - `services/backend/cmd/backfill-org-claims` (new) — the repair path for a failed push
  - `services/backend/pkg/api/router.go` — wires the admin client, drops the header from CORS
- **Deviation from the original plan, deliberate:** no Supabase Auth Hook, and no per-request membership re-check. See the 19-03 summary for the reasoning on both; the short version is that the hook cannot reach the app's database (separate Postgres instance), and re-querying membership on every request would add a DB round-trip to the hot path to defend against an attacker who would already need Supabase's signing key.
- **Correction to an earlier version of this entry.** It described the org-context push as converging "on every webhook delivery". That is false and was caught in review: Supabase database webhooks fire once and never retry (recorded in `04-06-SUMMARY.md`), and the trigger behind ours is `AFTER INSERT ON auth.users`, so each user gets exactly one delivery for all time. A failed push therefore leaves that user permanently claim-less. The code is replay-safe, but nothing replays it — `cmd/backfill-org-claims` is the actual repair, and it is also the migration step for users provisioned before the claim existed.
- **Second correction.** The residual risk of skipping the per-request membership check was described as "the token lifetime window". Also false: `raw_app_meta_data` is written once and never recomputed, so Supabase re-reads the same stale value at every mint and a *refreshed* token carries the *same* organization. Removing a user from an org does not expire their claim — short token TTLs do not mitigate this at all. Nothing removes memberships today, but **19-04 must rewrite the claim on every membership change**; that, not token expiry, is what bounds the exposure.
- **Verified:** the claim shape was confirmed against the live Supabase project by round-trip (admin write → sign in → decode token), not assumed. `testjwt.Sign` now emits the same nested shape, with a drift-guard test asserting the flat shape is *not* emitted — the pre-19-03 harness signed flat claims, so every isolation test passed against tokens production could never receive.
- **Tests:** 11 isolation scenarios across `search_isolation_test.go` (7) and `chat_isolation_test.go` (4), plus unit coverage for claim extraction. Scenario 7 is the regression guard for this very issue: with the header path re-added to the middleware, it is the only test in the suite that fails. Both it and scenario 5 were confirmed non-vacuous by mutation — breaking the middleware turns them red.

### ISS-006: Test database connectivity configuration ✅

- **Discovered:** Phase 4 Plan 4 (Integration Tests)
- **Closed:** 2026-09-05 (Phase 17-01 — Go isolation harness)
- **Type:** Infrastructure / Testing
- **Priority:** LOW (superseded by a better approach)
- **Original problem:** Integration tests could not connect to the docker-compose Postgres from the Windows host. Structurally the tests were correct but the connectivity story was flaky.
- **Resolution:** Replaced the docker-compose dependency entirely with `testcontainers-go`. Every test now spins up (or reuses) an ephemeral Postgres via the Docker API — no host port binding, no `pg_hba.conf` tuning, no host-vs-container URL split. Migrations are applied programmatically via `golang-migrate`. Container reuse across `go test` invocations keeps the second-and-onward run under ~1.5s.
- **Files created:**
  - `services/backend/pkg/testing/isolation/container.go` — `SetupTestDB` and container reuse
  - `services/backend/pkg/testing/isolation/migrator.go` — programmatic migration runner
- **Verified:** Works from Windows host without docker-compose running; two consecutive `go test` invocations complete in <5s.

### ISS-003: OAuth state validation (CSRF protection) ✅

- **Discovered:** Phase 4 (Authentication System)
- **Closed:** 2026-01-13 (Phase 4 - Security Fix)
- **Type:** Security / Authentication
- **Priority:** HIGH (security gap)
- **Description:** OAuth handlers were generating CSRF state tokens but not validating them on callback, creating CSRF vulnerability.
- **Resolution:** Implemented StateStore using Redis with 5-minute TTL. State tokens are:
  - Generated on login and stored in Redis (key: `oauth:state:{token}`)
  - Validated on callback (checks existence in Redis)
  - Single-use (deleted after validation)
  - Auto-expire after 5 minutes (TTL)
- **Files Created:**
  - `services/backend/pkg/auth/state_store.go` - Redis-based state storage
  - `services/backend/pkg/auth/state_store_test.go` - 5 tests verifying CSRF protection
- **Files Modified:**
  - `services/backend/pkg/auth/handlers.go` - Updated all OAuth handlers to use StateStore
  - `services/backend/.env.example` - Added REDIS_URL configuration
- **Tests Added:**
  - TestStateStore_StoreAndValidate: Basic functionality
  - TestStateStore_SingleUse: Tokens work once only
  - TestStateStore_InvalidToken: Unknown tokens rejected
  - TestStateStore_ExpiredToken: Tokens expire after TTL
  - TestStateStore_CSRFProtection: CSRF attack prevention verified
- **Impact:** CSRF vulnerability closed, OAuth flow now secure
