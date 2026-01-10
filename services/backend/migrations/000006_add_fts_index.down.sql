-- Drop full-text search indexes
DROP INDEX IF EXISTS chunks_breadcrumb_fts_idx;
DROP INDEX IF EXISTS chunks_content_fts_idx;

-- Drop breadcrumb column
ALTER TABLE chunks DROP COLUMN IF EXISTS breadcrumb;
