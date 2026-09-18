-- Reverses 000016. Nothing at this version uses the type; 22-02's 000017,
-- which does, drops its own columns and indexes on the way down first.
--
-- Dropping an extension takes its owner or a superuser. In the deployment
-- shape the operator created it (see the up migration), so rolling back past
-- 16 as the non-superuser table owner fails here with 42501, `must be owner
-- of extension vector` (measured 2026-09-17, 22-01), and needs the operator
-- again. That is the honest failure: the owner never had the extension to
-- give back. As a superuser (compose, CI, the test harnesses) it succeeds.
DROP EXTENSION IF EXISTS vector;
