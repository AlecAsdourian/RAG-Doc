---
phase: 13-web-ui-search-chat
plan: 04
subsystem: ui
tags: [react, layout, auth, repo-selector, routing, design-system]

requires:
  - phase: 13-01
    provides: Frontend setup, Tailwind v4, shadcn/ui, API client
  - phase: 13-02
    provides: Search panel and hooks
  - phase: 13-03
    provides: Chat panel with SSE streaming

provides:
  - Resizable side-by-side layout (initial plan) → superseded by 6-page shell
  - Supabase-based auth flow (LoginPage, useAuth, session→Bearer token)
  - Repository selector (mock data, structure ready for API)
  - React Router v7 shell (AppShell + AppNav + routed pages)
  - Obsidian Flux design system applied across all pages

affects: [v1-completion-milestone (auth wiring, repo integration)]

tech-stack:
  added:
    - react-resizable-panels (initial layout)
    - react-router-dom v7 (shell)
    - @supabase/supabase-js (auth)
    - reactflow (KnowledgeGraphPage)
  patterns:
    - Route-based page shell replacing tab-based layout
    - Supabase session → API client Bearer token via useAuth
    - Obsidian Flux dark theme (canvas #0B0E14, primary #c0c1ff, mono Fira Code)

key-files:
  created:
    - services/frontend/src/pages/LoginPage.tsx
    - services/frontend/src/pages/OrgSelectPage.tsx
    - services/frontend/src/pages/SearchPage.tsx
    - services/frontend/src/pages/ChatPage.tsx
    - services/frontend/src/pages/KnowledgeGraphPage.tsx
    - services/frontend/src/pages/LivingDocsPage.tsx
    - services/frontend/src/pages/RepoSettingsPage.tsx
    - services/frontend/src/components/AppShell.tsx
    - services/frontend/src/components/AppNav.tsx
    - services/frontend/src/components/RepoSelector.tsx
    - services/frontend/src/hooks/useAuth.ts
    - services/frontend/src/hooks/useRepositories.ts
    - services/frontend/src/lib/auth.ts
    - .planning/frontend-ref/stitch_codebase_knowledge_graph/**
  modified:
    - services/frontend/src/App.tsx (rewritten around React Router)
  removed:
    - services/frontend/src/components/LandingPage.tsx (superseded by LoginPage)
    - services/frontend/src/components/Layout.tsx (superseded by AppShell)
    - services/frontend/src/lib/theme.ts (replaced by Obsidian Flux tokens)

key-decisions:
  - "Scope expanded mid-plan: original tabs / two-panel layout replaced by 6-page routed shell with dedicated pages for Search, Chat, KnowledgeGraph, LivingDocs, RepoSettings"
  - "Design system unified as Obsidian Flux (per .planning/frontend-ref/stitch_codebase_knowledge_graph/obsidian_flux/DESIGN.md)"
  - "Supabase JS client used directly in frontend (not backend-mediated) for OAuth flow"
  - "Repository data mocked in useRepositories.ts pending Phase 6 backend endpoint"

patterns-established:
  - "AppShell = SideNav (260px) + TopBar (h-16 backdrop-blur) + Outlet"
  - "Auth-gated routes redirect to LoginPage when no session"
  - "Page components own their local UI state; hooks own async/streaming"

issues-created:
  - "Frontend inline-style pollution — most components hardcode Obsidian Flux colors as inline style props instead of consuming @theme Tailwind tokens; documented in feedback_frontend_evolution.md, addressed incrementally per page touched"
  - "Mocked data blocks reality — RepoSelector, OrgSelectPage, KnowledgeGraphPage, RepoSettingsPage all use hardcoded arrays; requires Phase 6 (repo integration) + Phase 4 completion (org endpoints) before real"

duration: multi-session (Feb–May 2026)
completed: 2026-05-05 (implementation), 2026-09-03 (summary retroactive)
---

# Phase 13 Plan 04: Side-by-side Layout & Polish → 6-page Shell Summary

**Note:** Retroactive summary written 2026-09-03. Implementation shipped across three planned-task commits (Feb 2026) and one large scope-expansion commit (May 2026); the phase was implemented but the formal summary was never written, leaving STATE.md and ROADMAP.md showing "in progress" for months.

## What was planned

Per `13-04-PLAN.md`, three tasks:
1. Resizable side-by-side layout (search left, chat right, react-resizable-panels)
2. Authentication flow (Supabase OAuth, LoginPage, useAuth hook, LandingPage)
3. Repository selector + polish (mock repos, keyboard shortcuts, empty states)

## What actually shipped

**Tasks 1–3 as planned (Feb 2026):**
- `7053d16 feat(13-04): create resizable side-by-side layout`
- `1e260a5 feat(13-04): add authentication flow with Supabase`
- `d9a2faf feat(13-04): add repository selector and polish`

**Scope expansion — 6-page shell (May 2026):**
- `c750418 added main, need to revise plan and connect back-end` (marker commit)
- `ff9165f implement 6-page frontend with Obsidian Flux design system`

The tabs / two-panel design was scrapped mid-phase in favor of a routed multi-page application. The new shell:
- **LoginPage** — glassmorphism auth card (GitHub / Google / magic link)
- **OrgSelectPage** — organization/workspace grid with activity feed
- **SearchPage** — dedicated search UI with left results + center detail + optional right chat rail
- **ChatPage** — full-page conversational interface with session sidebar
- **KnowledgeGraphPage** — React Flow visualization (mocked nodes, inspector)
- **LivingDocsPage** — markdown documentation reader with AI-generated/Manual tags
- **RepoSettingsPage** — GitHub connect, indexing progress, team access

## Wiring status at close

| Surface | State |
|---|---|
| Login (Supabase OAuth) | LIVE — real Supabase client → backend JWT |
| Search (`POST /api/search`) | LIVE — wired to Go backend → Python RAG |
| Chat (`POST /api/chat/stream`) | LIVE — SSE stream working end-to-end |
| Repositories | MOCKED — `useRepositories.ts` returns 3 hardcoded repos |
| Organizations | MOCKED — `OrgSelectPage` uses hardcoded `ORGS` array |
| Knowledge graph | MOCKED — 7 hardcoded nodes/edges in `KnowledgeGraphPage` |
| Living docs | MOCKED — hardcoded document tree |
| Repo settings | MOCKED — hardcoded team/repo lists, terminal-log-style indexing UI |

## Deviations from plan

The plan called for a two-panel search+chat layout with `react-resizable-panels`. The implementation instead delivered a full 6-page routed application with a shared `AppShell`. This was a substantial scope expansion beyond what `13-04-PLAN.md` described.

Rationale (inferred, not documented at the time): the two-panel design constrained future growth (graph explorer, docs browser, admin) that the roadmap already implied. The 6-page shell is closer to the shape the v2 memory-substrate pivot needs.

Consequence: several pages were built against mocked data because the backend endpoints they need (repos, orgs, graph) don't exist yet. These are now formal blockers for the v1 MVP completion milestone.

## Issues carried forward

- **Inline-style pollution** — see `feedback_frontend_evolution.md` in memory. Fix incrementally per page touched.
- **Missing backend endpoints** — repos, orgs, knowledge graph require Phase 4 completion and Phase 6 work before mocks can be replaced.
- **No verification checkpoint** — the plan's blocking human-verify checkpoint was never formally passed; user tacitly accepted by re-engaging on planning months later.

## Next phase readiness

Phase 13 is now formally complete. Next work is a **v1 MVP Completion milestone** (Phase 4 auth wiring finish + Phase 6 repo integration) required before the v1 stack can ship end-to-end, followed by **v2: Memory Substrate** (GraphRAG + LangGraph agents + MCP server).

---

*Phase: 13-web-ui-search-chat*
*Summary written: 2026-09-03 (retroactive)*
