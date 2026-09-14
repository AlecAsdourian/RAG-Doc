-- Phase 21-01: repositories.organization_id, stored, and unable to drift.
--
-- WHY A STORED COLUMN. `repositories` reaches its tenant through
-- `project_id -> projects.organization_id`. 21-02's `ingestion_jobs` needs a
-- composite foreign key `(repository_id, organization_id) REFERENCES
-- repositories (id, organization_id)`, so that a job filed under the wrong
-- tenant is UNREPRESENTABLE rather than merely rejected (21-CONTEXT L2, L5).
-- A foreign key can only reference columns, so the tenant has to be one.
--
-- WHY IT CANNOT DRIFT. A copy of a value two tables away can disagree with
-- it; DECISIONS.md D5 exists because of that. This migration guarantees the
-- copy with a COMPOSITE FOREIGN KEY to `projects (id, organization_id)`,
-- which is stronger than the trigger D5's wording asks for:
--
--   - Foreign-key checks run with row-level security bypassed, so the key
--     holds even where a trigger's read would be filtered.
--   - It makes a project's organization unchangeable while the project has
--     repositories. Nothing changes it today: the only writer of
--     `projects.organization_id` was 000010's one-time backfill.
--
-- The triggers below stay, for what a key cannot do: fill the column on
-- insert, so no writer has to supply it, and give readable errors. D5's
-- `reject_cross_org_reparent` is transcribed verbatim.
--
-- NO GRANTs, deliberately: `rag_doc_app` is a test-harness role (000010).

-- =====================================================================
-- 1. The column, nullable until the backfill has filled it
-- =====================================================================
ALTER TABLE repositories ADD COLUMN organization_id UUID;

-- =====================================================================
-- 2. The key the composite foreign key references
-- =====================================================================
--
-- `projects.id` is already unique, so this adds no constraint on the data;
-- a foreign key can only reference a unique column set, and this is that set.
ALTER TABLE projects
  ADD CONSTRAINT projects_id_org_key UNIQUE (id, organization_id);

-- =====================================================================
-- 3. Backfill, one organization at a time
-- =====================================================================
--
-- WHY ONE ORGANIZATION AT A TIME. Two guards stand between a migration and
-- these rows, and this block satisfies both rather than lifting either:
--
--   - `trg_assert_tenant` (000009) refuses ANY write to `repositories` while
--     `app.current_tenant` is unset or '' — superusers included, because a
--     trigger is not row-level security. Lifting FORCE does nothing to it.
--   - `FORCE ROW LEVEL SECURITY` (000008) filters the table owner too. In
--     the deployment shape, where the application role owns the tables,
--     an unscoped UPDATE matches zero rows and reports success.
--
-- Setting the tenant to each organization in turn satisfies both. No guard
-- is lifted, disabled or bypassed.
--
-- WHY 000012'S PATTERN DOES NOT APPLY. 000012 lifted FORCE for one
-- statement and wrote into `github_installation_tenants`, a table with no
-- tenant trigger. Copied here it passes on an empty database — CI's, and
-- the harnesses' — and raises 42501 on any database with a repository in it.
--
-- WHAT IT LEAVES BEHIND (ISS-013). `set_config(..., true)` lasts until the
-- end of this migration's transaction, so for the rest of this file the
-- tenant is the LAST organization's id: DML after this block against a
-- row-level-security table would silently see one organization. After
-- commit the session holds '', which nothing can return to NULL, and
-- `current_setting(...)::uuid` then raises 22P02. So:
--
--   - nothing after this block, in this file, may run DML against a table
--     with row-level security. DDL is fine: the validation scans below are
--     not subject to it.
--   - a LATER migration applied in the same run must set a tenant itself
--     before it touches a row-level-security table.
DO $$
DECLARE org RECORD;
BEGIN
  FOR org IN SELECT id FROM public.organizations LOOP
    PERFORM set_config('app.current_tenant', org.id::text, true);
    UPDATE public.repositories r
    SET organization_id = p.organization_id
    FROM public.projects p
    WHERE p.id = r.project_id
      AND p.organization_id = org.id
      AND r.organization_id IS NULL;
  END LOOP;
