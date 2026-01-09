-- Chunks table with file/line citations and ingestion run linkage
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
