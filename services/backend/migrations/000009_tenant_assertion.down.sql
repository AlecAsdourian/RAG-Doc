-- Reverse of 000009_tenant_assertion.up.sql
-- Order: triggers first, then the function they reference.

DROP TRIGGER IF EXISTS trg_assert_tenant ON repositories;
DROP TRIGGER IF EXISTS trg_assert_tenant ON ingestion_runs;
DROP TRIGGER IF EXISTS trg_assert_tenant ON chunks;
DROP TRIGGER IF EXISTS trg_assert_tenant ON queries;
DROP TRIGGER IF EXISTS trg_assert_tenant ON retrievals;
DROP TRIGGER IF EXISTS trg_assert_tenant ON feedback;

DROP FUNCTION IF EXISTS assert_tenant_scoped();
