package jobs

// The Go producer: the only supported way for backend code to put work on
// the ingestion queue.
//
// 21-02 proved every statement below on PostgreSQL 16 and parked them as
// named constants in schema_test.go, because there was no production code
// to hold them yet. They are here now, VERBATIM — the test file references
// these constants rather than keeping copies, so a change to one of them
// fails a test here before it reaches a caller.
//
// Read doc.go before changing anything in this file. The two rules that
// bite are that the ON CONFLICT inference clause is not optional, and that
// supersede-before-enqueue is an ordering rule whose violation raises
// nothing at all.

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// =====================================================================
// The statements
// =====================================================================

// enqueueConflictClause is the half of the enqueue upsert that every
// producer shares, split out so the set form below is structurally the
// same statement rather than a copy that has to be kept in step.
//
// ⚠ THE INFERENCE CLAUSE IS NOT OPTIONAL and two shorter forms both fail.
// Arbiter inference will not select a PARTIAL index unless the predicate is
// repeated, so `ON CONFLICT (repository_id)` raises 42P10, and
// `ON CONFLICT DO UPDATE` with no target at all raises 42601. Both measured
// on PostgreSQL 16 by TestIngestionJobs_EnqueueUpsertParsesAndReports.
const enqueueConflictClause = `
ON CONFLICT (repository_id) WHERE state IN ('queued','running')
DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()`

// enqueueReturning is 21-02's RETURNING list, split out of the clause above
// for one reason: the set form has to report WHICH repository each row
// belongs to, and the single-row form does not. Splitting the list rather
// than copying the ON CONFLICT clause keeps the part that is easy to get
// wrong in exactly one place. `enqueueUpsertSQL` below is byte-identical to
// the constant 21-02 shipped.
//
// `xmax <> 0` distinguishes an insert from an update: on a freshly inserted
// tuple xmax is 0, on one the upsert updated it is the locking transaction.
const enqueueReturning = `
RETURNING id, (xmax <> 0) AS was_existing`

// enqueueUpsertSQL is 21-CONTEXT L7's single enqueue statement, used by
// every producer — push, relink and bulk `installation_repositories.added`
// alike. $1 organization_id, $2 repository_id, $3 job_type.
//
// It never raises 23505: each row either enqueues or flags `needs_rerun` on
// the live job, and `was_existing` tells the caller which. That is what
// makes a bulk enqueue racing a relink safe (L8) — the earlier
// catch-23505-and-return-success design lost two repositories of three
// while reporting success.
//
// Enqueue below runs the SET form, not this one. This is the canonical
// statement the set form generalises, and the one W2 in schema_test.go
// points at the two shorter ON CONFLICT spellings that fail.
const enqueueUpsertSQL = `
INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
VALUES ($1, $2, $3, 'queued')` + enqueueConflictClause + enqueueReturning

// enqueueSetSQL is the same statement over a set of repositories:
// $1 organization_id[], $2 repository_id[], $3 job_type[], positionally
// aligned. One repository and two hundred take the same code path, so the
// single-repository case cannot drift from the bulk one.
//
// ⚠ THE INPUT MUST BE DE-DUPLICATED ON repository_id. `ON CONFLICT DO
// UPDATE` may not touch the same row twice in one statement and raises
// 21000 `ON CONFLICT DO UPDATE command cannot affect row a second time` if
// asked to. Enqueue does the de-duplication; nothing in SQL does it here.
//
// `unnest` with three arrays rather than a generated VALUES list: the
// statement text is then constant, so it plans once and cannot be built
// wrong for some lengths and right for others.
const enqueueSetSQL = `
INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
SELECT o, r, j, 'queued'
FROM unnest($1::uuid[], $2::uuid[], $3::text[]) AS t(o, r, j)` +
	enqueueConflictClause + `
RETURNING repository_id::text, id::text, (xmax <> 0) AS was_existing`

// liveSetPredicate is the live set: the two states the partial unique index
// idx_ingestion_jobs_one_live_per_repo covers, and therefore the two a
// supersede has to take a job out of. Shared by both supersede forms so
// they cannot disagree about what "live" means.
const liveSetPredicate = `state IN ('queued','running')`

