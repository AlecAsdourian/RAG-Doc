-- Phase 20-05: GitHub webhook delivery log, for idempotency.
--
-- NOTE ON THE NUMBER. The plan called this 000011; that number was taken
-- by 20-03's `repository_identity`, which was written after the plan.
-- Renumbered rather than renamed, because golang-migrate keys on the
-- integer and a duplicate is a hard error at startup.

-- =====================================================================
-- github_webhook_deliveries
-- =====================================================================
--
-- GitHub redelivers on failure, and the **Redeliver** button in the App's
-- Advanced settings is a normal part of development — so a duplicate
-- delivery is routine rather than exceptional, and "process it twice" has
-- to be impossible rather than unlikely.
--
-- `delivery_id` is GitHub's `X-GitHub-Delivery` header, a UUID (verified
-- 2026-09-08 against real deliveries: `ebf306b0-ac06-11f1-997e-...`). It
-- is stable across redeliveries of the same event, which is exactly what
-- makes it usable as an idempotency key.
--
-- THIS TABLE IS DELIBERATELY NOT TENANT-SCOPED, and that is not an
-- oversight — every other table this phase touches carries RLS.
--
-- Two reasons it cannot be:
--
--   1. A delivery arrives BEFORE we know which tenant it belongs to. The
--      idempotency check has to happen first, or a redelivery that failed
--      tenant resolution would be reprocessed forever.
--   2. Some events concern a tenant that is going away. `installation`
--      `deleted` is the clearest case: scoping its own delivery record to
--      the organization being disconnected would make the record
--      unreadable at exactly the moment it matters.
--
-- `organization_id` is therefore a nullable ANNOTATION — filled in when
-- the handler works out which tenant an event belonged to, useful for
-- support and for tracing, and never used to authorize anything.
CREATE TABLE github_webhook_deliveries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- The idempotency key. UNIQUE is the whole point of the table.
  delivery_id TEXT NOT NULL UNIQUE,

  event TEXT NOT NULL,
  action TEXT,

  -- GitHub's numeric installation id, when the payload carries one.
  -- `ping` does not.
  github_installation_id BIGINT,

  -- Annotation only. See the note above: never an authorization input.
  organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,

  -- What we did with it, for support questions of the form "I pushed and
  -- nothing happened".
  outcome TEXT NOT NULL,

  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_github_webhook_deliveries_received_at
  ON github_webhook_deliveries (received_at DESC);

CREATE INDEX idx_github_webhook_deliveries_installation
  ON github_webhook_deliveries (github_installation_id)
  WHERE github_installation_id IS NOT NULL;

-- RETENTION. This table grows forever and nothing prunes it.
--
-- One row per delivery, and a busy installation pushes often, so this is
-- the fastest-growing table in the schema by a wide margin. It is small
-- per row and has no dependants, so deletion is safe:
--
--   DELETE FROM github_webhook_deliveries WHERE received_at < NOW() - INTERVAL '30 days';
--
-- 30 days comfortably exceeds GitHub's own redelivery window, so pruning
-- older rows cannot resurrect a duplicate. Phase 24 owns scheduling it;
-- until then it is a manual operation, and this comment is the only thing
-- that will remind anyone.

-- =====================================================================
-- Installation lifecycle columns
-- =====================================================================
--
-- `suspended_at` ALREADY EXISTS — 000010 line 109 added it, for exactly
-- the `suspend` / `unsuspend` events this plan handles. Only the
-- uninstall marker is new. (An earlier draft of this migration re-added
-- `suspended_at` with IF NOT EXISTS, which is a harmless no-op forward
-- and a schema-breaking DROP backward: the down file would have removed
-- a column 000010 owns.)
ALTER TABLE github_installations
  -- Set when `installation.deleted` arrives. The row is KEPT rather than
  -- deleted: an uninstall means access was lost, not that the user asked
  -- us to forget what we ingested. `repositories.installation_id` is
  -- ON DELETE SET NULL (000010), so deleting the row here would silently
  -- orphan every repository under it.
  ADD COLUMN IF NOT EXISTS uninstalled_at TIMESTAMPTZ;

COMMENT ON COLUMN github_installations.uninstalled_at IS
  'Set by the installation.deleted webhook. The row is retained so the '
  'repositories under it keep their link and their ingested history; a '
  'reinstall relinks via POST /api/repositories.';

