package handlers_test

// Isolation and contract tests for `current_job` on the repository API
// (ISS-034, shape 1) and for `status` on the job object (22.1-03).
//
// ⚠ THIS FILE EXISTS BECAUSE NOTHING WOULD HAVE ASKED FOR IT, like
// jobs_isolation_test.go and for the same two reasons.
//
//   - No route was added. `current_job` rides on GET /api/repositories, GET
//     /api/repositories/{id} and POST /api/repositories, which already have
//     isolation tests, and `scripts/ci/check-isolation-tests.py` matches only
//     POST/PUT/PATCH/DELETE route lines ADDED in a diff. So the gate passes
//     this change with or without this file (Scenario7, run as a process
//     check and recorded in 22.1-03-SUMMARY.md).
//   - `ingestion_jobs` has NO row-level security (21-CONTEXT L5). The two
//     `organization_id = $1` filters in currentJobJoinSQL are the tenant
//     guard on this read, and nothing in the database applies them for us.
//
// Worse than jobs.go's case: here the filters are MASKED. The job is reached
// by correlating on a repository the caller can already see under
// `repositories`' row-level security, and ingestion_jobs_repo_tenant_fk makes
// a job's organization equal its repository's, so no request a test can make
// observes a neutered filter. Scenario4 therefore plants the row each filter
// exists for (another organization's job on the caller's repository) in a
// superuser transaction under replica mode that is never committed, and runs
// the PRODUCTION statements (export_test.go) inside it as the app role.
//
// Every scenario below is mutation-checked; the table and its results are in
// 22.1-03-SUMMARY.md.

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
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
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// currentJobAppRole is the harness's non-superuser role, spelled out because
// pkg/testing/isolation keeps its constant (appRole, container.go)
// unexported, and exporting it for one caller is not worth the surface.
const currentJobAppRole = "rag_doc_app"

// jobObjectKeys is the job object's contract: GET /api/admin/jobs/{id} and
// every `current_job` carry exactly these keys. A LITERAL list, never one
// derived from the struct, so that adding a column to the struct and the
// statement together (`lease_owner`, say) fails here.
var jobObjectKeys = []string{
	"id", "repository_id", "job_type", "state",
	"attempts", "max_attempts", "run_after",
	"lease_expires_at", "stalled", "status",
	"last_stage", "progress", "needs_rerun", "last_error",
	"ingestion_run_id", "created_at", "updated_at",
}

// newCurrentJobServer is the real router with a RAG client that fails the
// test if called, as jobs_isolation_test.go builds it.
func newCurrentJobServer(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("the repository endpoints must never call the RAG service; got %s", r.URL.Path)
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(deadRAG.Close)
	router := api.NewRouterWithValidatorAndAdmin(
		pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
		api.Config{LogLevel: slog.LevelWarn},
	)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	return server.URL
}

// rawRepository is a repository response decoded only far enough to reach
// `current_job` as raw JSON, so a test can tell `null` from an absent key.
type rawRepository struct {
	ID         string          `json:"id"`
	CurrentJob json.RawMessage `json:"current_job"`
}

// listCurrentJobs GETs the first page (limit 100) as token and returns
// repository id -> current_job (raw), with the raw body.
func listCurrentJobs(t *testing.T, baseURL, token string) (map[string]json.RawMessage, string) {
	t.Helper()
	status, body := doRepoRequest(t, baseURL, http.MethodGet, "/api/repositories?limit=100", token, "")
	require.Equal(t, http.StatusOK, status, "body=%s", body)
	var page struct {
		Repositories []map[string]json.RawMessage `json:"repositories"`
		NextCursor   *string                      `json:"next_cursor"`
	}
	require.NoError(t, json.Unmarshal([]byte(body), &page), "body=%s", body)
	require.Nil(t, page.NextCursor, "premise: every repository fits on one page")
	out := map[string]json.RawMessage{}
	for _, repo := range page.Repositories {
		var id string
		require.NoError(t, json.Unmarshal(repo["id"], &id))
		current, present := repo["current_job"]
		require.True(t, present, "every repository carries the current_job key; repository %s did not", id)
		out[id] = current
	}
	return out, body
}

