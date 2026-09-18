-- The seeded-migration gate's fixture (22-01, ISS-031): rows in every
-- tenant-scoped table at SCHEMA VERSION 10, the compose database's version
-- when the gate was written.
--
-- HOW IT IS LOADED (migration_seeded_test.go): as `rag_doc_app`, the
-- unprivileged application role, so every row passes the same row-level
-- security and `trg_assert_tenant` checks an application write would. Each
-- organization's rows are one transaction under that organization's
-- `set_config('app.current_tenant', ..., true)`, as 000013 and 000015 do.
--
-- WHAT IT CAN AND CANNOT HOLD. Only columns that exist at 10. In
-- particular `github_installations.uninstalled_at` arrives in 000012, so
-- the uninstalled installation (a2 below) is seeded LIVE here and marked
-- uninstalled by the gate after migrating to 12 (fact-check a2).
--
-- THE SHAPE, and what each part is for:
--
--   alpha    installation a1 (live) and a2 (uninstalled at version 12)
--            a-pending               pending, a1      -> one job, pending
--            a-pending-no-install    pending, NULL    -> no job, never_synced
--            a-pending-uninstalled   pending, a2      -> no job, never_synced
--            a-synced                synced,  a1      -> no job, synced
--                                    one run, two chunks, and the
--                                    queries -> retrievals -> feedback chain
--            a user and an owner membership
--   bravo    installation b1 (live)
--            b-pending               pending, b1      -> one job, pending
--            b-syncing               syncing, b1      -> one job, pending
--            b-synced                synced,  b1      -> no job, synced
--                                    one run, one chunk
--   charlie  installation c1 (live)
--            c-pending               pending, c1      -> one job, pending
--   delta    a default project and nothing else: an organization the
--            backfill loops visit with nothing to do. It is inserted LAST.
--            000013's and 000015's loops read `organizations` with no
--            ORDER BY, which in a fresh database is insertion order, so a
--            loop that handled only the last organization it saw would
--            backfill nothing at all.
--
-- Three organizations with repositories to backfill, so a backfill that
-- reached one tenant and not another leaves a row a later assertion sees.
--
-- Fixed ids, so a failure names a row a reader can find in this file. The
-- scratch database is new for every run, so they cannot collide.

-- =====================================================================
-- alpha
-- =====================================================================
BEGIN;
SELECT set_config('app.current_tenant', '10000000-0000-4000-8000-00000000000a', true);

INSERT INTO organizations (id, name, slug)
VALUES ('10000000-0000-4000-8000-00000000000a', 'Seed Alpha', 'seed-alpha');
INSERT INTO projects (id, organization_id, name, slug, is_default)
VALUES ('20000000-0000-4000-8000-00000000000a', '10000000-0000-4000-8000-00000000000a',
        'Default', 'default', true);

INSERT INTO users (id, supabase_user_id, email, full_name)
VALUES ('e0000000-0000-4000-8000-000000000a01', 'f0000000-0000-4000-8000-000000000a01',
        'owner@seed-alpha.example', 'Seed Alpha Owner');
INSERT INTO organization_memberships (user_id, organization_id, role)
VALUES ('e0000000-0000-4000-8000-000000000a01', '10000000-0000-4000-8000-00000000000a', 'owner');

INSERT INTO github_installations
  (id, organization_id, github_installation_id, account_login, account_type, repository_selection)
VALUES
  ('30000000-0000-4000-8000-0000000000a1', '10000000-0000-4000-8000-00000000000a',
   22010001, 'seed-alpha', 'Organization', 'selected'),
  ('30000000-0000-4000-8000-0000000000a2', '10000000-0000-4000-8000-00000000000a',
   22010002, 'seed-alpha-old', 'Organization', 'all');

INSERT INTO repositories
  (id, project_id, installation_id, github_repo_id, name, git_url, sync_state, last_synced_at)
VALUES
  ('40000000-0000-4000-8000-0000000000a1', '20000000-0000-4000-8000-00000000000a',
   '30000000-0000-4000-8000-0000000000a1', 22011001, 'a-pending',
   'https://github.com/seed-alpha/a-pending.git', 'pending', NULL),
  ('40000000-0000-4000-8000-0000000000a2', '20000000-0000-4000-8000-00000000000a',
   NULL, NULL, 'a-pending-no-install',
   'https://github.com/seed-alpha/a-pending-no-install.git', 'pending', NULL),
  ('40000000-0000-4000-8000-0000000000a3', '20000000-0000-4000-8000-00000000000a',
   '30000000-0000-4000-8000-0000000000a2', 22011003, 'a-pending-uninstalled',
   'https://github.com/seed-alpha-old/a-pending-uninstalled.git', 'pending', NULL),
  ('40000000-0000-4000-8000-0000000000a4', '20000000-0000-4000-8000-00000000000a',
   '30000000-0000-4000-8000-0000000000a1', 22011004, 'a-synced',
   'https://github.com/seed-alpha/a-synced.git', 'synced', NOW());

INSERT INTO ingestion_runs (id, repository_id, commit_sha, branch, status, completed_at, chunks_processed)
VALUES ('50000000-0000-4000-8000-0000000000a4', '40000000-0000-4000-8000-0000000000a4',
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'main', 'completed', NOW(), 2);

INSERT INTO chunks
  (id, ingestion_run_id, repository_id, file_path, start_line, end_line,
   content, content_hash, language, chunk_type, breadcrumb)
