-- Reverse of 000011. Written to be run: apply, roll back, re-apply is
-- part of this migration's verification.
--
-- 000011 legalises rows that 000010 forbade, so a rollback is not always
-- possible. The two shapes are:
--
--   * two GitHub-sourced rows in one project sharing a `git_url` — the
--     transient state 000011 exists to tolerate, since a rename frees the
--     old URL for someone else to take;
--   * the same `(installation_id, github_repo_id)` in two projects.
--
-- Either one makes recreating the old keys impossible without discarding
-- a row, which a migration must never do silently.
--
-- So the check comes FIRST, and it is an assertion rather than a repair.
-- An earlier version of this file relied on the recreations themselves to
-- fail, with a comment claiming the constraint was "recreated last" so it
-- would refuse rather than discard. That reasoning was wrong twice over:
-- golang-migrate runs this file in one transaction, so statement order
-- decides nothing, and the operator got a bare "could not create unique
-- index" naming neither the offending rows nor the way out.
--
-- NOTE FOR WHOEVER HITS THIS: golang-migrate marks the database dirty on
-- ANY failed migration, this one included. Nothing here has been applied
-- when the assertion fires — the transaction rolls back and the schema is
-- still at 000011 — so the recovery is `migrate force 11`, then resolve
-- the rows named below.

DO $$
DECLARE
  dup_url  bigint;
  dup_inst bigint;
BEGIN
  SELECT count(*) INTO dup_url FROM (
    SELECT project_id, git_url
    FROM repositories
    GROUP BY project_id, git_url
    HAVING count(*) > 1
  ) d;

  SELECT count(*) INTO dup_inst FROM (
    SELECT installation_id, github_repo_id
    FROM repositories
    WHERE installation_id IS NOT NULL AND github_repo_id IS NOT NULL
    GROUP BY installation_id, github_repo_id
    HAVING count(*) > 1
  ) d;

  IF dup_url > 0 OR dup_inst > 0 THEN
    RAISE EXCEPTION
      'cannot roll back 000011: % duplicate (project_id, git_url) group(s) '
      'and % duplicate (installation_id, github_repo_id) group(s) exist',
      dup_url, dup_inst
      USING HINT =
        'These rows are legal under 000011 and illegal under 000010. '
        'Nothing was changed: run `migrate force 11` to clear the dirty '
        'flag, resolve the duplicates by hand, then retry the rollback.';
  END IF;
END $$;

ALTER TABLE repositories
  DROP CONSTRAINT IF EXISTS repositories_sync_state_valid;

DROP INDEX IF EXISTS idx_repositories_project_git_url_ungithubbed;
DROP INDEX IF EXISTS idx_repositories_project_github_repo;

CREATE UNIQUE INDEX IF NOT EXISTS idx_repositories_installation_github_id
  ON repositories (installation_id, github_repo_id)
  WHERE installation_id IS NOT NULL AND github_repo_id IS NOT NULL;

ALTER TABLE repositories
  ADD CONSTRAINT repositories_project_id_git_url_key UNIQUE (project_id, git_url);
