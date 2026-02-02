# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08)

**Core value:** The RAG pipeline gives accurate, relevant answers - not generic advice, not hallucinated answers. Accuracy over features.
**Current focus:** Phase 5 — API Framework (dependency for Phase 13)

## Current Position

Phase: 5 of 16 (API Framework)
Plan: 3 of 4 in current phase (05-01, 05-02, 05-03 complete)
Status: Plan 05-03 complete, proceeding to 05-04
Last activity: 2026-02-02 — Completed 05-03-PLAN.md (Go RAG Client & Search Handler)

Progress: █████████░ 75% (12/16 phases complete, working on dependency)

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

(None yet)

### Deferred Issues

- **ISS-001:** Implement shared type definitions for cross-phase data contracts (suggested before Phase 13)
- **ISS-002:** Add cross-phase verification pattern to planning workflow (template updated)

### Blockers/Concerns

None yet.

## Session Continuity

Last session: 2026-02-02
Stopped at: Completed 05-03-PLAN.md (Go RAG Client & Search Handler) - ready for 05-04
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
