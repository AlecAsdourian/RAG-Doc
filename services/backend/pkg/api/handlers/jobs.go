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
// ⚠ THIS IS THE ONLY HTTP HANDLER OVER `ingestion_jobs`, AND THAT TABLE HAS
// NO ROW-LEVEL SECURITY (21-CONTEXT L5). Every other tenant handler in this
// package is wrong-by-default-safe: a cross-tenant read of `repositories` or
// `github_installations` returns zero rows because a policy refuses it. Here
// nothing refuses it. `AND organization_id = $2` in jobByIDSQL below is the
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
// but the read joins nothing and needs no pool, and holding one would make an
// unscoped query expressible for no benefit.
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

	// LastStage is coarse resumability, not a progress bar.
	//
	// Conventionally one of clone|parse|embed|store, and ADVISORY rather
	// than an enum: migration 000014 declares it `TEXT` with those four
	// values in a comment and no `CHECK`, and the worker writes whatever
	// sanitised string a handler reports. A consumer switching on it needs
	// a default branch. Constraining it is a migration, which this plan
	// deliberately does not ship.
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
// ⚠ `AND organization_id = $2` IS THE ONLY TENANT GUARD THERE IS.
// `ingestion_jobs` has no row-level security, by decision (21-CONTEXT L5): a
// worker claims a job BEFORE it knows the tenant — `organization_id` is on the
// row it is trying to claim — so scoping the claim by the answer would be
// circular. The cost of that decision is paid here. Delete the predicate and
// this statement returns any tenant's job, with no error, no empty result and
// nothing in the database to stop it. PR #38's review measured the same shape
// from the other side: an unscoped session claiming another organization's job.
//
// `stalled` is evaluated here rather than in Go so it uses the database clock
// — the same clock `claimSQL` and `_SWEEP_SQL` compare against. A Go-side
// comparison would answer a slightly different question on any machine whose
// clock differs from the server's, which is every machine.
const jobByIDSQL = `
SELECT id::text, repository_id::text, job_type, state,
       attempts, max_attempts, run_after,
       lease_expires_at,
       (state = 'running'
        AND (lease_expires_at IS NULL OR lease_expires_at < NOW())) AS stalled,
       last_stage, progress, needs_rerun, last_error,
       ingestion_run_id::text,
       created_at, updated_at
FROM ingestion_jobs
WHERE id = $1 AND organization_id = $2`

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

	var job IngestionJob
	var progress []byte
	err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, jobByIDSQL, id, orgID).Scan(
			&job.ID, &job.RepositoryID, &job.JobType, &job.State,
			&job.Attempts, &job.MaxAttempts, &job.RunAfter,
			&job.LeaseExpiresAt, &job.Stalled,
			&job.LastStage, &progress, &job.NeedsRerun, &job.LastError,
			&job.IngestionRunID,
			&job.CreatedAt, &job.UpdatedAt,
		)
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

	// A NULL jsonb scans as a nil []byte, and a nil json.RawMessage marshals
	// as `null` — which is the right answer for "this job has reported no
	// progress", and not the same as `{}`.
	job.Progress = json.RawMessage(progress)

	render.Render(w, r, &job)
}
