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

-- Taken BEFORE the check, and held to the end of the transaction.
--
-- Without it the guard is check-then-act: a connect committing between
-- the assertion and the ALTER puts back exactly the duplicate the check
-- just cleared, and the operator gets the bare "could not create unique
-- index" this file exists to replace. The ALTER takes this lock anyway,
-- so taking it early costs nothing but the order.
LOCK TABLE repositories IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
  dup_url  text;
  dup_inst text;
BEGIN
  SELECT string_agg(format('project %s / %s (%s rows)', project_id, git_url, n), '; ')
    INTO dup_url
    FROM (
      SELECT project_id, git_url, count(*) AS n
      FROM repositories
      GROUP BY project_id, git_url
      HAVING count(*) > 1
      ORDER BY count(*) DESC
      LIMIT 20
    ) d;

  SELECT string_agg(format('installation %s / repo %s (%s rows)', installation_id, github_repo_id, n), '; ')
    INTO dup_inst
    FROM (
      SELECT installation_id, github_repo_id, count(*) AS n
      FROM repositories
      WHERE installation_id IS NOT NULL AND github_repo_id IS NOT NULL
      GROUP BY installation_id, github_repo_id
      HAVING count(*) > 1
      ORDER BY count(*) DESC
      LIMIT 20
    ) d;

  IF dup_url IS NOT NULL OR dup_inst IS NOT NULL THEN
    RAISE EXCEPTION
      'cannot roll back 000011. duplicate (project_id, git_url): [%]. '
      'duplicate (installation_id, github_repo_id): [%]',
      coalesce(dup_url, 'none'), coalesce(dup_inst, 'none')
      USING HINT =
        'These rows are legal under 000011 and illegal under 000010. '
        'Nothing was changed: run `migrate force 11` to clear the dirty '
        'flag, resolve the rows named above, then retry the rollback. '
        '(At most 20 groups of each kind are listed.)';
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
