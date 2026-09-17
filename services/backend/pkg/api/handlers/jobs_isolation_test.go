package handlers_test

// Isolation tests for GET /api/admin/jobs/{id}.
//
// ⚠ THIS FILE EXISTS BECAUSE NOTHING WOULD HAVE ASKED FOR IT.
//
// Every other isolation test in this package was demanded by the 17-05 CI
// ratchet: add a mutation route, and `scripts/ci/check-isolation-tests.py`
// fails the PR until a test in the same diff names its path. That gate matches
// POST/PUT/PATCH/DELETE only (its ENDPOINT_PATTERNS), and only route lines
// added in the diff, so the GET this file covers passes it with no test at all.
//
// And the database would not have caught a mistake either. `repositories` and
// `github_installations` carry row-level security, so a cross-tenant read of
// them returns zero rows because a policy refused it, and the handler's own
// filtering is a second layer. `ingestion_jobs` has NO row-level security, by
// decision (21-CONTEXT L5), so `AND organization_id = $2` in `jobByIDSQL` is
// the first layer and the last one.
//
// Two mechanisms that normally overlap are therefore both absent, which is why
// Scenario2 is MUTATION-CHECKED rather than merely written: deleting that
// predicate must fail it. The run is recorded in 21-07-SUMMARY.md.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// seedJobRow is one `ingestion_jobs` row a test wants to exist.
//
// Every field is explicit rather than defaulted, because several of these
// tests are about what the endpoint does with a PARTICULAR shape, and a
// fixture that quietly supplied one state everywhere would make them pass for
// the wrong reason. That is 21-06's finding twice over: every repository in
// its Python fixture started `installation_id IS NULL`, which is one branch of
// three, and a suite built on the bare fixture could not tell the branches
// apart while still passing.
type seedJobRow struct {
	jobType    string
	state      string
	attempts   int
	lease      *time.Duration // nil => lease_expires_at IS NULL
	leaseOwner string
	lastStage  string
	progress   string // raw JSON; "" => NULL
	needsRerun bool
	lastError  string
	payload    string // raw JSON; "" => NULL
}

// seedJob inserts one job for org/repo and returns its id.
//
// It runs inside a tenant-scoped transaction because `trg_ingestion_jobs_tenant`
// reads `repositories`, which carries FORCE ROW LEVEL SECURITY: an unscoped
// insert is refused with "repository ... does not exist" (migration 000014).
func seedJob(t *testing.T, pool *pgxpool.Pool, orgID, repoID string, row seedJobRow) string {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)

	var leaseExpiry, owner, stage, progress, lastErr, payload any
	if row.lease != nil {
		leaseExpiry = time.Now().Add(*row.lease)
	}
	if row.leaseOwner != "" {
		owner = row.leaseOwner
	}
	if row.lastStage != "" {
		stage = row.lastStage
	}
	if row.progress != "" {
		progress = row.progress
	}
	if row.lastError != "" {
		lastErr = row.lastError
	}
	if row.payload != "" {
		payload = row.payload
	}

	var id string
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID),
		func(tx pgx.Tx) error {
			return tx.QueryRow(ctx, `
				INSERT INTO ingestion_jobs
				  (organization_id, repository_id, job_type, state, attempts,
				   run_after, lease_owner, lease_expires_at, last_stage,
				   progress, needs_rerun, last_error, payload)
				VALUES ($1, $2, $3, $4, $5, NOW(), $6, $7, $8, $9, $10, $11, $12)
				RETURNING id::text
			`, orgID, repoID, row.jobType, row.state, row.attempts,
				owner, leaseExpiry, stage, progress, row.needsRerun, lastErr, payload,
			).Scan(&id)
		}))
	return id
}

// seedJobRepo creates one extra repository for org and returns its id.
//
// `idx_ingestion_jobs_one_live_per_repo` allows exactly one queued-or-running
// job per repository, so every test that needs its own LIVE job needs its own
// repository. Sharing one would make the second insert fail with 23505 — an
// error about the fixture rather than about the endpoint.
func seedJobRepo(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, name string) string {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)

	var id string
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, org.ID),
		func(tx pgx.Tx) error {
			return tx.QueryRow(ctx, `
				INSERT INTO repositories (project_id, name, git_url)
				VALUES ($1, $2, $3) RETURNING id::text
			`, org.ProjectID, name,
				fmt.Sprintf("https://example.test/%s-%s.git", org.Slug, name)).Scan(&id)
		}))
	return id
}

