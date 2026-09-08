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

### ISS-011: OAuth callback routes are live and broken, and bypass org-context push

- **Discovered:** Phase 19-03 review (2026-09-08)
- **Type:** Correctness / Dead-but-reachable code
- **Priority:** MEDIUM — a live 500, on routes that are mounted whenever Redis is up
- **Description:** `/auth/github/callback` and `/auth/gitlab/callback` are mounted by `pkg/api/router.go` whenever a Redis state store is reachable. They call `ProvisionOAuthUser` with `fmt.Sprintf("%d", githubUser.ID)` (`pkg/auth/handlers.go`), a numeric GitHub id. Since 19-02, `ProvisionOAuthUser` runs `uuid.Parse` on that argument, so the call cannot succeed — every completed GitHub OAuth callback is a 500.
- **Second problem:** even if it worked, this is a provisioning path that never calls `pushOrgContext`, so a user created through it would have no `app_metadata.organization_id` and would be locked out of every tenant-scoped route.
- **Impact:** Today, low — the intended signup path is Supabase-native (ISS-005), and these handlers were explicitly kept as "reference implementation" in Phase 4. But they are *mounted*, not commented out, so they are reachable in any environment with Redis.
- **Options:** (a) unmount them until there is a real use, (b) delete them, (c) make them a genuine second provisioning path — which means a non-UUID identity column and a shared post-provision hook that includes the org-context push.
- **Effort:** Low for (a) or (b); medium for (c).
- **Related:** ISS-005.

### ISS-010: Isolation harness setup races when test packages run in parallel

- **Discovered:** Phase 19-03 (2026-09-08), running several packages in one `go test` invocation
- **Type:** Test infrastructure / Flake
- **Priority:** MEDIUM — makes any multi-package `go test` unreliable, which blocks CI gating
- **Description:** `pkg/testing/isolation`'s container setup runs `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_doc_app` against the shared, reused testcontainers Postgres. `go test` runs packages in parallel by default, so two packages' setup routines issue that GRANT concurrently and Postgres refuses with `tuple concurrently updated (SQLSTATE XX000)`. Reproduced twice, failing a *different* package each time — a race, not a deterministic break.
- **Workaround:** `go test -p 1 ./pkg/...` (documented in `docs/local-development.md`).
- **Fix options:** take a Postgres advisory lock around role setup; make the grant idempotent-by-construction and retry on XX000; or give each package its own container (costly — container reuse is what keeps the suite fast).
- **Impact:** Any CI job running the whole tree in parallel will flake. Related in spirit to the 19-02 reviewer's note that this shared container makes cross-package interference possible.
- **Effort:** Low (advisory lock is a few lines).

### ISS-009: `pkg/vectordb` does not compile against its pinned Qdrant client

- **Discovered:** Phase 19-03 (2026-09-08), while running the full backend suite
- **Type:** Build / Dependency drift
- **Priority:** LOW today, BLOCKING whenever vector search is wired up
- **Description:** `go build ./...` fails with `pkg/vectordb/client.go:18:17: undefined: qdrant.Client`. `go.mod` pins `github.com/qdrant/go-client v1.7.0`, which predates the high-level `qdrant.Client` type the package imports (introduced in a later minor). The package has not been touched since Phase 3 (`3c47b7e`), so it has almost certainly never compiled since the dependency was pinned — nothing imports it yet, so nothing surfaced it.
- **Why it went unnoticed:** no other package imports `pkg/vectordb`, and the phases since have run targeted `go test ./pkg/...` on specific packages rather than the whole module.
- **Impact:** `go build ./...` and `go test ./...` are red at the module level, which means CI cannot use the whole-module form as a gate until this is fixed. No runtime impact — the package is dead code today.
- **Effort:** Low-to-medium. Either bump `go-client` to a version that exports `qdrant.Client` and fix the call sites, or rewrite `pkg/vectordb` against the v1.7 gRPC-level API.
- **Suggested phase:** whichever phase first wires vector search through Go (or an infrastructure cleanup pass before CI gating).
- **Not fixed in 19-03:** out of scope and unrelated to auth; fixing it would have meant a dependency bump inside a security PR.

### ISS-008: Request-scoped tenant transaction for DB-hitting endpoints

- **Discovered:** Phase 17-02 (2026-09-06)
- **Type:** Architecture / Correctness
- **Priority:** HIGH before any handler starts reading tenant-scoped tables directly
- **Description:** The original `TenantMiddleware` tried to `SET LOCAL app.current_tenant` on a pool-acquired connection, then released the connection before the handler ran. That approach was broken twice over — `SET LOCAL` outside an explicit transaction is a no-op, and pgx's extended query protocol rejects parameterized `SET`. The block was removed in Phase 17-02 (the SET was crashing every request with 500 and blocking isolation tests). For today's endpoints this is fine — Search and StreamChat proxy to Python and never touch RLS-scoped tables from Go. But Phase 20+ handlers (repositories, chunks, queries) WILL query RLS-scoped tables from Go and need a real request-scoped tenant scope.
- **Resolution options (pick in 17-03):**
  1. Middleware begins a transaction, `SET LOCAL app.current_tenant` inside it, stashes the tx on request context, handler pulls tx from context for every query, tx commits on 2xx / rolls back on error.
  2. Every handler that needs DB access calls `isolation.TenantScope` (or a production-equivalent) explicitly, opening its own transaction. Simpler wiring, more boilerplate per handler.
- **Impact:** Correctness — without one of these, Phase 20+ handlers will either bypass RLS or return zero rows.
- **Effort:** Medium (design decision + one refactor to the middleware chain).
- **Related code:** `services/backend/pkg/auth/middleware.go` (TenantMiddleware, currently a context-only pass-through with a `_ = db` reserved for this work).

## Closed Enhancements

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
