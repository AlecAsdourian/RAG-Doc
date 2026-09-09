-- Reverse of 000011. Written to be run: apply, roll back, re-apply is
-- part of this migration's verification.
--
-- Restoring `UNIQUE (project_id, git_url)` as a table constraint can fail
-- where 000011 allowed duplicates to accumulate. That is correct
-- behaviour for a rollback — it refuses rather than silently discarding a
-- row — and it is why the constraint is recreated last.

ALTER TABLE repositories
  DROP CONSTRAINT IF EXISTS repositories_sync_state_valid;

DROP INDEX IF EXISTS idx_repositories_project_git_url_ungithubbed;
DROP INDEX IF EXISTS idx_repositories_project_github_repo;

CREATE UNIQUE INDEX IF NOT EXISTS idx_repositories_installation_github_id
  ON repositories (installation_id, github_repo_id)
  WHERE installation_id IS NOT NULL AND github_repo_id IS NOT NULL;

ALTER TABLE repositories
  ADD CONSTRAINT repositories_project_id_git_url_key UNIQUE (project_id, git_url);
