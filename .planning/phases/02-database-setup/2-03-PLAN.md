---
phase: 02-database-setup
plan: 03
type: execute
---

<objective>
Create query and feedback logging infrastructure for accuracy measurement.

Purpose: Complete the audit trail by capturing every query, every retrieval result, and every user feedback. This closes the loop: we can now measure if we're delivering accurate answers and trace any answer back to the exact chunks and source code that informed it.
Output: Queries, retrievals, and feedback tables with complete traceability, verified end-to-end audit trail from user query → retrieval results → source chunks → commit SHA.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/02-database-setup/2-CONTEXT.md
@.planning/phases/02-database-setup/2-01-SUMMARY.md
@.planning/phases/02-database-setup/2-02-SUMMARY.md

**From Phase Context - Essential:**
"Query logs must capture what was asked, what was retrieved, what was shown. Feedback must be traceable back to specific retrievals and source chunks."

**From Phase Context - Vision:**
"Every search, every result, every thumbs-up/down gets recorded. This isn't just operational data - it's the accuracy measurement system."

**Tech stack available:**
- Postgres 16 with golang-migrate
- Core schema (organizations, projects, repositories)
- Lineage tracking (ingestion_runs, chunks with file/line citations)

**Constraining decisions:**
- UUIDs for primary keys
- TIMESTAMPTZ for timestamps
- Audit-first design - everything traceable
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create queries and retrievals tables</name>
  <files>services/backend/migrations/000004_create_queries_retrievals.up.sql, services/backend/migrations/000004_create_queries_retrievals.down.sql</files>
  <action>Create migration using make migrate-create NAME=create_queries_retrievals.

**UP migration (000004_create_queries_retrievals.up.sql):**

CREATE TABLE queries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, -- Queries are scoped to projects
  query_text TEXT NOT NULL, -- User's actual question
  query_embedding_id VARCHAR(255), -- Reference to vector DB embedding (nullable - Phase 3 adds this)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_queries_project_id ON queries(project_id);
CREATE INDEX idx_queries_created_at ON queries(created_at DESC); -- For recent queries

CREATE TABLE retrievals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  query_id UUID NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
  chunk_id UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL CHECK (rank > 0), -- Position in results (1 = top result)
  score DECIMAL(5,4), -- Relevance score 0.0000-1.0000 (nullable - may not always have score)
  shown_to_user BOOLEAN NOT NULL DEFAULT false, -- Was this chunk actually displayed?
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(query_id, chunk_id) -- Same chunk can't appear twice in same query results
);

CREATE INDEX idx_retrievals_query_id ON retrievals(query_id);
CREATE INDEX idx_retrievals_chunk_id ON retrievals(chunk_id);
CREATE INDEX idx_retrievals_rank ON retrievals(rank); -- For ordering results
CREATE INDEX idx_retrievals_shown_to_user ON retrievals(shown_to_user); -- For filtering displayed chunks

**DOWN migration:**
DROP TABLE IF EXISTS retrievals CASCADE;
DROP TABLE IF EXISTS queries CASCADE;

**Rationale:**
- Queries table: Minimal for now (just query_text and project scope). query_embedding_id is a string reference to vector DB (Phase 3).
- Retrievals table: Links queries to chunks with ranking and relevance scores. This is the "what was retrieved" log.
- rank tracks position (1st result vs 10th result matters for analysis)
- shown_to_user tracks whether chunk was actually displayed (may retrieve 10, show 3)
- UNIQUE(query_id, chunk_id) prevents duplicate chunks in same result set
- Indexes optimize query lookups, chunk reverse lookups, and filtering by rank/shown status
</action>
  <verify>make migrate-up succeeds. make migrate-version shows version 4. psql shows queries and retrievals tables with correct schema, indexes, and constraints.</verify>
  <done>queries and retrievals tables created with ranking, relevance scores, and shown-to-user tracking</done>
</task>

<task type="auto">
  <name>Task 2: Create feedback table with full traceability</name>
  <files>services/backend/migrations/000005_create_feedback.up.sql, services/backend/migrations/000005_create_feedback.down.sql</files>
  <action>Create migration using make migrate-create NAME=create_feedback.

