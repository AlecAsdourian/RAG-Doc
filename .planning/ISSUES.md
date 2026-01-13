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

### ISS-006: Test database connectivity configuration

- **Discovered:** Phase 4 Plan 4 (Integration Tests)
- **Type:** Infrastructure / Testing
- **Priority:** LOW (tests compile and are structurally correct, just need infrastructure setup)
- **Description:** Integration tests fail to authenticate to PostgreSQL in Docker container from host machine. Tests compile successfully and are structurally correct, but need database configuration (pg_hba.conf to allow host connections OR run tests inside Docker container). This is infrastructure/setup issue, not code issue.
- **Impact:** Low (tests work inside Docker, just need documentation)
- **Effort:** Low (documentation + pg_hba.conf config OR Docker exec approach)
- **Suggested phase:** Document in Phase 4 or fix in CI/CD setup phase
- **Current status:** Tests at `services/backend/pkg/auth/isolation_test.go` compile and run (5 tests), just need proper DB connectivity
- **Resolution options:**
  1. Run tests inside Docker: `docker exec -it backend go test ./pkg/auth/...`
  2. Configure PostgreSQL pg_hba.conf to allow host machine connections
  3. Use host networking mode for PostgreSQL container
  4. Document test setup requirements in README

## Closed Enhancements

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
