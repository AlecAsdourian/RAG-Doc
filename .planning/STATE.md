# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08; product-vision reframe recorded in memory at `product_vision.md`, 2026-09-03)

**Core value:** Persistent, shared, code-aware memory substrate for parallel AI coding agents. v1.0 ships as a smart docs platform (hybrid RAG over connected code repos) — the foundation the substrate is built on.
**Current focus:** Milestone v1.0 MVP — first shippable version. In flight: Phase 19 (Auth Wiring & Org Provisioning).

## Current Position

Milestone: v1.0 MVP (9 phases: 17-25)
Phase: 19 IN PROGRESS — Auth Wiring & Org Provisioning
Plan: 4 of 4 executed (19-01 through 19-04). Phase 19 closes when 19-04 merges.
Status: Phase 17 closed 2026-09-06. Phase 18 deprioritized. 19-03 removed the last cross-tenant hole in the request path; 19-04 makes multi-org real and closes ISS-004. Next phase: 20 (Repository Integration Backend) — needs planning.
Last activity: 2026-09-08 — 19-04 multi-org endpoints opened as a PR

Progress: v1.0 MVP █░░░░░░░░ 1/9 phases complete, Phase 19 at 4/4 plans (Phase 18 Observability deferred — see project_phase18_deprioritized memory)

**Note on this section's history:** 19-01 and 19-02 both shipped without updating STATE.md, so this file sat two plans stale. Brought current in 19-03.

## Performance Metrics

**Velocity:**
- Total plans completed: — (v1.0 tracking starts here)
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| — | — | — | — |

**Recent Trend:**
- Last 5 plans: —
- Trend: —

## Accumulated Context

### Decisions (v1.0 MVP milestone)

- **Milestone version:** `v1.0 MVP` (nothing has formally shipped; v0.9 = pre-milestone initial build)
- **Sign-ups:** Public (open registration) with abuse protections in Phase 24
- **Repository integration:** GitHub App (not OAuth App) — per-installation permissions, higher rate limits, built-in webhook signing
- **Phase ordering:** Isolation → Observability → Auth → Repo → Job Infra → Ingestion → Frontend → Deploy → Launch. Cross-cutting concerns (isolation, observability) established BEFORE more endpoints ship so every new endpoint inherits them
- **Cost controls:** Per-org rate limits + LLM cost caps included in v1.0 Phase 24 (mandatory; not optional)
- **Billing:** Explicitly deferred to post-v1.0 (need traction data first)
- **v2 door-keeping:** Every design decision noted for whether it closes GraphRAG/agent/MCP doors — chunking output events (Phase 22-04), job progress payload shape, etc.
- **Reviewer session:** Live since Phase 17-01; bootstrap prompt at `.planning/fleet/reviewer-session-prompt.md`. Every code PR since has gone through it.

### Decisions (retained from v0.9)

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions still affecting current work:

- **Tailwind v4 syntax:** `@theme` directive + CSS variable syntax
- **SSE streaming:** AsyncGenerator pattern for chat responses
- **Path aliases:** `@/` import alias for frontend
- **Vector DB technology:** Qdrant (self-hosted, cosine similarity, 1536-dim)
- **LLM model choice:** GPT-4o Mini (cost efficiency)
- **Semantic cache threshold:** 0.95 cosine similarity, 1h TTL
- **Database primary keys:** UUIDs over SERIAL
- **Timestamps:** TIMESTAMPTZ
- **RLS:** Enabled + FORCED in migration 000008 (verification pending Phase 17)
- **Migration tool:** golang-migrate
- **Deduplication:** SHA256 content_hash

### Deferred Issues