// supersedeLiveSQL takes a repository's live job out of the live set (L4).
// It runs BEFORE the enqueue of its replacement, in the same transaction.
//
// ⚠ IT DELIBERATELY LEAVES `lease_owner` AND `lease_expires_at` ATTACHED.
// A supersede is written by a different actor from the worker that holds the
// lease, and the pair is the only record of which worker was running the job
// when it was taken away — which 21-07's admin endpoint wants. The
// consequence is that a lease-fenced statement CANNOT rely on the lease
// alone to mean "still mine": every one of them also carries
// `AND state = 'running'`. See clearRerunSQL, where that predicate was
// missing.
const supersedeLiveSQL = `
UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW()
WHERE repository_id = $1 AND ` + liveSetPredicate

// supersedeLiveSetSQL is the same statement over a set, and reports which
// repositories actually had a live job to supersede. $1 repository_id[].
//
// ⚠ THIS STATEMENT IS NOT TENANT-SCOPED BY THE DATABASE. It touches neither
// organization_id nor repository_id, so trg_ingestion_jobs_tenant does not
// fire, and `ingestion_jobs` has no row-level security. A repository id from
// another organization would be superseded exactly as readily as one of the
// caller's. SupersedeLive's doc comment states the obligation that follows.
const supersedeLiveSetSQL = `
UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW()
WHERE repository_id = ANY($1::uuid[]) AND ` + liveSetPredicate + `
RETURNING repository_id::text`

// projectPendingSQL is the `sync_state` projection (L2). It runs in the
// same transaction as the enqueue, over the repositories that got a NEW
// job — never over one whose live job was merely flagged `needs_rerun`,
// which is still running and whose state belongs to its worker.
//
// `repositories` HAS row-level security and trg_assert_tenant, so this one
// is scoped by the caller's transaction, unlike the supersede above. An
// unscoped caller gets 42501 from the trigger rather than a silent no-op.
const projectPendingSQL = `
UPDATE repositories SET sync_state = 'pending', updated_at = NOW()
WHERE id = ANY($1::uuid[])`

// =====================================================================
// The API
// =====================================================================

// JobType is the kind of work a job represents. The two values match the
// CHECK constraint on ingestion_jobs.job_type (migration 000014); a third
// would need a migration, so this is validated in Go rather than left to
// the database, where it would arrive as an opaque 23514.
type JobType string

const (
	// JobTypeFullIngest re-reads the whole repository. What a connect, a
	// relink and a first sync all ask for.
	JobTypeFullIngest JobType = "full_ingest"
	// JobTypeIncremental re-reads what a push changed.
	JobTypeIncremental JobType = "incremental"
)

func (j JobType) valid() bool {
	return j == JobTypeFullIngest || j == JobTypeIncremental
}

// EnqueueRequest asks for one repository's ingestion.
//
// OrganizationID is the repository's tenant, not the caller's convenience
// label: it is written to the row, guarded by ingestion_jobs_repo_tenant_fk,
// and later used by the worker to scope every write it makes. Supplying the
// wrong one is refused by the database (42501 from the tenant trigger, or
// 23503 from the composite key if the trigger is ever gone).
type EnqueueRequest struct {
	OrganizationID string
	RepositoryID   string
	JobType        JobType
}

// EnqueueResult is what happened to one repository.
//
// WasExisting = true means a job for this repository was ALREADY live and
// has been flagged `needs_rerun` instead; JobID is that job's id, not a new
// one. The repository's `sync_state` is deliberately left alone in that
// case — the live job owns it.
type EnqueueResult struct {
	RepositoryID string
	JobID        string
	WasExisting  bool
}

// ErrInvalidJobType is returned for a job type outside the CHECK constraint.
var ErrInvalidJobType = errors.New("jobs: unknown job type")

// ErrNoTransaction is returned when Enqueue or SupersedeLive is called with
// a nil transaction. Both take the caller's transaction on purpose: the
// caller owns atomicity and tenant scope, and there is no pool in this
// package to fall back to.
var ErrNoTransaction = errors.New("jobs: a transaction is required")

