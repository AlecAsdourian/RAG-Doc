-- Reverse of 000012.
--
-- Dropping the delivery log means the next redelivery of anything already
-- processed will be processed AGAIN — the table is the only record that
-- it happened. That is acceptable for a rollback (the handlers are
-- individually idempotent against their own tables) but it is the reason
-- this file is not a no-op worth skipping.

DROP TRIGGER IF EXISTS trg_sync_github_installation_tenant ON github_installations;
DROP FUNCTION IF EXISTS sync_github_installation_tenant();
DROP TABLE IF EXISTS github_installation_tenants;

DROP INDEX IF EXISTS idx_github_webhook_deliveries_installation;
DROP INDEX IF EXISTS idx_github_webhook_deliveries_received_at;
DROP TABLE IF EXISTS github_webhook_deliveries;

-- Only `uninstalled_at`. `suspended_at` belongs to 000010 and dropping it
-- here would leave the schema unable to record a suspension while
-- 000010's comment still promised it could.
ALTER TABLE github_installations
  DROP COLUMN IF EXISTS uninstalled_at;