- **ISS-001:** Shared type definitions for cross-phase data contracts — surface again during Phase 18 (structured logging conventions) or Phase 20 (repos API shape)
- **ISS-002:** Cross-phase verification pattern in planning workflow — template updated; apply to all v1.0 plans
- **ISS-004:** Org selection mechanism — **✅ closed 2026-09-08** across 19-03 (JWT-carried claim, header gone) and 19-04 (list + select endpoints). Frontend picker is Phase 23 work against `docs/auth-frontend-contract.md`.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today (nothing removes memberships), but **whatever ships membership removal must rewrite the claim** — short token TTLs do not bound this. See ISSUES.md.
- **ISS-005:** Supabase Native OAuth webhook handler — **scheduled: Phase 19-01**
- **ISS-006:** Test database connectivity — **✅ closed 2026-09-05** in Phase 17-01 via testcontainers-go harness (`pkg/testing/isolation`); see ISSUES.md
- **ISS-007:** JWT-carried tenant claim — **✅ closed 2026-09-08** in Phase 19-03. Tenant identity now comes only from the Supabase-signed `app_metadata.organization_id` claim; the `X-Organization-ID` path is deleted, including from CORS. Closed without the per-request membership re-check the original filing called for — reasoning in ISSUES.md and 19-03-SUMMARY.md.
- **ISS-008:** Request-scoped tenant transaction for DB-hitting endpoints — **filed 2026-09-06** during 17-02; must resolve before any Phase 20+ handler reads a tenant-scoped table directly from Go. See ISSUES.md.
- **ISS-009:** `pkg/vectordb` build — **✅ closed 2026-09-08.** Qdrant client bumped v1.7.0 → v1.19.2; the pin had always predated the API the package was written against. Fixing it surfaced a panicking unit test that had never been able to run.
- **ISS-010:** Isolation harness parallel race — **✅ closed 2026-09-08.** Role setup now holds a `pg_advisory_xact_lock`; verified over 5 cold-container parallel runs.
- **ISS-011:** OAuth callback routes — **✅ closed 2026-09-08** by unmounting them (not repairing). Repairing the 500 alone would have converted a loud failure into a silently claim-less account. Handlers kept as reference; deleting them is a planner/user call.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today; **whatever ships membership removal must rewrite the claim.** See ISSUES.md.
- **Frontend inline-style pollution** — ongoing rule, cleaned per component touched
- **Mocked repos/orgs/graph in frontend** — **replaced in Phase 23**

### Blockers/Concerns

- **User action needed:** GitHub App registration during Phase 20-01 (creating the App in GitHub UI is a manual step — planner will hand user a runbook when we get there)
- **User action needed:** Deployment target choice in Phase 24-01 (recommend I bring back options + trade-offs at that point)
- **User action needed:** Observability stack choice in Phase 18 (self-hosted vs SaaS — cost implications)

## Session Continuity

Last session: 2026-09-08
Stopped at: 19-04 executed and opened as a PR; reviewer launched
Resume file: None

Next command suggested: plan Phase 20 (Repository Integration Backend) once 19-04 merges. Phase 19 is then complete. Note the ROADMAP flags a **user action** for 20-01: registering the GitHub App is a manual step in GitHub's UI, so the planner should hand over a runbook rather than assume it can be scripted.

**Environment note (new in 19-03):** local dev now has two separate Postgres instances — Supabase's (auth only) and docker-compose's on port 5434 (all application tables). They are NOT the same database, which is why 19-03 could not use a Supabase Auth Hook. Runbook: `docs/local-development.md`. The Go backend does not read `.env`; only docker-compose does.

**CI now builds and tests the Go code** (`.github/workflows/backend-ci.yml`, added 2026-09-08). `go build ./...`, `go vet ./...`, and `go test -p 1 ./...` all gate every PR. Before this, nothing in CI had ever invoked a compiler — the isolation check runs a Python script over the diff — which is how `pkg/vectordb` stayed uncompilable from Phase 3 to Phase 19.

The workflow supplies Postgres and Redis service containers for `pkg/auth`'s pre-17-01 helpers. Migrating those onto the testcontainers harness would let both be dropped; tracked in `19-02-SUMMARY.md`.

One step is **advisory, not gating**: `go test -race` on the concurrency-sensitive packages. It needs cgo and could not be executed on the authoring machine, so it is marked `continue-on-error` rather than shipped as an unverified gate. Promote it once it has run green a few times.

**Fleet handoff notes for the worker session:**
- Read the phase `-CONTEXT.md` first for vision context
- Plans lock design decisions inline — don't re-litigate them without cause
- Every commit follows `feedback_commit_convention.md`: one sentence, conventional prefix, no attribution trailers
- Every PR references its plan file in the description
- All work on a feature branch → PR → reviewer session comments → merge after approval
- When a plan's stated approach turns out to be impossible against the real system, revise the PLAN file with a REVISION NOTICE recording what was actually verified, then execute the revised version — do not silently improvise (pattern established in 19-03)

**Fleet handoff notes for the reviewer session:**
- Bootstrap prompt: `.planning/fleet/reviewer-session-prompt.md`
- Isolation-test-coverage hard rule is live as of 17-05

### Roadmap Evolution

- 2026-09-03 — Milestone v1.0 MVP created: 9 phases (17-25); roadmap restructured with milestone groupings; v0.9 disposition table records disposition of original phases 1-16 (some shipped, some superseded, some deferred to v2)
- 2026-09-03 — Product vision reframed: from "smart documentation platform" to "persistent shared memory substrate for parallel AI coding agents"; v1.0 remains RAG-over-code as the foundation, v2 adds GraphRAG + agents + MCP
- 2026-09-03 — Phase 17 planned: 3 sub-plans in ROADMAP expanded to 5 executable plans (17-01 harness, 17-02 endpoint verification, 17-03 DB trigger migration, 17-04 Python harness + workers audit, 17-05 CI gate + docs); design decisions locked inline (testcontainers, PL/pgSQL BEFORE trigger, regex-on-diff scanner)
