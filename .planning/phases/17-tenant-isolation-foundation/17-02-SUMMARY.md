---
phase: 17-tenant-isolation-foundation
plan: 02
subsystem: api

requires:
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs, TenantScope, AssertNoCrossTenantLeak, testjwt.Sign (from Fix 3 refactor)
provides:
  - Verified cross-tenant isolation on /api/search (5 scenarios)
  - Verified cross-tenant isolation on /api/chat/stream (4 scenarios)
  - Audited RAG client wire contract: organization_id is present on every Search and StreamChat request body
  - auth.TokenValidator interface — production JWTValidator satisfies it; testjwt.NewValidator injectable
  - api.NewRouterWithValidator — router constructor with swappable token validator
affects: [17-03 (request-scoped tx design), 19-03 (JWT tenant claim replaces header trust), 20+ (any handler doing DB-scoped reads)]

tech-stack:
  added: []
  patterns:
    - "Real router in tests via httptest.NewServer(api.NewRouterWithValidator(...))"
    - "Stub Python side as httptest.NewServer that runs the retrieval query under isolation.TenantScope on the shared pool — RLS is exercised on every scenario, not stubbed away"
    - "Black-box tests in package handlers_test to permit importing pkg/api"

key-files:
  created:
    - services/backend/pkg/testing/isolation/testjwt/validator.go
    - services/backend/pkg/api/handlers/search_isolation_test.go
    - services/backend/pkg/api/handlers/chat_isolation_test.go
    - services/backend/pkg/client/rag_client_test.go
    - .planning/phases/17-tenant-isolation-foundation/17-02-SUMMARY.md
  modified:
    - services/backend/pkg/auth/jwt.go (added TokenValidator interface)
    - services/backend/pkg/auth/middleware.go (JWTAuthMiddleware interface arg; TenantMiddleware SET LOCAL removed)
    - services/backend/pkg/api/router.go (NewRouterWithValidator; NewRouter sugars over it)
    - services/backend/pkg/client/types.go (OrganizationID added to SearchRequest and ChatRequest)
    - services/backend/pkg/api/handlers/search.go (extract auth.OrgIDKey from ctx, propagate to RAG client)
    - services/backend/pkg/api/handlers/chat.go (same)
    - services/backend/pkg/testing/isolation/container.go (waitForDB ping loop before migrations)
    - .planning/ISSUES.md (ISS-007, ISS-008 filed)

key-decisions:
  - "TokenValidator interface + NewRouterWithValidator lets router integration tests run without a live Supabase JWKS. The alternative (fake JWKS server serving HS256 as an oct key + issuer plumbing) was more code and more moving parts for the same outcome."
  - "Reframed Scenario 5 (JWT tampering) to X-Organization-ID header tampering, since the current TenantMiddleware sources tenant from the header not the JWT claim. Scenario 5 pins today's behavior; ISS-007 tracks the JWT variant that lands in 19-03."
  - "Removed the SET LOCAL block from TenantMiddleware in this plan rather than deferring. It crashed every request with 500 (parameterized SET rejected by pgx extended protocol; SET LOCAL outside an explicit tx is a no-op anyway) and blocked router-based testing. ISS-008 tracks the request-scoped tenant transaction design that must come back before any handler does tenant-scoped DB reads (Phase 20+)."
  - "RAG stub server runs the retrieval query under isolation.TenantScope, so RLS is the last line of defense on every scenario — a leak that survives the Go layer's tenant propagation but is caught by RLS still shows as empty, and a leak that survives both would fail loudly."
  - "Test suite lives in black-box package handlers_test / client_test to permit importing pkg/api (which itself imports pkg/api/handlers) without an import cycle."

patterns-established:
  - "Every isolation test that goes through the router builds it with api.NewRouterWithValidator(pool, ragClient, testjwt.NewValidator(), api.Config{...}) and wraps it in httptest.NewServer."
  - "Any test that needs a signed bearer token calls testjwt.Sign(userID, orgID, role) directly — never fabricates a token inline."
  - "The RAG-side stub matches the wire contract exactly: reads SearchRequest/ChatRequest JSON, respects OrganizationID, emits response bytes the real client can parse."

