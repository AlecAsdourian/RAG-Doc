-- Complete audit trail demonstration: query → retrieval → feedback → source code

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
WHERE f.id = (SELECT id FROM feedback WHERE feedback_type = 'positive' LIMIT 1)
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
