---
phase: 11-rag-query-engine
plan: 01
subsystem: retrieval
tags: [postgresql, fts, full-text-search, gin-index, psycopg2]
requires: [2-02, 7-02]
provides: [fts-infrastructure, lexical-retrieval]
affects: [11-02, 11-03]
tech-stack:
  added: []
  patterns: [fts-retrieval, hybrid-search-foundation]
key-files:
  created:
    - services/backend/migrations/000006_add_fts_index.up.sql
    - services/backend/migrations/000006_add_fts_index.down.sql
    - services/workers/workers/retrieval/__init__.py
    - services/workers/workers/retrieval/fts_retriever.py
  modified: []
key-decisions:
  - decision: Added breadcrumb column to chunks table in this migration
    rationale: Phase 7 implemented breadcrumb generation in code but didn't add database column; required for FTS indexing on qualified names
  - decision: GIN index on both content and breadcrumb fields
    rationale: Enables fast full-text search on code content and qualified names (e.g., "AuthService.validateToken")
  - decision: English text search configuration
    rationale: Standard for code search; provides stemming and stop word handling
  - decision: ts_rank_cd for scoring
    rationale: Cover density ranking considers proximity of query terms, better for code search than simple ts_rank
issues-created: []
duration: 15 min
completed: 2026-01-09
---

# Phase 11 Plan 1: FTS Infrastructure & Retrieval Summary

**PostgreSQL full-text search infrastructure with GIN indexes and Python retrieval module.**

## Accomplishments

- **GIN indexes on chunks table** for fast full-text search (10-100x faster than sequential scans)
- **Dual-field FTS** searches both code content and breadcrumb qualified names
- **FTSRetriever Python module** with ts_rank_cd scoring for relevance ranking
- **Automatic run selection** defaults to latest completed ingestion run when run_id not provided
- **SQL injection protection** through parameterized queries
- **Breadcrumb column added** to chunks table (required for qualified name FTS)

## Files Created/Modified

- `services/backend/migrations/000006_add_fts_index.up.sql` - GIN index creation and breadcrumb column addition
- `services/backend/migrations/000006_add_fts_index.down.sql` - Rollback migration for indexes and column
- `services/workers/workers/retrieval/__init__.py` - Retrieval package exports
- `services/workers/workers/retrieval/fts_retriever.py` - FTSRetriever class with search implementation (178 lines)

## Decisions Made

### Database Schema Extension
- **Added breadcrumb TEXT column to chunks table**
  - Rationale: Phase 7 implemented breadcrumb generation in code (stored in metadata JSONB) but didn't create a dedicated column. FTS indexing requires a dedicated column for optimal performance. This was a planned feature that needed to be added now.
  - Impact: Enables FTS on qualified names like "UserService.authenticate"

### Full-Text Search Configuration
- **GIN indexes on both content and breadcrumb**
  - Rationale: Content FTS catches code and comments; breadcrumb FTS catches qualified name searches
  - Performance: GIN index provides 10-100x speedup over sequential scans
- **English text search configuration (to_tsvector('english', ...))**
  - Rationale: Standard for code search, provides stemming (e.g., "authenticate" matches "authentication")
  - Alternative considered: 'simple' configuration, but lacks stemming which helps with conceptual queries
- **ts_rank_cd for scoring**
  - Rationale: Cover density ranking considers proximity of query terms, better for code than simple frequency-based ts_rank
  - Use case: "auth error" ranks higher when terms appear close together

### Retrieval Implementation
- **GREATEST() for combining content and breadcrumb scores**
  - Rationale: Takes best score from either field, ensures breadcrumb-only matches aren't penalized
  - Alternative: Could add scores, but this creates bias toward chunks matching in both fields
- **COALESCE(breadcrumb, '') for NULL handling**
  - Rationale: Breadcrumb column is nullable (not all chunks have qualified names); COALESCE prevents FTS errors
- **Automatic latest run selection**
  - Rationale: Default behavior for "search current codebase"; run_id parameter allows pinning to specific commits
  - Query: Filters by status = 'completed' and sorts by completed_at DESC

## Issues Encountered

### Deviation: Breadcrumb Column Addition

**Issue:** The plan assumed the breadcrumb column existed (planned for Phase 7), but Phase 7 only implemented breadcrumb generation in Python code (stored in metadata JSONB field). The database column didn't exist.

**Resolution:** Added breadcrumb TEXT column in this migration (000006_add_fts_index.up.sql) before creating FTS indexes. This follows deviation Rule 3 (auto-fix blocking issues) - the column was architecturally planned but not yet implemented, and it blocks FTS indexing on qualified names.

**Rationale:** The plan explicitly calls for FTS indexing on breadcrumb field. Without the column, Task 1 would be blocked. Adding the column now aligns with the intended architecture and unblocks the plan.

**Files affected:**
- `000006_add_fts_index.up.sql` - Added `ALTER TABLE chunks ADD COLUMN IF NOT EXISTS breadcrumb TEXT;`
- `000006_add_fts_index.down.sql` - Added corresponding DROP COLUMN statement

## Next Step

Ready for 11-02-PLAN.md (Vector retrieval + query parser)
