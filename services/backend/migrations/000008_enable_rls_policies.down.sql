-- Drop all tenant isolation policies
DROP POLICY IF EXISTS tenant_isolation ON feedback;
DROP POLICY IF EXISTS tenant_isolation ON retrievals;
DROP POLICY IF EXISTS tenant_isolation ON queries;
DROP POLICY IF EXISTS tenant_isolation ON chunks;
DROP POLICY IF EXISTS tenant_isolation ON ingestion_runs;
DROP POLICY IF EXISTS tenant_isolation ON repositories;

-- Disable RLS on all tables
ALTER TABLE feedback DISABLE ROW LEVEL SECURITY;
ALTER TABLE retrievals DISABLE ROW LEVEL SECURITY;
ALTER TABLE queries DISABLE ROW LEVEL SECURITY;
ALTER TABLE chunks DISABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs DISABLE ROW LEVEL SECURITY;
ALTER TABLE repositories DISABLE ROW LEVEL SECURITY;
