-- Phase 21-04: a job for every repository the OLD webhook path left
-- `pending`.
--
-- WHY THIS EXISTS. Until 21-03 and 21-04, asking for work meant writing
-- `repositories.sync_state = 'pending'` — the thing ISS-016 was about. As
-- of this plan only `pkg/jobs` writes that column as a projection, and the
-- work item is a row in `ingestion_jobs`. Rows stamped `pending` by the
-- code this plan deleted therefore have NO JOB: nothing claims them, and
-- they would wait until somebody pushed to the repository or reconnected
-- it. This migration gives each of them the job the old path would have
-- become.
--
-- IT IS IDEMPOTENT, and not by accident: `ON CONFLICT (repository_id)
-- WHERE state IN ('queued','running') DO NOTHING` is the same arbiter the
-- producer's upsert infers against, so a repository that already has a
-- live job — one this migration created on an earlier run, or one a
-- producer created a second ago — is left exactly as it is. The predicate
-- is NOT OPTIONAL: arbiter inference will not select a partial index
-- unless it is repeated, so `ON CONFLICT (repository_id)` alone raises
-- 42P10 and a bare `ON CONFLICT` raises 42601. Measured on PostgreSQL 16
-- by 21-02.
--
-- ⚠ DO NOTHING, NOT DO UPDATE. The producer's upsert flags `needs_rerun`
-- on the live job it collides with, because a push arriving during a run
-- is new work. A backfill is not new work — it is the same work, already
-- queued — so flagging here would buy a SECOND full ingest of a repository
-- that is being ingested right now.
--
-- ONE ORGANIZATION AT A TIME, for the same two reasons as 000013's
-- backfill, neither of which is lifted, disabled or bypassed here:
--
--   - `trg_assert_tenant` (000009) refuses ANY write to `repositories`
--     while `app.current_tenant` is unset or '' — superusers included,
--     because a trigger is not row-level security.
--   - `trg_ingestion_jobs_tenant` (000014) reads `repositories`, which
--     carries FORCE ROW LEVEL SECURITY (000008). Unscoped, that read finds
--     nothing and the insert is refused with "repository ... does not
--     exist" — or, on a connection that has committed a `SET LOCAL`, with
--     22P02 from `''::uuid` inside the policy (ISS-013).
--
-- ⚠ ISS-031: NOTHING AFTER THE `DO` BLOCK IN THIS FILE MAY RUN DML
-- AGAINST A TABLE WITH ROW-LEVEL SECURITY. `set_config(..., true)` lasts
-- to the end of this migration's transaction, so once the loop finishes
-- the session holds the LAST organization's id, and after commit it holds
-- ''. This file satisfies that by ending with the block: the only
-- statements after it are comments (DDL), which are not subject to it.

-- =====================================================================
-- 1. A job for every stranded repository, one tenant at a time
-- =====================================================================
DO $$
DECLARE org RECORD;
BEGIN
  FOR org IN SELECT id FROM public.organizations LOOP
    PERFORM set_config('app.current_tenant', org.id::text, true);

    -- WHAT MAKES A REPOSITORY ELIGIBLE, and why each clause is here:
    --
    --   - `sync_state IN ('pending','syncing')` — the two states the old
    --     producers wrote. `synced`, `never_synced` and `failed` are not
    --     requests for work.
    --   - `installation_id IS NOT NULL` — a repository with no
    --     installation is UNSYNCABLE, not failed (ISS-016's note, and
    --     docs/api-github-webhooks.md). A job for one would clone nothing,
    --     fail five times and dead-letter.
    --
    --     ⚠ THIS ONE IS REDUNDANT, and is kept as documentation rather
    --     than as a guard: the INNER JOIN below already drops a row whose
    --     `installation_id` is NULL. Measured — removing it changes
    --     nothing, which is recorded in 21-04-SUMMARY.md's mutation table
    --     rather than left for a reader to assume it is load-bearing.
    --   - `gi.uninstalled_at IS NULL` — the same argument, one hop out.
    --     The App was removed; the link is kept so a reinstall can recover,
    --     but no token can be minted until it is. The join is to
    --     `github_installations`, which has row-level security of its own,
    --     so it reads under the tenant set above like everything else here.
    INSERT INTO public.ingestion_jobs (organization_id, repository_id, job_type, state)
    SELECT r.organization_id, r.id, 'full_ingest', 'queued'
    FROM public.repositories r
    JOIN public.github_installations gi ON gi.id = r.installation_id
    WHERE r.organization_id = org.id
      AND r.sync_state IN ('pending','syncing')
      AND r.installation_id IS NOT NULL
      AND gi.uninstalled_at IS NULL
    ON CONFLICT (repository_id) WHERE state IN ('queued','running') DO NOTHING;

    -- THE `syncing` BRANCH SHOULD MATCH NOTHING, and that is the point.
    -- No production code has ever written `syncing` — 21-05's worker will
    -- be the first — so this exists so that no row CAN be left stranded in
    -- a state whose meaning ("a worker has this open") is false the moment
    -- this migration runs. It carries the same eligibility predicate as the
    -- insert above, deliberately: a row moved to `pending` without a job
    -- would be the exact stranding this file is here to end.
    UPDATE public.repositories r
    SET sync_state = 'pending', updated_at = NOW()
    FROM public.github_installations gi
    WHERE gi.id = r.installation_id
      AND r.organization_id = org.id
      AND r.sync_state = 'syncing'
      AND r.installation_id IS NOT NULL
      AND gi.uninstalled_at IS NULL;
  END LOOP;
END $$;

-- =====================================================================
-- 2. What this migration deliberately does NOT do
-- =====================================================================
--
-- A repository left `pending` whose `installation_id` is NULL, or whose
-- installation is uninstalled, keeps that state and gets no job. Its
-- correct projection is `never_synced`, and the handlers write that when
-- the events that cause it arrive (`installation_repositories.removed`,
-- `installation.deleted`). Rewriting them here would be a third piece of
-- DML doing a handler's job on rows nobody can ingest either way, so the
-- residue is recorded rather than swept: the invariant this file
-- establishes is that no SYNCABLE repository is left `pending` without a
-- job.