**UP migration (000005_create_feedback.up.sql):**

CREATE TABLE feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  retrieval_id UUID NOT NULL REFERENCES retrievals(id) ON DELETE CASCADE, -- Feedback is on specific retrieval
  feedback_type VARCHAR(20) NOT NULL CHECK (feedback_type IN ('positive', 'negative', 'neutral')),
  feedback_text TEXT, -- Optional user comment explaining why (nullable)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_feedback_retrieval_id ON feedback(retrieval_id);
CREATE INDEX idx_feedback_type ON feedback(feedback_type); -- For aggregating positive vs negative
CREATE INDEX idx_feedback_created_at ON feedback(created_at DESC); -- For recent feedback

**DOWN migration:**
DROP TABLE IF EXISTS feedback CASCADE;

**Rationale:**
- Feedback on retrievals (not queries) - users rate specific chunks shown, not the whole query
- feedback_type enum: positive (thumbs up), negative (thumbs down), neutral (flag for review)
- feedback_text optional - users may just click thumbs up/down, or they may explain
- Full traceability chain: feedback → retrieval → query + chunk → ingestion_run → commit SHA
- Indexes optimize retrieval lookups (which chunks got feedback), type aggregation (accuracy metrics), and recent feedback queries
</action>
  <verify>make migrate-up succeeds. make migrate-version shows version 5. psql shows feedback table with correct schema and indexes. feedback_type constraint allows only valid values.</verify>
  <done>feedback table created with type constraints and traceability to retrievals</done>
</task>

<task type="auto">
  <name>Task 3: Seed complete audit trail and verify end-to-end traceability</name>
  <files>services/backend/scripts/seed-complete.sql</files>
  <action>Create services/backend/scripts/seed-complete.sql demonstrating complete audit trail:

-- Sample query
INSERT INTO queries (id, project_id, query_text, created_at)
VALUES (
  '50000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001', -- main-platform project
  'How does user authentication work?',
  NOW() - INTERVAL '2 hours'
);

-- Retrievals linking query to chunks (top 3 results)
INSERT INTO retrievals (id, query_id, chunk_id, rank, score, shown_to_user, created_at)
VALUES
  (
    '60000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000001',
    '40000000-0000-0000-0000-000000000001', -- validateCredentials function chunk
    1,
    0.9234,
    true,
    NOW() - INTERVAL '2 hours'
  ),
  (
    '60000000-0000-0000-0000-000000000002',
    '50000000-0000-0000-0000-000000000001',
    '40000000-0000-0000-0000-000000000002', -- SessionManager class chunk
    2,
    0.8756,
    true,
    NOW() - INTERVAL '2 hours'
  ),
  (
    '60000000-0000-0000-0000-000000000003',
    '50000000-0000-0000-0000-000000000001',
    '40000000-0000-0000-0000-000000000003', -- README chunk
    3,
    0.7123,
    true,
    NOW() - INTERVAL '2 hours'
  );

-- User feedback on retrievals
INSERT INTO feedback (retrieval_id, feedback_type, feedback_text, created_at)
VALUES
  (
    '60000000-0000-0000-0000-000000000001',
    'positive',
    'Exactly what I needed - clear explanation of the validation flow',
    NOW() - INTERVAL '1 hour'
  ),
  (
    '60000000-0000-0000-0000-000000000003',
    'negative',
    'This is just a README overview, not helpful for understanding implementation',
    NOW() - INTERVAL '1 hour'
  );

-- VERIFICATION QUERIES demonstrating complete audit trail:

-- Query 1: Complete lineage for a single piece of feedback
-- Trace: feedback → retrieval → query & chunk → ingestion run → commit SHA & source file
SELECT
  f.feedback_type,
  f.feedback_text,
  q.query_text AS user_question,
  r.rank AS result_position,
  r.score AS relevance_score,
  c.file_path,
  c.start_line,
  c.end_line,
  c.content AS chunk_content,
  ir.commit_sha,
  ir.commit_message,
  ir.commit_timestamp,
  repo.git_url,
  proj.name AS project_name,
  org.name AS organization_name
