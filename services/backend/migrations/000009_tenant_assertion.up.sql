-- Phase 17-03: DB-level tenant assertion trigger.
--
-- Purpose: second wall of the tenant-isolation defense. Migration 000008
-- put RLS on 6 tenant-scoped tables (repositories, ingestion_runs, chunks,
-- queries, retrievals, feedback), which silently filters SELECTs to the
-- current tenant. RLS does NOT catch a raw INSERT/UPDATE/DELETE that never
-- set app.current_tenant — it just runs, and if the caller happens to
-- omit the tenant filter in a WHERE clause, the write can leak. This
-- trigger closes that gap: any mutation on a tenant-scoped table without
-- app.current_tenant set is refused loudly with SQLSTATE 42501.
--
-- Coverage decision (Phase 17-03 plan drift):
-- The 17-03-PLAN listed 9 tables — the 6 above plus organizations,
-- projects, and organization_memberships. Attaching the trigger to those
-- three creates a chicken-and-egg with signup: a new user signing up
-- creates an organization before any tenant exists to scope the write to,
-- and the fixture WithTwoOrgs (which Task 2 scenario 6 requires to still
-- work) inserts organizations/projects/memberships without a preceding
-- SET LOCAL. Fixing that would drag production auth-provisioning code
-- (pkg/auth/provisioning.go, webhook_handler.go) into this migration's
-- scope. The chosen resolution — Option A of three presented to the
-- planner — attaches the trigger only to the 6 tables migration 000008
-- already covers with RLS. Organizations, projects, memberships, and
-- users stay exempt. See .planning/phases/17-tenant-isolation-foundation/
-- 17-03-SUMMARY.md for full rationale.
--
-- How to add a new tenant-scoped table later (v2 graph tables, memory
-- records, etc.):
--   CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE
--     ON <new_table> FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();
-- Also add the table to the table-driven test list in
-- pkg/testing/isolation/db_assertion_test.go so a missing trigger fails
-- CI instead of silently leaking.
--
-- Why users / organizations / projects / memberships are NOT covered:
--   - users: created during signup before any org exists.
--   - organizations: same bootstrap problem — the org IS the tenant, it
--     can't reference itself before it's inserted.
--   - projects, organization_memberships: could reference a tenant, but
--     including them here would require every signup/provisioning path
--     to wrap its writes in SET LOCAL — out of scope for this migration.
--     If a future phase wants to extend coverage, that phase attaches
--     the trigger and updates the affected provisioning code as one unit.

CREATE OR REPLACE FUNCTION assert_tenant_scoped() RETURNS trigger AS $$
BEGIN
  -- The `true` second arg means "return NULL if unset" instead of raising
  -- a generic error — that lets us raise a clearer, actionable message.
  IF current_setting('app.current_tenant', true) IS NULL
     OR current_setting('app.current_tenant', true) = '' THEN
    RAISE EXCEPTION 'tenant isolation violated: app.current_tenant must be set for % on %',
      TG_OP, TG_TABLE_NAME
      USING HINT = 'Call TenantScope() (Go) or require_tenant() (Python) before this operation. See docs/isolation.md.',
            ERRCODE = '42501';
  END IF;
  -- BEFORE trigger convention: return NEW for INSERT/UPDATE, OLD for
  -- DELETE. COALESCE handles both cases in one line.
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION assert_tenant_scoped() IS
  'Second wall of tenant isolation. Fires BEFORE INSERT/UPDATE/DELETE on tenant-scoped tables. Middleware sets app.current_tenant; this function ensures no code path bypasses that requirement. Coverage: repositories, ingestion_runs, chunks, queries, retrievals, feedback. See migration 000009 header for the coverage-decision rationale.';

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON repositories
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON ingestion_runs
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON chunks
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON queries
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON retrievals
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();

CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE ON feedback
  FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();