issues-created:
  - ISS-007 (JWT-carried tenant claim + membership check — resolves via 19-03)
  - ISS-008 (Request-scoped tenant transaction — resolves via 17-03)

duration: ~45 min
completed: 2026-09-06
---

# Phase 17 Plan 02: Cross-tenant isolation for /api/search and /api/chat/stream

**Search and chat streaming are now proven isolated end-to-end. The RAG client's request body carries organization_id on every call. Two real leaks and one architectural bug were fixed as part of this plan; two follow-up issues (17-03 request-scoped tx, 19-03 JWT tenant claim) capture the work that intentionally stays out of scope.**

## Accomplishments

- 9 subtests across 3 test files pin isolation for both live HTTP endpoints and the RAG client wire format
- Plan's Sub-step A audit found the RAG client was **not** carrying `organization_id` at all — real leak on both `/search` and `/chat/stream`; fixed in-plan
- `TenantMiddleware`'s `SET LOCAL app.current_tenant` block was crashing every request with 500 (broken parameterized SET on a released pool conn); removed with an ISS-008 follow-up for the proper request-scoped design
- Cold-start `EOF` on the golang-migrate open eliminated with a `waitForDB` pre-flight ping in the 17-01 harness
- Router now takes a `auth.TokenValidator` interface so tests can inject an HS256 validator without wiring a live Supabase JWKS

## Task Commits

Six atomic commits, in order:

1. `baab647` — **test(17-02):** TokenValidator interface + testjwt HS256 validator + NewRouterWithValidator (infra so tests can build the real router without Supabase JWKS)
2. `a03be02` — **fix(17-02):** propagate `organization_id` from tenant context through search and chat handlers to RAG client (the discovered leak)
3. `b6f8d42` — **fix(17-02):** remove broken SET LOCAL from TenantMiddleware and stash tenant on context only (see ISS-008)
4. `5ac7bc5` — **fix(17-02):** ping Postgres until reachable before applying migrations to eliminate cold-start EOF (17-01 harness stability fix)
5. `eecc392` — **test(17-02):** TestSearchIsolation with five cross-tenant scenarios exercising /api/search through the real router
6. `13c60bb` — **test(17-02):** TestChatIsolation and TestRAGClient_PropagatesTenant covering the streaming path and Python request-body contract

_This SUMMARY and the ISSUES.md updates land in a follow-up docs commit._

## Deviations from Plan

### Fixed / adapted in-plan

**1. Sub-step A audit found real leaks; fixed as plan directed.**
The RAG client had no `organization_id` field at all. Both `Search` and `StreamChat` requests would reach Python with no tenant marker, and Python would have to invent one or return cross-tenant data. Fix: added `OrganizationID` to `SearchRequest`/`ChatRequest`, updated both handlers to read `auth.OrgIDKey` from context and set it on the request. `TestRAGClient_PropagatesTenant` pins the wire contract; a rename or removal of the JSON tag breaks the test.

**2. `TestJWT` → `testjwt.Sign` rename.**
Plan referenced `TestJWT(userID, orgID, role)` from `pkg/testing/isolation`. Fix #3 in the 17-01 reviewer follow-up moved it to `pkg/testing/isolation/testjwt.Sign` before this plan started. All references in the new tests use `testjwt.Sign` directly.

**3. Scenario 5 (Task 1): JWT tampering → header tampering.**
Plan expected the JWT to carry `organization_id`. Current `TenantMiddleware` sources tenant from `X-Organization-ID`, not the JWT (the JWT-carried variant ships in 19-03 per ROADMAP). Scenario 5 is reframed to tamper the header and pin today's behavior with a `TODO_1903` marker; ISS-007 tracks flipping the assertion to expect 403 when 19-03 introduces JWT-carried tenant + membership check.

**4. Scenario 3 (Task 2): cross-tenant `session_id` → cross-tenant `repository_id`.**
Chat sessions are not a table yet; the plan's `session_id` scenario has no code to test. Reframed as the streaming analog of search's cross-tenant repo access — orgB asks for orgA's repo, RLS returns empty, sources list is empty, no leak.

