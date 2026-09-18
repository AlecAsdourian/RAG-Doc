-- Phase 21-02: `ingestion_jobs`, the durable queue.
--
-- Transcribed from 21-CONTEXT L2 (the table, both indexes, the composite
-- foreign key) and L5 (the tenant trigger). The schema was decided there;
-- this file is the transcription, not a redesign.
--
-- WHY A TABLE AND NOT `repositories.sync_state`. ISS-016: a status column
-- has no owner, no lease and no attempt counter, so two writers can each
-- believe they own the same repository. A work item has all three.
-- `sync_state` goes back to being a projection the frontend reads.
--
-- WHAT GUARDS WHAT, because three mechanisms overlap here:
--
--   - `idx_ingestion_jobs_one_live_per_repo` makes "two live jobs for one
--     repository" unrepresentable. That is the ISS-016 fix, in the schema.
--   - `ingestion_jobs_repo_tenant_fk` makes a job filed under the wrong
--     tenant unrepresentable. Per-row foreign-key checks run with row-level
--     security bypassed, so it holds whatever tenant context the writer
--     has, and survives the trigger below being disabled or dropped. It is
--     declared inside CREATE TABLE, for the reason section 4 gives
--     (ISS-031).
--   - `trg_ingestion_jobs_tenant` provides the MESSAGE, not the guarantee.
--     A bare foreign-key violation names a constraint, not the problem.
--
-- NO GRANTs, deliberately: `rag_doc_app` is a test-harness role (000010).

-- =====================================================================
-- 1. The table
-- =====================================================================
CREATE TABLE ingestion_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- ⚠ A DENORMALISED TENANT, GUARDED BY A COMPOSITE FOREIGN KEY (section 4).
  --
  -- A worker uses this value to scope every write it then makes, so a
  -- drifted row would write another tenant's data. That makes it an
  -- AUTHORIZATION INPUT, not an annotation -- the distinction 21-CONTEXT
  -- L5 draws between 000012's two patterns.
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  repository_id   UUID NOT NULL REFERENCES repositories(id)  ON DELETE CASCADE,

  -- Set when the run begins, so the job points at its result record.
  --
  -- ⚠ A RETRY REUSES THIS ROW; it does not create a second one.
  -- `ingestion_runs` carries `UNIQUE (repository_id, commit_sha)` (000002),
  -- so attempt 2 inserting a fresh run for the same commit raises 23505.
  -- The worker resolves the run rather than inserting it:
  --
  --   INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
  --   VALUES ($1, $2, $3, 'pending')
  --   ON CONFLICT (repository_id, commit_sha) DO UPDATE
  --     SET started_at = NOW()
  --   RETURNING id;
  --
  -- correct for a retry (same commit, same run) and for a superseded run's
  -- replacement (same commit, run reopened). A job for a DIFFERENT commit
  -- gets its own row, which is the normal case. Pinned by W6 in
  -- pkg/jobs/schema_test.go; used by 21-05.
  ingestion_run_id UUID REFERENCES ingestion_runs(id) ON DELETE SET NULL,

  job_type TEXT NOT NULL CHECK (job_type IN ('full_ingest','incremental')),

  -- FIVE STATES, NOT SIX. `failed` was removed after review (decision O2).
  --
  -- It had no edge back to the claimable set and no place in the partial
  -- unique index below, so a failed job could neither be retried nor
  -- prevent a second live job for the same repository. Instead a failed
  -- attempt sets `state='queued'` with `run_after` in the future and
  -- records `last_error`.
  --
  -- Nothing is lost. "This repository is currently failing" is
  -- `state='queued' AND attempts > 0`. `dead` is the only failure terminal.
  state TEXT NOT NULL CHECK (state IN
    ('queued','running','completed','dead','superseded')),

  -- Lease. Short (5 minutes), extended by heartbeat every 60s -- L3. A
  -- lease as long as the worst-case job would strand a repository for that
  -- long when a worker dies.
  --
  -- ⚠ Every terminal write is fenced on it:
  --     WHERE id = $1 AND lease_owner = $2 AND state = 'running'
  -- so a reclaimed or superseded worker's write matches zero rows instead
  -- of clobbering the new attempt or colliding with its replacement.
  lease_owner      TEXT,
  lease_expires_at TIMESTAMPTZ,

  -- ⚠ RECLAIM IS A RETRY: the claim query increments `attempts` on the
  -- reclaim branch too, so a job that repeatedly kills its worker
  -- eventually dead-letters instead of looping forever.
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  run_after    TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- backoff target

  -- Coarse resumability: skip a clone we already completed on a retry.
  last_stage TEXT,          -- clone|parse|embed|store
  progress   JSONB,         -- files_parsed, chunks_embedded, current_file

  -- Set when a push arrives while this job is already live (L7). The worker
  -- re-queues once on completion and clears it -- conditionally, because
  -- `RETURNING needs_rerun` after clearing returns the NEW value and would
  -- drop the rerun. `RETURNING OLD.*` is PostgreSQL 18; we run 16.
  needs_rerun BOOLEAN NOT NULL DEFAULT FALSE,

  last_error TEXT,

  -- ⚠ DOES NOT CARRY CREDENTIALS OR AN INSTALLATION ID.
  --
  -- The worker resolves the repository's CURRENT installation when it
  -- claims the job, not when the job was enqueued. Two reconnects racing
  -- produce one job (L8); if that job had snapshotted the loser's
  -- installation, the winner's newer credentials would be silently lost.
  -- Reading at claim time is what makes the dedup safe.
  payload    JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- ⚠ NO UPDATE TRIGGER, deliberately. Every statement in 21-03 through
  -- 21-06 writes `updated_at = NOW()` explicitly, as the statements in
  -- 21-CONTEXT and 21-RESEARCH already do. A trigger here would be a
  -- second writer of a column those statements already set.
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- The tenant guarantee. Section 4 says what it guarantees and why it is
  -- declared HERE, with the table, and never by a later ALTER TABLE.
  CONSTRAINT ingestion_jobs_repo_tenant_fk
    FOREIGN KEY (repository_id, organization_id)
    REFERENCES repositories (id, organization_id) ON DELETE CASCADE
);

