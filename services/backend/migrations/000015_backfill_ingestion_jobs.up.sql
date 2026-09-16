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
-- 1. Three statements per tenant: queue, normalise, stand down
-- =====================================================================
--
--   1. a job for every repository we can still ingest
--   2. `syncing` -> `pending` for those same rows
--   3. `never_synced` for the ones nothing can ever ingest
--
-- The third was added after PR #40's review; see the comment on it for why
-- "a handler will fix those later" was false.
DO $$
DECLARE org RECORD;
BEGIN
  FOR org IN SELECT id FROM public.organizations LOOP
    PERFORM set_config('app.current_tenant', org.id::text, true);

    -- ⚠ THE PROOF THAT THE LINE ABOVE RAN, AND IT IS NOT DECORATION.
    --
    -- 000013 could prove its backfill with `SET NOT NULL`, whose
    -- validation scan ignores row-level security. This migration has no
    -- such statement, and MEASURED, a backfill that lost its tenant scope
    -- fails in two different ways depending on who runs it:
    --
    --   - as a SUPERUSER (the test harness): 42501 from trg_assert_tenant,
    --     because row-level security is bypassed, a row reaches the
    --     `syncing` UPDATE below and the row trigger fires. Loud.
    --   - as the RLS-SUBJECT OWNER WE DEPLOY AS: nothing at all. Every
    --     read is filtered to zero rows before a row trigger can fire, the
    --     `DO` block succeeds, no job is created, and the migration is
    --     recorded as applied. Measured on a scratch database in the
    --     deployment shape: exit 0, `DO`, zero rows backfilled.
    --
    -- The second is the shape that matters and the one nothing else here
    -- would catch, so the assertion is explicit. It costs one
    -- `current_setting` per organization. 21-01 measured the same
    -- asymmetry for 000013's backfill; this is that lesson, applied.
    --
    -- ⚠ IT PROVES NOTHING IF THE LOOP BODY NEVER RUNS, and what makes that
    -- safe is invisible from here: `organizations` and `projects` carry NO
    -- row-level security (000008 covers six tables and leaves both out, for
    -- the signup bootstrap reason 000009's header records), so the
    -- `FOR org IN SELECT id FROM public.organizations` above cannot itself
    -- be filtered to zero rows by a missing tenant. If either table ever
    -- gains row-level security, this assertion becomes vacuous and the
    -- backfill becomes silent again. Raised by PR #40's review, which
    -- confirmed `relrowsecurity = f` on both.
    --
    -- The message carries organization UUIDs and nothing else: no token,
    -- no secret, and it is safe in a deploy log.
    IF current_setting('app.current_tenant', true) IS DISTINCT FROM org.id::text THEN
      RAISE EXCEPTION
        'backfill is not scoped to organization %: app.current_tenant is %',
        org.id, coalesce(current_setting('app.current_tenant', true), '<unset>')
        USING ERRCODE = '42501',
              HINT = 'Every statement below reads or writes a table with row-level '
                     'security; unscoped, they silently match nothing.';
    END IF;

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

    -- ⚠ AND THE COMPLEMENT: THE ROWS NOTHING CAN EVER INGEST.
    --
    -- A `pending` or `syncing` repository whose installation is gone or
    -- uninstalled gets no job above, correctly — a job for one would clone
    -- nothing, burn five attempts and dead-letter. But leaving it
    -- `pending` is not neutral: `sync_state` is what the frontend renders,
    -- so the row says "queued, syncing soon" forever.
    --
    -- AN EARLIER VERSION OF THIS FILE LEFT THEM, on the reasoning that the
    -- handlers write `never_synced` when the relevant event arrives. THAT
    -- IS FALSE FOR EXACTLY THESE ROWS, and PR #40's review traced why:
    -- both production writers of `never_synced` key on
    -- `installation_id = $1` (`github_webhook_events.go`, the uninstall
    -- stand-down and `standDownRepositories`), and `installation_id = $1`
    -- can never match `installation_id IS NULL`. For the uninstalled case
    -- the event has already been processed — that is how `uninstalled_at`
    -- came to be set. No handler can reach these rows, no job exists for
    -- them, and 21-05's worker only writes states for jobs that exist. The
    -- only exit was a user reconnecting a repository that was telling them
    -- work was already under way.
    --
    -- `never_synced` is the documented meaning of "we have this row and
    -- cannot sync it" (docs/api-repositories.md), and it is what both
    -- handlers write for the same condition when they CAN reach the row.
    -- `NOT EXISTS` rather than a join, because the NULL case has no row on
    -- the other side to join to.
    --
    -- It does not touch `synced`: a repository that finished keeps its
    -- content and its state, and it is not re-synced or re-labelled
    -- because the App went away.
    UPDATE public.repositories r
    SET sync_state = 'never_synced', updated_at = NOW()
    WHERE r.organization_id = org.id
      AND r.sync_state IN ('pending','syncing')
      AND NOT EXISTS (
        SELECT 1 FROM public.github_installations gi
        WHERE gi.id = r.installation_id AND gi.uninstalled_at IS NULL);
  END LOOP;
END $$;

-- =====================================================================
-- 2. The invariant this file establishes
-- =====================================================================
--
-- After it, no repository is left asking for work that nothing will do:
--
--   - a SYNCABLE repository at `pending` or `syncing` has exactly one live
--     job (statements 1 and 2)
--   - an UNSYNCABLE one — no installation, or an uninstalled one — is
--     `never_synced` and has no job (statement 3)
--
-- Both halves are asserted over the whole table by
-- pkg/jobs/backfill_migration_test.go, not fixture by fixture, so a row
-- shape nobody thought of still fails the test.
