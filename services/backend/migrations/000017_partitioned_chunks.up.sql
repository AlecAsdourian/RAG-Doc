-- Phase 22-02: `chunks` rebuilt as DECISIONS.md D2 decided, corrected by
-- what 22-RESEARCH.md measured, and `symbols` (D1) created beside it.
--
-- WHAT THIS FILE DOES, in order, and why the order is what it is:
--
--   1. drops `retrievals_chunk_id_fkey` (P17, U9). A foreign key into a
--      table whose primary key is (organization_id, id) cannot exist:
--      `retrievals` has no organization_id, and a unique key on a
--      partitioned table must include the partition key. Measured:
--      "there is no unique constraint matching given keys for referenced
--      table". The column stays, so a logged result keeps the chunk id it
--      was shown. It has to go BEFORE step 3, or DROP TABLE names it.
--   2. creates `symbols`, unpartitioned (P7), with its tenant key.
--   3. drops `chunks`. NO DML (P1): every row in it is harness or benchmark
--      data whose vectors live only in Qdrant, so D2's `embedding NOT NULL`
--      could not be met by moving them. Re-ingesting costs cents.
--   4. creates `chunks` partitioned by HASH (organization_id), MODULUS 64,
--      with EVERY constraint inside the CREATE TABLE (ISS-031, below).
--   5. creates the 64 partitions and puts row-level security on EACH ONE
--      (P2, below).
--   6. clones `trg_assert_tenant` onto them by creating it on the parent.
--   7. creates the indexes on the parent, so every partition gets them.
--   8. records what a reader must know in COMMENT ON TABLE.
--
-- NO GRANTs, deliberately: `rag_doc_app` is a test-harness role (000010).
-- Both harnesses run `GRANT ... ON ALL TABLES IN SCHEMA public` after
-- migrating, which covers the partitions; production's grants are Phase
-- 24's. ⚠ Whatever those grants are, they must NEVER include TRUNCATE on
-- these tables: row-level security does not govern TRUNCATE, so it is the
-- one statement per-partition RLS cannot stop (22-CONTEXT P2's addendum).
--
-- ⚠ ISS-031: EVERY FOREIGN KEY HERE IS DECLARED INSIDE ITS CREATE TABLE.
-- This file runs in the same migrating session as 000013's and 000015's
-- tenant-setting loops, and after 000015 that session's app.current_tenant
-- reads '' (measured by the fact-check, 2026-09-17). An `ALTER TABLE ...
-- ADD CONSTRAINT ... FOREIGN KEY` validates with one query that reads the
-- referenced table through its policy as the owner, evaluates ''::uuid and
-- fails with 22P02, leaving this version dirty. A key declared with its
-- table has no rows to validate, so no validation query runs and the
-- setting is never read. Partitions created with PARTITION OF inherit the
-- parent's keys without validating anything either. The seeded gate
-- (pkg/testing/isolation/migration_seeded_test.go) runs this file in
-- exactly that poisoned session and audits every ALTER TABLE; the mutation
-- that moves chunks_repo_tenant_fk out of the CREATE TABLE fails it with
-- 22P02 at 17, dirty (22-02-SUMMARY.md).
--
-- ⚠ P2: ROW-LEVEL SECURITY ON A PARTITIONED PARENT DOES NOT REACH ITS
-- PARTITIONS. Measured on this DDL with only the parent secured: all 64
-- partitions showed relrowsecurity = false, and as the NOSUPERUSER
-- NOBYPASSRLS app role with tenant A set, a SELECT on tenant B's partition
-- returned B's row and an UPDATE on it reported UPDATE 1 and overwrote it.
-- The parent's policy applies only to queries made THROUGH the parent, and
-- the harnesses grant on ALL TABLES, partitions included. So step 5 enables
-- and forces row-level security, and creates the policy, on every
-- partition. Measured with that in place: B's partition shows A zero rows,
-- the update matches zero, and pruning still fires (below).
--
-- THE POLICY IS SCALAR, on both the parent and the partitions:
--
--     USING (organization_id = current_setting('app.current_tenant', true)::uuid)
--
-- Scalar equality on the partition key is what lets the planner prune:
-- with no organization_id in the SQL at all, the production query shape
-- shows `Subplans Removed: 63` from the policy alone (measured; pinned by
-- pkg/testing/isolation/chunks_partition_test.go). 000008's two-hop EXISTS
-- form would not prune, and it is not needed: organization_id is now a
-- stored column, guaranteed by the key below.
--
-- ⚠ P3: TENANCY IS GUARANTEED BY chunks_repo_tenant_fk, the composite key
-- (repository_id, organization_id) -> repositories (id, organization_id),
-- which makes a misfiled chunk unrepresentable WHILE FOREIGN-KEY TRIGGERS
-- ARE ENABLED. Measured: a misfiled insert fails with 23503. What it does
-- NOT guarantee, measured by the fact-check: under
-- `SET session_replication_role = replica`, or `ALTER TABLE ... DISABLE
-- TRIGGER`, a misfiled chunk inserts cleanly past the key, because foreign
-- keys are enforced by triggers and those settings switch them off. So:
--
--   - LOADING `chunks` OR `symbols` UNDER replica MODE OR WITH TRIGGERS
--     DISABLED IS FORBIDDEN. This repository uses replica mode for test
--     cleanup only (pkg/auth/testing.go), which deletes rather than loads.
--   - the drift query (isolation.CheckChunkTenantDrift) stays load-bearing
--     and runs in CI; the replica-mode acceptance is pinned as a test so
--     nobody re-derives the claim that the key "holds without triggers".
--
-- ⚠ A TRIGGER CANNOT FILL IN organization_id. 000013 fills
-- repositories.organization_id from the project in a BEFORE trigger; the
-- same trick on a partitioned table fails with 0A000, "moving row to
-- another partition during a BEFORE FOR EACH ROW trigger is not
-- supported", because the row was already routed by the value it arrived
-- with (measured). EVERY WRITER SUPPLIES organization_id. The key above
-- refuses a wrong one.
--
-- P4: `embedding_model` records which model produced the vector, and a
-- query embedded with one model must never be compared against rows
-- embedded with another: two models at the same dimension produce vectors
-- in different spaces, and mixing them fails silently (22-RESEARCH.md Q3).
-- The model stays text-embedding-ada-002 through the storage move (U3).
--
-- P15, the transition: between this migration and 22-03 the pipeline
-- writes each vector to BOTH Postgres and Qdrant, from one embedding call,
-- because 22-03's equivalence gate compares the two. Nothing reads the
-- Postgres vectors until 22-03, and nothing runs in production until 22-05.

-- =====================================================================
-- 1. The key that cannot survive the partitioned primary key (P17)
-- =====================================================================
ALTER TABLE retrievals DROP CONSTRAINT retrievals_chunk_id_fkey;

-- =====================================================================
-- 2. `symbols` (D1), unpartitioned (P7)
-- =====================================================================
--
-- Unpartitioned because partitioning it would turn every foreign key INTO
-- it (chunks.symbol_id here; symbol_edges and memory_anchors later) into a
-- composite key, which is the same break P17 records for retrievals. It
-- carries no vector index, so D2's index-size argument does not apply.
--
-- Transcribed from DECISIONS.md D1, with its `archived_at` and
-- idx_symbols_live (symbols are archived, never deleted by ingest), and
-- with P3's composite tenant key declared here rather than by ALTER TABLE.
CREATE TABLE symbols (
  -- D1's DETERMINISTIC id: uuid_v5 over (repository_id, file_path,
  -- symbol_path, kind, ordinal) joined by E'\x1f'. Nothing in Phase 22
  -- writes this table; 22.1-01 generates the id and the resurrection
  -- upsert (INSERT ... ON CONFLICT (id) DO UPDATE SET archived_at = NULL,
  -- ...), which this table's shape already supports.
  id UUID PRIMARY KEY,

  -- The tenant, guaranteed by symbols_repo_tenant_fk below. An
  -- AUTHORIZATION INPUT, like chunks.organization_id: the policy reads it.
  organization_id UUID NOT NULL,
  repository_id   UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,

  file_path   TEXT NOT NULL,
  -- Normalized breadcrumb, the full ancestor chain (22-CONTEXT P8).
  symbol_path TEXT NOT NULL,
  kind        TEXT NOT NULL,              -- function|method|class|type|const|module

  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,

  -- SHA-256 over the symbol's span (decorators and leading doc comments
  -- included, P8). The drift detector D4's anchors compare against.
  span_digest TEXT NOT NULL,

  first_seen_commit TEXT NOT NULL,
  last_seen_commit  TEXT NOT NULL,

  -- 0-based index among symbols sharing (file_path, symbol_path, kind) in
  -- source order: two Go init(), a Python property and its setter, a
  -- TypeScript interface merged with a function, typing.overload stubs.
  ordinal SMALLINT NOT NULL DEFAULT 0,

  -- Set when the symbol vanishes from the source; cleared when it returns.
  archived_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (repository_id, file_path, symbol_path, kind, ordinal),

  -- P3: the tenant guarantee, declared with the table (ISS-031).
  CONSTRAINT symbols_repo_tenant_fk
    FOREIGN KEY (repository_id, organization_id)
    REFERENCES repositories (id, organization_id) ON DELETE CASCADE
);

