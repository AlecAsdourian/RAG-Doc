-- Phase 20-03 (review): make a GitHub repository's identity its GitHub id.
--
-- The bug this closes: `POST /api/repositories` returned 500 whenever the
-- new row's `git_url` collided with a stored one, because `repositories`
-- carries TWO keys that both claim to identify a repository, and the
-- upsert could only name one of them.
--
--   000001: UNIQUE (project_id, git_url)
--   000010: UNIQUE (installation_id, github_repo_id) WHERE both NOT NULL
--
-- The worst case is the one `docs/api-repositories.md` tells clients to
-- expect. Uninstalling the App sets `installation_id` to NULL (000010,
-- ON DELETE SET NULL — we keep what was ingested). Reinstalling and
-- reconnecting then misses the 000010 index, because the orphaned row's
-- NULL installation excludes it from that partial index; the INSERT falls
-- through to `UNIQUE (project_id, git_url)` and raises 23505. So the
-- documented recovery path could not work, and nothing else relinks
-- `installation_id`.
--
-- The fix is to pick one identity and mean it.

-- =====================================================================
-- 1. (project_id, github_repo_id) is the identity
-- =====================================================================
--
-- GitHub's numeric id is stable across renames and transfers; that is
-- already why `POST /api/repositories` takes it instead of a URL. It is
-- the right thing to key on, and — unlike the installation — it does not
-- change underneath us. Keying identity on a value that changes is what
-- produced the bug above.
--
-- Partial, because rows that predate GitHub integration (and any future
-- non-GitHub source) have a NULL `github_repo_id`, and several of them
-- may coexist in one project.
CREATE UNIQUE INDEX idx_repositories_project_github_repo
  ON repositories (project_id, github_repo_id)
  WHERE github_repo_id IS NOT NULL;

-- The 000010 index is now redundant and actively harmful: it is a second
-- arbiter the upsert does not name, so it can only ever surface as a 500.
-- Installation is a credential, not an identity — the same repository
-- reached through a reinstalled App is the same repository.
--
-- WHAT THIS GIVES UP, precisely. 000010's index forbade the same
-- `(installation_id, github_repo_id)` anywhere; the index above forbids
-- it only within one project. An organization may hold several projects
-- (`idx_projects_one_default_per_org` constrains how many are DEFAULT,
-- not how many exist), and `docs/api-repositories.md` says a repository
-- connected before this API may sit in a non-default one. So at the
-- schema level the same repository can now appear twice in one
-- organization, in two projects.
--
-- An earlier version of this comment claimed the index above preserved
-- the old guarantee "wherever it matters". It does not, and the gap is
-- not academic: a duplicate would be ingested twice in Phase 21, doubling
-- chunks and duplicating every search hit.
--
-- It is closed in the handler instead, which resolves a repository across
-- ALL of the organization's projects before inserting — see
-- `Connect` in pkg/api/handlers/repositories.go. That belongs there
-- rather than here because the rule is "one per organization" and
-- `repositories` reaches its organization only through a join, which a
-- unique index cannot span.
DROP INDEX IF EXISTS idx_repositories_installation_github_id;

-- =====================================================================
-- 2. git_url stops being a competing key for GitHub-sourced rows
-- =====================================================================
--
-- For a GitHub repository, `git_url` is DERIVED from the id above: we
-- store whatever GitHub currently reports as `clone_url`. A rename
-- changes it and frees the old URL for someone else to take, so two of
-- our rows can briefly hold the same stored URL while we catch up. That
-- transient state must not be a 500 on an unrelated repository's connect.
--
-- It stays a real key where it is still the only identity available:
-- rows with no GitHub id.
ALTER TABLE repositories
  DROP CONSTRAINT IF EXISTS repositories_project_id_git_url_key;

CREATE UNIQUE INDEX idx_repositories_project_git_url_ungithubbed
  ON repositories (project_id, git_url)
  WHERE github_repo_id IS NULL;

-- =====================================================================
-- 3. sync_state gets the CHECK it has been missing
-- =====================================================================
--
-- Carried from 20-02's review (L6). `idx_repositories_sync_state` is a
-- partial index on 'pending' and 'failed'; a typo elsewhere in the code
-- would sit outside it silently and never be picked up by the Phase 21
-- queue. Values are the five in `docs/api-repositories.md`.
ALTER TABLE repositories
  ADD CONSTRAINT repositories_sync_state_valid
  CHECK (sync_state IN ('never_synced', 'pending', 'syncing', 'synced', 'failed'));
