-- Reverse of 000014, in reverse order.
--
-- Dropping the table takes both indexes and the composite foreign key with
-- it; the trigger goes with the table too, but it is dropped explicitly so
-- the order of teardown is readable rather than implied. The function is
-- not owned by the table and must be dropped separately.
--
-- This runs BEFORE 000013's down migration, which drops
-- `repositories_id_org_key` -- the key `ingestion_jobs_repo_tenant_fk`
-- references. That ordering is golang-migrate's, and it is the reason
-- 000013's down migration can drop the key unconditionally.
--
-- Everything in this table is reconstructible: a queue is work not yet
-- done, and the record of what happened lives in `ingestion_runs`.

DROP TRIGGER IF EXISTS trg_ingestion_jobs_tenant ON ingestion_jobs;
DROP FUNCTION IF EXISTS ingestion_jobs_fix_tenant();

DROP TABLE IF EXISTS ingestion_jobs;