CREATE INDEX idx_symbols_live ON symbols (repository_id, file_path)
  WHERE archived_at IS NULL;
CREATE INDEX idx_symbols_organization_id ON symbols (organization_id);

ALTER TABLE symbols ENABLE ROW LEVEL SECURITY;
ALTER TABLE symbols FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON symbols
  FOR ALL
  USING (organization_id = current_setting('app.current_tenant', true)::uuid);

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON symbols
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

-- =====================================================================
-- 3. The old table goes, rows and all (P1)
-- =====================================================================
--
-- Plain DROP TABLE, not CASCADE: if anything still depended on `chunks`
-- this must fail here and say what, rather than silently take it along.
-- The one dependency that existed was dropped in section 1.
DROP TABLE chunks;

-- =====================================================================
-- 4. The new table, partitioned, every constraint declared with it
-- =====================================================================
CREATE TABLE chunks (
  id UUID NOT NULL DEFAULT gen_random_uuid(),

  -- The partition key and the policy's column. Supplied by EVERY writer
  -- (a trigger cannot fill it; header). Guaranteed by chunks_repo_tenant_fk.
  organization_id UUID NOT NULL,

  ingestion_run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
  repository_id    UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE, -- Denormalized for query performance (000003)

  -- P7: at most one symbol per chunk, nullable. A chunk spanning several
  -- symbols points at the file's module symbol, or at nothing.
  symbol_id UUID REFERENCES symbols(id) ON DELETE SET NULL,

  file_path TEXT NOT NULL, -- Relative path from repo root, e.g., "src/utils/parser.ts"
  start_line INTEGER NOT NULL CHECK (start_line > 0),
  end_line INTEGER NOT NULL CHECK (end_line >= start_line),
  content TEXT NOT NULL, -- Actual code/text chunk
  content_hash VARCHAR(64) NOT NULL, -- SHA256 of content for deduplication
  language VARCHAR(50), -- e.g., "typescript", "python", "go" (nullable - may be unknown)
  chunk_type VARCHAR(50), -- e.g., "function", "class", "comment" (nullable - may be generic)
  metadata JSONB, -- Extensible - function names, imports, etc. (nullable)
  breadcrumb TEXT, -- 000006: the qualified name keyword search matches

  -- D2: the vector lives here, under the same policy as the text. Every
  -- chunk carries one, duplicates included (22-RESEARCH.md Q3 found 92
  -- duplicate-content chunks with no Qdrant point at all).
  embedding vector(1536) NOT NULL,
  -- P4: which model produced it. The retriever refuses to compare across
  -- models.
  embedding_model TEXT NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- The partition key must be part of the primary key.
  PRIMARY KEY (organization_id, id),

  -- P3: the tenant guarantee, declared with the table (ISS-031).
  CONSTRAINT chunks_repo_tenant_fk
    FOREIGN KEY (repository_id, organization_id)
    REFERENCES repositories (id, organization_id) ON DELETE CASCADE
) PARTITION BY HASH (organization_id);

