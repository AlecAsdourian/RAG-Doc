---
phase: 02-database-setup
plan: 01
type: execute
---

<objective>
Set up PostgreSQL database infrastructure and create core multi-tenant schema.

Purpose: Establish the foundation for all data storage with migration tooling and the core organizational structure. This is the base layer that audit trails and query logging will build upon.
Output: Running Postgres database with migrations configured, core tables created (organizations, projects, repositories), and sample data seeded for verification.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/02-database-setup/2-CONTEXT.md
@.planning/phases/01-project-setup/1-01-SUMMARY.md
@.planning/phases/01-project-setup/1-03-SUMMARY.md

**Tech stack available:**
- Go 1.21 backend (services/backend/)
- Docker Compose orchestration
- Python 3.11 workers (services/workers/)

**From Phase Context - Essential:**
Audit trail and lineage tracking from day one. Every table must support traceability - created_at/updated_at timestamps, who/what/when for all changes.

**From Phase Context - Specifics:**
- Use golang-migrate for schema versioning
- Raw SQL or sqlc for type-safe queries (avoid heavy ORMs)
- Migrations tracked in version control
- Layered implementation: Core tables first (orgs, projects, repos)

**Constraining decisions:**
- Docker-first development (from Phase 1)
- Go backend owns the database schema and migrations
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add Postgres to Docker Compose and configure golang-migrate</name>
  <files>docker-compose.yml, services/backend/Makefile, services/backend/.env.example, services/backend/migrations/.gitkeep</files>
  <action>Add Postgres 16 service to docker-compose.yml with:
  - Image: postgres:16-alpine
  - Port: 5432:5432
  - Environment: POSTGRES_DB=smartdocs, POSTGRES_USER=smartdocs, POSTGRES_PASSWORD=dev_password_change_in_prod
  - Volume: postgres_data for persistence
  - Health check: pg_isready

Add postgres volume to docker-compose.yml volumes section.

Update services/backend/.env.example with DATABASE_URL connection string.

Install golang-migrate in backend Dockerfile: RUN go install -tags 'postgres' github.com/golang-migrate/migrate/v4/cmd/migrate@latest

Create services/backend/migrations/ directory with .gitkeep.

Add Makefile targets:
- migrate-create: Create new migration file
- migrate-up: Apply all pending migrations
- migrate-down: Rollback last migration
- migrate-version: Show current migration version

Use golang-migrate (not other tools) - it's the standard for Go projects and has excellent Postgres support.</action>
  <verify>docker-compose config validates. docker-compose up postgres starts successfully. pg_isready check passes. make migrate-version shows "dirty: false".</verify>
  <done>Postgres running in Docker, golang-migrate installed and configured, Makefile targets working, migrations directory created</done>
</task>

<task type="auto">
  <name>Task 2: Create initial migration with core multi-tenant schema</name>
  <files>services/backend/migrations/000001_create_core_schema.up.sql, services/backend/migrations/000001_create_core_schema.down.sql</files>
  <action>Create migration files using make migrate-create NAME=create_core_schema.

**UP migration (000001_create_core_schema.up.sql):**

Create tables in order:
1. organizations table:
   - id (UUID primary key, default gen_random_uuid())
   - name (VARCHAR(255) NOT NULL)
   - slug (VARCHAR(255) UNIQUE NOT NULL) -- URL-friendly identifier
   - created_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - updated_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - CONSTRAINT organizations_slug_format CHECK (slug ~ '^[a-z0-9-]+$')

2. projects table:
   - id (UUID primary key, default gen_random_uuid())
   - organization_id (UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE)
   - name (VARCHAR(255) NOT NULL)
   - slug (VARCHAR(255) NOT NULL) -- unique within org
   - created_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - updated_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - UNIQUE(organization_id, slug)
   - CONSTRAINT projects_slug_format CHECK (slug ~ '^[a-z0-9-]+$')

3. repositories table:
   - id (UUID primary key, default gen_random_uuid())
   - project_id (UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE)
   - name (VARCHAR(255) NOT NULL)
   - git_url (TEXT NOT NULL) -- full Git URL
   - default_branch (VARCHAR(255) NOT NULL DEFAULT 'main')
   - created_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - updated_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())
   - UNIQUE(project_id, git_url)

Create indexes:
- CREATE INDEX idx_projects_org_id ON projects(organization_id);
- CREATE INDEX idx_repositories_project_id ON repositories(project_id);

**DOWN migration (000001_create_core_schema.down.sql):**
DROP TABLE IF EXISTS repositories CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP TABLE IF EXISTS organizations CASCADE;

