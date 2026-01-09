# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08)

**Core value:** The RAG pipeline gives accurate, relevant answers - not generic advice, not hallucinated answers. Accuracy over features.
**Current focus:** Phase 3 — Vector Database

## Current Position

Phase: 2 of 16 (Database Setup)
Plan: All plans complete (3/3)
Status: Phase complete
Last activity: 2026-01-08 — Phase 2 completed (Database Setup)

Progress: ██░░░░░░░░ 12% (2/16 phases)

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
Stopped at: Phase 2 complete (Database Setup - all 3 plans complete)
Resume file: None

## Recent Decisions

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
