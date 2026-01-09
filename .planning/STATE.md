# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08)

**Core value:** The RAG pipeline gives accurate, relevant answers - not generic advice, not hallucinated answers. Accuracy over features.
**Current focus:** Phase 4 — Authentication System

## Current Position

Phase: 3 of 16 (Vector Database)
Plan: All plans complete (1/1)
Status: Phase complete
Last activity: 2026-01-08 — Phase 3 completed (Vector Database)

Progress: ███░░░░░░░ 18% (3/16 phases)

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

None yet.

### Blockers/Concerns

None yet.

## Session Continuity

Last session: 2026-01-08
Stopped at: Phase 3 complete (Vector Database - all 1 plans complete)
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