// getRepositoryCurrentJob GETs one repository as token and returns its raw
// current_job, with the raw body.
func getRepositoryCurrentJob(t *testing.T, baseURL, token, repoID string) (json.RawMessage, string) {
	t.Helper()
	status, body := doRepoRequest(t, baseURL, http.MethodGet, "/api/repositories/"+repoID, token, "")
	require.Equal(t, http.StatusOK, status, "body=%s", body)
	var raw map[string]json.RawMessage
	require.NoError(t, json.Unmarshal([]byte(body), &raw), "body=%s", body)
	current, present := raw["current_job"]
	require.True(t, present, "the current_job key must be present; body=%s", body)
	return current, body
}

// decodeCurrentJob requires a non-null current_job and decodes it.
func decodeCurrentJob(t *testing.T, raw json.RawMessage) handlers.IngestionJob {
	t.Helper()
	require.NotEqual(t, "null", string(raw), "expected a current job, got null")
	var job handlers.IngestionJob
	require.NoError(t, json.Unmarshal(raw, &job), "current_job=%s", raw)
	return job
}

func keysOf(t *testing.T, raw json.RawMessage) []string {
	t.Helper()
	var m map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw, &m), "raw=%s", raw)
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}

func ago(d time.Duration) *time.Time {
	at := time.Now().Add(-d)
	return &at
}

// ---------------------------------------------------------------------
// Scenario1 and Scenario2: the tenant boundary, through the real router
// ---------------------------------------------------------------------

