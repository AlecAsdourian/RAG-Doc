---
phase: 02-database-setup
plan: 02
type: execute
---

<objective>
Create chunk storage with file/line citations and ingestion run lineage tracking.

Purpose: Implement the forensic layer that makes accurate answers possible. Every chunk must be traceable to its source file, line numbers, and the exact commit SHA it came from. This is the audit trail foundation.
Output: Chunks table with file/line citations, ingestion_runs table with commit tracking, verified end-to-end lineage from chunk → ingestion run → commit SHA → source file.
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

**From Phase Context - Essential:**
"Ingestion runs must capture commit SHAs. Chunks must reference exact file paths and line numbers. This is the forensics layer that makes everything else possible."

**From Phase Context - Specifics:**
Layered implementation - now building the lineage/audit layer on top of core schema.

**Tech stack available:**
- Postgres 16 with golang-migrate
- Core schema (organizations, projects, repositories) from Plan 1

**Constraining decisions:**
- UUIDs for primary keys (from Plan 1)
- TIMESTAMPTZ for timestamps (from Plan 1)
- Audit-first design philosophy
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create ingestion_runs table for commit SHA tracking</name>
  <files>services/backend/migrations/000002_create_ingestion_runs.up.sql, services/backend/migrations/000002_create_ingestion_runs.down.sql</files>
  <action>Create migration using make migrate-create NAME=create_ingestion_runs.

**UP migration (000002_create_ingestion_runs.up.sql):**

CREATE TABLE ingestion_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  commit_sha VARCHAR(40) NOT NULL, -- Full Git commit SHA (40 hex chars)
  commit_message TEXT,
  commit_author VARCHAR(255),
  commit_timestamp TIMESTAMPTZ,
  branch VARCHAR(255) NOT NULL,
  status VARCHAR(50) NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  chunks_processed INTEGER DEFAULT 0,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(repository_id, commit_sha) -- Same commit can't be ingested twice for same repo
);

CREATE INDEX idx_ingestion_runs_repository_id ON ingestion_runs(repository_id);
CREATE INDEX idx_ingestion_runs_commit_sha ON ingestion_runs(commit_sha);
CREATE INDEX idx_ingestion_runs_status ON ingestion_runs(status);
CREATE INDEX idx_ingestion_runs_started_at ON ingestion_runs(started_at DESC); -- For recent runs queries

**DOWN migration:**
DROP TABLE IF EXISTS ingestion_runs CASCADE;

**Rationale:**
- commit_sha is VARCHAR(40) not TEXT - Git SHAs are exactly 40 hex characters
- UNIQUE(repository_id, commit_sha) prevents duplicate ingestion of same commit
- status enum ensures valid states only
- chunks_processed tracks progress (useful for partial failures)
- Indexes on repository_id (FK lookups), commit_sha (lineage queries), status (monitoring), started_at (recency)
</action>
  <verify>make migrate-up succeeds. make migrate-version shows version 2. psql shows ingestion_runs table with all columns and indexes. Status constraint allows only valid values.</verify>
  <done>ingestion_runs table created with commit SHA tracking, status management, and proper indexes</done>
</task>

<task type="auto">
  <name>Task 2: Create chunks table with file/line citations and ingestion run linkage</name>
  <files>services/backend/migrations/000003_create_chunks.up.sql, services/backend/migrations/000003_create_chunks.down.sql</files>
  <action>Create migration using make migrate-create NAME=create_chunks.

**UP migration (000003_create_chunks.up.sql):**

CREATE TABLE chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ingestion_run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
  repository_id UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE, -- Denormalized for query performance
  file_path TEXT NOT NULL, -- Relative path from repo root, e.g., "src/utils/parser.ts"
  start_line INTEGER NOT NULL CHECK (start_line > 0),
  end_line INTEGER NOT NULL CHECK (end_line >= start_line),
  content TEXT NOT NULL, -- Actual code/text chunk
  content_hash VARCHAR(64) NOT NULL, -- SHA256 of content for deduplication
  language VARCHAR(50), -- e.g., "typescript", "python", "go" (nullable - may be unknown)
  chunk_type VARCHAR(50), -- e.g., "function", "class", "comment" (nullable - may be generic)
  metadata JSONB, -- Extensible - function names, imports, etc. (nullable)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_chunks_ingestion_run_id ON chunks(ingestion_run_id);
CREATE INDEX idx_chunks_repository_id ON chunks(repository_id);
CREATE INDEX idx_chunks_file_path ON chunks(file_path); -- For file-specific queries
CREATE INDEX idx_chunks_content_hash ON chunks(content_hash); -- For deduplication checks
CREATE INDEX idx_chunks_language ON chunks(language); -- For language-specific queries

**DOWN migration:**
DROP TABLE IF EXISTS chunks CASCADE;

**Rationale:**
- file_path is TEXT not VARCHAR - file paths can be long (deep directory structures)
- start_line/end_line provide exact citation (line numbers can change between commits)
- content_hash enables deduplication (same chunk across different files/commits)
- repository_id denormalized - avoids JOIN through ingestion_runs for common queries
- language/chunk_type nullable - parsing may not always determine these
- metadata JSONB - extensible without schema changes (function names, class info, etc.)
- Indexes on ingestion_run_id (lineage), repository_id (scoped queries), file_path (citations), content_hash (dedup), language (filtered searches)
</action>
  <verify>make migrate-up succeeds. make migrate-version shows version 3. psql shows chunks table with all columns, CHECK constraints work, indexes exist.</verify>
  <done>chunks table created with file/line citations, ingestion run linkage, and content hash for deduplication</done>