-- =====================================================================
-- 5. The 64 partitions, each with row-level security of its own (P2)
-- =====================================================================
--
-- MODULUS 64 fixes a ratio, not a size (D2): with a few thousand
-- organizations each tenant is ~1.3% of its partition. Changing the modulus
-- rewrites every row. Revisit above ~1,000 organizations or a p95 we
-- cannot meet, and before the customer count gets there.
--
-- The ALTER TABLEs in this loop are what the seeded gate's tenant audit
-- watches. They validate no foreign key (a PARTITION OF inherits the
-- parent's keys as already-validated constraints), so nothing is flagged.
DO $$
DECLARE
  p TEXT;
BEGIN
  FOR i IN 0..63 LOOP
    p := 'chunks_p' || i;
    EXECUTE format(
      'CREATE TABLE public.%I PARTITION OF public.chunks FOR VALUES WITH (MODULUS 64, REMAINDER %s)',
      p, i);
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', p);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', p);
    EXECUTE format(
      $q$CREATE POLICY tenant_isolation ON public.%I
           FOR ALL
           USING (organization_id = current_setting('app.current_tenant', true)::uuid)$q$,
      p);
  END LOOP;
END $$;

-- And the parent, for queries made through it (which is every query the
-- application makes).
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
  FOR ALL
  USING (organization_id = current_setting('app.current_tenant', true)::uuid);