func TestRepositoriesCurrentJob_Isolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		url := newCurrentJobServer(t, pool)
		tokenB := testjwt.Sign(orgB.OwnerSupabaseID, orgB.ID, "owner")
		// A plain member: the repository endpoints have no role gate (21-07's
		// decision for the job endpoint, unchanged here), and an owner token
		// would prove nothing about that.
		memberTokenA := testjwt.Sign(orgA.MemberSupabaseID, orgA.ID, "member")
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")

		// THE FIXTURE MUST TELL THE TWO CORRELATIONS APART. P has a LIVE job
		// created LATER; Q has only a TERMINAL job, created earlier, so Q's
		// lookup falls through to the newest-job branch. A neutered live
		// correlation hands Q P's live job, and a neutered newest correlation
		// hands Q P's newer job: with one repository, or with Q's job the
		// newer, neither mutation would show.
		liveLease := 4 * time.Minute
		repoP := seedJobRepo(t, pool, orgA, "current-p")
		repoQ := seedJobRepo(t, pool, orgA, "current-q")
		jobQ := seedJob(t, pool, orgA.ID, repoQ, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1,
			lastStage: "store", progress: `{"chunks": 3}`, createdAt: ago(time.Hour),
		})
		jobP := seedJob(t, pool, orgA.ID, repoP, seedJobRow{
			jobType: "full_ingest", state: "running", attempts: 1,
			lease: &liveLease, leaseOwner: "worker-p-must-not-be-returned",
			lastStage: "parse",
			payload:   `{"reason": "payload-p-must-not-be-returned"}`,
		})

		// B's own repositories and jobs, for Scenario2.
		repoB2 := seedJobRepo(t, pool, orgB, "current-b2")
		jobB1 := seedJob(t, pool, orgB.ID, orgB.RepoID, seedJobRow{
			jobType: "incremental", state: "queued", attempts: 0,
		})
		jobB2 := seedJob(t, pool, orgB.ID, repoB2, seedJobRow{
			jobType: "full_ingest", state: "dead", attempts: 5, lastError: "b-dead-must-stay-in-b",
		})

		t.Run("Scenario1_AMemberReadsTheirOwnOrganizationsCurrentJobs", func(t *testing.T) {
			list, body := listCurrentJobs(t, url, memberTokenA)
			require.Len(t, list, 3, "orgA has its fixture repository, P and Q")

			require.Equal(t, jobP, decodeCurrentJob(t, list[repoP]).ID, "P's current job is its live job")
			require.Equal(t, jobQ, decodeCurrentJob(t, list[repoQ]).ID,
				"Q's current job is its own terminal job, not P's live or newer one")
			require.Equal(t, "null", string(list[orgA.RepoID]), "a repository with no job reads null")

			for _, repo := range []struct{ id, job string }{{repoP, jobP}, {repoQ, jobQ}} {
				current, getBody := getRepositoryCurrentJob(t, url, memberTokenA, repo.id)
				job := decodeCurrentJob(t, current)
				require.Equal(t, repo.job, job.ID, "GET /{id} agrees with the list")
				require.Equal(t, repo.id, job.RepositoryID)

				// ⚠ THE KEY SET, NOT JUST THE ABSENCE OF TWO NAMES.
				require.ElementsMatch(t, jobObjectKeys, keysOf(t, current),
					"current_job's shape changed; lease_owner and payload must never appear")
				require.NotContains(t, getBody, "worker-p-must-not-be-returned")
				require.NotContains(t, getBody, "payload-p-must-not-be-returned")
			}
			require.ElementsMatch(t, jobObjectKeys, keysOf(t, list[repoP]))
			require.NotContains(t, body, "worker-p-must-not-be-returned")
			require.NotContains(t, body, "payload-p-must-not-be-returned")

			// The same object the job endpoint returns, field for field.
			status, jobBody := getJob(t, url, "/api/admin/jobs/"+jobP, memberTokenA)
			require.Equal(t, http.StatusOK, status, "body=%s", jobBody)
			current, _ := getRepositoryCurrentJob(t, url, memberTokenA, repoP)
			var fromJobs, fromRepo map[string]any
			require.NoError(t, json.Unmarshal([]byte(jobBody), &fromJobs))
			require.NoError(t, json.Unmarshal(current, &fromRepo))
			delete(fromJobs, "updated_at") // no write happens between the reads, but stay robust
			delete(fromRepo, "updated_at")
			require.Equal(t, fromJobs, fromRepo,
				"current_job must be the object GET /api/admin/jobs/{id} returns")
		})

		t.Run("Scenario2_AnotherOrganizationIsInvisibleAndIts404IsUnchanged", func(t *testing.T) {
			list, body := listCurrentJobs(t, url, tokenA)
			for _, id := range []string{orgB.RepoID, repoB2, jobB1, jobB2} {
				require.NotContains(t, body, id, "orgA's list carries orgB's id %s", id)
			}
			require.NotContains(t, body, "b-dead-must-stay-in-b")
			require.Len(t, list, 3)

			crossStatus, crossBody := doRepoRequest(t, url, http.MethodGet,
				"/api/repositories/"+orgB.RepoID, tokenA, "")
			nonexistentStatus, nonexistentBody := doRepoRequest(t, url, http.MethodGet,
				"/api/repositories/99999999-9999-9999-9999-999999999999", tokenA, "")
			malformedStatus, malformedBody := doRepoRequest(t, url, http.MethodGet,
				"/api/repositories/not-a-uuid", tokenA, "")
			require.Equal(t, http.StatusNotFound, crossStatus, "body=%s", crossBody)
			require.Equal(t, nonexistentStatus, crossStatus)
			require.Equal(t, nonexistentBody, crossBody,
				"'another tenant's repository' and 'no such repository' must be byte-identical")
			require.Equal(t, malformedStatus, crossStatus)
			require.Equal(t, malformedBody, crossBody, "'malformed id' must be byte-identical too")

			// The owner's view: B still reads its own. Without this, a handler
			// that hid every job would pass everything above.
			ownList, _ := listCurrentJobs(t, url, tokenB)
			require.Equal(t, jobB1, decodeCurrentJob(t, ownList[orgB.RepoID]).ID)
			deadJob := decodeCurrentJob(t, ownList[repoB2])
			require.Equal(t, jobB2, deadJob.ID)
			require.Equal(t, "dead", deadJob.Status)
			current, _ := getRepositoryCurrentJob(t, url, tokenB, orgB.RepoID)
			require.Equal(t, jobB1, decodeCurrentJob(t, current).ID)
		})
	})
}

