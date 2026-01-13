-- Enable RLS on all tenant-scoped tables
ALTER TABLE repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrievals ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;

-- Force RLS (CRITICAL: apply to table owner too, not just roles)
ALTER TABLE repositories FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE queries FORCE ROW LEVEL SECURITY;
ALTER TABLE retrievals FORCE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;

-- Tenant isolation policy for repositories
-- Checks that repository belongs to a project in the current tenant's organization
CREATE POLICY tenant_isolation ON repositories
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM projects
      WHERE projects.id = repositories.project_id
      AND projects.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );

-- Tenant isolation policy for ingestion_runs
-- Checks that ingestion_run's repository belongs to current tenant
CREATE POLICY tenant_isolation ON ingestion_runs
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM repositories r
      JOIN projects p ON r.project_id = p.id
      WHERE r.id = ingestion_runs.repository_id
      AND p.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );

-- Tenant isolation policy for chunks
-- Checks that chunk's repository belongs to current tenant
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

-- Tenant isolation policy for queries
-- Checks that query's project belongs to current tenant
CREATE POLICY tenant_isolation ON queries
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM projects
      WHERE projects.id = queries.project_id
      AND projects.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );

-- Tenant isolation policy for retrievals
-- Checks that retrieval's query belongs to current tenant (via query's project)
CREATE POLICY tenant_isolation ON retrievals
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM queries q
      JOIN projects p ON q.project_id = p.id
      WHERE q.id = retrievals.query_id
      AND p.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );

-- Tenant isolation policy for feedback
-- Checks that feedback's retrieval belongs to current tenant (via retrieval → query → project)
CREATE POLICY tenant_isolation ON feedback
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM retrievals r
      JOIN queries q ON r.query_id = q.id
      JOIN projects p ON q.project_id = p.id
      WHERE r.id = feedback.retrieval_id
      AND p.organization_id = current_setting('app.current_tenant', true)::uuid
    )
  );
