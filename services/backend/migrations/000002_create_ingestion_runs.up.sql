-- Ingestion runs table for tracking commit SHA lineage
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
