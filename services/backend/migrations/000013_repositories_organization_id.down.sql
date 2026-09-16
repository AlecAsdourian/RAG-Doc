-- Reverse of 000013, in reverse order.
--
-- Nothing is lost: `repositories.organization_id` was a copy of
-- `projects.organization_id`, and tenancy goes back to being derived
-- through the join, which the 000008 policies never stopped using.
--
-- 21-02's `ingestion_jobs` references `repositories_id_org_key`. Its down
-- migration runs before this one, so the key is free to drop by then.

DROP TRIGGER IF EXISTS trg_reject_cross_org_reparent ON repositories;
DROP FUNCTION IF EXISTS reject_cross_org_reparent();

DROP TRIGGER IF EXISTS trg_repositories_organization_id ON repositories;
DROP FUNCTION IF EXISTS repositories_organization_id_guard();

ALTER TABLE repositories DROP CONSTRAINT IF EXISTS repositories_id_org_key;

-- Before `projects_id_org_key`, which this foreign key depends on.
ALTER TABLE repositories DROP CONSTRAINT IF EXISTS repositories_project_org_fkey;

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_id_org_key;

ALTER TABLE repositories DROP COLUMN IF EXISTS organization_id;
