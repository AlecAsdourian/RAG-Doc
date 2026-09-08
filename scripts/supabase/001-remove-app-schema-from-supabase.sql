-- ---------------------------------------------------------------------
-- Remove the duplicated application schema from the Supabase project,
-- and close the anon-key read exposure on the signup-event bridge.
--
-- WHY THIS EXISTS
--
-- The application's tables were at some point created inside the Supabase
-- project's `public` schema (back when SUPABASE_URL and DATABASE_URL both
-- pointed at Supabase). The application no longer uses them — DATABASE_URL
-- points at the local/self-hosted Postgres, and the workers do too.
--
-- Supabase publishes everything in `public` over HTTP via PostgREST. The
-- anon key — which is public by design and ships in frontend JavaScript —
-- could read every one of those tables. Supabase's own advisor flags this
-- as CRITICAL ("RLS Disabled in Public").
--
-- Verified 2026-09-08 with the anon key: users(1 row), organizations(1),
-- organization_memberships(1), projects(0), repositories(0), chunks(0),
-- and auth_user_events(1) were all readable.
--
-- WHAT THIS DOES NOT TOUCH
--
--   * The `auth` schema. Nothing here references it. `auth.users` and
--     every Supabase-managed object is left completely alone.
--   * `public.auth_user_events`. That table is NOT ours — it is the
--     bridge that makes signup work: a trigger on `auth.users` inserts a
--     row, and a Supabase Database Webhook on that insert POSTs to our
--     backend's /webhooks/supabase. Dropping it breaks signup. It is
--     kept, and Step 3 locks it down instead.
--
-- HOW TO RUN
--
--   Supabase Studio -> SQL Editor. Run Step 1 alone first and read the
--   output. Only then run Steps 2 and 3.
-- ---------------------------------------------------------------------


-- =====================================================================
-- STEP 1 — INSPECT (read-only, changes nothing). Run this first.
-- =====================================================================

-- 1a. Everything currently in the public schema, with row counts.
SELECT
    c.relname                                    AS table_name,
    c.relrowsecurity                             AS rls_enabled,
    (SELECT count(*) FROM pg_policies p
      WHERE p.schemaname = 'public'
        AND p.tablename = c.relname)             AS policy_count,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
    (xpath('/row/c/text()',
           query_to_xml(format('SELECT count(*) AS c FROM public.%I', c.relname),
                        false, true, '')))[1]::text::bigint AS row_count
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
ORDER BY c.relname;

-- 1b. Anything referencing the tables we are about to drop, from OUTSIDE
--     the set. If this returns rows, STOP and reassess — something you
--     did not expect depends on this schema.
SELECT
    con.conname            AS constraint_name,
    src.relname            AS referencing_table,
    tgt.relname            AS referenced_table
FROM pg_constraint con
JOIN pg_class src ON src.oid = con.conrelid
JOIN pg_class tgt ON tgt.oid = con.confrelid
JOIN pg_namespace sn ON sn.oid = src.relnamespace
WHERE con.contype = 'f'
  AND tgt.relname IN (
        'users','organizations','organization_memberships','projects',
        'repositories','ingestion_runs','chunks','queries','retrievals',
        'feedback','schema_migrations')
  AND src.relname NOT IN (
        'users','organizations','organization_memberships','projects',
        'repositories','ingestion_runs','chunks','queries','retrievals',
        'feedback','schema_migrations')
ORDER BY 1;

-- 1c. Confirm the signup bridge exists and note what feeds it. These
--     must survive — do not drop anything listed here.
SELECT tgname AS trigger_name, tgrelid::regclass AS on_table
FROM pg_trigger
WHERE NOT tgisinternal
  AND tgrelid::regclass::text IN ('auth.users', 'public.auth_user_events')
ORDER BY 1;


-- =====================================================================
-- STEP 2 — DROP the duplicated application schema.
--
-- Only after Step 1b returned zero rows. CASCADE is scoped to these
-- tables' own inter-dependencies (they reference each other via FKs);
-- it will not reach outside the listed set unless 1b showed something,
-- which is exactly what 1b is for.
-- =====================================================================

BEGIN;

DROP TABLE IF EXISTS
    public.feedback,
    public.retrievals,
    public.queries,
    public.chunks,
    public.ingestion_runs,
    public.repositories,
    public.projects,
    public.organization_memberships,
    public.users,
    public.organizations,
    public.schema_migrations
CASCADE;

-- Created by migration 000009 if it was ever applied here. Harmless if
-- absent.
DROP FUNCTION IF EXISTS public.assert_tenant_scoped() CASCADE;

COMMIT;


-- =====================================================================
-- STEP 3 — Lock down the signup bridge (auth_user_events).
--
-- This table stays, but must stop being world-readable: it holds email,
-- supabase_user_id and raw_user_meta_data for every signup.
--
-- Revoking the PostgREST-facing grants is used here rather than enabling
-- RLS, deliberately. RLS with no policies would also block the trigger
-- that inserts into this table during signup, breaking the very flow it
-- is meant to protect. Revoking `anon` and `authenticated` removes it
-- from PostgREST's reach while leaving the trigger and the service role
-- (which the Database Webhook uses) working.
-- =====================================================================

BEGIN;

REVOKE ALL ON public.auth_user_events FROM anon;
REVOKE ALL ON public.auth_user_events FROM authenticated;

COMMIT;


-- =====================================================================
-- STEP 4 — VERIFY (read-only).
-- =====================================================================

-- 4a. public should now contain auth_user_events and nothing else of ours.
SELECT c.relname AS remaining_table
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY 1;

-- 4b. anon and authenticated should hold no privileges on the bridge.
--     Expect zero rows.
SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'auth_user_events'
  AND grantee IN ('anon', 'authenticated')
ORDER BY 1, 2;

-- 4c. The signup bridge trigger must still be present.
SELECT tgname AS trigger_name, tgrelid::regclass AS on_table
FROM pg_trigger
WHERE NOT tgisinternal
  AND tgrelid::regclass::text IN ('auth.users', 'public.auth_user_events')
ORDER BY 1;


-- ---------------------------------------------------------------------
-- AFTER RUNNING
--
-- From a machine with the anon key, these should now fail (404 or 401)
-- rather than returning data:
--
--   curl -H "apikey: $ANON" "$SUPABASE_URL/rest/v1/users?select=*"
--   curl -H "apikey: $ANON" "$SUPABASE_URL/rest/v1/auth_user_events?select=*"
--
-- Then re-run Supabase's Advisor. The "RLS Disabled in Public" findings
-- should be gone, because the tables they referred to no longer exist.
--
-- Signup should still work end-to-end: auth.users insert -> trigger ->
-- auth_user_events insert -> Database Webhook -> our /webhooks/supabase.
-- Worth testing once with a real signup after this runs.
-- ---------------------------------------------------------------------
