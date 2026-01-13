# Phase 4 Plan 1: Authentication Database Foundation Summary

**Multi-tenant database foundation with users, organization memberships, and PostgreSQL RLS policies for defense-in-depth isolation**

## Accomplishments

- Users table with Supabase Auth integration (supabase_user_id linking)
- Organization memberships with role-based access (owner/admin/member)
- RLS policies enabled with FORCE on all tenant-scoped tables (repositories, chunks, ingestion_runs, queries, retrievals, feedback)
- Defense-in-depth isolation at database level using current_setting('app.current_tenant')
- Proper CASCADE delete behavior for multi-tenant hierarchy
- Comprehensive indexing for performance (supabase_user_id, user_id, organization_id)

## Files Created/Modified

- `services/backend/migrations/000007_create_users_and_memberships.up.sql` - Users and memberships schema
- `services/backend/migrations/000007_create_users_and_memberships.down.sql` - Rollback migration
- `services/backend/migrations/000008_enable_rls_policies.up.sql` - RLS enablement and policies (forced row security)
- `services/backend/migrations/000008_enable_rls_policies.down.sql` - RLS removal
- `services/backend/Makefile` - Updated DATABASE_URL to match docker-compose.yml credentials

## Decisions Made

**Migration numbering:** Adjusted to 000007/000008 after discovering existing 000006_add_fts_index migration.

**RLS policy pattern:** Used EXISTS with JOIN pattern for performance (recommended by PostgreSQL RLS docs) instead of correlated sub-SELECTs. Each policy traces back to organization_id through the foreign key hierarchy.

**FORCE ROW LEVEL SECURITY:** Applied to all tenant-scoped tables to ensure even the table owner (coderag user) is subject to policies, preventing accidental cross-tenant access.

## Issues Encountered

**Database credentials mismatch:** Makefile had incorrect credentials (smartdocs user/db) vs docker-compose.yml (coderag). Updated Makefile to match running Docker container.

**golang-migrate not installed:** Installed via `go install github.com/golang-migrate/migrate/v4/cmd/migrate@latest`.

**Missing intermediate tables:** queries, retrievals, and feedback tables hadn't been created yet. Ran migrations 000004 and 000005 first to establish full schema before applying RLS policies.

**Partial RLS migration run:** First run created some policies before erroring on missing tables. Re-running migration after creating missing tables resulted in duplicate policy errors (non-blocking).

## Verification Results

**Tables created:**
- users (10 rows in pg_tables)
- organization_memberships (10 rows in pg_tables)

**RLS enabled (rowsecurity = t):**
- repositories
- ingestion_runs
- chunks
- queries
- retrievals
- feedback

**All policies verified:**
- tenant_isolation policy exists on all 6 tenant-scoped tables
- Policies use EXISTS with JOIN pattern for performance
- FORCED row security enabled confirmed via `\d+ repositories`

**Indexes confirmed:**
- idx_users_supabase_user_id
- users_email_key (UNIQUE)
- users_supabase_user_id_key (UNIQUE)
- idx_organization_memberships_user_id
- idx_organization_memberships_organization_id
- organization_memberships_user_id_organization_id_key (UNIQUE)

**Constraints verified:**
- role CHECK constraint: IN ('owner', 'admin', 'member')
- CASCADE deletes on user_id → users(id) and organization_id → organizations(id)

## Next Phase Readiness

Ready for 04-02: Supabase Auth integration and JWT validation middleware.

**TODOs for next plan:**
- Install Supabase Go client and lestrrat-go/jwx v3
- Implement JWT validation with JWK auto-refresh from Supabase JWKS endpoint
- Create middleware chain: JWT auth → tenant extraction → RLS context (SET LOCAL app.current_tenant)
- Handle organization_id extraction (from JWT custom claims or X-Organization-ID header temporarily)

**Concerns:**
- Multi-org users (users in multiple organizations) will need organization selection mechanism - Plan 3 or later to address
- Supabase Auth integration approach decision in Plan 3 (native OAuth vs custom OAuth with Supabase API)
