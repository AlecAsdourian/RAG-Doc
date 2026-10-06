package handlers

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/render"
	"github.com/jackc/pgx/v5"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
)

// JobsHandler serves the ingestion queue's read surface: one job by id.
//
// ⚠ `ingestion_jobs` HAS NO ROW-LEVEL SECURITY (21-CONTEXT L5), and this is
// one of its two tenant-facing readers; the other is currentJobJoinSQL in
// repositories.go, which shares this handler's column list (jobColumnsSQL).
// docs/isolation.md lists both. Every other tenant read in this package is
// wrong-by-default-safe: a cross-tenant read of `repositories` or
// `github_installations` returns zero rows because a policy refuses it. Here
// nothing refuses it. `AND j.organization_id = $2` in jobByIDSQL below is the
// whole of the tenant boundary, and removing it silently turns this endpoint
// into a queue-wide reader.
//
// Two consequences that are easy to miss:
//
//   - THE CI ISOLATION GATE DOES NOT COVER THIS ROUTE. `check-isolation-tests.py`
//     matches POST/PUT/PATCH/DELETE only, so a GET is invisible to it and a
//     handler shipped with no isolation test at all would pass. That is why
//     `jobs_isolation_test.go` exists deliberately rather than by ratchet, and
//     why its cross-tenant case is mutation-checked.
//   - IT IS STILL RUN INSIDE `InTenantTx`. Not because this table needs it —
//     it does not — but because "every tenant handler opens a tenant
//     transaction" is a rule a reader can check at a glance, and an exception
//     would have to be re-justified by everyone who meets it. The `SET LOCAL`
//     costs one statement and makes this handler look like its neighbours.
//
// Who may read it: ANY MEMBER of the job's organization (the user's decision,
// 2026-09-14). It shows their own repository's indexing status and Phase 23's
// progress UI reads it directly, so there is no role gate and this handler
// never reads auth.OrgRoleKey.
//
// ⚠ ISS-012 APPLIES HERE AS ON EVERY TENANT ROUTE: a revoked membership still
// carries the organization claim, so a removed member could keep reading their
// former organization's job status until whatever ships membership removal
// rewrites the claim. Nothing removes memberships today; this endpoint is
// named in that issue's affected surfaces.
type JobsHandler struct {
	scoper *db.TenantScoper
}

// NewJobsHandler builds the handler.
//
// It takes a *db.TenantScoper and NOT a *pgxpool.Pool, like every other
// Phase 20+ handler (20-01-DESIGN.md). `ingestion_jobs` carries no RLS, so
// the scoper is not what makes this handler safe — the explicit filter is —
// but the read needs no pool, and holding one would make an unscoped query
// expressible for no benefit. (Since 22.1-03 the read joins `repositories`
// and `github_installations` for `status`, and the scoper is what applies
// their row-level security.)
func NewJobsHandler(scoper *db.TenantScoper) *JobsHandler {
	if scoper == nil {
		panic("handlers.NewJobsHandler: scoper is nil")
	}
	return &JobsHandler{scoper: scoper}
}

