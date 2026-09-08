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


### ISS-004: Organization selection mechanism for multi-org users

- **Discovered:** Phase 4 (Authentication System)
- **Type:** User Experience / Authorization
- **Priority:** MEDIUM (functional but not production-ready)
- **Partially resolved:** Phase 19-03 (2026-09-08) — see "Remaining scope" below.
- **Description:** Users belonging to multiple organizations had no way to choose which org context they operate in beyond the `X-Organization-ID` header. Production needs: (1) API endpoint to list user's organizations, (2) Frontend UI to select organization, (3) Store selection in JWT custom claims or session, (4) Middleware reads org from JWT instead of header.
- **Done in 19-03:** items (3) and (4). `app_metadata.organization_id` is written onto the Supabase user at provisioning time and read back off the verified JWT by `TenantMiddleware`; the header path is deleted, not deprecated. This closes the *security* half of the issue.
- **Remaining scope (19-04):** items (1) and (2) — a user with two organizations still gets whichever one provisioning stamped, with no way to switch. Needs `GET /api/user/organizations`, `POST /api/user/select-organization` (validates membership, then rewrites `app_metadata.organization_id` through the same `auth.AdminClient` 19-03 added), and the frontend picker.
- **Note on the original implementation sketch:** it called for a Supabase Auth Hook to inject the claim at token-mint time. 19-03 deliberately did not use one — the hook is a Postgres function living in the Supabase instance, and the app's data lives in a *separate* Postgres, so the hook could not see `organization_memberships` to make the decision. Writing `raw_app_meta_data` from the backend achieves the same claim with no cross-instance dependency. Cost: the claim only refreshes on token refresh, which 19-04's select-organization endpoint has to account for.
- **Impact:** Medium (multi-org users still can't switch contexts).
- **Effort:** Medium (backend API + frontend UI).

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

### ISS-007: JWT-carried tenant claim (supersedes header trust) ✅

- **Discovered:** Phase 17-02 (2026-09-06)
- **Closed:** 2026-09-08 (Phase 19-03)
- **Type:** Security / Authorization
- **Priority:** HIGH before v1 public rollout
- **Original problem:** `TenantMiddleware` sourced the caller's tenant from the `X-Organization-ID` request header. Any authenticated user could set it to any org id and the middleware forwarded it downstream unchecked — a valid login for one organization could read another organization's data. 17-02 pinned the behavior in `search_isolation_test.go` scenario 5 rather than leaving it undetected.
- **Resolution:** Tenant identity now comes exclusively from `app_metadata.organization_id`, a Supabase-signed claim on the access token. The header path is **deleted**, not deprecated — including from the CORS `Access-Control-Allow-Headers` list, so a client cannot even send it. A token with no organization claim gets 403 rather than defaulting into anyone's org.
- **Files:**
  - `services/backend/pkg/auth/supabase_admin.go` (new) — writes `app_metadata` onto the Supabase user via the admin API
  - `services/backend/pkg/auth/jwt.go` — `ExtractOrganizationID` / `ExtractOrganizationRole` read the nested claim
  - `services/backend/pkg/auth/middleware.go` — header read replaced by claim read
  - `services/backend/pkg/auth/webhook.go` — pushes org context on every delivery so a failed prior push converges
  - `services/backend/pkg/api/router.go` — wires the admin client, drops the header from CORS
- **Deviation from the original plan, deliberate:** no Supabase Auth Hook, and no per-request membership re-check. See the 19-03 summary for the reasoning on both; the short version is that the hook cannot reach the app's database (separate Postgres instance), and re-querying membership on every request would add a DB round-trip to the hot path to defend against an attacker who would already need Supabase's signing key.
- **Verified:** the claim shape was confirmed against the live Supabase project by round-trip (admin write → sign in → decode token), not assumed. `testjwt.Sign` now emits the same nested shape, with a drift-guard test asserting the flat shape is *not* emitted — the pre-19-03 harness signed flat claims, so every isolation test passed against tokens production could never receive.
- **Tests:** 10 isolation scenarios across `search_isolation_test.go` (6) and `chat_isolation_test.go` (4), all on the JWT path, including a tampered-claim scenario and a no-claim scenario.

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
