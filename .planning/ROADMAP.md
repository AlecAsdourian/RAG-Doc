# Roadmap: RAG-Doc — Memory Substrate for Vibecoding Fleets

## Overview

Build a persistent, shared, code-aware memory substrate for parallel AI coding agents, distributed via MCP. The v1 MVP ships as a smart documentation platform (hybrid RAG over connected code repos) — the working prototype developers use today and the foundation the substrate is built on. v2 layers a code knowledge graph, LangGraph-based agentic retrieval, and an MCP server that any AI coding session can plug into. Later milestones extend to shared session memory, PR-driven memory extraction, and code-anchored notes.

## Domain Expertise

None

## Milestones

- 🟡 **v0.9 Initial Build** — Phases 1-16 (foundation shipped in prototype form; several phases deferred or superseded — see disposition table below)
- 🚧 **v1.0 MVP** — Phases 17-25 (in progress — the first shippable version)
- 📋 **v2.0 Memory Substrate** — Phases planned separately once v1.0 ships (GraphRAG + LangGraph agents + MCP server)

## v0.9 Initial Build — Disposition

The original 1-16 phase plan was pre-milestone monolithic planning. Some phases shipped as working code, some remain unfinished, and some have been reframed for later milestones. This table records the final disposition.

| Phase | Original Goal | Disposition |
|---|---|---|
| 1. Project Setup | Init structure, tooling, dev env | ✅ Shipped 2026-01-08 |
| 2. Database Setup | Postgres schema, migrations, multi-tenant | ✅ Shipped 2026-01-08 (8 migrations, RLS in place) |
| 3. Vector Database | Qdrant setup, Go client | ✅ Shipped 2026-01-08 |
| 4. Authentication System | User auth + org access control | 🟡 Partial — JWT/JWKS/handlers written but OAuth handlers not wired to router; **completed by v1.0 Phase 19** |
| 5. API Framework | REST foundation + middleware | 🟢 De facto complete (chi router, middleware chain, error helpers all in `pkg/api/`); **hardened as part of v1.0 Phase 18** |
| 6. Repository Integration | GitHub/GitLab OAuth, sync, webhooks | ⛔ Not started; **implemented by v1.0 Phases 20-22** |
| 7. Code Parsing & Chunking | Tree-sitter, semantic chunks | ✅ Shipped in workers (Python/Go/TS supported) |
| 8. Embedding Pipeline | OpenAI ada-002, batching, storage | ✅ Shipped |
| 9. AI Documentation Agent | Autonomous doc generation | 📦 **Deferred to v2.0 Memory Substrate** — agentic feature |
| 10. Manual Documentation System | Developer-authored markdown workflow | 📦 **Deferred to v2.0 Memory Substrate** — memory-storage concern |
| 11. RAG Query Engine | Hybrid retrieval, RRF fusion, boosting | ✅ Shipped 2026-01-10 (5 plans) |
| 12. LLM Answer Generation | GPT-4o Mini + semantic cache | ✅ Shipped 2026-01-12 |
| 13. Web UI - Search & Chat | React frontend | ✅ Shipped 2026-05-05 (6-page shell) |
| 14. AI Context Export | Markdown/JSON/YAML exports for AI agents | ♻️ **Superseded by v2.0 MCP server** |
| 15. Feedback & Analytics | Quality tracking + metrics | 🚫 **Deferred to post-v1.0** — not shipping-blocker |
| 16. Multi-tenant & Deployment | Isolation + prod deploy | ♻️ **Superseded by v1.0 Phases 17, 24, 25** (broken into properly-sized phases) |

## v1.0 MVP — Phase Details

**Milestone Goal:** Deliver the first shippable version of the platform: users sign in, install the GitHub App on their org, connect a repo, wait for it to be indexed, and get accurate cited answers to code questions — with production-grade multi-tenant isolation, observability, cost controls, and deployment. Every design decision keeps v2 doors open.

**Cross-cutting rules for every phase:**
- All work ships as PRs. Every PR references its `<phase>-<plan>-PLAN.md`. Reviewer session reviews before merge. See `.planning/fleet/reviewer-session-prompt.md`.
- Commit convention per `feedback_commit_convention.md`: one sentence, conventional prefix, no attribution trailers.
- Every mutation endpoint gets a tenant-isolation integration test (pattern established in Phase 17).
- Every user-visible endpoint emits structured logs + traces (pattern established in Phase 18).
- Frontend components touched during a phase have their inline styles converted to `@theme` Tailwind classes.
- Design decisions that could close v2 doors (GraphRAG, agents, MCP) get an explicit **v2 breadcrumb** note in the plan.