END $$;

-- =====================================================================
-- 4. The proof that the backfill filled every row
-- =====================================================================
--
-- THIS STATEMENT IS THE PROOF. `SET NOT NULL` validates by scanning the
-- whole table, and that scan is not subject to row-level security: a row
-- the loop above missed fails the migration here.
--
-- A `SELECT count(*) ... WHERE organization_id IS NULL` check would prove
-- nothing. Under FORCE ROW LEVEL SECURITY it sees only the tenant still set
-- by the loop — or, with no tenant, nothing at all — and passes.
--
-- Measured on seeded data rather than assumed; see 21-01-SUMMARY.md.
ALTER TABLE repositories ALTER COLUMN organization_id SET NOT NULL;

-- =====================================================================
-- 5. The guarantee
-- =====================================================================
--
-- THIS KEY, NOT A TRIGGER, IS WHAT MAKES DRIFT UNREPRESENTABLE. A
-- repository's `(project_id, organization_id)` must be a real
-- `(id, organization_id)` pair in `projects`. Foreign-key checks run with
-- row-level security bypassed, so no tenant context and no filtered read can
-- let a mismatched pair through, and a trigger that is disabled, dropped or
-- buggy leaves the guarantee standing.
--
-- It also fixes a project's organization while the project has
-- repositories: `UPDATE projects SET organization_id` fails with 23503.
--
-- The single-column `project_id` foreign key from 000001 is kept, not
-- replaced. It is no longer usually the error a writer sees for a missing
-- project: RLS, NOT NULL or D5's trigger speaks first (see sections 7 and
-- 8), and this key stays as the backstop beneath them.
ALTER TABLE repositories
  ADD CONSTRAINT repositories_project_org_fkey
  FOREIGN KEY (project_id, organization_id)
  REFERENCES projects (id, organization_id) ON DELETE CASCADE;

-- =====================================================================
-- 6. The key 21-02's composite foreign key references
-- =====================================================================
ALTER TABLE repositories
  ADD CONSTRAINT repositories_id_org_key UNIQUE (id, organization_id);

-- =====================================================================
-- 7. Fill on insert; refuse a rewrite
-- =====================================================================
--
-- Every existing writer — `RepositoriesHandler.Connect`, the fixtures in
-- both harnesses, seed.sql, the quality harness — inserts without naming
-- this column. The trigger fills it from the project, so none of them
-- changes.
--
-- `projects` has no row-level security and no tenant trigger (000008 and
-- 000009 leave it out), so this read always sees the project.
--
-- `search_path` pinned and the body schema-qualified, following
-- `sync_github_installation_tenant` (000012): an unqualified name is
-- resolvable through a caller's temp schema.
--
-- Branches on TG_OP explicitly. OLD is not a row in an INSERT trigger, and
-- D5 records that bug being shipped once.
CREATE OR REPLACE FUNCTION repositories_organization_id_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
  project_org UUID;
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT p.organization_id INTO project_org
    FROM public.projects p
    WHERE p.id = NEW.project_id;

    -- No such project. Leave the column NULL and raise nothing, so this
    -- trigger says nothing about which project ids exist. What refuses the
    -- row then depends on the writer (measured on PostgreSQL 16):
    --
    --   - a role row-level security applies to: RLS's WITH CHECK, 42501,
    --     exactly as before this migration. A project that exists in
    --     ANOTHER organization gets the identical error, so there is no
    --     existence oracle here.
    --   - a role that bypasses RLS: NOT NULL on this column, 23502. Before
    --     this migration it was the `project_id` foreign key, 23503; NOT
    --     NULL is checked as the row is written, foreign keys at the end of
    --     the statement.
    IF NOT FOUND THEN
      RETURN NEW;
    END IF;

    -- The normal case: the writer did not name the column.
    IF NEW.organization_id IS NULL THEN
      NEW.organization_id := project_org;
      RETURN NEW;
    END IF;

    -- The writer named it and got it wrong. That is a bug worth surfacing,
    -- not correcting. Without this the composite foreign key still refuses
    -- the row, but its error names a constraint rather than the problem.
    IF NEW.organization_id IS DISTINCT FROM project_org THEN
      RAISE EXCEPTION
        'organization_id % does not match project %, which belongs to organization %',
        NEW.organization_id, NEW.project_id, project_org
        USING ERRCODE = '42501',
              HINT = 'Omit organization_id; the database fills it from the project.';
    END IF;

    RETURN NEW;
  END IF;

  -- UPDATE OF organization_id.
  IF NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
    RAISE EXCEPTION
      'organization_id is maintained by the database; move the repository''s project instead'
      USING ERRCODE = '42501';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_repositories_organization_id
  BEFORE INSERT OR UPDATE OF organization_id ON repositories
  FOR EACH ROW EXECUTE FUNCTION repositories_organization_id_guard();

