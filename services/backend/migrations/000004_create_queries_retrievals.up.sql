-- Queries table for capturing user questions
CREATE TABLE queries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, -- Queries are scoped to projects
  query_text TEXT NOT NULL, -- User's actual question
  query_embedding_id VARCHAR(255), -- Reference to vector DB embedding (nullable - Phase 3 adds this)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_queries_project_id ON queries(project_id);
CREATE INDEX idx_queries_created_at ON queries(created_at DESC); -- For recent queries

-- Retrievals table linking queries to chunks (what was retrieved)
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
