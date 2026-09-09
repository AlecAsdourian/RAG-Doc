-- Phase 20-02: GitHub App integration.
--
-- Three things:
--   1. Every organization gets exactly one default project, because
--      `repositories.project_id` is NOT NULL and nothing in production
--      has ever created a project.
--   2. `github_installations` — tenant-scoped, RLS + trigger.
--   3. New columns on `repositories`, shaped by what GitHub actually
--      sends (verified against the live App 2026-09-08; see 20-02-PLAN).

-- =====================================================================
-- 1. Default project per organization
-- =====================================================================
--
-- The problem: `repositories.project_id` is NOT NULL and references
-- `projects`, but the only code that has ever inserted a project is a
-- test helper. A user who signs up today gets an organization and no
-- project, so `POST /api/repositories` would have nothing to point at.
--
-- Two ways out were considered:
--
--   A. give every organization a default project  (chosen)
--   B. re-parent `repositories` to organizations directly
--
-- B is the tempting one — "connect a repo to my organization" is the
-- product concept, and the project layer is unused. It was rejected
-- because the tenancy machinery is built on the current shape: the RLS
-- policies in 000008 reach the tenant via
-- `repositories -> projects -> organization_id`, and so do the policies
-- for `chunks` and `ingestion_runs`, which join through `repositories`.
-- Re-parenting means rewriting those policies and the 000009 trigger's
-- tenant derivation in the same migration that introduces a new table —
-- changing the isolation boundary and adding to it at the same time.
--
-- A keeps the boundary untouched, is reversible, and does not discard a
-- grouping layer the product may still want. If projects turn out to be
-- genuinely unwanted, B remains available later as a migration that
-- changes one thing.

ALTER TABLE projects
  ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT false;

-- Exactly one default per organization, enforced by the database rather
-- than by convention. Same reasoning as the UNIQUE on
-- github_installations below: if code has to pick "the" default project,
-- the schema should guarantee there is only one to pick.
CREATE UNIQUE INDEX idx_projects_one_default_per_org
  ON projects (organization_id)
  WHERE is_default;

-- Backfill. Every existing organization without any project gets one.
-- `Default` / `default` matches what CreateOrganizationForUser now
-- creates for new organizations.
INSERT INTO projects (organization_id, name, slug, is_default)
SELECT o.id, 'Default', 'default', true
FROM organizations o
WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.organization_id = o.id);

-- An organization that already had projects (only fixtures today) gets
-- its oldest marked as the default, so the invariant "every org has
-- exactly one default" holds for every row, not just the new ones.
UPDATE projects p
SET is_default = true
WHERE p.id = (
  SELECT p2.id FROM projects p2
  WHERE p2.organization_id = p.organization_id
  ORDER BY p2.created_at ASC, p2.id ASC
  LIMIT 1
)
AND NOT EXISTS (
  SELECT 1 FROM projects d
  WHERE d.organization_id = p.organization_id AND d.is_default
);

-- =====================================================================
-- 2. github_installations
-- =====================================================================
--
-- An installation is an ORGANIZATION-level fact, not a repository-level
-- one. The roadmap originally put `github_installation_id` on
-- `repositories`; denormalizing it onto every repository row invites the
-- two to disagree about which installation owns what.
CREATE TABLE github_installations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

  -- THIS UNIQUE IS THE TENANCY BOUNDARY, not a dedup convenience.
  --
  -- It is what enforces "one installation serves exactly one
  -- organization". Without it, two tenants could both claim the same
  -- GitHub installation and a repository's owner would become ambiguous.
  -- An organization MAY hold several installations (someone connecting
  -- two different GitHub accounts); an installation never fans out.
  github_installation_id BIGINT NOT NULL UNIQUE,

  -- Verified 2026-09-08: account.type is 'User' for the dev installation,
  -- and 'Organization' for org-owned ones. Both occur — deliberately no
  -- CHECK constraint assuming one.
  account_login TEXT NOT NULL,
  account_type TEXT NOT NULL,

  -- 'all' or 'selected'.
  repository_selection TEXT NOT NULL,

  -- Set when GitHub sends installation.suspend; cleared on unsuspend.
  suspended_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_github_installations_org_id
  ON github_installations(organization_id);