// ---------------------------------------------------------------------
// Scenario3: "current" means what the doc says
// ---------------------------------------------------------------------

func TestRepositoriesCurrentJob_TheDefinitionOfCurrent(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		url := newCurrentJobServer(t, pool)
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")

		// (a) A live job OLDER than a terminal one: the live job. This is the
		// commit-order skew case: created_at is the transaction's start, so a
		// live job can carry an earlier timestamp than a job that finished.
		repoLiveOlder := seedJobRepo(t, pool, orgA, "def-live-older")
		liveOlder := seedJob(t, pool, orgA.ID, repoLiveOlder, seedJobRow{
			jobType: "full_ingest", state: "queued", attempts: 0, createdAt: ago(2 * time.Hour),
		})
		// The newer terminal job, which a newest-first rule would pick.
		_ = seedJob(t, pool, orgA.ID, repoLiveOlder, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1, createdAt: ago(time.Hour),
		})

		// (b) Only a dead job: it, with its last_error.
		repoDead := seedJobRepo(t, pool, orgA, "def-dead")
		dead := seedJob(t, pool, orgA.ID, repoDead, seedJobRow{
			jobType: "full_ingest", state: "dead", attempts: 5,
			lastError: "FetchFailed: the archive is not a readable gzip tar",
		})

		// (c) A completed job, then a newer superseded one: the superseded one.
		repoSuperseded := seedJobRepo(t, pool, orgA, "def-superseded")
		_ = seedJob(t, pool, orgA.ID, repoSuperseded, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1, createdAt: ago(2 * time.Hour),
		})
		superseded := seedJob(t, pool, orgA.ID, repoSuperseded, seedJobRow{
			jobType: "full_ingest", state: "superseded", attempts: 0, createdAt: ago(time.Hour),
		})

		// (d) No job: orgA's fixture repository.

		// (e) Two terminal jobs with an EQUAL created_at: the higher id.
		repoTie := seedJobRepo(t, pool, orgA, "def-tie")
		tie := ago(time.Hour)
		tieOne := seedJob(t, pool, orgA.ID, repoTie, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1, createdAt: tie,
		})
		tieTwo := seedJob(t, pool, orgA.ID, repoTie, seedJobRow{
			jobType: "full_ingest", state: "dead", attempts: 5, createdAt: tie,
		})
		// A uuid compares as its 16 bytes, which is the order of its
		// lowercase hex form, so the higher id is the greater string.
		higher := tieOne
		if tieTwo > tieOne {
			higher = tieTwo
		}

		list, _ := listCurrentJobs(t, url, tokenA)
		cases := []struct {
			name, repo, want string
		}{
			{"a_LiveOlderThanTerminal", repoLiveOlder, liveOlder},
			{"b_OnlyDead", repoDead, dead},
			{"c_CompletedThenSuperseded", repoSuperseded, superseded},
			{"e_EqualCreatedAtTakesTheHigherID", repoTie, higher},
		}
		for _, c := range cases {
			t.Run(c.name, func(t *testing.T) {
				current, _ := getRepositoryCurrentJob(t, url, tokenA, c.repo)
				require.Equal(t, c.want, decodeCurrentJob(t, current).ID, "GET /{id}")
				require.Equal(t, c.want, decodeCurrentJob(t, list[c.repo]).ID, "the list")
			})
		}

		t.Run("b_TheDeadJobCarriesItsLastError", func(t *testing.T) {
			job := decodeCurrentJob(t, list[repoDead])
			require.Equal(t, "dead", job.Status)
			require.NotNil(t, job.LastError)
			require.Equal(t, "FetchFailed: the archive is not a readable gzip tar", *job.LastError)
		})

		t.Run("d_NoJobIsNullWithTheKeyPresent", func(t *testing.T) {
			// On the RAW JSON: a decoded nil pointer cannot tell `null` from
			// an absent key, and the contract is the key, always.
			current, body := getRepositoryCurrentJob(t, url, tokenA, orgA.RepoID)
			require.Equal(t, "null", string(current))
			require.Contains(t, body, `"current_job":null`)
			require.Equal(t, "null", string(list[orgA.RepoID]))
		})
	})
}

