-- Add breadcrumb column to chunks table (required for qualified name searches)
-- This column was planned for Phase 7 but is needed now for FTS indexing
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS breadcrumb TEXT;

-- Create full-text search index on chunks.content using to_tsvector('english', content)
-- GIN index speeds up full-text queries significantly (10-100x faster than sequential scans)
CREATE INDEX chunks_content_fts_idx ON chunks USING GIN(to_tsvector('english', content));

-- Create full-text search index on breadcrumb for qualified name searches
-- Enables searches like "auth.middleware.validateToken"
CREATE INDEX chunks_breadcrumb_fts_idx ON chunks USING GIN(to_tsvector('english', breadcrumb));
