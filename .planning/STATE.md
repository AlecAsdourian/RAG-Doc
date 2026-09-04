# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08)

**Core value:** Persistent, shared, code-aware memory substrate for parallel AI coding agents (v2 reframe). v1 remains the RAG-powered smart docs platform that ships first.
**Current focus:** Planning v1 MVP Completion milestone (Phase 4 auth wiring finish + Phase 6 repo integration), then v2 Memory Substrate milestone.

## Current Position

Phase 13 of 16: Complete (all 4 plans shipped; retroactive summary written 2026-09-03)
Milestone state: v1 build in progress — RAG + UI complete, auth wiring + repo integration still open
Next milestone: v1 MVP Completion, then v2 Memory Substrate (GraphRAG + LangGraph agents + MCP server)
Last activity: 2026-09-03 — Retroactive Phase 13-04 summary; pushed local master → GitHub main; product reframe to memory substrate

Progress: v1 build ~85% done (RAG pipeline complete, UI shell complete, missing: OAuth wiring, repo ingestion, admin endpoints)

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: —
- Total execution time: 0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| — | — | — | — |

**Recent Trend:**
- Last 5 plans: —
- Trend: —

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- **Tailwind v4 syntax:** Used @theme directive and CSS variable syntax for future-proof styling
- **SSE streaming:** AsyncGenerator pattern for chat responses
- **Path aliases:** @/ import alias for clean imports

### Deferred Issues

- **ISS-001:** Implement shared type definitions for cross-phase data contracts (suggested before Phase 13)
- **ISS-002:** Add cross-phase verification pattern to planning workflow (template updated)
- **ISS-004:** Organization selection mechanism for multi-org users (v1 MVP Completion milestone)
- **ISS-005:** Supabase Native OAuth webhook handler (v1 MVP Completion milestone)
- **ISS-006:** Test database connectivity configuration (LOW)
- **Phase 13 carryovers:** frontend inline-style pollution; mocked repos/orgs/graph awaiting backend endpoints

### Blockers/Concerns

- v1 MVP is not shippable end-to-end until Phase 4 OAuth handlers are wired into the router and Phase 6 (repo integration: GitHub OAuth app, clone, webhook sync) is built.

## Session Continuity

Last session: 2026-09-03
Stopped at: Phase 13 formally closed, awaiting user go-ahead on v1 MVP Completion milestone scope
Resume file: None

## Recent Decisions

- **Vector DB technology:** Qdrant (self-hosted, zero cost, no vendor lock-in)
- **Embedding dimension:** 1536 (OpenAI ada-002 standard)
- **Distance metric:** Cosine similarity for semantic search
- **Metadata schema:** chunk_id, repository_id, file_path, language
- **Indexed fields:** repository_id and language for efficient filtering
- **Client pattern:** Dependency injection with Config struct (testable)
- **Database primary keys:** UUIDs over SERIAL for distributed-systems readiness
- **Timestamp type:** TIMESTAMPTZ to always store timezone information
- **Delete behavior:** CASCADE deletes for multi-tenant hierarchy (org → projects → repos)
- **Migration tool:** golang-migrate as the standard for Go projects
- **Schema ownership:** Go backend owns all migrations and schema versioning
- **Commit SHA storage:** VARCHAR(40) for exact Git SHA length
- **Deduplication strategy:** SHA256 content_hash enables detection across files/commits
- **Query optimization:** Denormalized repository_id on chunks avoids JOINs
- **Feedback scope:** Feedback on retrievals (not queries) - users rate specific chunks
- **Vector DB reference:** query_embedding_id as VARCHAR, not FK (separate system)
- **FTS configuration:** PostgreSQL GIN indexes with 'english' text search for stemming support
- **FTS scoring:** ts_rank_cd (cover density) over ts_rank for proximity-aware relevance
- **Breadcrumb storage:** Dedicated TEXT column for FTS indexing on qualified names
- **LLM model choice:** GPT-4o Mini for cost efficiency ($0.15/$0.60 per MTok) - 10x cheaper than GPT-4
- **Semantic cache threshold:** 0.95 cosine similarity for 40% hit rate with 97% answer quality
- **Cache TTL:** 1-hour expiration balances freshness vs cost savings
- **RAG temperature:** 0.0 for deterministic factual answers
- **Context window safety margin:** 100K token limit for 128K window to prevent overflow
- **Integration contract:** QueryEngine must return full 'content' field, not just 'content_preview'
