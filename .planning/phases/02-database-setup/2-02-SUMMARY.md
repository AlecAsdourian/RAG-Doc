# Phase 2 Plan 2: Chunk Storage & Lineage Tracking Summary

**Forensic-quality chunk storage with complete lineage to source commits.**

## Accomplishments

- Ingestion runs table tracking commit SHAs and processing status
- Chunks table with file path and line number citations
- Complete audit trail: chunk → ingestion run → commit SHA → git URL
- Content hash for deduplication detection
- Sample data demonstrating end-to-end traceability

## Files Created/Modified

- `services/backend/migrations/000002_create_ingestion_runs.up.sql` - Commit tracking with status management
- `services/backend/migrations/000002_create_ingestion_runs.down.sql` - Rollback migration
- `services/backend/migrations/000003_create_chunks.up.sql` - Chunk storage with file/line citations
- `services/backend/migrations/000003_create_chunks.down.sql` - Rollback migration
- `services/backend/scripts/seed-lineage.sql` - Sample lineage data and verification queries
- `services/backend/Makefile` - Added seed-lineage target

## Decisions Made

- commit_sha VARCHAR(40) - Git SHAs are exactly 40 hex characters
- UNIQUE(repository_id, commit_sha) - Prevents duplicate ingestion of same commit
- Denormalized repository_id on chunks - Avoids JOIN for common queries (read optimization)
- content_hash SHA256 - Enables deduplication across files/commits
- start_line/end_line citations - Exact source location for every chunk
- metadata JSONB - Extensible storage for language-specific parsing results
- Status enum on ingestion_runs - Enforces valid states (pending/processing/completed/failed)

## Issues Encountered

None - extending schema with standard relational patterns completed successfully.

## Next Step

Ready for 2-03-PLAN.md (Query & Feedback Logging)
