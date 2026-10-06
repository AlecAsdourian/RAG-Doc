-- Reverses 000018: drops the index for "this repository's most recent job".
-- Nothing in the schema depends on it; `currentJobJoinSQL` still answers
-- without it, by reading and sorting every job of each repository.
DROP INDEX IF EXISTS idx_ingestion_jobs_repository_recent;