VALUES
  ('60000000-0000-4000-8000-00000000a401', '50000000-0000-4000-8000-0000000000a4',
   '40000000-0000-4000-8000-0000000000a4', 'main.go', 1, 5,
   'func main() {}', repeat('a', 64), 'go', 'function', 'main'),
  ('60000000-0000-4000-8000-00000000a402', '50000000-0000-4000-8000-0000000000a4',
   '40000000-0000-4000-8000-0000000000a4', 'util.go', 1, 3,
   'func helper() {}', repeat('b', 64), 'go', 'function', 'helper');

INSERT INTO queries (id, project_id, query_text)
VALUES ('70000000-0000-4000-8000-000000000a01', '20000000-0000-4000-8000-00000000000a',
        'where does the program start?');
INSERT INTO retrievals (id, query_id, chunk_id, rank, score, shown_to_user)
VALUES ('80000000-0000-4000-8000-000000000a01', '70000000-0000-4000-8000-000000000a01',
        '60000000-0000-4000-8000-00000000a401', 1, 0.9100, true);
INSERT INTO feedback (id, retrieval_id, feedback_type, feedback_text)
VALUES ('90000000-0000-4000-8000-000000000a01', '80000000-0000-4000-8000-000000000a01',
        'positive', 'found it');
COMMIT;

-- =====================================================================
-- bravo
-- =====================================================================
BEGIN;
SELECT set_config('app.current_tenant', '10000000-0000-4000-8000-00000000000b', true);

INSERT INTO organizations (id, name, slug)
VALUES ('10000000-0000-4000-8000-00000000000b', 'Seed Bravo', 'seed-bravo');
INSERT INTO projects (id, organization_id, name, slug, is_default)
VALUES ('20000000-0000-4000-8000-00000000000b', '10000000-0000-4000-8000-00000000000b',
        'Default', 'default', true);

INSERT INTO github_installations
  (id, organization_id, github_installation_id, account_login, account_type, repository_selection)
VALUES ('30000000-0000-4000-8000-0000000000b1', '10000000-0000-4000-8000-00000000000b',
        22010003, 'seed-bravo', 'User', 'selected');

INSERT INTO repositories
  (id, project_id, installation_id, github_repo_id, name, git_url, sync_state, last_synced_at)
VALUES
  ('40000000-0000-4000-8000-0000000000b1', '20000000-0000-4000-8000-00000000000b',
   '30000000-0000-4000-8000-0000000000b1', 22012001, 'b-pending',
   'https://github.com/seed-bravo/b-pending.git', 'pending', NULL),
  ('40000000-0000-4000-8000-0000000000b2', '20000000-0000-4000-8000-00000000000b',
   '30000000-0000-4000-8000-0000000000b1', 22012002, 'b-syncing',
   'https://github.com/seed-bravo/b-syncing.git', 'syncing', NULL),
  ('40000000-0000-4000-8000-0000000000b3', '20000000-0000-4000-8000-00000000000b',
   '30000000-0000-4000-8000-0000000000b1', 22012003, 'b-synced',
   'https://github.com/seed-bravo/b-synced.git', 'synced', NOW());

INSERT INTO ingestion_runs (id, repository_id, commit_sha, branch, status, completed_at, chunks_processed)
VALUES ('50000000-0000-4000-8000-0000000000b3', '40000000-0000-4000-8000-0000000000b3',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'main', 'completed', NOW(), 1);

INSERT INTO chunks
  (id, ingestion_run_id, repository_id, file_path, start_line, end_line,
   content, content_hash, language, chunk_type, breadcrumb)
VALUES
  ('60000000-0000-4000-8000-00000000b301', '50000000-0000-4000-8000-0000000000b3',
   '40000000-0000-4000-8000-0000000000b3', 'app.py', 1, 2,
   'def app(): pass', repeat('c', 64), 'python', 'function', 'app');
COMMIT;

-- =====================================================================
-- charlie
-- =====================================================================
BEGIN;
SELECT set_config('app.current_tenant', '10000000-0000-4000-8000-00000000000c', true);

INSERT INTO organizations (id, name, slug)
VALUES ('10000000-0000-4000-8000-00000000000c', 'Seed Charlie', 'seed-charlie');
INSERT INTO projects (id, organization_id, name, slug, is_default)
VALUES ('20000000-0000-4000-8000-00000000000c', '10000000-0000-4000-8000-00000000000c',
        'Default', 'default', true);

INSERT INTO github_installations
  (id, organization_id, github_installation_id, account_login, account_type, repository_selection)
VALUES ('30000000-0000-4000-8000-0000000000c1', '10000000-0000-4000-8000-00000000000c',
        22010004, 'seed-charlie', 'Organization', 'selected');

INSERT INTO repositories
  (id, project_id, installation_id, github_repo_id, name, git_url, sync_state)
VALUES ('40000000-0000-4000-8000-0000000000c1', '20000000-0000-4000-8000-00000000000c',
        '30000000-0000-4000-8000-0000000000c1', 22013001, 'c-pending',
        'https://github.com/seed-charlie/c-pending.git', 'pending');
COMMIT;

-- =====================================================================
-- delta: an organization with nothing to backfill, inserted last
-- =====================================================================
BEGIN;
SELECT set_config('app.current_tenant', '10000000-0000-4000-8000-00000000000d', true);

INSERT INTO organizations (id, name, slug)
VALUES ('10000000-0000-4000-8000-00000000000d', 'Seed Delta', 'seed-delta');
INSERT INTO projects (id, organization_id, name, slug, is_default)
VALUES ('20000000-0000-4000-8000-00000000000d', '10000000-0000-4000-8000-00000000000d',
        'Default', 'default', true);
COMMIT;
