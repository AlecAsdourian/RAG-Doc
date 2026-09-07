# Phase 19: Auth Wiring & Org Provisioning — Context

## Objective

Finish what Phase 4 started. First-time user signs up via Supabase-native
OAuth, receives a JWT that carries their tenant scope, and hits
tenant-scoped APIs end-to-end. All the isolation plumbing from Phase 17
is inert until this phase mints real users into real orgs with real
JWTs.

By the end of Phase 19:

- OAuth handlers from `pkg/auth/handlers.go` are actually mounted on the
  router (they compile today but are unreachable).
- Supabase webhook receives `user.created` events, verifies the HMAC
  signature, and auto-provisions the user + a starter organization.
- The JWT carries `organization_id` and `organization_role` as custom
  claims; `TenantMiddleware` reads them and the `X-Organization-ID`
  header path is gone.
- Multi-org users can list their memberships and switch active org via
  JWT rotation.
- Public-signup path has minimum-viable abuse protection (per-IP rate
  limit on the webhook; disposable-email-domain block).

## Essential Deliverables

1. **OAuth routes reachable.** `/auth/github/login`,
   `/auth/github/callback`, `/auth/gitlab/login`, `/auth/gitlab/callback`
   mounted on `pkg/api/router.go` under a public (unauthenticated)
   group.
2. **Supabase webhook with signature check + provisioning.** `POST
   /webhooks/supabase` verifies `X-Webhook-Signature` HMAC; on
   `user.created` calls `ProvisionOAuthUser` + auto-creates a starter
   org; idempotent under replay.
3. **JWT custom claims.** Supabase Auth Hook injects `organization_id`
   and `organization_role` into the access token; `TenantMiddleware`
   reads them. Header path removed.
4. **Multi-org UX (backend).** `GET /api/user/organizations` lists
   memberships. `POST /api/user/select-organization` validates and
   triggers JWT rotation (via `raw_app_meta_data` update + frontend
   `session.refresh()`).
5. **Abuse protection minimum.** Per-IP rate limit on the webhook + a
   disposable-email-domain blocklist. Real rate limits, captcha, and
   full billing controls remain a Phase 24 concern.

## Boundaries

**In scope:**
- Wiring existing auth code, writing new webhook/provisioning/JWT-hook
  code, backend endpoints for multi-org, isolation tests for every new
  endpoint (CI gate from 17-05 enforces this).
- Small production fix to `pkg/auth/webhook_test.go::TestGenerateOrgSlugFromEmail`
  — the pre-existing failure that Phases 17-02 through 17-04 flagged.
  Slug generation lives in the provisioning code path this phase
  touches; fixing it here is in-scope.
- Removing the `X-Organization-ID` header entirely (hard cutover per
  user decision). All existing isolation tests that pinned header-trust
  behavior update in the same PR that ships the JWT claim.

**Out of scope:**
- Frontend `OrgSelectPage` wire-up to real data. Frontend work
  continues in Phase 23 per ROADMAP; this phase only ships the
  endpoints and stubs the frontend contract.
- Full observability primitives. Basic `slog` structured logs on the
  webhook and auth handlers only; no metrics, no tracing. Phase 18
  deferred (see `project_phase18_deprioritized` memory).
- Full rate-limiting / abuse infrastructure. That's Phase 24. This
  phase adds a minimum viable webhook rate-limit and email-domain
  block, no more.
- Repository connection flows. That's Phase 20+.
- Session management via Redis / cookies. JWT-scoped active-org (per
  user decision) means no server-side session state.

## Decisions Locked

| Decision | Choice | Rationale |
|---|---|---|
| JWT/header transition | **Hard cutover** in 19-03 | No users to break; cleaner code path; forces isolation tests to prove the JWT-carried path works. |
| Multi-org active-org tracking | **JWT-scoped** (baked into JWT, rotation on switch) | Cleanest with 19-03. No Redis dependency on the auth path. Switch = update `raw_app_meta_data` + `session.refresh()`. |
| Abuse protection | **Minimum viable in Phase 19** | Per-IP webhook rate limit + disposable-email block. Cheap to add now, protects OAuth path from bot floods. Full protection ships Phase 24. |
| Supabase project status | **Assumed configured** | User confirmed URL, keys, JWKS reachable locally. Plans do not include Supabase project setup. |
| Structured logging | **Light inline** | `slog` on the webhook + auth handlers only. Full observability primitives deferred to whenever Phase 18 comes back. |
| Rate-limit implementation | **Chi's `httprate` middleware** | Already in the chi ecosystem, zero new dependencies. Configurable per-route. |