// Enqueue puts one job on the queue per repository, in one statement.
//
// It takes the CALLER'S transaction. That is the whole design: the row the
// job is for, the supersede that precedes it, the enqueue itself and the
// `sync_state` projection all commit together or not at all, and the
// transaction's `app.current_tenant` is what makes the writes legal.
//
// ⚠ ORDER MATTERS, AND GETTING IT WRONG RAISES NOTHING. If a repository may
// already have a live job that this call is meant to REPLACE — a relink, a
// completion followed by a rerun — call SupersedeLive (or write the
// completion) FIRST, in this same transaction. Backwards, the upsert flags
// `needs_rerun` on the job that is about to leave the live set, the
// supersede then removes it, and the repository ends with NO LIVE JOB, no
// error, and a terminal row carrying `needs_rerun = true` that nothing will
// ever read. 21-CONTEXT L4 and L7 originally recorded 23505 here; that is
// what a plain INSERT does, and this is not one. Measured both ways by
// TestIngestionJobs_SupersedeBeforeEnqueue.
//
// What it does, in order:
//
//  1. Validates every request — canonical UUIDs and a known job type —
//     BEFORE touching the database, so a bad request cannot poison a
//     transaction the caller still wants to use.
//  2. De-duplicates on RepositoryID, keeping the first request for each.
//     `ON CONFLICT DO UPDATE` may not touch the same row twice in one
//     statement (21000), and a bulk webhook payload can name a repository
//     more than once.
//  3. Runs enqueueSetSQL, which either inserts a `queued` job or flags the
//     live one.
//  4. Writes `sync_state = 'pending'` for the repositories that got a NEW
//     job only.
//
// The results come back in the order of the de-duplicated input, one per
// distinct repository — not necessarily one per element of reqs.
func Enqueue(ctx context.Context, tx pgx.Tx, reqs []EnqueueRequest) ([]EnqueueResult, error) {
	if tx == nil {
		return nil, ErrNoTransaction
	}
	if len(reqs) == 0 {
		return nil, nil
	}

	orgIDs := make([]string, 0, len(reqs))
	repoIDs := make([]string, 0, len(reqs))
	jobTypes := make([]string, 0, len(reqs))
	seen := make(map[string]struct{}, len(reqs))

	for i, req := range reqs {
		orgID, err := canonicalUUID(req.OrganizationID)
		if err != nil {
			return nil, fmt.Errorf("jobs: enqueue request %d: organization id: %w", i, err)
		}
		repoID, err := canonicalUUID(req.RepositoryID)
		if err != nil {
			return nil, fmt.Errorf("jobs: enqueue request %d: repository id: %w", i, err)
		}
		if !req.JobType.valid() {
			return nil, fmt.Errorf("jobs: enqueue request %d: %w: %q", i, ErrInvalidJobType, req.JobType)
		}
		// Validation happens for every request, including the duplicates
		// dropped here: a caller that sent garbage should hear about it
		// whether or not the same repository appeared earlier.
		if _, duplicate := seen[repoID]; duplicate {
			continue
		}
		seen[repoID] = struct{}{}
		orgIDs = append(orgIDs, orgID)
		repoIDs = append(repoIDs, repoID)
		jobTypes = append(jobTypes, string(req.JobType))
	}

	rows, err := tx.Query(ctx, enqueueSetSQL, orgIDs, repoIDs, jobTypes)
	if err != nil {
		return nil, fmt.Errorf("jobs: enqueue %d repositories: %w", len(repoIDs), err)
	}
	collected, err := pgx.CollectRows(rows, pgx.RowToStructByPos[EnqueueResult])
	if err != nil {
		return nil, fmt.Errorf("jobs: enqueue %d repositories: %w", len(repoIDs), err)
	}

	// One row per repository, back in the caller's order. The statement
	// reports every row it handled (that is W3's whole point), so a short
	// result is a bug in this file, not a repository that quietly lost its
	// job — say so rather than returning a gap.
	byRepo := make(map[string]EnqueueResult, len(collected))
	for _, result := range collected {
		byRepo[result.RepositoryID] = result
	}
	results := make([]EnqueueResult, 0, len(repoIDs))
	fresh := make([]string, 0, len(repoIDs))
	for _, repoID := range repoIDs {
		result, ok := byRepo[repoID]
		if !ok {
			return nil, fmt.Errorf(
				"jobs: enqueue reported %d rows for %d repositories; %s is missing",
				len(collected), len(repoIDs), repoID)
		}
		results = append(results, result)
		if !result.WasExisting {
			fresh = append(fresh, repoID)
		}
	}

	if len(fresh) > 0 {
		tag, err := tx.Exec(ctx, projectPendingSQL, fresh)
		if err != nil {
			return nil, fmt.Errorf("jobs: project sync_state for %d repositories: %w", len(fresh), err)
		}
		// Under row-level security a write to somebody else's repository
		// matches nothing and reports success, so "it did not error" is not
		// evidence the projection landed. Count the rows.
		if tag.RowsAffected() != int64(len(fresh)) {
			return nil, fmt.Errorf(
				"jobs: sync_state projection updated %d of %d repositories; "+
					"the transaction's tenant scope does not cover them all",
				tag.RowsAffected(), len(fresh))
		}
	}

	return results, nil
}