</task>

<task type="auto">
  <name>Task 3: Seed lineage data and verify complete audit trail</name>
  <files>services/backend/scripts/seed-lineage.sql</files>
  <action>Create services/backend/scripts/seed-lineage.sql with sample ingestion and chunks:

-- Ingestion run for the backend repo
INSERT INTO ingestion_runs (id, repository_id, commit_sha, commit_message, commit_author, commit_timestamp, branch, status, completed_at, chunks_processed)
VALUES (
  '30000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001', -- backend repo from seed.sql
  'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0', -- Sample commit SHA
  'feat: add user authentication',
  'dev@example.com',
  NOW() - INTERVAL '1 day',
  'main',
  'completed',
  NOW() - INTERVAL '23 hours',
  3
);

-- Sample chunks from that ingestion run
INSERT INTO chunks (id, ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash, language, chunk_type, metadata)
VALUES
  (
    '40000000-0000-0000-0000-000000000001',
    '30000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'src/auth/login.ts',
    1,
    25,
    'export async function validateCredentials(email: string, password: string): Promise<boolean> { /* ... */ }',
    'abc123def456...', -- Truncated for example
    'typescript',
    'function',
    '{"functionName": "validateCredentials", "exports": true, "async": true}'::jsonb
  ),
  (
    '40000000-0000-0000-0000-000000000002',
    '30000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'src/auth/session.ts',
    10,
    45,
    'export class SessionManager { /* ... */ }',
    'xyz789ghi012...',
    'typescript',
    'class',
    '{"className": "SessionManager", "exports": true}'::jsonb
  ),
  (
    '40000000-0000-0000-0000-000000000003',
    '30000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'README.md',
    1,
    10,
    '# Authentication System\n\nThis module handles user authentication...',
    'readme999...',
    'markdown',
    'documentation',
    NULL
  );

-- Verify complete lineage trail with sample queries:

-- Query 1: Trace chunk back to source commit
SELECT
  c.file_path,
  c.start_line,
  c.end_line,
  ir.commit_sha,
  ir.commit_message,
  ir.commit_timestamp,
  r.git_url
FROM chunks c
JOIN ingestion_runs ir ON c.ingestion_run_id = ir.id
JOIN repositories r ON ir.repository_id = r.id
WHERE c.id = '40000000-0000-0000-0000-000000000001';

-- Query 2: Find all chunks from a specific commit
SELECT file_path, start_line, end_line, language, chunk_type
FROM chunks
WHERE ingestion_run_id IN (
  SELECT id FROM ingestion_runs WHERE commit_sha = 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0'
);

-- Query 3: Get ingestion run summary
SELECT
  r.name AS repo_name,
  ir.commit_sha,
  ir.branch,
  ir.status,
  ir.chunks_processed,
  COUNT(c.id) AS actual_chunks
FROM ingestion_runs ir
JOIN repositories r ON ir.repository_id = r.id
LEFT JOIN chunks c ON c.ingestion_run_id = ir.id
GROUP BY r.name, ir.commit_sha, ir.branch, ir.status, ir.chunks_processed;

Add Makefile target 'seed-lineage' to apply this SQL and run verification queries.

Expected results:
- Query 1: Returns complete lineage (file + lines + commit SHA + git URL)
- Query 2: Returns 3 chunks for the sample commit
- Query 3: Shows chunks_processed matches actual COUNT
</action>
  <verify>make seed-lineage succeeds. Verification queries return expected results. Complete audit trail visible: chunk → ingestion run → commit SHA → repository → project → organization.</verify>
  <done>Lineage data seeded, audit trail verified end-to-end, queries demonstrate traceability from any chunk back to exact source commit</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] make migrate-up applies both new migrations successfully
- [ ] make migrate-version shows version 3, dirty: false
- [ ] psql shows ingestion_runs and chunks tables with correct schema
- [ ] make seed-lineage populates sample data
- [ ] Audit trail query traces chunk → commit SHA → git URL
- [ ] Chunks count matches ingestion_run.chunks_processed
- [ ] file_path, start_line, end_line provide exact citations
- [ ] content_hash enables deduplication detection
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- ingestion_runs table tracks commit SHAs with status management
- chunks table includes file/line citations (forensic-quality references)
- Complete lineage: chunk → ingestion run → commit SHA → repository
- Denormalized repository_id on chunks optimizes common queries
- Sample data demonstrates end-to-end audit trail
- **Forensic layer complete, ready for query/feedback logging (Plan 3)**
</success_criteria>

<output>
After completion, create `.planning/phases/02-database-setup/2-02-SUMMARY.md`:

# Phase 2 Plan 2: Chunk Storage & Lineage Tracking Summary

**Forensic-quality chunk storage with complete lineage to source commits.**

## Accomplishments

- Ingestion runs table tracking commit SHAs and processing status
- Chunks table with file path and line number citations
- Complete audit trail: chunk → ingestion run → commit SHA → git URL
- Content hash for deduplication detection
- Sample data demonstrating end-to-end traceability

## Files Created/Modified

- `services/backend/migrations/000002_create_ingestion_runs.up.sql` - Commit tracking
- `services/backend/migrations/000002_create_ingestion_runs.down.sql` - Rollback
- `services/backend/migrations/000003_create_chunks.up.sql` - Chunk storage with citations
- `services/backend/migrations/000003_create_chunks.down.sql` - Rollback
- `services/backend/scripts/seed-lineage.sql` - Sample data and verification queries
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

None expected - extending schema with standard relational patterns.

## Next Step

Ready for 2-03-PLAN.md (Query & Feedback Logging)
</output>