-- =====================================================================
-- 6. The tenant assertion (000009), cloned onto every partition
-- =====================================================================
--
-- A row trigger on a partitioned table is cloned to each partition
-- (measured: 64 of 64), so TG_TABLE_NAME in its message names the
-- partition, chunks_pNN, not chunks.
CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON chunks
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

-- =====================================================================
-- 7. Indexes, on the parent so that every partition carries them
-- =====================================================================

-- The vector index, local to each partition: the working set of a query
-- pruned to one partition is that partition's graph alone, which is the
-- index-size runway D2 keeps partitioning for. 22-03's vector leg passes
-- the query vector as a bound parameter (so the index is eligible) and
-- sets hnsw.iterative_scan, which the repository filter makes load-bearing
-- (22-RESEARCH.md Q5, P5).
CREATE INDEX idx_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_chunks_organization_id ON chunks (organization_id);
CREATE INDEX idx_chunks_repository_file_path ON chunks (repository_id, file_path);
CREATE INDEX idx_chunks_ingestion_run_id ON chunks (ingestion_run_id);
CREATE INDEX idx_chunks_content_hash ON chunks (content_hash);

-- Partial, so `ON DELETE SET NULL` from symbols finds the few chunks that
-- point at a symbol without scanning all 64 partitions per deleted symbol.
CREATE INDEX idx_chunks_symbol_id ON chunks (symbol_id) WHERE symbol_id IS NOT NULL;

-- 000006's keyword indexes. The breadcrumb one now indexes the expression
-- the keyword leg queries, COALESCE(breadcrumb, '')
-- (workers/retrieval/fts_retriever.py); 000006 indexed the bare column,
-- which that query cannot use. 22-03 owns the proof that this one matches,
-- with a plan shape measured to tell a match from a mismatch.
CREATE INDEX chunks_content_fts_idx ON chunks USING GIN (to_tsvector('english', content));
CREATE INDEX chunks_breadcrumb_fts_idx ON chunks USING GIN (to_tsvector('english', COALESCE(breadcrumb, '')));