## Existing Code Inventory

Phase 4 left partial auth infrastructure. Take stock before adding
new files:

| File | State | Phase 19 action |
|---|---|---|
| `pkg/auth/handlers.go` | Written — `HandleGitHubLogin`, `HandleGitHubCallback`, GitLab equivalents | Mount on router in 19-01 |
| `pkg/auth/oauth.go` | Written — `NewOAuthConfig` reads env vars | Use as-is |
| `pkg/auth/state_store.go` | Written — Redis-backed CSRF state | Use as-is |
| `pkg/auth/provisioning.go` | Written — `UserProvisioner.ProvisionOAuthUser`, `CreateOrganizationForUser` | Called from 19-02 webhook handler |
| `pkg/auth/webhook.go` | Written — `SupabaseWebhookEvent` type, HMAC verification skeleton | Complete the handler in 19-01; wire provisioning in 19-02 |
| `pkg/auth/webhook_test.go` | 1 test failing on `main` (`TestGenerateOrgSlugFromEmail`) | Fix in 19-02 as part of provisioning work |
| `pkg/auth/jwt.go` | Written — `JWTValidator` fetches JWKS, `ExtractOrganizationID` reads `organization_id` claim | Extended in 19-03 with role extraction |
| `pkg/auth/middleware.go` | `TenantMiddleware` reads header (17-02 removed the broken SET LOCAL) | Rewritten in 19-03 to read from JWT |
| `pkg/api/router.go` | Webhook route mounted; OAuth routes NOT mounted | 19-01 mounts OAuth; 19-04 mounts `/api/user/*` |

## Issue Resolution Map

Phase 19 closes several deferred issues:

- **ISS-004** (org selection for multi-org users) — resolved by 19-04.
- **ISS-005** (Supabase Native OAuth webhook handler) — resolved by
  19-01 + 19-02.
- **ISS-007** (JWT-carried tenant claim + membership validation) —
  resolved by 19-03.
- Not resolved this phase: **ISS-008** (request-scoped tenant tx for
  DB-hitting endpoints) — no Phase 19 handler reads a tenant-scoped
  table directly from Go; that ISS lands with Phase 20+ handlers.

## v2 Door-Keeping

- **JWT-scoped active org.** MCP client sessions in v2 will need a way
  to declare their tenant. JWT-scoped means the MCP server can accept a
  Supabase JWT and read the org claim without a separate handshake.
  Session-scoped would have required MCP to maintain its own session
  store.
- **Hard cutover on header.** Removing `X-Organization-ID` closes a
  door: MCP tools cannot spoof tenant via a header. They must present a
  JWT. Good.
- **`raw_app_meta_data` for active org.** Supabase uses this field for
  server-controlled metadata (as opposed to `raw_user_meta_data` which
  the client can set). This is the correct home for `organization_id`
  and `organization_role` — closes the door on client-side tampering.
- **Auth Hook injection over post-signup patch.** The alternative
  (issue JWT, immediately call a "patch claims" endpoint) races the
  first authenticated request. Auth Hook runs before the JWT is issued.
  Cleaner. v2 MCP tools won't need special-case handling.

## Notes

**Rollout order and dependencies:** 19-01 → 19-02 → 19-03 → 19-04.
Each depends on the prior. In particular, 19-03's header-removal
requires the isolation tests from 17-02 to still pass under the JWT
path — that's why 19-03 both adds the JWT claim AND flips the assert
on the reframed Scenario 5 in one PR. Splitting them would leave a
broken commit in the middle.

**Fleet-doctrine reminders:**
- Every plan gets one PR; every PR gets a reviewer subagent
  auto-launched by the worker (per 2026-09-06 rule change).
- The 17-05 CI gate now enforces isolation coverage on every mutation
  endpoint added this phase. Plans include the test files explicitly.

**Reference implementation carry-over:** the OAuth handler patterns
from `pkg/auth/handlers.go` are usable as-is. The provisioning
functions in `pkg/auth/provisioning.go` are the correct primitives to
call. The webhook signature verification in `pkg/auth/webhook.go`
needs a completeness pass (verify what's stubbed vs. what's real)
during 19-01 execution — the plan flags this as a Sub-step A audit.

---

*Phase authored: 2026-09-07 (following 17-05 close-out)*
*Milestone: v1.0 MVP*
*Phase 18 (Observability) deferred — see `project_phase18_deprioritized` memory.*