-- =====================================================================
-- 8. D5: cross-organization re-parenting is forbidden
-- =====================================================================
--
-- Transcribed verbatim from DECISIONS.md D5. What it adds here:
--
--   - a cross-organization re-parent raises THIS readable error first.
--     Without it, RLS's WITH CHECK refuses the row, and failing that the
--     composite foreign key does.
--   - a same-organization re-parent carries `organization_id` over
--     unchanged, and the composite foreign key accepts the new pair.
--
-- ONE CORRECTION TO D5's PROSE, not to its code. D5 says a move to a project
-- that does not exist "yields NULL on both sides of the comparison", leaving
-- the `project_id` foreign key to reject it. Only the NEW side is NULL, since
-- the repository's current project exists, so `IS DISTINCT FROM` is true
-- and this trigger raises its own "across organisations" message. Measured
-- on PostgreSQL 16; the move is refused either way, and the function is
-- left verbatim.
CREATE OR REPLACE FUNCTION reject_cross_org_reparent() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  -- ⚠ UPDATE ONLY. An earlier revision shipped this function with no
  -- CREATE TRIGGER at all, and the natural attachment -- BEFORE INSERT OR
  -- UPDATE -- makes EVERY INSERT fail, because OLD is not defined in a
  -- BEFORE INSERT trigger. Attached for DELETE it breaks every delete.
  -- Measured in the third review. The guard and the attachment below are
  -- both load-bearing.
  IF TG_OP <> 'UPDATE' THEN
    RETURN NEW;
  END IF;

  IF NEW.project_id IS NOT DISTINCT FROM OLD.project_id THEN
    RETURN NEW;                     -- not a re-parent at all
  END IF;

  IF (SELECT organization_id FROM public.projects WHERE id = NEW.project_id)
     IS DISTINCT FROM
     (SELECT organization_id FROM public.projects WHERE id = OLD.project_id) THEN
    RAISE EXCEPTION
      'cannot move repository % across organisations; export and re-ingest instead',
      OLD.id;
  END IF;
  RETURN NEW;
END; $$;

CREATE TRIGGER trg_reject_cross_org_reparent
  BEFORE UPDATE OF project_id ON repositories
  FOR EACH ROW EXECUTE FUNCTION reject_cross_org_reparent();

-- =====================================================================
-- 9. What the column is
-- =====================================================================
COMMENT ON COLUMN repositories.organization_id IS
  'Copy of projects.organization_id, guaranteed by repositories_project_org_fkey '
  'and filled by trg_repositories_organization_id (DECISIONS.md D5). An '
  'AUTHORIZATION INPUT: 21-02''s composite foreign key on ingestion_jobs and the '
  'worker''s tenant scope rely on it. Application code never writes it.';
