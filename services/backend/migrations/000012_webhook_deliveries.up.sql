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
-- Verified rather than assumed: as `rag_doc_app` with no tenant set, a
-- direct `SELECT ... FROM github_installations` returns **0 rows**, and
-- this function returns the row. A handler that used the pool directly
-- would silently do nothing — which is exactly what the first draft of
-- 20-05's handler did.
--
-- WHY THIS IS NARROW ENOUGH TO BE SAFE:
--
--   * It takes a single BIGINT and returns two ids. No filtering, no
--     projection of anything else, nothing caller-controlled beyond the
--     id GitHub signed for.
--   * It leaks only "this installation belongs to some organization" to
--     anyone who can already guess a valid installation id — and the
--     caller has already proved it is GitHub via HMAC before reaching it.
--   * Everything the handler does WITH the answer goes through a normal
--     tenant transaction, so RLS still governs every read and write of
--     actual tenant data.
--
-- `search_path` is pinned: a SECURITY DEFINER function without one is the
-- classic privilege-escalation shape, because a caller can point it at
-- their own schema.
CREATE OR REPLACE FUNCTION github_installation_owner(p_github_installation_id BIGINT)
RETURNS TABLE (installation_id UUID, organization_id UUID)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
  SELECT id, organization_id
  FROM github_installations
  WHERE github_installation_id = p_github_installation_id
$fn$;

COMMENT ON FUNCTION github_installation_owner(BIGINT) IS
  'Maps a GitHub installation id to its owning organization, bypassing RLS. '
  'The ONLY sanctioned way for the webhook receiver to discover a tenant. '
  'Everything done with the answer must go through a normal tenant transaction.';

-- No GRANT here. EXECUTE on functions is granted to PUBLIC by default, and
-- 20-02 learned the hard way that a migration must not name `rag_doc_app`:
-- that role does not exist in production, and in the test harness it is
-- created AFTER migrations run.