// setSyncState writes `repositories.sync_state` directly, so a test can build
// the disagreement between the projection and the job row that the endpoint
// exists to resolve the right way round.
func setSyncState(t *testing.T, pool *pgxpool.Pool, orgID, repoID, state string) {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID),
		func(tx pgx.Tx) error {
			_, err := tx.Exec(ctx,
				`UPDATE repositories SET sync_state = $2 WHERE id = $1`, repoID, state)
			return err
		}))
}

// readSyncState reads the projection back, so a test can assert its PREMISE
// before asserting the thing it is about. Without this, "the endpoint said
// running" would be consistent with the projection never having been set.
func readSyncState(t *testing.T, pool *pgxpool.Pool, orgID, repoID string) string {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)
	var state string
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID),
		func(tx pgx.Tx) error {
			return tx.QueryRow(ctx,
				`SELECT sync_state FROM repositories WHERE id = $1`, repoID).Scan(&state)
		}))
	return state
}

// getJob performs the request and returns (status, raw body).
func getJob(t *testing.T, baseURL, path, token string) (int, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, baseURL+path, nil)
	require.NoError(t, err)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	return resp.StatusCode, string(raw)
}

// decodeJob performs the request, requires 200 and returns the decoded body.
func decodeJob(t *testing.T, baseURL, id, token string) handlers.IngestionJob {
	t.Helper()
	status, body := getJob(t, baseURL, "/api/admin/jobs/"+id, token)
	require.Equal(t, http.StatusOK, status, "body=%s", body)
	var job handlers.IngestionJob
	require.NoError(t, json.Unmarshal([]byte(body), &job), "body=%s", body)
	return job
}

// upperHex returns an id with its hex digits uppercased — a spelling
// `uuid.Parse` accepts and `canonicalUUID` refuses, because it is not the one
// this API emits.
func upperHex(id string) string {
	out := []rune(id)
	for i, r := range out {
		if r >= 'a' && r <= 'f' {
			out[i] = r - 32
		}
	}
	return string(out)
}

func TestJobsIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("the job endpoint must never call the RAG service; got %s", r.URL.Path)
			http.Error(w, "unexpected", http.StatusInternalServerError)
		}))
		t.Cleanup(deadRAG.Close)

		router := api.NewRouterWithValidatorAndAdmin(
			pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
			api.Config{LogLevel: slog.LevelWarn},
		)
		server := httptest.NewServer(router)
		t.Cleanup(server.Close)

		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		tokenB := testjwt.Sign(orgB.OwnerSupabaseID, orgB.ID, "owner")
		// A plain member, not an owner. The endpoint has NO role gate by
		// decision, and asserting that with an owner token would prove
		// nothing — every role-gated route in this codebase admits an owner.
		memberTokenA := testjwt.Sign(orgA.MemberSupabaseID, orgA.ID, "member")

		liveLease := 4 * time.Minute
		jobA := seedJob(t, pool, orgA.ID, orgA.RepoID, seedJobRow{
			jobType: "full_ingest", state: "running", attempts: 1,
			lease: &liveLease, leaseOwner: "worker-a-must-not-be-returned",
			lastStage: "parse",
			progress:  `{"files_parsed": 12, "chunks_embedded": 40}`,
			lastError: "previous attempt: git exited 128",
			payload:   `{"reason": "payload-must-not-be-returned"}`,
		})
		jobB := seedJob(t, pool, orgB.ID, orgB.RepoID, seedJobRow{
			jobType: "incremental", state: "queued", attempts: 0,
		})

		t.Run("Scenario1_AMemberReadsTheirOwnOrganizationsJob", func(t *testing.T) {
			status, body := getJob(t, server.URL, "/api/admin/jobs/"+jobA, memberTokenA)
			require.Equal(t, http.StatusOK, status, "body=%s", body)

			var job handlers.IngestionJob
			require.NoError(t, json.Unmarshal([]byte(body), &job), "body=%s", body)

			require.Equal(t, jobA, job.ID)
			require.Equal(t, orgA.RepoID, job.RepositoryID)
			require.Equal(t, "full_ingest", job.JobType)
			require.Equal(t, "running", job.State)
			require.EqualValues(t, 1, job.Attempts)
			require.EqualValues(t, 5, job.MaxAttempts, "migration 000014's default")
			require.NotNil(t, job.LastStage)
			require.Equal(t, "parse", *job.LastStage)
			require.JSONEq(t, `{"files_parsed": 12, "chunks_embedded": 40}`, string(job.Progress))
			require.False(t, job.NeedsRerun)
			require.NotNil(t, job.LastError)
			require.Equal(t, "previous attempt: git exited 128", *job.LastError)
			require.Nil(t, job.IngestionRunID)
			require.NotNil(t, job.LeaseExpiresAt, "a job under a live worker has a lease")
			require.False(t, job.Stalled, "the lease has four minutes left")

			// ⚠ THE KEY SET, NOT JUST THE ABSENCE OF TWO NAMES.
			//
			// `NotContains(body, "lease_owner")` would pass against a handler
			// that returned the worker id under any other name, and against
			// any future column added without thought. Asserting the exact
			// set makes "never return lease_owner or payload" a property of
			// the response rather than of two string literals, and it is what
			// kills the mutation that adds either column back.
			var raw map[string]json.RawMessage
			require.NoError(t, json.Unmarshal([]byte(body), &raw))
			keys := make([]string, 0, len(raw))
			for k := range raw {
				keys = append(keys, k)
			}
			require.ElementsMatch(t, []string{
				"id", "repository_id", "job_type", "state",
				"attempts", "max_attempts", "run_after",
				"lease_expires_at", "stalled",
				"last_stage", "progress", "needs_rerun", "last_error",
				"ingestion_run_id", "created_at", "updated_at",
			}, keys, "the response shape changed; lease_owner and payload must never appear")

			// And the values themselves, in case a future field carries one
			// of them under a name the set above happens to allow.
			require.NotContains(t, body, "worker-a-must-not-be-returned")
			require.NotContains(t, body, "payload-must-not-be-returned")
		})

		t.Run("Scenario2_AnotherOrganizationsJobIsIndistinguishableFromNoJob", func(t *testing.T) {
			// ⚠ THE TEST THIS FILE EXISTS FOR. `ingestion_jobs` has no
			// row-level security, so nothing beneath the handler refuses this
			// read: the statement's `AND organization_id = $2` is all of it.
			crossStatus, crossBody := getJob(t, server.URL, "/api/admin/jobs/"+jobB, tokenA)
			require.Equal(t, http.StatusNotFound, crossStatus,
				"orgA read orgB's job; body=%s", crossBody)
			require.NotContains(t, crossBody, "incremental",
				"the refusal must not leak the other tenant's job type")
			require.NotContains(t, crossBody, orgB.RepoID)

			// And the refusal must be INDISTINGUISHABLE from the other two
			// ways to miss, or the endpoint answers "does this job id exist?"
			// for every tenant in the system.
			nonexistentStatus, nonexistentBody := getJob(t, server.URL,
				"/api/admin/jobs/99999999-9999-9999-9999-999999999999", tokenA)
			malformedStatus, malformedBody := getJob(t, server.URL,
				"/api/admin/jobs/not-a-uuid", tokenA)

			require.Equal(t, nonexistentStatus, crossStatus)
			require.Equal(t, nonexistentBody, crossBody,
				"'another tenant's job' and 'no such job' must be byte-identical")
			require.Equal(t, malformedStatus, crossStatus)
			require.Equal(t, malformedBody, crossBody,
				"'malformed id' must be byte-identical too, or it is a 400 in disguise")

			// The row is still orgB's and still readable BY orgB. Without
			// this, a handler that 404'd everything would pass every
			// assertion above.
			ownersView := decodeJob(t, server.URL, jobB, tokenB)
			require.Equal(t, jobB, ownersView.ID)
			require.Equal(t, "incremental", ownersView.JobType)
		})

		t.Run("Scenario3_EveryMalformedSpellingIsTheSame404", func(t *testing.T) {
			_, want := getJob(t, server.URL,
				"/api/admin/jobs/99999999-9999-9999-9999-999999999999", tokenA)

			// `uuid.Parse` is a PARSER, not a validator: it accepts all of
			// these. Postgres accepts some and raises 22P02 on others, so
			// without `canonicalUUID` the malformed cases would split into
			// 404s and unhandled 500s depending on which spelling arrived —
			// measured on the cursor path before `canonicalUUID` existed.
			for _, spelling := range []string{
				"not-a-uuid",
				"urn:uuid:" + jobA,
				"{" + jobA + "}",
				"99999999999999999999999999999999",
				upperHex(jobA),
			} {
				status, body := getJob(t, server.URL, "/api/admin/jobs/"+spelling, tokenA)
				require.Equal(t, http.StatusNotFound, status, "spelling=%q", spelling)
				require.Equal(t, want, body, "spelling=%q must be the same 404", spelling)
			}

			// A bare `/api/admin/jobs/` matches no route; chi answers it.
			status, _ := getJob(t, server.URL, "/api/admin/jobs/", tokenA)
			require.Equal(t, http.StatusNotFound, status)
		})

		t.Run("Scenario4_AClaimlessTokenIsRefused", func(t *testing.T) {
			claimless := testjwt.SignWithoutOrg(orgA.OwnerSupabaseID)
			status, body := getJob(t, server.URL, "/api/admin/jobs/"+jobA, claimless)
			require.Equal(t, http.StatusForbidden, status,
				"a token with no organization claim must not reach the queue; body=%s", body)
			require.NotContains(t, body, jobA)
		})

		t.Run("Scenario5_TheHandlerRefusesAnOrganizationlessContextOnItsOwn", func(t *testing.T) {
			// Scenario4 proves TenantMiddleware refuses. This proves the
			// HANDLER refuses, which is a different statement: the middleware
			// could be dropped from the group, or the route moved out of it,
			// and Scenario4 would still pass while this one would not.
			//
			// No chi route context is installed, so `chi.URLParam` returns "".
			// The 403 therefore also pins the ORDER of the two checks — read
			// the organization first, parse the id second — because with them
			// the other way round this would be a 404.
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/api/admin/jobs/"+jobA, nil)
			handlers.NewJobsHandler(db.NewTenantScoper(pool)).Get(rec, req)
			require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
			require.NotContains(t, rec.Body.String(), jobA)
		})

		t.Run("Scenario6_TheRouteIsMountedUnderApiExactlyOnce", func(t *testing.T) {
			// The `/api` prefix comes from the enclosing chi Route. A handler
			// registered as "/api/admin/jobs/{id}" inside it would serve
			// /api/api/... — reachable by nothing, and a 404 indistinguishable
			// from a tenant refusal.
			status, body := getJob(t, server.URL, "/api/admin/jobs/"+jobA, tokenA)
			require.Equal(t, http.StatusOK, status, "body=%s", body)

			doubled, _ := getJob(t, server.URL, "/api/api/admin/jobs/"+jobA, tokenA)
			require.Equal(t, http.StatusNotFound, doubled,
				"/api/api/... must not resolve; the route would be double-prefixed")

			// And it is inside the authenticated group.
			unauthenticated, _ := getJob(t, server.URL, "/api/admin/jobs/"+jobA, "")
			require.Equal(t, http.StatusUnauthorized, unauthenticated)
		})

		// ⚠ 21-06's ruling, as tests. `sync_state = 'syncing'` is NOT evidence
		// of a live worker: `mark_started` writes it and `defer` writes no
		// projection at all, deliberately — so a crashed worker, a worker
		// whose connection died mid-job and a job deferred part-way through a
		// shutdown all leave `syncing` behind with nobody working, and they
		// always have. The evidence is on the JOB ROW.
		//
		// Every case below sets the projection to `syncing`, asserts that it
		// really is `syncing` (otherwise the test proves nothing), and then
		// asks the endpoint what is true.
		t.Run("Scenario7_LivenessComesFromTheJobRowNotFromSyncState", func(t *testing.T) {
			t.Run("ACrashedWorkerLeavesAnExpiredLeaseAndReadsAsStalled", func(t *testing.T) {
				repo := seedJobRepo(t, pool, orgA, "crashed-worker")
				expired := -30 * time.Second
				id := seedJob(t, pool, orgA.ID, repo, seedJobRow{
					jobType: "full_ingest", state: "running", attempts: 2,
					lease: &expired, leaseOwner: "worker-that-died",
					lastStage: "embed",
				})
				setSyncState(t, pool, orgA.ID, repo, "syncing")
				require.Equal(t, "syncing", readSyncState(t, pool, orgA.ID, repo),
					"premise: the projection must say syncing, or this test proves nothing")

				job := decodeJob(t, server.URL, id, tokenA)
				require.Equal(t, "running", job.State)
				require.True(t, job.Stalled,
					"an expired lease on a running job is the stalled predicate "+
						"CLAIM_SQL and _SWEEP_SQL use; the UI must not read syncing as alive")
				require.NotNil(t, job.LeaseExpiresAt)
				require.True(t, job.LeaseExpiresAt.Before(time.Now()),
					"the evidence itself, not only the derived flag")
			})

			t.Run("ARunningJobWithANullLeaseReadsAsStalled", func(t *testing.T) {
				// THE NULL HALF OF THE PREDICATE, which is not decoration:
				// `NULL < NOW()` is NULL rather than true, so a version of
				// `stalled` written as `lease_expires_at < NOW()` alone
				// reports this row healthy — while it is exactly the strand
				// 21-RESEARCH's second correction added `lease_expires_at IS
				// NULL` to `claimSQL` to catch. This row is invisible to a
				// naive liveness check and still occupies the partial unique
				// index, blocking every future job for its repository.
				repo := seedJobRepo(t, pool, orgA, "null-lease-strand")
				id := seedJob(t, pool, orgA.ID, repo, seedJobRow{
					jobType: "incremental", state: "running", attempts: 1,
					lease: nil, leaseOwner: "worker-whose-lease-vanished",
				})
				setSyncState(t, pool, orgA.ID, repo, "syncing")
				require.Equal(t, "syncing", readSyncState(t, pool, orgA.ID, repo))

				job := decodeJob(t, server.URL, id, tokenA)
				require.Equal(t, "running", job.State)
				require.Nil(t, job.LeaseExpiresAt)
				require.True(t, job.Stalled,
					"a running job with a NULL lease is stalled; "+
						"`lease_expires_at < NOW()` alone would call it healthy")
			})

			t.Run("AJobDeferredPartWayThroughAShutdownReadsAsQueued", func(t *testing.T) {
				// 21-06's `Unfinished` ending: `defer` hands the attempt back
				// and writes NO projection, so the repository is left at
				// `syncing` while the job is queued and claimable that
				// instant. The endpoint must say `queued`, not repeat the
				// projection.
				repo := seedJobRepo(t, pool, orgA, "deferred-on-shutdown")
				id := seedJob(t, pool, orgA.ID, repo, seedJobRow{
					jobType: "full_ingest", state: "queued", attempts: 0,
					lastStage: "clone",
				})
				setSyncState(t, pool, orgA.ID, repo, "syncing")
				require.Equal(t, "syncing", readSyncState(t, pool, orgA.ID, repo))

				job := decodeJob(t, server.URL, id, tokenA)
				require.Equal(t, "queued", job.State)
				require.EqualValues(t, 0, job.Attempts, "defer returns the attempt")
				require.False(t, job.Stalled, "a queued job is not a stalled running one")
				require.Nil(t, job.LeaseExpiresAt)
				require.NotNil(t, job.LastStage)
				require.Equal(t, "clone", *job.LastStage,
					"the breadcrumb the replacement worker resumes from")
			})

			t.Run("ARetryingJobIsQueuedWithAttemptsAboveZero", func(t *testing.T) {
				// There is no `failed` job state (decision O2). "Currently
				// retrying" is `state == queued && attempts > 0` with
				// `run_after` in the future, and this endpoint is one of the
				// two readers 21-CONTEXT names for that rule.
				repo := seedJobRepo(t, pool, orgA, "retrying")
				id := seedJob(t, pool, orgA.ID, repo, seedJobRow{
					jobType: "incremental", state: "queued", attempts: 3,
					lastError: "clone failed: exited 128",
				})
				job := decodeJob(t, server.URL, id, tokenA)
				require.Equal(t, "queued", job.State)
				require.EqualValues(t, 3, job.Attempts)
				require.NotNil(t, job.LastError)
				require.False(t, job.Stalled)
			})
		})
	})
}