-- =====================================================================
-- Tenant discovery for the webhook
-- =====================================================================
--
-- THE PROBLEM. `github_installations` is FORCE ROW LEVEL SECURITY
-- (000010), so every read is filtered by `app.current_tenant`. A webhook
-- has no tenant: GitHub sends a numeric installation id and the whole
-- point is to discover WHICH tenant it belongs to. The lookup therefore
-- cannot itself be tenant-scoped, because scoping it by the answer is
-- circular.
--
-- WHAT WAS TRIED FIRST, AND WHY IT WAS WRONG. The first version of this
-- migration used a SECURITY DEFINER function. That was verified — in the
-- TEST HARNESS, where migrations run as a superuser and the application
-- connects as a separate non-superuser role, so the function inherited a
-- privilege the caller lacked.
--
-- `FORCE ROW LEVEL SECURITY` applies policies to the TABLE OWNER TOO, and
-- SECURITY DEFINER only switches `current_user` to the function's owner.
-- So in the deployment shape this repo actually documents — where the
-- application connects as the role that owns the tables — the function is
-- filtered exactly like a direct SELECT and returns **zero rows**. The
-- receiver would then answer 202 to every event while doing nothing at
-- all: no error, no warning, a plausible-looking outcome in the delivery
-- log, and nothing in the test suite able to see it.
--
-- Reproduced on a database owned by a NOSUPERUSER NOBYPASSRLS role:
-- direct SELECT → 0 rows, and the SECURITY DEFINER function → 0 rows.
--
-- THE FIX IS TO STOP DEPENDING ON PRIVILEGES. `github_installation_tenants`
-- is an ordinary table with NO row-level security, holding only the
-- mapping the webhook needs to discover a tenant, and maintained by a
-- trigger so it cannot drift from the table it mirrors.
--
-- It behaves identically whoever owns it and whoever connects, which is
-- the property the function did not have — and it is also STRICTLY LESS
-- EXPOSED than the function was: EXECUTE on a function defaults to
-- PUBLIC, so any database role at all could call the old one and
-- enumerate the whole installation-to-organization map by guessing small
-- sequential ids. A table answers to normal grants instead.
--
-- Same reasoning as `github_webhook_deliveries` above: this is discovery
-- data consulted BEFORE a tenant is known, not tenant data.
CREATE TABLE github_installation_tenants (
  github_installation_id BIGINT PRIMARY KEY,
  -- UNIQUE, so an installation can hold at most ONE mapping and a stale
  -- key cannot coexist with its replacement. The trigger below deletes
  -- the old row on a re-key; this constraint is what makes the drift
  -- unrepresentable rather than merely unlikely.
  installation_id UUID NOT NULL UNIQUE REFERENCES github_installations(id) ON DELETE CASCADE,
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE
);

COMMENT ON TABLE github_installation_tenants IS
  'Discovery index for the webhook receiver: GitHub installation id -> tenant. '
  'Deliberately has no RLS, because it is consulted before the tenant is known. '
  'Maintained by trg_sync_github_installation_tenant; never written directly.';

-- Kept in step by a trigger rather than by application code.
--
-- `search_path` is pinned. The function this replaced was SECURITY
-- DEFINER and pinned it for the classic reason; dropping the pin along
-- with the definer was a mistake, because an unqualified write is still
-- resolvable through a caller's temp schema. Measured: with a TEMP table
-- named `github_installation_tenants` carrying a matching primary key,
-- the parent INSERT succeeded and the mirror write landed in the temp
-- table — leaving an installation that exists and can never be resolved.
CREATE OR REPLACE FUNCTION sync_github_installation_tenant()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    DELETE FROM public.github_installation_tenants WHERE installation_id = OLD.id;
    RETURN OLD;
  END IF;

  -- Drop any mapping this installation used to have under a DIFFERENT
  -- GitHub id. Without this, re-keying an installation left the old id
  -- pointing at it forever: the freed id kept routing to this tenant, so
  -- a later, legitimately-signed event for whoever GitHub next issued it
  -- to would be applied inside the wrong organization. No code path
  -- re-keys today; "maintained by a trigger so it cannot drift" was still
  -- an overstatement until this line existed.
  DELETE FROM public.github_installation_tenants
  WHERE installation_id = NEW.id
    AND github_installation_id <> NEW.github_installation_id;

  INSERT INTO public.github_installation_tenants
    (github_installation_id, installation_id, organization_id)
  VALUES (NEW.github_installation_id, NEW.id, NEW.organization_id)
  ON CONFLICT (github_installation_id) DO UPDATE
    SET installation_id = EXCLUDED.installation_id,
        organization_id = EXCLUDED.organization_id;
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sync_github_installation_tenant
  AFTER INSERT OR UPDATE OR DELETE ON github_installations
  FOR EACH ROW EXECUTE FUNCTION sync_github_installation_tenant();

-- Backfill, PER TENANT.
--
-- A plain `INSERT ... SELECT FROM github_installations` mirrors NOTHING:
-- the source is FORCE RLS and the migration runs with no tenant set, so
-- the SELECT is filtered to zero rows. An earlier version of this file
-- did exactly that and excused it — "the trigger populates each row the
-- first time it is next written, and existing installations are re-read
-- by the webhook on any subsequent event" — which is false in its second
-- half. `resolveInstallation` reads ONLY this table; an installation
-- missing from it makes every handler answer "unknown installation" and
-- write nothing, so no webhook event can ever heal it. On the deploy that
-- shipped this feature, every already-connected organization would have
-- silently stopped receiving webhook effects — 202 on everything, a
-- plausible outcome in the delivery log — until each user re-ran the
-- install flow.
--
-- So: set the tenant for each organization in turn and copy what becomes
-- visible. `organizations` carries no RLS (000008 scopes repositories,
-- ingestion_runs, chunks, queries, retrievals and feedback), so the loop
-- can enumerate it.
DO $$
DECLARE
  org RECORD;
BEGIN
  FOR org IN SELECT id FROM organizations LOOP
    PERFORM set_config('app.current_tenant', org.id::text, true);
    INSERT INTO github_installation_tenants
      (github_installation_id, installation_id, organization_id)
    SELECT github_installation_id, id, organization_id
    FROM github_installations
    ON CONFLICT (github_installation_id) DO NOTHING;
  END LOOP;
  -- Leave no tenant set behind for whatever runs next in this session.
  PERFORM set_config('app.current_tenant', '', true);
END;
$$;