**5. Scenario 4 (Task 2): "malformed JWT" → "missing tenant header".**
Same reason as #3: tenant lives in the header today, so missing-header is the actual pre-stream rejection path. Middleware returns 400 before any SSE frames are emitted.

### Fixed opportunistically (in scope of "leaks discovered")

**6. `TenantMiddleware` was crashing every request with 500.**
The `SET LOCAL app.current_tenant = $1` line was broken twice over — pgx's extended protocol rejects parameterized `SET`, and even if it were accepted, `SET LOCAL` outside an explicit transaction is a no-op that would leak state back to the pool when the connection released. The middleware never actually enforced RLS; it just crashed. Block removed; tenant is passed to handlers via `OrgIDKey` context value only. ISS-008 tracks the request-scoped-tx design that must come back before any handler does tenant-scoped DB reads (Phase 20+).

### Not fixed (deferred, documented)

**7. Pre-existing `pkg/auth/webhook_test.go` failure.**
`TestGenerateOrgSlugFromEmail` fails on `main` (`expected "my-org", actual "-org"`), unrelated to any 17-02 change. Reproduced against `RAG-Doc/main` with my changes stashed. Not in scope; suggest planner routes this to whatever phase touches organization slug generation next.

**8. Pre-existing `pkg/vectordb` compile errors.**
`go build ./...` still fails in `pkg/vectordb` due to a qdrant client API drift (documented in 17-01 SUMMARY). All 17-02 touched packages build and vet cleanly.

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/api/handlers/... ./pkg/client/... ./pkg/auth/... ./pkg/testing/isolation/...` | clean |
| `go test -count=1 -run TestSearchIsolation ./pkg/api/handlers/...` | 5/5 pass (~0.2s warm) |
| `go test -count=1 -run TestChatIsolation ./pkg/api/handlers/...` | 4/4 pass (~0.2s warm) |
| `go test -count=1 -run TestRAGClient ./pkg/client/...` | 2/2 pass (~0.5s) |
| `go test -count=1 ./pkg/testing/isolation/...` (17-01 regression) | 8/8 pass |
| Combined touched packages | 19/20 pass; 1 pre-existing unrelated failure (see deviations #7) |
| Cold-start container + full suite | ~12s |
| Warm run | ~1.5s |

Test-time budget from plan (<60s cold, <10s warm) met with plenty of headroom.

## Issues Encountered

- **RAG client had no tenant field.** Sub-step A audit paid off — the plan predicted this might be missing, and it was. Fix scoped to the client's request types and both handlers.
- **TenantMiddleware crashed every request.** Discovered while running scenario 1 — every request returned 500. Root cause was two layered bugs on one line (parameterized SET + SET LOCAL outside tx). Removed; ISS-008 filed for the proper design.
- **Cold-start `EOF` from golang-migrate.** Testcontainers' "ready to accept connections" log strategy fires slightly before the port routing settles on cold Docker starts. `waitForDB` in the harness polls until a `pgx.Connect` + `Ping` succeeds before handing off to the migrator. First warm-cache run showed the fix eliminated the flake.

## Next Phase Readiness

- **17-03** (PL/pgSQL trigger for RLS enforcement): can lean on the same test harness; add scenarios that exercise the trigger path via `TenantScope`.
- **17-04** (Python isolation harness): the SearchRequest/ChatRequest wire contract now includes `organization_id` — the Python side already has what it needs to scope retrieval.
- **19-03** (JWT tenant claim + membership check): ISS-007 lists the exact test scenarios to flip and the code path to change.
- **Phase 20+** (repos/chunks/queries handlers): ISS-008 must be resolved before any handler does a direct tenant-scoped DB read from Go. `TenantMiddleware` currently gives you `OrgIDKey` on context but no RLS-scoped transaction; a handler that queries under RLS today would get zero rows.

---
*Phase: 17-tenant-isolation-foundation*
*Completed: 2026-09-06*