Use UUIDs (not SERIAL) - better for distributed systems and imports. Use TIMESTAMPTZ (not TIMESTAMP) - always store timezone. Use CASCADE deletes - org deletion should cascade to projects and repos.</action>
  <verify>make migrate-up succeeds. make migrate-version shows version 1. psql query shows all 3 tables exist with correct columns and constraints.</verify>
  <done>Migration applied, tables created with proper relationships, indexes in place, constraints enforced</done>
</task>

<task type="auto">
  <name>Task 3: Seed sample data and verify schema integrity</name>
  <files>services/backend/scripts/seed.sql</files>
  <action>Create services/backend/scripts/seed.sql with sample data for development:

INSERT INTO organizations (id, name, slug) VALUES
  ('00000000-0000-0000-0000-000000000001', 'Acme Corp', 'acme-corp'),
  ('00000000-0000-0000-0000-000000000002', 'Demo Org', 'demo-org');

INSERT INTO projects (id, organization_id, name, slug) VALUES
  ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'Main Platform', 'main-platform'),
  ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'Mobile App', 'mobile-app'),
  ('10000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002', 'Demo Project', 'demo-project');

INSERT INTO repositories (id, project_id, name, git_url, default_branch) VALUES
  ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'backend', 'https://github.com/acme/platform-backend', 'main'),
  ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'frontend', 'https://github.com/acme/platform-frontend', 'main'),
  ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000002', 'mobile-ios', 'https://github.com/acme/mobile-ios', 'develop');

Test cascade deletes by attempting to delete org (should fail if repos/projects exist - FK constraint).
Test slug uniqueness by attempting to insert duplicate slug (should fail).
Test multi-tenancy by verifying projects are properly scoped to their organizations.

Run verification queries to confirm:
- Row counts match expected (2 orgs, 3 projects, 3 repos)
- Timestamps are populated
- Foreign key relationships work correctly
- Constraints are enforced (slug format, uniqueness)

Add Makefile target 'seed' to apply this SQL file.</action>
  <verify>make seed succeeds. SELECT COUNT(*) queries return expected counts. FK constraints work. Unique constraints enforced. Timestamps auto-populate.</verify>
  <done>Sample data seeded, schema integrity verified through constraint tests, multi-tenant structure confirmed working</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] docker-compose up starts Postgres successfully
- [ ] make migrate-up applies migration without errors
- [ ] make migrate-version shows version 1, dirty: false
- [ ] psql lists all 3 tables with correct schema
- [ ] make seed populates sample data
- [ ] Foreign key CASCADE works correctly
- [ ] UNIQUE constraints prevent duplicates
- [ ] Timestamps auto-populate on INSERT
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- Postgres running in Docker with persistence
- golang-migrate configured and working
- Core schema created with proper relationships
- Multi-tenant structure (orgs → projects → repos) established
- Sample data loaded for development
- **Core foundation ready for lineage tracking (Plan 2)**
</success_criteria>

<output>
After completion, create `.planning/phases/02-database-setup/2-01-SUMMARY.md`:

# Phase 2 Plan 1: Database Infrastructure & Core Schema Summary

**PostgreSQL database running with migration tooling and multi-tenant core schema.**

## Accomplishments

- Postgres 16 added to Docker Compose with persistence
- golang-migrate configured for schema versioning
- Core multi-tenant schema (organizations, projects, repositories)
- Sample development data seeded
- Schema integrity verified through constraint testing

## Files Created/Modified

- `docker-compose.yml` - Added Postgres service with health check
- `services/backend/.env.example` - DATABASE_URL configuration
- `services/backend/Dockerfile` - golang-migrate installation
- `services/backend/Makefile` - Migration and seed targets
- `services/backend/migrations/000001_create_core_schema.up.sql` - Core schema
- `services/backend/migrations/000001_create_core_schema.down.sql` - Rollback
- `services/backend/scripts/seed.sql` - Development sample data

## Decisions Made

- Go backend owns database schema and migrations (orchestration layer role)
- golang-migrate for version control (standard for Go, excellent Postgres support)
- UUIDs for primary keys (better for distributed systems, avoid SERIAL)
- TIMESTAMPTZ for timestamps (always store timezone information)
- CASCADE deletes for multi-tenant hierarchy (org deletion cascades)
- Fixed development credentials in docker-compose (production uses secrets)

## Issues Encountered

None expected - standard Postgres + golang-migrate setup.

## Next Step

Ready for 2-02-PLAN.md (Chunk Storage & Lineage Tracking)
</output>