-- =====================================================================
-- 2. The claimable index
-- =====================================================================
--
-- The claim query sorts by `run_after` over the claimable set only.
-- Without this it degrades to a full scan as completed rows accumulate.
CREATE INDEX idx_ingestion_jobs_claimable
  ON ingestion_jobs (run_after)
  WHERE state IN ('queued','running');

-- =====================================================================
-- 3. At most one live job per repository -- the ISS-016 guard
-- =====================================================================
--
-- In the schema rather than in application logic. It is also the arbiter
-- the single enqueue upsert (L7) infers against, which is why that
-- statement has to repeat this predicate: arbiter inference will not
-- select a PARTIAL index unless the predicate is given. `ON CONFLICT
-- (repository_id)` alone raises 42P10, measured.
--
-- ⚠ `CREATE UNIQUE INDEX` is not deferrable. Enqueue-before-supersede
-- (L4) and re-enqueue-before-completion (L7) are therefore both ordering
-- rules, not preferences -- see the COMMENT in section 7.
CREATE UNIQUE INDEX idx_ingestion_jobs_one_live_per_repo
  ON ingestion_jobs (repository_id)
  WHERE state IN ('queued','running');

-- =====================================================================
-- 4. The guarantee: a mismatched tenant is unrepresentable
-- =====================================================================
--
-- `ingestion_jobs_repo_tenant_fk`, declared inside CREATE TABLE in
-- section 1. It references `repositories_id_org_key`, added by 000013
-- (21-01).
--
-- Not merely rejected. PER-ROW foreign-key checks run with row-level
-- security bypassed, so no tenant context and no filtered read can let a
-- mismatched pair through, and the trigger in section 5 being disabled,
-- dropped or buggy leaves this standing.
--
-- ⚠ WHY IT IS DECLARED WITH THE TABLE (ISS-031, fixed in 22-01). Per-row
-- checks bypass row-level security; the VALIDATION `ALTER TABLE ... ADD
-- CONSTRAINT` runs does NOT. It is one query joining this table to
-- `repositories` as the migrating role. In the deployment shape that role
-- owns `repositories`, which forces row-level security on its owner, so
-- the query reads it through the tenant policy. 000013's backfill loop
-- leaves `app.current_tenant = ''` on the migrating session once it
-- commits (ISS-013), the policy evaluates `''::uuid`, and this file, when
-- it added the key with ALTER TABLE, failed with 22P02 and left
-- `schema_migrations` at 14, dirty. Measured on a seeded database owned by
-- a NOSUPERUSER NOBYPASSRLS role and migrated in one session. A superuser
-- bypasses row-level security even under FORCE, and an empty database
-- never sets the tenant, which is why neither CI nor the harnesses saw it.
-- A key declared with its table has no rows to validate, so no validation
-- query runs and the setting is never read.
--
-- ⚠ THIS EDITS A MIGRATION THAT HAD ALREADY SHIPPED, which is acceptable
-- only because nothing was deployed. golang-migrate never re-applies a
-- recorded version, so a database that already applied 000014 keeps the
-- constraint it has, and 22-01 measured that constraint identical to this
-- one: the two paths' catalogs match, `convalidated` included. Only
-- databases below 14 take the new path. After the first production
-- deploy that escape is gone, and the class has to be handled by each
-- migration as it is written. Hence the rule:
--
--   - declare foreign keys on new tables INSIDE CREATE TABLE;
--   - never rely on the session's tenant. A migration that needs one sets
--     it itself, per organization, as 000015 does. 000013 and 000015 both
--     leave it at '' for whatever runs after them in the same session;
--   - never make a validation pass by setting a sentinel tenant (a valid
--     id no organization has). It makes the validation see zero rows and
--     pass vacuously, and no test can tell that from a real fix.
--
-- pkg/testing/isolation/migration_seeded_test.go enforces the first two:
-- it migrates a seeded database from version 10 as a non-superuser owner,
-- in one session, and it failed on the ALTER TABLE form of this key.

