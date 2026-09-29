-- Reverse of 000017, in reverse order.
--
-- WHAT IS LOST. Every row in the partitioned `chunks`, vectors included,
-- and every `symbols` row. Both are re-ingested from source (P1); nothing
-- in either is user-authored. `retrievals` and `feedback` are NOT touched,
-- for the reason the last section gives.
--
-- `chunks` is recreated exactly as 000003 + 000006 + 000008 + 000009 left
-- it: the 000003 columns and indexes, 000006's breadcrumb column and two
-- GIN indexes, 000008's two-hop EXISTS policy with FORCE, and 000009's
-- trigger. Column order matters to nothing, but it is 000003's with
-- breadcrumb last, which is where ALTER TABLE ADD COLUMN put it.

-- chunks first: it references symbols (symbol_id).
DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS symbols;

-- 000003, verbatim.
CREATE TABLE chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ingestion_run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
  repository_id UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  start_line INTEGER NOT NULL CHECK (start_line > 0),
  end_line INTEGER NOT NULL CHECK (end_line >= start_line),
  content TEXT NOT NULL,
  content_hash VARCHAR(64) NOT NULL,
  language VARCHAR(50),
  chunk_type VARCHAR(50),
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- 000006.
  breadcrumb TEXT
);

CREATE INDEX idx_chunks_ingestion_run_id ON chunks(ingestion_run_id);
CREATE INDEX idx_chunks_repository_id ON chunks(repository_id);
CREATE INDEX idx_chunks_file_path ON chunks(file_path);
CREATE INDEX idx_chunks_content_hash ON chunks(content_hash);
CREATE INDEX idx_chunks_language ON chunks(language);

-- 000006.
CREATE INDEX chunks_content_fts_idx ON chunks USING GIN(to_tsvector('english', content));
CREATE INDEX chunks_breadcrumb_fts_idx ON chunks USING GIN(to_tsvector('english', breadcrumb));

-- 000008.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM repositories r
      JOIN projects p ON r.project_id = p.id
      WHERE r.id = chunks.repository_id
      AND p.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );

-- 000009.
CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON chunks
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

-- 000004's key back, NOT VALID.
--
-- ⚠ NOT VALID IS DELIBERATE, AND A PLAIN ADD CONSTRAINT WOULD BE WRONG.
-- The up migration dropped the key and then the table it pointed at, so
-- every retrievals row that existed before it now carries a chunk_id no
-- chunk has (the seeded gate creates exactly that, on purpose). A plain
-- restore validates those rows and fails with 23503 whenever retrievals
-- holds any (measured by the fact-check with dangling rows present), and
-- the only way to make it pass would be to delete them, and the feedback
-- hanging off them, which is user-authored and the one thing a rollback
-- must not destroy. NOT VALID enforces the key for every NEW row and
-- checks none of the old ones. Whoever wants it validated deletes or
-- repoints the dangling rows first and runs
-- `ALTER TABLE retrievals VALIDATE CONSTRAINT retrievals_chunk_id_fkey`.
ALTER TABLE retrievals
  ADD CONSTRAINT retrievals_chunk_id_fkey
  FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE NOT VALID;
