# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08; product-vision reframe recorded in memory at `product_vision.md`, 2026-09-03)

**Core value:** Persistent, shared, code-aware memory substrate for parallel AI coding agents. v1.0 ships as a smart docs platform (hybrid RAG over connected code repos) — the foundation the substrate is built on.
**Current focus:** Milestone v1.0 MVP — first shippable version. First phase up: Phase 17 (Multi-tenant Isolation Foundation).

## Current Position

Milestone: v1.0 MVP (9 phases: 17-25)
Phase: 17 of 25 — Multi-tenant Isolation Foundation
Plan: 5 plans created (17-01 through 17-05), 0 executed
Status: Ready to execute
Last activity: 2026-09-03 — Phase 17 CONTEXT and all 5 PLAN.md files committed (PRs #3 and #4 merged to main)

Progress: v1.0 MVP ░░░░░░░░░ 0/9 phases (17-01 next up for execution)

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
- **Reviewer session:** Not yet spun up; bootstrap prompt at `.planning/fleet/reviewer-session-prompt.md` will be pasted when the first code PR is about to open

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
- **ISS-004:** Org selection mechanism — **scheduled: Phase 19-03 and 19-04**
- **ISS-005:** Supabase Native OAuth webhook handler — **scheduled: Phase 19-01**
- **ISS-006:** Test database connectivity — **scheduled: Phase 17-01** (built into the isolation test harness)
- **Frontend inline-style pollution** — ongoing rule, cleaned per component touched
- **Mocked repos/orgs/graph in frontend** — **replaced in Phase 23**

### Blockers/Concerns

- **User action needed:** GitHub App registration during Phase 20-01 (creating the App in GitHub UI is a manual step — planner will hand user a runbook when we get there)
- **User action needed:** Deployment target choice in Phase 24-01 (recommend I bring back options + trade-offs at that point)
- **User action needed:** Observability stack choice in Phase 18 (self-hosted vs SaaS — cost implications)

## Session Continuity

Last session: 2026-09-03
Stopped at: Phase 17 fully planned (5 PLAN.md files); ready for worker session to execute 17-01
Resume file: None

Next command suggested: `/gsd:execute-plan .planning/phases/17-tenant-isolation-foundation/17-01-PLAN.md` — recommend clearing context first (`/clear`), then spinning up a fresh worker session for execution. Reviewer session should be spun up before 17-01's PR opens (bootstrap prompt at `.planning/fleet/reviewer-session-prompt.md`).

**Fleet handoff notes for the worker session:**
- Read `.planning/phases/17-tenant-isolation-foundation/17-CONTEXT.md` first for vision context
- The plans lock design decisions inline (testcontainers, PL/pgSQL trigger, regex-on-diff scanner) — don't re-litigate them without cause
- Every commit follows `feedback_commit_convention.md`: one sentence, conventional prefix, no attribution trailers
- Every PR references its plan file in the description
- All work on a feature branch → PR → reviewer session comments → planner merges after approval

**Fleet handoff notes for the reviewer session:**
- Bootstrap prompt: `.planning/fleet/reviewer-session-prompt.md`
- After 17-05 ships, the reviewer prompt gets updated with the isolation-test-coverage hard rule (a task inside 17-05 itself)

### Roadmap Evolution

- 2026-09-03 — Milestone v1.0 MVP created: 9 phases (17-25); roadmap restructured with milestone groupings; v0.9 disposition table records disposition of original phases 1-16 (some shipped, some superseded, some deferred to v2)
- 2026-09-03 — Product vision reframed: from "smart documentation platform" to "persistent shared memory substrate for parallel AI coding agents"; v1.0 remains RAG-over-code as the foundation, v2 adds GraphRAG + agents + MCP
- 2026-09-03 — Phase 17 planned: 3 sub-plans in ROADMAP expanded to 5 executable plans (17-01 harness, 17-02 endpoint verification, 17-03 DB trigger migration, 17-04 Python harness + workers audit, 17-05 CI gate + docs); design decisions locked inline (testcontainers, PL/pgSQL BEFORE trigger, regex-on-diff scanner)