-- =====================================================================
-- 5. The message
-- =====================================================================
--
-- THE FOREIGN KEY ABOVE IS THE GUARANTEE; THIS TRIGGER IS THE MESSAGE.
-- Keep both, and keep which is which straight: a bare 23503 names
-- `ingestion_jobs_repo_tenant_fk`, not the problem.
--
-- Rejecting rather than silently correcting: a producer that supplies the
-- wrong tenant has a bug worth surfacing.
--
-- Since 21-01 this reads `repositories.organization_id` directly instead
-- of joining `projects`, which 21-CONTEXT L5 wrote because the column did
-- not exist yet. Either way it reads `repositories`, which carries FORCE
-- ROW LEVEL SECURITY -- so:
--
--   ⚠ A WRITE TO `ingestion_jobs` MUST RUN UNDER THE JOB'S TENANT SCOPE,
--     or this read sees nothing and the row is refused. Without a tenant
--     the shape of the refusal depends on the connection's history
--     (ISS-013):
--       - `app.current_tenant` unset -> the policy yields NULL, the read
--         finds nothing, and the NOT FOUND branch below raises
--         "does not exist"
--       - a connection that earlier COMMITTED a `SET LOCAL` holds '', and
--         `''::uuid` raises 22P02 inside the policy
--     Both are pinned in pkg/jobs/schema_test.go so that nobody later
--     "fixes" this trigger instead of scoping the enqueue.
--
-- ⚠ It is a BEFORE INSERT trigger on THIS table, not an AFTER mirror on
-- `repositories`: the check has to sit where the value arrives. It is not
-- attached to the columns the claim, heartbeat, completion, failure or
-- sweeper write, so none of those fire it -- which matters, because the
-- claim is genuinely pre-tenant.
--
-- WHY THE MISMATCH MESSAGE MAY NAME THE OWNER, where 000013's may not.
-- 000013's equivalent branch reads `projects`, which has NO row-level
-- security, so it could answer "does this project exist and who owns it?"
-- from inside another tenant (PR #37's review). Here the read goes through
-- `repositories`, which does have it: a caller scoped to another tenant
-- sees no row at all and gets "does not exist", identical to a repository
-- id that exists nowhere. The mismatch branch is reachable only by a role
-- that bypasses row-level security -- a superuser or a migration, which
-- can already read the whole table. Pinned by
-- TestIngestionJobs_TheTenantTriggerIsNotAnExistenceOracle.
--
-- `search_path` pinned and the body schema-qualified, following
-- `sync_github_installation_tenant` (000012): an unqualified name is
-- resolvable through a caller's temp schema.
CREATE OR REPLACE FUNCTION ingestion_jobs_fix_tenant() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
  real_org UUID;
BEGIN
  SELECT r.organization_id INTO real_org
  FROM public.repositories r
  WHERE r.id = NEW.repository_id;

  IF real_org IS NULL THEN
    RAISE EXCEPTION 'repository % does not exist', NEW.repository_id
      USING ERRCODE = '42501',
            HINT = 'Enqueue inside a transaction scoped to the job''s organization.';
  END IF;

  IF NEW.organization_id IS DISTINCT FROM real_org THEN
    RAISE EXCEPTION 'organization_id % does not match repository % (owner %)',
      NEW.organization_id, NEW.repository_id, real_org
      USING ERRCODE = '42501';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ingestion_jobs_tenant
  BEFORE INSERT OR UPDATE OF organization_id, repository_id ON ingestion_jobs
  FOR EACH ROW EXECUTE FUNCTION ingestion_jobs_fix_tenant();

-- =====================================================================
-- 6. What the columns are
-- =====================================================================
COMMENT ON COLUMN ingestion_jobs.organization_id IS
  'Copy of repositories.organization_id, guaranteed by ingestion_jobs_repo_tenant_fk. '
  'An AUTHORIZATION INPUT: the worker scopes every write it makes to this value, and '
  'request handlers must filter by it explicitly because this table has no RLS.';

COMMENT ON COLUMN ingestion_jobs.payload IS
  'Producer-supplied job parameters. NEVER credentials and NEVER an installation id: '
  'the worker resolves the repository''s current installation at claim time, so that '
  'two racing reconnects deduplicating to one job cannot lose the winner''s newer '
  'credentials (21-CONTEXT L8).';

COMMENT ON COLUMN ingestion_jobs.needs_rerun IS
  'A push arrived while this job was already live (21-CONTEXT L7). Cleared '
  'conditionally -- `WHERE ... AND needs_rerun RETURNING id` -- because `RETURNING '
  'needs_rerun` after clearing returns the new value and would drop the rerun.';

-- =====================================================================
-- 7. What the table is, and the three things a reader must know
-- =====================================================================
COMMENT ON TABLE ingestion_jobs IS
  'Durable ingestion queue: one work item per repository ingest, claimed with '
  'FOR UPDATE SKIP LOCKED, leased, retried and dead-lettered. Replaces '
  'repositories.sync_state as the queue (ISS-016); sync_state becomes a projection. '
  'NO ROW-LEVEL SECURITY, DELIBERATELY (21-CONTEXT L5): a worker claims a job BEFORE '
  'it knows the tenant -- organization_id is on the row it is trying to claim -- so '
  'scoping the claim by the answer is circular. Same reasoning as '
  'github_installation_tenants (000012). '
  'CONSEQUENCE, WHICH THE DATABASE WILL NOT ENFORCE FOR YOU: organization_id here is '
  'an AUTHORIZATION INPUT, not an annotation. Every request handler reading this table '
  'must filter by organization_id explicitly (21-07''s GET /api/admin/jobs/{id}), and '
  'the CI isolation gate will not catch a mistake, because it scans mutation endpoints '
  'and that is a GET. '
  'WRITES MUST RUN UNDER THE JOB''S TENANT SCOPE: trg_ingestion_jobs_tenant reads '
  'repositories, which has FORCE ROW LEVEL SECURITY. Unscoped, an insert fails with '
  '"repository ... does not exist" or with 22P02 on a connection that has committed a '
  'SET LOCAL (ISS-013). The claim, heartbeat, completion, failure and sweeper '
  'statements do not touch organization_id or repository_id, so they do not fire it. '
  'ORDERING IS MANDATORY (L4, L7): supersede or complete the live job BEFORE enqueueing '
  'its replacement. The reverse order raises no error through the enqueue upsert -- it '
  'flags needs_rerun on the job about to leave the live set, and the repository ends '
  'with no live job at all. Silent loss, which is worse than the 23505 a plain INSERT '
  'would give. '
  'PRUNING, like github_webhook_deliveries: this table grows forever. '
  'DELETE FROM ingestion_jobs WHERE state IN (''completed'',''dead'',''superseded'') '
  'AND updated_at < NOW() - INTERVAL ''30 days''. Phase 24 owns the schedule.';