### Phase 17: Multi-tenant Isolation Foundation

**Goal:** Prove RLS works end-to-end, codify tenant isolation as a testable pattern, and build the integration-test harness that every future endpoint uses.
**Depends on:** v0.9 foundation (migrations 000001-000008 exist)
**Research:** Unlikely (patterns exist; needs verification tooling and process)
**Plans:** 5 (planned 2026-09-03)

Plans:
- [ ] 17-01: Go test harness foundation — testcontainers-go + migration runner + `WithTwoOrgs` + `AssertNoCrossTenantLeak` primitives; resolves ISS-006
- [ ] 17-02: Verify Go endpoints — isolation tests for `/api/search` and `/api/chat/stream`; audit RAG client tenant propagation; fix any leaks
- [ ] 17-03: DB-level assertion migration — migration 000009 adds PL/pgSQL trigger on 9 tenant-scoped tables that raises when `app.current_tenant` not set
- [ ] 17-04: Python isolation harness + workers audit — pytest testcontainers mirror + `require_tenant` primitive; audit `postgres_writer.py` and `query_engine.py`; verify cross-language trigger firing
- [ ] 17-05: CI gate + documentation — `scripts/ci/check-isolation-tests.py` scanner, GitHub Action, `docs/isolation.md`, reviewer prompt updated

### Phase 18: Observability Foundation

**Goal:** Set the logging/tracing/error/health pattern before more endpoints ship. Retrofit `/api/search` and `/api/chat/stream` as reference implementations. Any endpoint added after this phase inherits observability by default.
**Depends on:** Phase 17
**Research:** Likely (tool choice matters)
**Research topics:** OpenTelemetry exporter target (self-hosted Jaeger/Tempo vs SaaS like Honeycomb/Grafana Cloud), error-tracking service (Sentry vs GlitchTip vs self-hosted), log aggregation destination, PII scrubbing strategy for LLM prompts/responses, cost estimation for chosen stack
**Plans:** TBD (target 4 plans)

Plans:
- [ ] 18-01: Structured logging — Go `slog` with JSON handler + context keys (request_id, tenant_id, user_id); Python `structlog` mirror; conventions doc
- [ ] 18-02: OpenTelemetry tracing — Go + Python SDK, trace context propagation across Go→Python HTTP calls and worker jobs, exporter to chosen backend
- [ ] 18-03: Error tracking + health/readiness — Sentry (or chosen alternative) SDK integration with PII scrubber; `/healthz` liveness + `/readyz` readiness endpoints on Go backend and Python API
- [ ] 18-04: Reference instrumentation — retrofit `/api/search` and `/api/chat/stream` (both Go and Python sides) as the canonical example of an observed endpoint

### Phase 19: Auth Wiring & Org Provisioning

**Goal:** Finish Phase 4 — first-time user can sign up, get an org auto-provisioned, receive a JWT with org context, and hit tenant-scoped APIs. Public sign-ups (per user decision).
**Depends on:** Phase 17 (isolation), Phase 18 (observability)
**Research:** Likely (auth architecture)
**Research topics:** Supabase auth hooks / webhooks (`user.created` event signature verification), JWT custom claims strategy (Supabase Auth Hooks vs post-signup patch), multi-org selection UX pattern (session-scoped vs URL-scoped), abuse patterns for public sign-ups (email domain rules, captcha, rate limiting)
**Plans:** TBD (target 4 plans)

Plans:
- [x] 19-01: Wire OAuth handlers + Supabase webhook — mount handlers in `pkg/api/router.go`, implement `POST /webhooks/supabase` receiver with HMAC signature verification (ISS-005)
- [x] 19-02: Org auto-provisioning — on `user.created` webhook, call `ProvisionOAuthUser` + `CreateOrganizationForUser`; idempotent so replays are safe
- [x] 19-03: JWT custom claim for org — **backend writes `raw_app_meta_data` via the Supabase admin API** (NOT an Auth Hook — see below); middleware reads `app_metadata.organization_id` from the verified JWT; `X-Organization-ID` header deleted. Closes ISS-007 and the security half of ISS-004.
- [x] 19-04: Multi-org handling — `GET /api/user/organizations` and `POST /api/user/select-organization`, both user-scoped (mounted outside `TenantMiddleware`, so a user with no organization claim can still reach them). Frontend `OrgSelectPage` implements `docs/auth-frontend-contract.md` in Phase 23. Closes ISS-004; files ISS-012.