-- =====================================================================
-- 8. What the tables are, and what a reader must know
-- =====================================================================
COMMENT ON TABLE chunks IS
  'Code chunks with their embeddings (D2). PARTITIONED BY HASH (organization_id), '
  'MODULUS 64, fixed: changing it rewrites every row, so revisit before ~1,000 '
  'organizations, not after. ROW-LEVEL SECURITY IS ENABLED AND FORCED ON EVERY '
  'PARTITION AS WELL AS THE PARENT (P2): the parent''s policy does not reach a '
  'partition addressed directly, and the app role read and overwrote another '
  'tenant''s row that way before this was fixed. The policy is the scalar '
  'organization_id = current_setting(''app.current_tenant'', true)::uuid, on every '
  'partition, so the planner prunes to one partition from the policy alone. '
  'organization_id IS SUPPLIED BY EVERY WRITER (a BEFORE trigger cannot route a row '
  'to another partition, 0A000) and guaranteed by chunks_repo_tenant_fk (P3). '
  'VECTORS LIVE HERE: embedding vector(1536) NOT NULL, and embedding_model records '
  'the model that produced it; a query must be embedded with the same model or not '
  'compared at all (P4). Created with NO DML (P1): the previous chunks table was '
  'dropped and its rows are re-ingested from source. '
  'FORBIDDEN: loading this table under session_replication_role = replica or with '
  'triggers disabled, which bypasses the tenant key (measured: a misfiled chunk '
  'inserts cleanly), and granting TRUNCATE on it or any partition to the app role, '
  'which row-level security does not govern. The drift query '
  '(isolation.CheckChunkTenantDrift) runs in CI to catch the day either is broken. '
  'Every partition has trg_assert_tenant (cloned from the parent).';

COMMENT ON TABLE symbols IS
  'Stable symbol identity (D1): one row per definition site, id = uuid_v5 over '
  '(repository_id, file_path, symbol_path, kind, ordinal), generated by the ingest '
  'from 22.1-01. UNPARTITIONED (P7): every foreign key into it stays single-column. '
  'ARCHIVED, NEVER DELETED, by ingest: a symbol that vanishes gets archived_at, and '
  'one that returns is resurrected by INSERT ... ON CONFLICT (id) DO UPDATE SET '
  'archived_at = NULL. Graph queries must exclude archived rows. Row-level security '
  'is enabled and forced with the scalar tenant policy; organization_id is supplied '
  'by every writer and guaranteed by symbols_repo_tenant_fk (P3). '
  'FORBIDDEN: loading under session_replication_role = replica or with triggers '
  'disabled (bypasses the key), and granting TRUNCATE to the app role.';

COMMENT ON COLUMN chunks.organization_id IS
  'The partition key and the policy''s column. Copy of repositories.organization_id, '
  'guaranteed by chunks_repo_tenant_fk while foreign-key triggers are enabled, and '
  'supplied by every writer: no trigger fills it (0A000 on a partitioned table). '
  'An AUTHORIZATION INPUT: the row-level-security policy reads it.';

COMMENT ON COLUMN chunks.embedding_model IS
  'The embedding model that produced `embedding` (P4). A query vector from another '
  'model must never be compared against this row: same dimension, different space.';

COMMENT ON COLUMN chunks.symbol_id IS
  'The one symbol this chunk belongs to, or NULL (P7). ON DELETE SET NULL; '
  'idx_chunks_symbol_id is partial so that update does not scan every partition.';

COMMENT ON COLUMN retrievals.chunk_id IS
  'The chunk that was shown, by id, WITHOUT a foreign key since 000017 (P17, U9): '
  'chunks is partitioned by organization and retrievals carries no organization_id, '
  'so the key cannot exist. A row may point at a chunk that a re-index or a repository '
  'delete has since removed. The shape of this link is decided when feedback ships.';
