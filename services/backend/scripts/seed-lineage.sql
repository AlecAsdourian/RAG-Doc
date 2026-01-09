-- Lineage data for development and testing

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
    'abc123def456789012345678901234567890123456789012345678901234', -- SHA256 placeholder
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
    'xyz789ghi012345678901234567890123456789012345678901234567890',
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
    '# Authentication System

This module handles user authentication...',
    'readme999012345678901234567890123456789012345678901234567890',
    'markdown',
    'documentation',
    NULL
  );

-- VERIFICATION QUERIES demonstrating complete lineage trail

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