// IngestionJob is the response body of GET /api/admin/jobs/{id}.
//
// WHAT IS DELIBERATELY ABSENT, and must stay absent:
//
//   - `lease_owner`. A worker id is infrastructure identity, not status. The
//     question a caller actually has is "is anything working on this right
//     now", and `lease_expires_at` plus `stalled` answer it without handing
//     out the value every terminal write is fenced on.
//   - `payload`. Producer-supplied job parameters; the column carries no
//     credentials by design (21-CONTEXT L2/L8), but it is input to the worker
//     rather than status for a reader, and nothing in the API contract should
//     make a producer think it is a safe place to put something.
//
// `last_error`, `last_stage` and `progress` ARE returned, and they are safe to
// return because the worker redacted them before they reached the column:
// 21-05's `sanitize_error` strips NULs, redacts GitHub tokens (all six
// prefixes), fine-grained PATs, OpenAI keys, JWTs and PEM private-key blocks,
// then caps the value at 2,000 characters; 21-06's `_sanitize_progress` does
// the same to progress values, nested values AND keys. This struct is the
// reason that redaction exists — `last_error` is a column an HTTP response
// hands back.
type IngestionJob struct {
	ID           string `json:"id"`
	RepositoryID string `json:"repository_id"`
	JobType      string `json:"job_type"`

	// State is one of queued / running / completed / dead / superseded.
	// There is no `failed` state (decision O2): a failed attempt goes back
	// to `queued` with `run_after` in the future, so "currently retrying" is
	// `state == "queued" && attempts > 0` and `dead` is the only failure
	// terminal.
	State       string `json:"state"`
	Attempts    int32  `json:"attempts"`
	MaxAttempts int32  `json:"max_attempts"`

	// RunAfter is the backoff target: the earliest moment a claim will
	// consider this job. In the future means "waiting out a retry".
	RunAfter time.Time `json:"run_after"`

	// LeaseExpiresAt and Stalled are THE evidence that a worker is alive,
	// and `repositories.sync_state` is not.
	//
	// ⚠ `sync_state = 'syncing'` IS NOT EVIDENCE OF A LIVE WORKER (21-06,
	// ruled on PR #42's second review). A crashed worker, a worker whose
	// connection died mid-job, and a job deferred part-way through a
	// shutdown all leave `syncing` behind with nobody working, and they
	// always have — `mark_started` projects `syncing` and `defer` writes no
	// projection at all, deliberately. So this endpoint answers "is this
	// actually running?" from the JOB ROW and never from the projection,
	// and Phase 23's UI gets the same rule.
	//
	// Stalled is computed in SQL, against the DATABASE clock, with the
	// `running`-with-a-dead-lease branch `claimSQL` and `_SWEEP_SQL` share
	// — including the `lease_expires_at IS NULL` half, which is not
	// decoration: `NULL < NOW()` is NULL rather than true, so a `running`
	// row with a null lease would otherwise report itself healthy while
	// being exactly the strand that clause was added to catch.
	//
	// ⚠ IT IS THAT BRANCH AND NOT "RECLAIMABLE". Both statements add a
	// condition on `attempts` that this expression does not read: the claim
	// wants `attempts < max_attempts` and the sweeper wants
	// `attempts >= max_attempts`. So a stalled job at the cap is not
	// waiting for a worker — the claim refuses it and the next sweep writes
	// `dead`. A caller that wants "will be retried" compares `attempts`
	// with `max_attempts` as well (PR #43's review measured the case).
	LeaseExpiresAt *time.Time `json:"lease_expires_at"`
	Stalled        bool       `json:"stalled"`

	// Status is what the job is doing, in one word a UI can switch on:
	// queued, scheduled, retrying, deferred_suspended, running, stalled,
	// dead_pending, completed, dead or superseded (22.1-03). It is computed
	// in SQL against the database clock, by the CASE in jobColumnsSQL, and
	// read from the job row and its installation, never from sync_state.
	//
	// The vocabulary, its precedence and what a UI shows for each value are
	// docs/api-ingestion-jobs.md's status table, the one authority; this
	// struct keeps no copy. A consumer still needs a default branch: the
	// CASE ends in the bare state, so a state added later reads as itself.
	Status string `json:"status"`

	// LastStage is coarse resumability, not a progress bar.
	//
	// ADVISORY rather than an enum: migration 000014 declares it `TEXT` with
	// no `CHECK`, and the worker writes whatever sanitised string a handler
	// reports, so a consumer switching on it needs a default branch. The
	// stage vocabulary and the progress contract are
	// docs/api-ingestion-jobs.md's; this struct keeps no copy.
	//
	// Progress is whatever the handler reported, redacted.
	LastStage *string         `json:"last_stage"`
	Progress  json.RawMessage `json:"progress"`

	// NeedsRerun means a push arrived while this job was already live
	// (21-CONTEXT L7). The worker re-queues once on completion and clears it.
	NeedsRerun bool `json:"needs_rerun"`

	LastError      *string `json:"last_error"`
	IngestionRunID *string `json:"ingestion_run_id"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (j *IngestionJob) Render(http.ResponseWriter, *http.Request) error { return nil }

// jobByIDSQL reads one job for one organization.
//
// ⚠ `AND j.organization_id = $2` IS THE ONLY TENANT GUARD THERE IS.
// `ingestion_jobs` has no row-level security, by decision (21-CONTEXT L5): a
// worker claims a job BEFORE it knows the tenant — `organization_id` is on the
// row it is trying to claim — so scoping the claim by the answer would be
// circular. The cost of that decision is paid here. Delete the predicate and
// this statement returns any tenant's job, with no error, no empty result and
// nothing in the database to stop it. PR #38's review measured the same shape
// from the other side: an unscoped session claiming another organization's job.
//
// The two LEFT JOINs exist for `status` alone: `deferred_suspended` reads the
// repository's installation, the way the worker's claim-time check does
// (INSTALLATION_SQL in workers/jobs/runtime.py). Both joined tables carry
// row-level security and this runs inside the caller's tenant transaction,
// so they can only ever reach the caller's own rows; and they are LEFT joins
// so that a job whose repository has no installation is still returned.
// They add no tenant guard and remove none: the job's own organization
// filter below stays the whole boundary.
const jobByIDSQL = `
SELECT ` + jobColumnsSQL + `
FROM ingestion_jobs j
LEFT JOIN repositories r ON r.id = j.repository_id
LEFT JOIN github_installations gi ON gi.id = r.installation_id
WHERE j.id = $1 AND j.organization_id = $2`

// jobColumnsSQL is the job object's SELECT list, shared by the two readers
// of ingestion_jobs that answer a tenant: jobByIDSQL above and
// currentJobJoinSQL (repositories.go). One list, so GET /api/admin/jobs/{id}
// and a repository's current_job are the same object by construction.
//
// It is written against two aliases the caller must provide: j for
// ingestion_jobs and gi for the repository's github_installations row (LEFT
// joined; NULL when there is none). Under a LEFT JOIN that found no job,
// every column is NULL, which is why jobScan scans into nullable targets.
//
// `stalled` and `status` are evaluated here rather than in Go so they use
// the database clock: the same clock `claimSQL` and `_SWEEP_SQL` compare
// against. A Go-side comparison would answer a slightly different question
// on any machine whose clock differs from the server's, which is every
// machine.
//
// `status` is docs/api-ingestion-jobs.md's status table, in its order, and
// the first match wins. Three branches are worth reading twice:
//
//   - dead_pending comes first and reads attempts against max_attempts, the
//     condition `stalled` alone does not (see IngestionJob.LeaseExpiresAt).
//     Its running arm keeps the lease condition: the claim increments
//     attempts, so a HEALTHY final attempt also has attempts equal to
//     max_attempts and must read running, not dead_pending.
//   - deferred_suspended reads the installation row, never the wording of
//     last_error. An uninstalled installation is not suspended, whatever
//     suspended_at says.
//   - the CASE ends in the bare state, so a state no branch names still
//     yields a value rather than NULL.
const jobColumnsSQL = `
       j.id::text, j.repository_id::text, j.job_type, j.state,
       j.attempts, j.max_attempts, j.run_after,
       j.lease_expires_at,
       (j.state = 'running'
        AND (j.lease_expires_at IS NULL OR j.lease_expires_at < NOW())) AS stalled,
       CASE
         WHEN j.attempts >= j.max_attempts
              AND (j.state = 'queued'
                   OR (j.state = 'running'
                       AND (j.lease_expires_at IS NULL OR j.lease_expires_at < NOW())))
           THEN 'dead_pending'
         WHEN j.state = 'running'
              AND (j.lease_expires_at IS NULL OR j.lease_expires_at < NOW())
           THEN 'stalled'
         WHEN j.state = 'running' THEN 'running'
         WHEN j.state = 'queued'
              AND gi.suspended_at IS NOT NULL AND gi.uninstalled_at IS NULL
           THEN 'deferred_suspended'
         WHEN j.state = 'queued' AND j.attempts > 0 THEN 'retrying'
         WHEN j.state = 'queued' AND j.run_after > NOW() THEN 'scheduled'
         WHEN j.state = 'queued' THEN 'queued'
         ELSE j.state
       END AS status,
       j.last_stage, j.progress, j.needs_rerun, j.last_error,
       j.ingestion_run_id::text,
       j.created_at, j.updated_at`

// jobScan receives jobColumnsSQL's columns.
//
// Every target is nullable because a repository with no job yields a row of
// NULLs from the LEFT JOINs, and pgx refuses to scan NULL into a string, an
// int32, a time.Time or a bool. job() turns it back into an *IngestionJob,
// nil when there was no job. jobByIDSQL, which never has a NULL job, uses
// the same struct, so the column-to-field mapping exists once.
type jobScan struct {
	id, repositoryID, jobType, state *string
	attempts, maxAttempts            *int32
	runAfter, leaseExpiresAt         *time.Time
	stalled                          *bool
	status                           *string
	lastStage                        *string
	progress                         []byte
	needsRerun                       *bool
	lastError, ingestionRunID        *string
	createdAt, updatedAt             *time.Time
}

// scanTargets returns the addresses Scan writes jobColumnsSQL into, in its
// order. A caller selecting more columns first appends these to its own.
func (s *jobScan) scanTargets() []any {
	return []any{
		&s.id, &s.repositoryID, &s.jobType, &s.state,
		&s.attempts, &s.maxAttempts, &s.runAfter,
		&s.leaseExpiresAt, &s.stalled, &s.status,
		&s.lastStage, &s.progress, &s.needsRerun, &s.lastError,
		&s.ingestionRunID,
		&s.createdAt, &s.updatedAt,
	}
}

// job returns the scanned job, or nil when the row carried none.
//
// The non-id columns of an existing job are NOT NULL in the schema
// (000014), so once the id is present they are too; deref still tolerates a
// nil rather than panicking on a schema change.
func (s *jobScan) job() *IngestionJob {
	if s.id == nil {
		return nil
	}
	job := &IngestionJob{
		ID:             *s.id,
		RepositoryID:   deref(s.repositoryID),
		JobType:        deref(s.jobType),
		State:          deref(s.state),
		Attempts:       deref(s.attempts),
		MaxAttempts:    deref(s.maxAttempts),
		RunAfter:       deref(s.runAfter),
		LeaseExpiresAt: s.leaseExpiresAt,
		Stalled:        deref(s.stalled),
		Status:         deref(s.status),
		LastStage:      s.lastStage,
		NeedsRerun:     deref(s.needsRerun),
		LastError:      s.lastError,
		IngestionRunID: s.ingestionRunID,
		CreatedAt:      deref(s.createdAt),
		UpdatedAt:      deref(s.updatedAt),
	}
	// A NULL jsonb scans as a nil []byte, and a nil json.RawMessage marshals
	// as `null`, which is the right answer for "this job has reported no
	// progress", and not the same as `{}`.
	job.Progress = json.RawMessage(s.progress)
	return job
}

func deref[T any](p *T) T {
	var zero T
	if p == nil {
		return zero
	}
	return *p
}

// Get handles GET /api/admin/jobs/{id}.
//
// ONE 404 FOR EVERY MISS. "No such job", "another organization's job" and "that
// is not a UUID" return an identical status and an identical body, on purpose:
// telling them apart would make this endpoint an existence oracle for other
// tenants' job ids. `repositories.go` and `github_install.go` take the same
// line, and `jobs_isolation_test.go` asserts the three responses are
// byte-identical rather than merely all 404.
func (h *JobsHandler) Get(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// THE ORGANIZATION COMES FROM THE VERIFIED CLAIM AND FROM NOWHERE ELSE.
	// TenantMiddleware should have refused a claim-less caller already; this
	// is belt and braces, because the value is the entire tenant boundary for
	// the statement below.
	//
	// It is read BEFORE the id is parsed so a request with no tenant is 403
	// whatever the id looks like — an ordering that also makes the guard
	// testable by calling the handler directly, with no chi route context.
	orgID, ok := auth.OrgIDFromContext(ctx)
	if !ok || orgID == "" {
		render.Render(w, r, ErrForbidden())
		return
	}

	id, ok := canonicalUUID(chi.URLParam(r, "id"))
	if !ok {
		// 404, not 400, and the same body as "not found" below.
		render.Render(w, r, ErrNotFound())
		return
	}

	var scanned jobScan
	err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, jobByIDSQL, id, orgID).Scan(scanned.scanTargets()...)
	})

	if errors.Is(err, pgx.ErrNoRows) {
		// Another organization's job lands here too, because the statement
		// filtered it out rather than because anything refused it. Identical
		// to "does not exist", deliberately.
		render.Render(w, r, ErrNotFound())
		return
	}
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("get ingestion job: %w", err)))
		return
	}

	render.Render(w, r, scanned.job())
}
