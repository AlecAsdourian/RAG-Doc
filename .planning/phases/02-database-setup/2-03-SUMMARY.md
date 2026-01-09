# Phase 2 Plan 3: Query & Feedback Logging Summary

**Complete audit trail from user feedback to source code commits.**

## Accomplishments

- Queries table capturing user questions scoped to projects
- Retrievals table linking queries to chunks with ranking and scores
- Feedback table enabling accuracy measurement
- Complete end-to-end traceability across 7 joined tables
- Accuracy metrics queries (positive/negative feedback breakdown)
- Chunk performance analysis (which source code gets good feedback)

## Files Created/Modified

- `services/backend/migrations/000004_create_queries_retrievals.up.sql` - Query and retrieval logging
- `services/backend/migrations/000004_create_queries_retrievals.down.sql` - Rollback
- `services/backend/migrations/000005_create_feedback.up.sql` - Feedback tracking
- `services/backend/migrations/000005_create_feedback.down.sql` - Rollback
- `services/backend/scripts/seed-complete.sql` - Complete audit trail demo with verification queries
- `services/backend/Makefile` - Added seed-complete target

## Decisions Made

- Feedback on retrievals (not queries) - Users rate specific chunks, not entire query
- shown_to_user boolean on retrievals - Track what was actually displayed vs just retrieved
- UNIQUE(query_id, chunk_id) on retrievals - Same chunk can't appear twice in results
- feedback_type enum ('positive', 'negative', 'neutral') - Enforces valid feedback values
- feedback_text optional - Users can thumbs up/down without explaining why
- query_embedding_id as VARCHAR reference - Points to vector DB (Phase 3), not FK (separate system)

## Issues Encountered

None - completing schema with standard relational patterns.

## Phase 2 Complete

**Database foundation established:**
✓ Postgres 16 with golang-migrate for schema versioning
✓ Multi-tenant core (organizations → projects → repositories)
✓ Forensic lineage (chunks → ingestion_runs → commit SHAs)
✓ Query logging (queries → retrievals → chunks)
✓ Feedback tracking (complete traceability to source)
✓ Accuracy measurement capability proven with sample queries

**Audit trail verified:**
feedback → retrieval → query & chunk → ingestion run → commit SHA → file path (lines X-Y) → repository → project → organization

**Next Phase:**
Ready for Phase 3: Vector Database (embeddings storage and similarity search)