FROM feedback f
JOIN retrievals r ON f.retrieval_id = r.id
JOIN queries q ON r.query_id = q.id
JOIN chunks c ON r.chunk_id = c.id
JOIN ingestion_runs ir ON c.ingestion_run_id = ir.id
JOIN repositories repo ON ir.repository_id = repo.id
JOIN projects proj ON repo.project_id = proj.id
JOIN organizations org ON proj.organization_id = org.id
WHERE f.id = '60000000-0000-0000-0000-000000000001'  -- Positive feedback example
LIMIT 1;

-- Query 2: Accuracy metrics - feedback breakdown for recent queries
SELECT
  f.feedback_type,
  COUNT(*) as count,
  ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) as percentage
FROM feedback f
GROUP BY f.feedback_type
ORDER BY count DESC;

-- Query 3: Query analysis - what chunks were retrieved and shown
SELECT
  q.query_text,
  r.rank,
  r.score,
  r.shown_to_user,
  c.file_path,
  c.language,
  c.chunk_type,
  COALESCE(f.feedback_type, 'no_feedback') as feedback_status
FROM queries q
JOIN retrievals r ON q.id = r.query_id
JOIN chunks c ON r.chunk_id = c.id
LEFT JOIN feedback f ON r.id = f.retrieval_id
WHERE q.id = '50000000-0000-0000-0000-000000000001'
ORDER BY r.rank;

-- Query 4: Chunk performance - which chunks get positive vs negative feedback
SELECT
  c.file_path,
  c.chunk_type,
  COUNT(DISTINCT r.id) as times_retrieved,
  COUNT(f.id) FILTER (WHERE f.feedback_type = 'positive') as positive_feedback,
  COUNT(f.id) FILTER (WHERE f.feedback_type = 'negative') as negative_feedback,
  ROUND(
    COUNT(f.id) FILTER (WHERE f.feedback_type = 'positive') * 100.0 /
    NULLIF(COUNT(f.id), 0),
    2
  ) as positive_percentage
FROM chunks c
JOIN retrievals r ON c.id = r.chunk_id
LEFT JOIN feedback f ON r.id = f.retrieval_id
GROUP BY c.file_path, c.chunk_type
HAVING COUNT(f.id) > 0  -- Only chunks with feedback
ORDER BY positive_percentage DESC NULLS LAST;

Add Makefile target 'seed-complete' to apply SQL and run all verification queries with formatted output.

Expected results:
- Query 1: Shows complete lineage from feedback through 7 joined tables to source file and organization
- Query 2: Shows feedback distribution (1 positive, 1 negative in sample data = 50/50)
- Query 3: Shows all 3 retrievals for the query with feedback status
- Query 4: Shows per-chunk feedback metrics
</action>
  <verify>make seed-complete succeeds. All verification queries return expected results. Query 1 demonstrates complete audit trail across entire schema. Feedback metrics calculate correctly.</verify>
  <done>Complete audit trail seeded and verified, end-to-end traceability demonstrated from user feedback → source code commit</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] make migrate-up applies final migrations successfully
- [ ] make migrate-version shows version 5, dirty: false
- [ ] psql shows queries, retrievals, feedback tables with correct schema
- [ ] make seed-complete populates sample data
- [ ] Complete audit trail query joins all tables successfully
- [ ] Feedback metrics calculate correctly (positive/negative percentages)
- [ ] Query analysis shows retrieved chunks with feedback status
- [ ] Chunk performance metrics demonstrate accuracy measurement capability
- [ ] **Phase 2 complete** - all database infrastructure in place
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- queries table captures user questions scoped to projects
- retrievals table tracks what chunks were returned with ranking
- feedback table enables accuracy measurement with traceability
- Complete audit trail: feedback → retrieval → query & chunk → ingestion run → commit SHA → source file
- Sample data demonstrates end-to-end traceability across all tables
- Accuracy measurement queries prove forensic capability
- **Phase 2 Database Setup complete** - foundation ready for Phase 3 (Vector Database)
</success_criteria>

<output>
After completion, create `.planning/phases/02-database-setup/2-03-SUMMARY.md`:

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
</output>