**19-03 course correction.** This section originally said "Supabase Auth Hook adds `organization_id`". That is impossible in this architecture: the hook is a Postgres function running inside Supabase's database, and `organization_memberships` lives in a *different* Postgres instance (docker-compose, port 5434), so the hook cannot query the table it would need. Verified during 19-03; full reasoning in `19-03-SUMMARY.md`.

Consequences, now settled by 19-04:
1. The claim is written once and never recomputed, so changing a user's organization requires an explicit rewrite plus a token refresh. `select-organization` does both — and `refreshSession()` was verified against the live project to genuinely re-read the metadata.
2. There is no automatic repair if a write fails (do not rely on webhook retry). `cmd/backfill-org-claims` is the repair path.
3. **Still open, inherited by Phase 20+:** a *revoked* membership does not revoke the claim, because nothing recomputes it. Whatever ships membership removal must rewrite the affected user's claim — short token TTLs do not bound this exposure. Filed as ISS-012.

### Phase 20: Repository Integration Backend

**Goal:** Backend surface for connecting, listing, and deleting repositories via a GitHub App (per user decision — App gives per-installation permissions, built-in webhook signing, higher rate limits).
**Depends on:** Phase 19 (auth), Phase 17 (isolation pattern)
**Research:** Likely (external integration)
**Research topics:** GitHub App configuration best practices, installation flow UX, webhook payload patterns and required event types (push, installation, installation_repositories), signature verification, handling large repos (>1GB, submodules, LFS), GitHub Enterprise support consideration
**Plans:** 5 (planned 2026-09-08 — see `20-CONTEXT.md`)

Plans:
- [ ] 20-01: **Request-scoped tenant transaction (ISS-008)** — a Go handler primitive that sets `app.current_tenant` from the verified claim, plus tests proving that bypassing it fails detectably
- [ ] 20-02: GitHub App contract verification + schema — verify against the live App FIRST, then migration adding `github_installations` (RLS + tenant trigger) and the new `repositories` columns; GitHub client with App JWT and installation tokens
- [ ] 20-03: Repositories CRUD API — `POST/GET/GET :id/DELETE /api/repositories`; tenant-scoped, cursor-paginated, isolation-tested
- [ ] 20-04: Installation flow — `GET /api/github/install`, `GET /api/github/callback` (CSRF-stated, organization bound to the state token), list-installation-repositories; deletes the dead GitLab handlers
- [ ] 20-05: Webhook receiver — `POST /webhooks/github`, HMAC-SHA256 over the raw body, idempotent by delivery id, handling push / installation / installation_repositories as recorded intent (the queue is Phase 21)

**Planning notes (2026-09-08).** Five plans, not four, and the order changed. Three things drove it:

1. **ISS-008 is blocking, and it is now verified rather than anticipated.** `repositories` is RLS-scoped; the only Go handler touching the database today reads `users`/`organizations`/`organization_memberships`, none of which have RLS. So no Go handler has ever read an RLS-scoped table, and `GET /api/repositories` is the first. Without the primitive it returns zero rows silently. Hence a new 20-01.
2. **Contract verification moved ahead of schema design**, per the user's decision to register the App first. This is the 19-03 pattern: that phase's plan was built on a Supabase Auth Hook that verification proved architecturally impossible, and the same class of mistake against GitHub would cost more, not less.
3. **Two items in the original sketch were wrong.** `webhook_secret` is App-level configuration, not a per-repository column. `github_installation_id` on `repositories` denormalizes an organization-level fact onto every repository row; it gets its own table with a UNIQUE constraint that *is* the one-installation-one-organization tenancy boundary.

Locked decisions: GitHub only (GitLab handlers deleted); one installation serves exactly one organization; size/visibility recorded but nothing rejected for being large — limits belong in Phase 22 where cloning happens.

**User action required before 20-02:** register the GitHub App by following `docs/github-app-setup.md` (~20 minutes, needs an ngrok tunnel). Nothing after 20-01 can proceed without it.

### Phase 21: Ingestion Job Infrastructure