-- RLS, matching the 000008 pattern. This table is tenant-scoped, so it
-- gets the same treatment as every other tenant-scoped table.
--
-- The CI scanner checks endpoints, not tables — it would not have caught
-- this being skipped. docs/isolation.md is the rule; this is it applied.
ALTER TABLE github_installations ENABLE ROW LEVEL SECURITY;
ALTER TABLE github_installations FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON github_installations
  FOR ALL
  USING (organization_id = current_setting('app.current_tenant', true)::uuid)
  WITH CHECK (organization_id = current_setting('app.current_tenant', true)::uuid);

-- The 000009 trigger: refuse any write that arrives without a tenant, so
-- a caller who bypasses the scope gets SQLSTATE 42501 rather than a
-- silently filtered no-op.
CREATE TRIGGER trg_assert_tenant
  BEFORE INSERT OR UPDATE OR DELETE ON github_installations
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

-- NO GRANT HERE. Deliberate, and it was a bug in the first draft.
--
-- `rag_doc_app` is a TEST-HARNESS role. It does not exist in production,
-- and in the harness it is created by ensureAppRole which runs AFTER
-- migrations — so `GRANT ... TO rag_doc_app` in this file fails with
-- "role does not exist", leaves migration 10 dirty, and every isolation
-- test then fails at container setup with a message about a dirty
-- database rather than about the grant.
--
-- It passed a scratch-database check only because that database had the
-- role pre-created by hand. The testcontainer, which does not, is what
-- caught it.
--
-- The harness's own `GRANT ... ON ALL TABLES IN SCHEMA public` covers
-- this table automatically. A migration should not know about a role that
-- only exists in tests.

-- =====================================================================
-- 3. repositories: GitHub-sourced columns
-- =====================================================================
--
-- All nullable: existing rows (fixtures) predate GitHub integration and
-- must not be invalidated by this migration.
ALTER TABLE repositories
  -- ON DELETE SET NULL, not CASCADE. Uninstalling the App means we lost
  -- access to the code; it does not mean the user asked us to delete
  -- everything we ingested from it. Keep the rows, mark them unsyncable.
  ADD COLUMN installation_id UUID REFERENCES github_installations(id) ON DELETE SET NULL,

  -- GitHub's numeric repository id. THE durable key — stable across
  -- renames and transfers, unlike full_name.
  ADD COLUMN github_repo_id BIGINT,

  ADD COLUMN visibility TEXT,

  -- KILOBYTES, and named so.
  --
  -- Verified 2026-09-08: GitHub reported size=75 for a real repository.
  -- 75 bytes is not a possible git repo; 75 KB is. The plan originally
  -- specified `size_bytes`, which would have under-reported every
  -- repository by ~1000x with nothing ever erroring.
  ADD COLUMN size_kb BIGINT,

  ADD COLUMN archived BOOLEAN NOT NULL DEFAULT false,

  -- never_synced -> pending -> syncing -> synced | failed
  -- Phase 21 owns the queue that moves these along; Phase 20 only ever
  -- records intent.
  ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'never_synced',
  ADD COLUMN last_synced_at TIMESTAMPTZ;

-- A repository appears at most once per installation. Partial, because
-- existing fixture rows have a NULL github_repo_id and several may share
-- a NULL installation_id.
CREATE UNIQUE INDEX idx_repositories_installation_github_id
  ON repositories (installation_id, github_repo_id)
  WHERE installation_id IS NOT NULL AND github_repo_id IS NOT NULL;

CREATE INDEX idx_repositories_sync_state
  ON repositories (sync_state)
  WHERE sync_state <> 'synced';