// ---------------------------------------------------------------------
// Scenario4: the planted drift rows, the organization filters' own test
// ---------------------------------------------------------------------

func TestRepositoriesCurrentJob_ThePlantedDriftRows(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	require.Contains(t, handlers.RepositoryWithCurrentJobSQL, handlers.CurrentJobJoinSQL,
		"premise: Get's statement is built from the fragment")
	require.Contains(t, handlers.RepositoryListWithCurrentJobSQL, handlers.CurrentJobJoinSQL,
		"premise: List's statement is built from the fragment")

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// X and Y each hold ONE completed job of A's and no live job, so a
		// planted LIVE row cannot collide with idx_ingestion_jobs_one_live_per_repo
		// (replica mode switches off triggers, and with them the foreign keys,
		// but never a unique index).
		repoX := seedJobRepo(t, pool, orgA, "drift-x")
		repoY := seedJobRepo(t, pool, orgA, "drift-y")
		ownX := seedJob(t, pool, orgA.ID, repoX, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1, createdAt: ago(time.Hour),
		})
		ownY := seedJob(t, pool, orgA.ID, repoY, seedJobRow{
			jobType: "full_ingest", state: "completed", attempts: 1, createdAt: ago(time.Hour),
		})

		isolation.WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			// NEVER COMMITTED: the planted rows exist only inside this
			// transaction, however the test ends.
			defer func() { _ = tx.Rollback(ctx) }()

			// Transaction-scoped, so it cannot outlive this test.
			_, err = tx.Exec(ctx, `SET LOCAL session_replication_role = replica`)
			require.NoError(t, err)

			// (a) For the LIVE lookup: a queued job of B's on X. Under a
			// neutered live filter it wins whatever its created_at, because
			// the live lookup runs first.
			var plantedLive string
			require.NoError(t, tx.QueryRow(ctx, `
				INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state, created_at)
				VALUES ($1, $2, 'full_ingest', 'queued', NOW() - INTERVAL '2 hours')
				RETURNING id::text`, orgB.ID, repoX).Scan(&plantedLive),
				"the drifted live row must be ACCEPTED under replica mode: the key is a trigger")

			// (b) For the NEWEST lookup: a completed job of B's on Y, newer
			// than A's own.
			var plantedNewest string
			require.NoError(t, tx.QueryRow(ctx, `
				INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state, attempts, created_at)
				VALUES ($1, $2, 'full_ingest', 'completed', 1, NOW())
				RETURNING id::text`, orgB.ID, repoY).Scan(&plantedNewest))

			// Back to normal mode before anything reads: the read below is
			// the production statement as production runs it.
			_, err = tx.Exec(ctx, `SET LOCAL session_replication_role = origin`)
			require.NoError(t, err)

			// PREMISE: both rows are there, as the superuser sees them, with
			// B's organization on A's repositories.
			var planted int
			require.NoError(t, tx.QueryRow(ctx, `
				SELECT count(*) FROM ingestion_jobs
				WHERE organization_id = $1
				  AND ((id = $2 AND repository_id = $3) OR (id = $4 AND repository_id = $5))`,
				orgB.ID, plantedLive, repoX, plantedNewest, repoY).Scan(&planted))
			require.Equal(t, 2, planted, "premise: both drifted rows exist in this transaction")

			// As the app role, under A's tenant, like a request.
			_, err = tx.Exec(ctx, "SET ROLE "+currentJobAppRole)
			require.NoError(t, err)
			_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
			require.NoError(t, err)
			var who string
			require.NoError(t, tx.QueryRow(ctx, `SELECT current_user`).Scan(&who))
			require.Equal(t, currentJobAppRole, who,
				"the statement must run as the non-superuser app role, or row-level security is bypassed")

			want := map[string]string{repoX: ownX, repoY: ownY}
			forbidden := map[string]string{repoX: plantedLive, repoY: plantedNewest}

			// Get's statement, for X and for Y.
			for _, repo := range []string{repoX, repoY} {
				got := currentJobIDsFrom(t, tx, handlers.RepositoryWithCurrentJobSQL, orgA.ID, repo)
				require.Len(t, got, 1)
				require.NotEqual(t, forbidden[repo], got[repo],
					"Get: B's planted job became A's current job on %s; an organization filter is gone", repo)
				require.Equal(t, want[repo], got[repo], "Get: %s's current job is A's own completed job", repo)
			}

			// List's statement, the whole first page.
			got := currentJobIDsFrom(t, tx, handlers.RepositoryListWithCurrentJobSQL,
				orgA.ID, (*time.Time)(nil), (*string)(nil), 100)
			for _, repo := range []string{repoX, repoY} {
				require.NotEqual(t, forbidden[repo], got[repo],
					"List: B's planted job became A's current job on %s; an organization filter is gone", repo)
				require.Equal(t, want[repo], got[repo], "List: %s", repo)
			}

			require.NoError(t, tx.Rollback(ctx))
		})

		// And the rows are gone: nothing was committed.
		var left int
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT count(*) FROM ingestion_jobs WHERE repository_id IN ($1, $2) AND organization_id = $3`,
			repoX, repoY, orgB.ID).Scan(&left))
		require.Zero(t, left, "the planted rows must not outlive their transaction")
	})
}

// currentJobIDsFrom runs one of the exported production statements and
// returns repository id -> current job id ("" when NULL). The repository's
// id is the first column and the job's id the first column after the
// repository's twelve (repositoryColumnsSQL), both checked by name.
func currentJobIDsFrom(t *testing.T, tx pgx.Tx, sql string, args ...any) map[string]string {
	t.Helper()
	rows, err := tx.Query(context.Background(), sql, args...)
	require.NoError(t, err)
	defer rows.Close()
	fields := rows.FieldDescriptions()
	require.Greater(t, len(fields), 12)
	require.Equal(t, "id", fields[0].Name)
	require.Equal(t, "id", fields[12].Name, "the job's id follows the repository's twelve columns")
	require.Equal(t, "repository_id", fields[13].Name)
	out := map[string]string{}
	for rows.Next() {
		values, err := rows.Values()
		require.NoError(t, err)
		repo, _ := values[0].(string)
		job, _ := values[12].(string)
		out[repo] = job
	}
	require.NoError(t, rows.Err())
	return out
}

// ---------------------------------------------------------------------
// Scenario5: the connect response carries its job
// ---------------------------------------------------------------------

func TestRepositoriesCurrentJob_TheConnectResponseCarriesItsJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 2213501)
		lister := &stubLister{repos: []github.Repository{
			stubRepo("connect-current", "https://github.com/someone/connect-current.git"),
		}}
		url := newConnectServer(t, pool, lister)

		t.Run("ANewConnectReturnsTheJobItEnqueued", func(t *testing.T) {
			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			var raw map[string]json.RawMessage
			require.NoError(t, json.Unmarshal([]byte(body), &raw))
			current, present := raw["current_job"]
			require.True(t, present, "the 201 carries current_job; body=%s", body)
			var repo handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &repo))

			live := liveJobsFor(t, pool, repo.ID)
			require.Len(t, live, 1, "premise: the connect enqueued exactly one live job")
			job := decodeCurrentJob(t, current)
			require.Equal(t, live[0].ID, job.ID, "the 201's current_job is the job this connect enqueued")
			require.Equal(t, "queued", job.State)
			require.Equal(t, "queued", job.Status)
			require.Equal(t, repo.ID, job.RepositoryID)
			require.ElementsMatch(t, jobObjectKeys, keysOf(t, current))
		})

		t.Run("AReconnectThatEnqueuesNothingReturnsTheExistingJob", func(t *testing.T) {
			repoID := onlyRepositoryWithGitHubID(t, pool, orgA, stubRepoID)
			before := jobsFor(t, pool, repoID)
			require.Len(t, before, 1)
			claimJob(t, pool, before[0].ID, "worker-current")

			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)
			require.Len(t, jobsFor(t, pool, repoID), 1, "premise: an unchanged reconnect enqueues nothing")

			var raw map[string]json.RawMessage
			require.NoError(t, json.Unmarshal([]byte(body), &raw))
			job := decodeCurrentJob(t, raw["current_job"])
			require.Equal(t, before[0].ID, job.ID, "the reconnect's 201 carries the job already there")
			require.Equal(t, "running", job.State)
			require.Equal(t, "running", job.Status, "claimJob gives it a five-minute lease")
			require.NotContains(t, body, "worker-current", "never the lease owner")
		})
	})
}

// ---------------------------------------------------------------------
// Scenario6: every status, read from the job row and never from sync_state
// ---------------------------------------------------------------------

func TestRepositoriesCurrentJob_EveryStatus(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		url := newCurrentJobServer(t, pool)
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		scoper := db.NewTenantScoper(pool)

		live := 4 * time.Minute
		expired := -30 * time.Second
		later := time.Hour
		backoff := 10 * time.Minute

		// suspend writes the installation's suspension columns under A's
		// tenant: suspended alone, or suspended AND uninstalled (the shape
		// github_webhook_isolation_test.go writes).
		suspend := func(installationID string, uninstalled bool) {
			stmt := `UPDATE github_installations SET suspended_at = NOW() WHERE id = $1`
			if uninstalled {
				stmt = `UPDATE github_installations SET suspended_at = NOW(), uninstalled_at = NOW() WHERE id = $1`
			}
			require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgA.ID), func(tx pgx.Tx) error {
				tag, err := tx.Exec(ctx, stmt, installationID)
				if err == nil && tag.RowsAffected() != 1 {
					err = fmt.Errorf("suspending installation %s updated %d rows", installationID, tag.RowsAffected())
				}
				return err
			}))
		}

		type statusCase struct {
			name   string
			row    seedJobRow
			want   string
			repo   func(name string) string // nil => seedJobRepo
			reason string
		}
		suspendedRepo := func(name string) string {
			repo, inst := seedJobRepoWithInstallation(t, pool, orgA, name, 2213601)
			suspend(inst, false)
			return repo
		}
		uninstalledRepo := func(name string) string {
			repo, inst := seedJobRepoWithInstallation(t, pool, orgA, name, 2213602)
			suspend(inst, true)
			return repo
		}
		cases := []statusCase{
			{name: "dead_pending_StalledRunningAtTheCap", want: "dead_pending",
				row:    seedJobRow{jobType: "full_ingest", state: "running", attempts: 5, lease: &expired, leaseOwner: "w"},
				reason: "a stalled running row at attempts = max_attempts is one sweep from dead"},
			{name: "dead_pending_QueuedAtTheCap", want: "dead_pending",
				row:    seedJobRow{jobType: "full_ingest", state: "queued", attempts: 5},
				reason: "a queued row at the cap is one the claim refuses"},
			{name: "running_AHealthyFinalAttempt", want: "running",
				row:    seedJobRow{jobType: "full_ingest", state: "running", attempts: 5, lease: &live, leaseOwner: "w"},
				reason: "the claim increments attempts, so a healthy final attempt is at the cap too"},
			{name: "stalled", want: "stalled",
				row: seedJobRow{jobType: "full_ingest", state: "running", attempts: 1, lease: &expired, leaseOwner: "w"}},
			{name: "running", want: "running",
				row: seedJobRow{jobType: "full_ingest", state: "running", attempts: 1, lease: &live, leaseOwner: "w"}},
			{name: "retrying", want: "retrying",
				row: seedJobRow{jobType: "full_ingest", state: "queued", attempts: 2, runAfter: &backoff,
					lastError: "FetchFailed: api.github.com answered 502"}},
			{name: "scheduled", want: "scheduled",
				row: seedJobRow{jobType: "full_ingest", state: "queued", attempts: 0, runAfter: &later}},
			{name: "queued", want: "queued",
				row: seedJobRow{jobType: "full_ingest", state: "queued", attempts: 0}},
			{name: "completed", want: "completed",
				row: seedJobRow{jobType: "full_ingest", state: "completed", attempts: 1}},
			{name: "dead", want: "dead",
				row: seedJobRow{jobType: "full_ingest", state: "dead", attempts: 5, lastError: "out of attempts"}},
			{name: "superseded", want: "superseded",
				row: seedJobRow{jobType: "full_ingest", state: "superseded", attempts: 0}},
			{name: "deferred_suspended", want: "deferred_suspended", repo: suspendedRepo,
				row:    seedJobRow{jobType: "full_ingest", state: "queued", attempts: 0, runAfter: &later},
				reason: "the installation row says suspended and not uninstalled"},
			{name: "NotDeferredSuspended_WhenAlsoUninstalled", want: "scheduled", repo: uninstalledRepo,
				row:    seedJobRow{jobType: "full_ingest", state: "queued", attempts: 0, runAfter: &later},
				reason: "an uninstalled installation is not suspended, whatever suspended_at says"},
		}

		seen := map[string]bool{}
		for i, c := range cases {
			name := fmt.Sprintf("status-%02d", i)
			var repo string
			if c.repo != nil {
				repo = c.repo(name)
			} else {
				repo = seedJobRepo(t, pool, orgA, name)
			}
			id := seedJob(t, pool, orgA.ID, repo, c.row)

			t.Run(c.name, func(t *testing.T) {
				// PREMISE FIRST: the projection says syncing for every case,
				// so a status that came from sync_state would be the same
				// word everywhere, and the assertions below would fail.
				setSyncState(t, pool, orgA.ID, repo, "syncing")
				require.Equal(t, "syncing", readSyncState(t, pool, orgA.ID, repo),
					"premise: the projection must say syncing, or this proves nothing")

				current, _ := getRepositoryCurrentJob(t, url, tokenA, repo)
				job := decodeCurrentJob(t, current)
				require.Equal(t, id, job.ID)
				require.Equal(t, c.want, job.Status, "the repository's current_job: %s", c.reason)

				// And the job endpoint, through jobByIDSQL: the same word.
				require.Equal(t, c.want, decodeJob(t, url, id, tokenA).Status,
					"GET /api/admin/jobs/{id}: %s", c.reason)
			})
			seen[c.want] = true
		}

		// Every value of the vocabulary is reached by a case above.
		for _, v := range []string{
			"queued", "scheduled", "retrying", "deferred_suspended", "running",
			"stalled", "dead_pending", "completed", "dead", "superseded",
		} {
			require.True(t, seen[v], "no case reaches status %q", v)
		}
	})
}

// ---------------------------------------------------------------------
// The handler-level twins of jobs_isolation_test.go's Scenario5
// ---------------------------------------------------------------------

// TestRepositoriesCurrentJob_TheHandlersRefuseAnOrganizationlessContext
// proves the HANDLERS refuse, not only TenantMiddleware: the claim is $1,
// the tenant guard on currentJobJoinSQL, and a handler moved out of the
// tenant group must not run the statement without one. No chi route
// context is installed, so Get's 403 also pins the order of its two checks
// (with the id parsed first it would be a 404).
func TestRepositoriesCurrentJob_TheHandlersRefuseAnOrganizationlessContext(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		h := handlers.NewRepositoriesHandler(db.NewTenantScoper(pool), nil, nil)

		t.Run("Get", func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/api/repositories/"+orgA.RepoID, nil)
			h.Get(rec, req)
			require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
			require.NotContains(t, rec.Body.String(), orgA.RepoID)
		})

		t.Run("List", func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/api/repositories", nil)
			h.List(rec, req)
			require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
			require.False(t, strings.Contains(rec.Body.String(), orgA.RepoID))
		})
	})
}