**Goal:** The bones — durable queue, job state machine, retries, worker registration. No actual repo cloning yet (that's Phase 22). Redis Streams is the tentative choice (already have Redis, at-least-once delivery, consumer groups) but validated in research.
**Depends on:** Phase 20 (repositories exist to enqueue for)
**Research:** Likely (infra decision)
**Research topics:** Job queue trade-offs (Redis Streams vs RQ vs Arq vs Celery vs Temporal vs custom), at-least-once vs exactly-once, dead-letter patterns, backpressure, observability of async jobs, orchestration for multi-step ingestion (clone → parse → embed → store)
**Plans:** TBD (target 3 plans)

Plans:
- [ ] 21-01: Queue infrastructure — chosen queue technology set up, producer library in Go backend, consumer library in Python worker, health-checked
- [ ] 21-02: Job state + retry — `ingestion_jobs` table (or extend `ingestion_runs` — decided during planning), state machine `queued → running → completed | failed | dead`, retry policy with exponential backoff, dead-letter for permanent failures
- [ ] 21-03: Job observability — every job emits structured logs + spans, admin endpoint `GET /api/admin/jobs/:id` for debugging; integration test covering enqueue → consume → status transitions → retry → dead-letter

### Phase 22: Repository Clone → Ingestion Orchestration

**Goal:** Actual repo clone into the existing chunking/embedding/storage pipeline, with real-time progress reporting to the frontend. Incremental updates on push events.
**Depends on:** Phase 21 (job infra)
**Research:** Unlikely (ingestion pipeline exists; clone strategy is standard)
**Plans:** TBD (target 4 plans)

Plans:
- [ ] 22-01: Clone worker — shallow clone (`--depth 1`) into scratch dir, size cap enforcement, respect `.gitignore`, cleanup on completion/failure; secrets isolated (no credentials in logs)
- [ ] 22-02: Full ingestion job — orchestrate clone → language detection → tree-sitter chunking → embedding batch → Postgres + Qdrant writers; wraps existing `IngestionPipeline` for a full repo
- [ ] 22-03: Incremental ingestion — on push event, diff previous commit vs new HEAD, re-index only changed files by SHA; delete removed files from indexes
- [ ] 22-04: Progress transport — job publishes progress events (files_parsed, chunks_embedded, current_file) to Redis pub/sub per job_id; SSE endpoint `GET /api/repositories/:id/sync-progress` streams to frontend. **v2 breadcrumb:** define chunk-level event payload so future graph-construction worker can subscribe to the same stream without pipeline changes

### Phase 23: Frontend Wiring & Onboarding UX

**Goal:** Replace every mocked frontend surface with real data. Ship a first-run onboarding flow that walks a new user from "signed up" to "first successful query." Fix inline-style debt on every component touched.
**Depends on:** Phases 19, 20, 22
**Research:** Unlikely (internal wiring against defined APIs)
**Plans:** TBD (target 5 plans)

Plans:
- [ ] 23-01: Live data for repos + orgs — `useRepositories` and `OrgSelectPage` and `RepoSettingsPage` wired to real APIs; error/loading/empty states; inline-style→Tailwind for every touched component
- [ ] 23-02: GitHub App install flow UI — post-org-creation prompt: "Install our GitHub App on your organization"; deep-link into GitHub install URL; post-install callback lands user back on repo-connect UI
- [ ] 23-03: Repo connect + progress UI — "Connect a repository" flow, live indexing progress via SSE from Phase 22, error handling for stuck jobs; `RepoSettingsPage` shows real sync history
- [ ] 23-04: First-run onboarding — new user detection (no repos yet), guided flow (install App → connect repo → wait → guided first query with a sample question relevant to their repo); dismissible with "I'll do it later"
- [ ] 23-05: Auth session polish — session expiry handling, sign-out flow, org switcher in top bar, unauthenticated redirect preserves intended destination

### Phase 24: Production Deployment & Cost Controls

**Goal:** Actually ship. Deploy target chosen and provisioned (staging + prod). CI/CD, secrets, TLS, backups. Per-org rate limits and cost caps so one abusive user can't drain the LLM budget.
**Depends on:** Phase 18 (observability), Phase 23 (wired app)
**Research:** Likely (deployment architecture)
**Research topics:** Deployment target trade-offs (Fly.io / Railway / Render / Cloud Run / EKS / self-hosted), secrets management (Doppler / cloud-native / Vault), Postgres backup + PITR strategy per host, Qdrant persistence and snapshot strategy in prod, rate-limit implementation (middleware vs API gateway), cost tracking for OpenAI usage per tenant
**Plans:** TBD (target 5 plans)

Plans:
- [ ] 24-01: Deployment target chosen + staging deployed — decision recorded, infra-as-code where practical (Fly.toml / Railway config / Terraform), staging fully functional
- [ ] 24-02: CI/CD pipeline — GitHub Actions: lint (golangci-lint, ruff, biome) → unit + integration tests → build → deploy to staging on merge, tag-triggered prod deploy
- [ ] 24-03: Secrets, TLS, custom domain — no env files in prod, secrets manager integration, TLS via provider (Let's Encrypt or provider-managed), custom domain configured
- [ ] 24-04: Backups + disaster recovery — Postgres PITR (per-host mechanism), Qdrant snapshot schedule + off-site copy, tested restore runbook, backup monitoring
- [ ] 24-05: Rate limits + cost controls — per-org QPS limit on `/api/search` and `/api/chat/stream` (Redis-based token bucket), `usage_records` table tracking LLM/embedding cost per org per day, monthly per-org budget cap with hard cutoff + admin alert, public-signup abuse protection (captcha on signup, email domain rules if abuse observed)

### Phase 25: Launch Readiness & Ops

**Goal:** Everything you need for the day after launch. Public landing page, security review pass, incident runbook, self-hosting docs, user-facing docs.
**Depends on:** Phase 24
**Research:** Unlikely (product/ops work)
**Plans:** TBD (target 4 plans)

Plans:
- [ ] 25-01: Public landing page — marketing site (can be a new route on the app or a separate marketing site), feature summary, sign-up CTA, screenshots or demo, product positioning aligned with memory-substrate north star
- [ ] 25-02: Security review pass — run `security-review` skill against the entire v1.0 surface, address findings, third-party dependency audit (`go mod tidy`, `pip-audit`, `npm audit`), secret-scanning check on git history
- [ ] 25-03: Incident runbook + ops docs — what to do when: LLM API down, Qdrant down, DB slow / running out of connections, cost cap exceeded, GitHub App deauthorized, stuck ingestion jobs; on-call handoff notes even if it's just you
- [ ] 25-04: User-facing + self-hosting docs — getting started guide, FAQ, limits, `docker-compose up` self-hosting doc that actually works standalone, API reference for the shipped endpoints

## Progress

| Phase | Milestone | Plans | Status | Completed |
|-------|-----------|-------|--------|-----------|
| 1. Project Setup | v0.9 | 4/4 | Complete | 2026-01-08 |
| 2. Database Setup | v0.9 | 3/3 | Complete | 2026-01-08 |
| 3. Vector Database | v0.9 | 1/1 | Complete | 2026-01-08 |
| 4. Authentication System | v0.9 | — | Partial — finished in Phase 19 | - |
| 5. API Framework | v0.9 | — | De facto complete | - |
| 6. Repository Integration | v0.9 | — | Implemented in Phases 20-22 | - |
| 7. Code Parsing & Chunking | v0.9 | 5/5 | Complete | 2026-01-09 |
| 8. Embedding Pipeline | v0.9 | — | Complete (shipped in workers) | - |
| 9. AI Documentation Agent | v0.9 | — | Deferred to v2.0 | - |
| 10. Manual Documentation System | v0.9 | — | Deferred to v2.0 | - |
| 11. RAG Query Engine | v0.9 | 5/5 | Complete | 2026-01-10 |
| 12. LLM Answer Generation | v0.9 | 1/1 | Complete | 2026-01-12 |
| 13. Web UI - Search & Chat | v0.9 | 4/4 | Complete | 2026-05-05 |
| 14. AI Context Export | v0.9 | — | Superseded by v2.0 MCP server | - |
| 15. Feedback & Analytics | v0.9 | — | Deferred to post-v1.0 | - |
| 16. Multi-tenant & Deployment | v0.9 | — | Superseded by Phases 17, 24, 25 | - |
| 17. Multi-tenant Isolation Foundation | v1.0 | 0/5 | Not started | - |
| 18. Observability Foundation | v1.0 | 0/4 | Not started | - |
| 19. Auth Wiring & Org Provisioning | v1.0 | 0/4 | Not started | - |
| 20. Repository Integration Backend | v1.0 | 0/4 | Not started | - |
| 21. Ingestion Job Infrastructure | v1.0 | 0/3 | Not started | - |
| 22. Repository Clone → Ingestion Orchestration | v1.0 | 0/4 | Not started | - |
| 23. Frontend Wiring & Onboarding UX | v1.0 | 0/5 | Not started | - |
| 24. Production Deployment & Cost Controls | v1.0 | 0/5 | Not started | - |
| 25. Launch Readiness & Ops | v1.0 | 0/4 | Not started | - |