// SupersedeLive takes each repository's live job out of the live set and
// returns the repository ids that actually had one. Step 1 of L4's
// supersede-then-enqueue; call Enqueue next, IN THE SAME TRANSACTION.
//
// A repository with no live job is not an error and is simply absent from
// the result, so the return value is the honest answer to "what did this
// interrupt?" — which is what a caller logs.
//
// ⚠ IT IS NOT TENANT-SCOPED, AND THE DATABASE WILL NOT SCOPE IT FOR YOU.
// `ingestion_jobs` has no row-level security (21-CONTEXT L5), and this
// statement touches neither organization_id nor repository_id, so
// trg_ingestion_jobs_tenant never fires. A repository id belonging to
// another organization is superseded just as readily as one of the
// caller's. THE OBLIGATION: pass only repository ids this same transaction
// has already read out of `repositories`, which IS row-level-security
// scoped. Connect reads its id through a `FOR UPDATE OF r` lookup and
// passes that. Pinned, as a measured property rather than an assumption, by
// TestSupersedeLive_IsNotScopedByTheDatabase.
func SupersedeLive(ctx context.Context, tx pgx.Tx, repositoryIDs []string) ([]string, error) {
	if tx == nil {
		return nil, ErrNoTransaction
	}
	if len(repositoryIDs) == 0 {
		return nil, nil
	}

	ids := make([]string, 0, len(repositoryIDs))
	seen := make(map[string]struct{}, len(repositoryIDs))
	for i, raw := range repositoryIDs {
		id, err := canonicalUUID(raw)
		if err != nil {
			return nil, fmt.Errorf("jobs: supersede repository %d: %w", i, err)
		}
		if _, duplicate := seen[id]; duplicate {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}

	rows, err := tx.Query(ctx, supersedeLiveSetSQL, ids)
	if err != nil {
		return nil, fmt.Errorf("jobs: supersede live jobs for %d repositories: %w", len(ids), err)
	}
	superseded, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, fmt.Errorf("jobs: supersede live jobs for %d repositories: %w", len(ids), err)
	}
	return superseded, nil
}

// canonicalUUID rejects anything that is not the canonical 36-character
// form, and returns it unchanged.
//
// REQUIRING THE CANONICAL FORM IS THE POINT, not the parse — the same
// reasoning as db.tenantFromContext, which is worth repeating because the
// conclusion is counter-intuitive. `uuid.Parse` is a parser, not a
// validator: its own documentation says so, and it accepts the URN form,
// the brace form, the undashed form and uppercase. Postgres accepts some of
// those and rejects others with 22P02, so passing a merely-parseable string
// through turns a caller's typo into a 500 from three layers down. Here it
// becomes an error before any statement runs, which also means a bad id
// cannot abort a transaction the caller is still using.
func canonicalUUID(raw string) (string, error) {
	if raw == "" {
		return "", errors.New("must not be empty")
	}
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("not a valid uuid: %w", err)
	}
	if canonical := parsed.String(); canonical != raw {
		return "", fmt.Errorf("not in canonical form (got %q, canonical %q)", raw, canonical)
	}
	return raw, nil
}
