-- Reverse of 000010. Written to be run, not to look complete — apply,
-- roll back, re-apply is part of this migration's verification.
--
-- Order matters: drop what depends on github_installations before the
-- table itself.

DROP TRIGGER IF EXISTS trg_assert_installation_tenant ON repositories;
DROP FUNCTION IF EXISTS assert_installation_matches_repository_tenant();

DROP INDEX IF EXISTS idx_repositories_sync_state;
DROP INDEX IF EXISTS idx_repositories_installation_github_id;

ALTER TABLE repositories
  DROP COLUMN IF EXISTS last_synced_at,
  DROP COLUMN IF EXISTS sync_state,
  DROP COLUMN IF EXISTS archived,
  DROP COLUMN IF EXISTS size_kb,
  DROP COLUMN IF EXISTS visibility,
  DROP COLUMN IF EXISTS github_repo_id,
  DROP COLUMN IF EXISTS installation_id;

DROP TRIGGER IF EXISTS trg_assert_tenant ON github_installations;
DROP POLICY IF EXISTS tenant_isolation ON github_installations;
DROP INDEX IF EXISTS idx_github_installations_org_id;
DROP TABLE IF EXISTS github_installations;

-- The default-project backfill is NOT undone.
--
-- Deliberate. Rolling back this migration drops the `is_default` flag,
-- but deleting the projects themselves would cascade to any repositories
-- created under them — destroying user data to reverse a schema change.
-- A stray project with slug 'default' is harmless; a deleted repository
-- is not.
DROP INDEX IF EXISTS idx_projects_one_default_per_org;

ALTER TABLE projects
  DROP COLUMN IF EXISTS is_default;
