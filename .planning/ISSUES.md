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
- **Description:** Users belonging to multiple organizations currently use `X-Organization-ID` header to select which org context to operate in. This is temporary. Production needs: (1) API endpoint to list user's organizations, (2) Frontend UI to select organization, (3) Store selection in JWT custom claims or session, (4) Middleware reads org from JWT instead of header.
- **Impact:** Medium (multi-org users can't switch contexts easily, security concern if header can be spoofed)
- **Effort:** Medium (backend API + JWT claims + frontend UI)
- **Suggested phase:** Phase 5 (API Framework) for backend API, Phase 6 for frontend UI
- **Current code:** `services/backend/pkg/auth/middleware.go` line 75 (uses `X-Organization-ID` header)
- **Implementation:**
  - Create `GET /user/organizations` endpoint (query organization_memberships)
  - Create `POST /user/select-organization` endpoint (validates membership, updates session/JWT)
  - Add `organization_id` to JWT custom claims via Supabase Auth hook
  - Update TenantMiddleware to read org from JWT instead of header

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

### ISS-007: JWT-carried tenant claim + membership validation (supersedes header trust)

- **Discovered:** Phase 17-02 (2026-09-06)
- **Type:** Security / Authorization
- **Priority:** HIGH before v1 public rollout, MEDIUM in current fleet-internal state
- **Description:** `TenantMiddleware` currently sources the caller's tenant from the `X-Organization-ID` request header. Any authenticated user can set this header to any org id and the middleware forwards it downstream unchecked. The isolation harness pins this behavior in `search_isolation_test.go` scenario 5 (`Scenario5_HeaderTamper_HeaderCurrentlyTrusted_TODO_1903`) so a silent change in either direction is caught.
- **Resolution plan (Phase 19-03):** Supabase Auth Hook stamps `organization_id` and `organization_role` on the JWT. TenantMiddleware reads from the claim, cross-checks against `organization_memberships`, and drops the `X-Organization-ID` header path. On completion of 19-03, update the scenario 5 assertion from 200/header-wins to 403 and add a fresh JWT-tampering scenario (sign a token with a mismatched `organization_id` claim and assert reject).
- **Impact:** Cross-tenant access via crafted header; currently gated by "no untrusted client contact" but a leak in v1 rollout.
- **Effort:** Medium (Supabase hook + middleware read path + membership query + fixture updates).
- **Blocked by:** 19-01, 19-02 (auth-cluster prerequisites).
- **Related tests to update in 19-03:** `services/backend/pkg/api/handlers/search_isolation_test.go` scenario 5, `chat_isolation_test.go` scenario 4 (add a JWT variant), `pkg/testing/isolation/testjwt` may need helpers for tampered claim minting.

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
