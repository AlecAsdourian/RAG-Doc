package internalapi_test

// Isolation tests for POST /internal/jobs/{id}/repository-token.
//
// ⚠ WRITTEN DELIBERATELY, NOT BY RATCHET. The CI scanner asks for this file
// because the route is a POST; what makes it necessary is that
// `ingestion_jobs` has no row-level security (21-CONTEXT L5), so nothing
// beneath the handler refuses a cross-tenant read: `liveLeaseSQL`'s own
// predicate is the first layer and the last one. Scenario2 is therefore
// MUTATION-CHECKED — neutering `AND lease_owner = $2` to
// `AND $2::text IS NOT NULL` must fail it — and the run is recorded in
// 22-04-SUMMARY.md.
//
// Every scenario runs a REAL github.Client against a fake GitHub, so the
// assertions about what the mint request carried are about bytes GitHub
// would have received, not about a stub's return value.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/internalapi"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// TestMain seeds the two secrets the PUBLIC router panics without, for the
// scenario that proves the public router does not serve this route.
func TestMain(m *testing.M) {
	if os.Getenv("SUPABASE_WEBHOOK_SECRET") == "" {
		os.Setenv("SUPABASE_WEBHOOK_SECRET", "isolation-tests-webhook-secret-not-for-production")
	}
	if os.Getenv("GITHUB_WEBHOOK_SECRET") == "" {
		os.Setenv("GITHUB_WEBHOOK_SECRET", "isolation-tests-github-webhook-secret-not-for-production")
	}
	os.Exit(m.Run())
}

// The measured token shape (20-02: `ghs_` plus 383 characters, length not
// fixed) and a short one. Both must stay out of every log line.
var longToken = "ghs_" + strings.Repeat("Ab3xYz9Q", 48)[:383]

const shortToken = "ghs_x1"

// tokenPath builds the route's path the way the worker does. The scanner
// recognises this line as the route's coverage: every static segment, in
// order, on one line.
func tokenPath(id string) string {
	return "/internal/jobs/" + id + "/repository-token"
}

func writeTestKey(t *testing.T) string {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	require.NoError(t, err)
	path := filepath.Join(t.TempDir(), "test-key.pem")
	require.NoError(t, os.WriteFile(path, pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(key),
	}), 0o600))
	return path
}

// fakeGitHub answers RepositoryToken's two calls and COUNTS them, so a
// scenario can assert that a refusal made no mint request at all.
type fakeGitHub struct {
	srv *httptest.Server

	mu        sync.Mutex
	mints     int
	lookups   int
	mintBody  []byte
	lookupTok string
	token     string
	failMint  bool // 500, echoing the App JWT and the would-be token
}

func newFakeGitHub(t *testing.T, token string) *fakeGitHub {
	t.Helper()
	f := &fakeGitHub{token: token}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/access_tokens"):
			f.mints++
			f.mintBody, _ = io.ReadAll(r.Body)
			if f.failMint {
				w.WriteHeader(http.StatusInternalServerError)
				fmt.Fprintf(w, `{"message":"boom","echo":{"authorization":%q,"token":%q}}`,
					r.Header.Get("Authorization"), f.token)
				return
			}
			var req struct {
				RepositoryIDs []int64 `json:"repository_ids"`
			}
			_ = json.Unmarshal(f.mintBody, &req)
			var repoID int64
			if len(req.RepositoryIDs) == 1 {
				repoID = req.RepositoryIDs[0]
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			fmt.Fprintf(w, `{"token":%q,"expires_at":%q,"permissions":{"contents":"read","metadata":"read"},"repository_selection":"selected","repositories":[{"id":%d}]}`,
				f.token, time.Now().Add(time.Hour).UTC().Format(time.RFC3339), repoID)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/repositories/"):
			f.lookups++
			f.lookupTok = strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			var id int64
			fmt.Sscanf(strings.TrimPrefix(r.URL.Path, "/repositories/"), "%d", &id)
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"id":%d,"name":"widgets","full_name":"acme/widgets-%d","private":true,"default_branch":"main","size":75}`, id, id)
		default:
			t.Errorf("unexpected GitHub call %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeGitHub) counts() (mints, lookups int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.mints, f.lookups
}

func (f *fakeGitHub) setFailMint(v bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failMint = v
}

// syncBuffer is a log sink the server's goroutines can write while the
// test reads, which -race would otherwise flag.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func (b *syncBuffer) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.buf.Reset()
}

// --- seeding, all inside the organization's tenant scope ---

func inTenant(t *testing.T, pool *pgxpool.Pool, orgID string, fn func(tx pgx.Tx) error) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(context.Background(), orgID), fn))
}

// seedInstallation inserts a github_installations row for org and returns
// its id. suspended / uninstalled set the corresponding timestamps.
func seedInstallation(t *testing.T, pool *pgxpool.Pool, orgID string, ghID int64, suspended, uninstalled bool) string {
	t.Helper()
	var id string
	var suspendedAt, uninstalledAt any
	if suspended {
		suspendedAt = time.Now().Add(-time.Hour)
	}
	if uninstalled {
		uninstalledAt = time.Now().Add(-time.Minute)
	}
	inTenant(t, pool, orgID, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			INSERT INTO github_installations
			  (organization_id, github_installation_id, account_login,
			   account_type, repository_selection, suspended_at, uninstalled_at)
			VALUES ($1, $2, 'someone', 'User', 'selected', $3, $4)
			RETURNING id::text
		`, orgID, ghID, suspendedAt, uninstalledAt).Scan(&id)
	})
	return id
}

// seedRepo creates one repository for org, linked to installationID
// (which may be "" for no installation) with the given GitHub id.
func seedRepo(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, name, installationID string, githubRepoID int64) string {
	t.Helper()
	var id string
	var inst, ghID any
	if installationID != "" {
		inst = installationID
	}
	if githubRepoID != 0 {
		ghID = githubRepoID
	}
	inTenant(t, pool, org.ID, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			INSERT INTO repositories (project_id, name, git_url, installation_id, github_repo_id)
			VALUES ($1, $2, $3, $4, $5) RETURNING id::text
		`, org.ProjectID, name, fmt.Sprintf("https://example.test/%s-%s.git", org.Slug, name),
			inst, ghID).Scan(&id)
	})
	return id
}

// seedJob inserts one ingestion_jobs row. lease nil means NULL.
func seedJob(t *testing.T, pool *pgxpool.Pool, orgID, repoID, state, owner string, lease *time.Duration) string {
	t.Helper()
	var id string
	var expiry, leaseOwner any
	if lease != nil {
		expiry = time.Now().Add(*lease)
	}
	if owner != "" {
		leaseOwner = owner
	}
	inTenant(t, pool, orgID, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			INSERT INTO ingestion_jobs
			  (organization_id, repository_id, job_type, state, attempts, run_after,
			   lease_owner, lease_expires_at)
			VALUES ($1, $2, 'full_ingest', $3, 1, NOW(), $4, $5)
			RETURNING id::text
		`, orgID, repoID, state, leaseOwner, expiry).Scan(&id)
	})
	return id
}

// post performs the worker's request and returns what came back.
func post(t *testing.T, baseURL, path, rawBody string) (int, http.Header, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+path, strings.NewReader(rawBody))
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	return resp.StatusCode, resp.Header, string(raw)
}

func leaseBody(owner string) string {
	b, _ := json.Marshal(map[string]string{"lease_owner": owner})
	return string(b)
}

func requireMarked(t *testing.T, h http.Header) {
	t.Helper()
	require.Equal(t, internalapi.MarkerValue, h.Get(internalapi.MarkerHeader),
		"every response from the route carries the marker")
}

func requireRefused(t *testing.T, status int, h http.Header, body string, why string) {
	t.Helper()
	require.Equal(t, http.StatusNotFound, status, "%s: body=%s", why, body)
	requireMarked(t, h)
	require.Equal(t, internalapi.RefusedBody, body, "%s: the 404 must be THE 404", why)
}

func upperHex(id string) string {
	out := []rune(id)
	for i, r := range out {
		if r >= 'a' && r <= 'f' {
			out[i] = r - 32
		}
	}
	return string(out)
}

// noStates keeps the public router from dialling Redis.
type noStates struct{}

func (noStates) StoreStateValue(context.Context, string, string) error { return nil }
func (noStates) ConsumeState(context.Context, string) (string, bool, error) {
	return "", false, nil
}

func TestRepositoryTokenIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		gh := newFakeGitHub(t, longToken)
		ghClient, err := github.NewClient("4880866", writeTestKey(t), github.WithBaseURL(gh.srv.URL))
		require.NoError(t, err)

		logs := &syncBuffer{}
		logger := slog.New(slog.NewJSONHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

		server := httptest.NewServer(internalapi.NewRouter(pool, ghClient, logger))
		t.Cleanup(server.Close)

		// Org A: a healthy installation, its starter repository linked to
		// GitHub, one running job under a live lease.
		instA := seedInstallation(t, pool, orgA.ID, 160225622, false, false)
		repoA := seedRepo(t, pool, orgA, "healthy", instA, 1103353668)
		ownerA := uuid.NewString()
		liveLease := 4 * time.Minute
		jobA := seedJob(t, pool, orgA.ID, repoA, "running", ownerA, &liveLease)

		// Org B: the same shape, its own installation and lease.
		instB := seedInstallation(t, pool, orgB.ID, 160225623, false, false)
		repoB := seedRepo(t, pool, orgB, "healthy", instB, 2200000001)
		ownerB := uuid.NewString()
		jobB := seedJob(t, pool, orgB.ID, repoB, "running", ownerB, &liveLease)

		t.Run("Scenario1_ALiveLeaseGetsAOneRepositoryReadOnlyToken", func(t *testing.T) {
			mintsBefore, _ := gh.counts()
			status, headers, body := post(t, server.URL, tokenPath(jobA), leaseBody(ownerA))
			require.Equal(t, http.StatusOK, status, "body=%s", body)
			requireMarked(t, headers)
			require.Equal(t, "application/json", headers.Get("Content-Type"))

			var resp map[string]json.RawMessage
			require.NoError(t, json.Unmarshal([]byte(body), &resp), "body=%s", body)
			keys := make([]string, 0, len(resp))
			for k := range resp {
				keys = append(keys, k)
			}
			require.ElementsMatch(t, []string{"token", "expires_at", "full_name", "default_branch"}, keys,
				"the response shape is the contract; nothing else rides along")

			var got struct {
				Token         string    `json:"token"`
				ExpiresAt     time.Time `json:"expires_at"`
				FullName      string    `json:"full_name"`
				DefaultBranch string    `json:"default_branch"`
			}
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, longToken, got.Token)
			require.Equal(t, "acme/widgets-1103353668", got.FullName,
				"the name comes from the lookup made with the new token")
			require.Equal(t, "main", got.DefaultBranch)
			require.WithinDuration(t, time.Now().Add(time.Hour), got.ExpiresAt, 2*time.Minute)

			// What GitHub was asked, decoded: exactly one repository id — the
			// job's repository — and exactly contents:read.
			mints, lookups := gh.counts()
			require.Equal(t, mintsBefore+1, mints)
			require.Equal(t, 1, lookups)
			gh.mu.Lock()
			mintBody, lookupTok := gh.mintBody, gh.lookupTok
			gh.mu.Unlock()

			var decoded map[string]json.RawMessage
			require.NoError(t, json.Unmarshal(mintBody, &decoded), "mint body=%s", mintBody)
			bodyKeys := make([]string, 0, len(decoded))
			for k := range decoded {
				bodyKeys = append(bodyKeys, k)
			}
			require.ElementsMatch(t, []string{"repository_ids", "permissions"}, bodyKeys)
			var ids []int64
			require.NoError(t, json.Unmarshal(decoded["repository_ids"], &ids))
			require.Equal(t, []int64{1103353668}, ids, "one repository, the job's")
			var perms map[string]string
			require.NoError(t, json.Unmarshal(decoded["permissions"], &perms))
			require.Equal(t, map[string]string{"contents": "read"}, perms)
			require.Equal(t, longToken, lookupTok, "the repository lookup used the minted token")
		})

		t.Run("Scenario2_AnotherOrganizationsLeaseOwnerGetsTheSame404AndGitHubIsNotCalled", func(t *testing.T) {
			// ⚠ THE TEST THIS FILE EXISTS FOR. `ingestion_jobs` has no
			// row-level security, so nothing beneath the handler refuses
			// this: `AND lease_owner = $2` in liveLeaseSQL is all of it.
			mintsBefore, _ := gh.counts()

			status, headers, body := post(t, server.URL, tokenPath(jobB), leaseBody(ownerA))
			requireRefused(t, status, headers, body, "orgA's lease owner asked for orgB's job")
			require.NotContains(t, body, repoB)
			require.NotContains(t, body, orgB.ID)

			mints, _ := gh.counts()
			require.Equal(t, mintsBefore, mints, "a refused request must never reach GitHub")

			// And orgB's own lease still works: a handler that 404'd
			// everything would pass every assertion above.
			status, headers, body = post(t, server.URL, tokenPath(jobB), leaseBody(ownerB))
			require.Equal(t, http.StatusOK, status, "body=%s", body)
			requireMarked(t, headers)
			require.Contains(t, body, `"full_name":"acme/widgets-2200000001"`)
		})

		t.Run("Scenario3_EveryMissIsByteIdentical", func(t *testing.T) {
			mintsBefore, _ := gh.counts()
			_, _, want := post(t, server.URL, tokenPath(uuid.NewString()), leaseBody(ownerA))
			require.Equal(t, internalapi.RefusedBody, want)

			expired := -30 * time.Second
			cases := map[string]struct {
				id, owner string
			}{
				"wrong owner": {jobA, uuid.NewString()},
				"the other org's owner": {jobA, ownerB},
				"unknown id": {uuid.NewString(), ownerA},
				"superseded, lease still attached": {
					seedJob(t, pool, orgA.ID, seedRepo(t, pool, orgA, "superseded", instA, 1103353669),
						"superseded", ownerA, &liveLease), ownerA},
				"expired lease": {
					seedJob(t, pool, orgA.ID, seedRepo(t, pool, orgA, "expired", instA, 1103353670),
						"running", ownerA, &expired), ownerA},
				"completed": {
					seedJob(t, pool, orgA.ID, seedRepo(t, pool, orgA, "completed", instA, 1103353671),
						"completed", ownerA, &liveLease), ownerA},
				"queued, no lease": {
					seedJob(t, pool, orgA.ID, seedRepo(t, pool, orgA, "queued", instA, 1103353672),
						"queued", "", nil), ownerA},
				"running with a NULL lease": {
					seedJob(t, pool, orgA.ID, seedRepo(t, pool, orgA, "null-lease", instA, 1103353673),
						"running", ownerA, nil), ownerA},
				"malformed id":   {"not-a-uuid", ownerA},
				"upper-hex id":   {upperHex(jobA), ownerA},
				"braced id":      {"{" + jobA + "}", ownerA},
				"undashed id":    {strings.ReplaceAll(jobA, "-", ""), ownerA},
				"urn id":         {"urn:uuid:" + jobA, ownerA},
				"empty id":       {"", ownerA},
			}
			for name, tc := range cases {
				status, headers, body := post(t, server.URL, tokenPath(tc.id), leaseBody(tc.owner))
				if tc.id == "" {
					// `/internal/jobs//repository-token` matches no route;
					// chi answers, unmarked. Recorded, not asserted identical.
					require.Equal(t, http.StatusNotFound, status, name)
					continue
				}
				requireRefused(t, status, headers, body, name)
				require.Equal(t, want, body, "%s must be byte-identical to 'unknown id'", name)
			}

			mints, _ := gh.counts()
			require.Equal(t, mintsBefore, mints, "no miss may reach GitHub")

			// A live lease still mints — the misses above are misses, not a
			// handler that refuses everything.
			status, _, body := post(t, server.URL, tokenPath(jobA), leaseBody(ownerA))
			require.Equal(t, http.StatusOK, status, "body=%s", body)
		})

		t.Run("Scenario4_InstallationStatesAreDistinct409sAndMintNothing", func(t *testing.T) {
			mintsBefore, _ := gh.counts()
			cases := []struct {
				name        string
				suspended   bool
				uninstalled bool
				noInstall   bool
				wantReason  string
			}{
				{"suspended", true, false, false, internalapi.ReasonSuspended},
				{"uninstalled", false, true, false, internalapi.ReasonUninstalled},
				{"both set is uninstalled", true, true, false, internalapi.ReasonUninstalled},
				{"no installation at all", false, false, true, internalapi.ReasonUninstalled},
			}
			for i, tc := range cases {
				t.Run(tc.name, func(t *testing.T) {
					ghInstallationID := int64(170000000 + i)
					var inst string
					if !tc.noInstall {
						inst = seedInstallation(t, pool, orgA.ID, ghInstallationID, tc.suspended, tc.uninstalled)
					}
					repo := seedRepo(t, pool, orgA, "inst-"+tc.name, inst, int64(1200000000+i))
					job := seedJob(t, pool, orgA.ID, repo, "running", ownerA, &liveLease)

					status, headers, body := post(t, server.URL, tokenPath(job), leaseBody(ownerA))
					require.Equal(t, http.StatusConflict, status, "body=%s", body)
					requireMarked(t, headers)
					require.JSONEq(t, fmt.Sprintf(`{"reason":%q}`, tc.wantReason), body)
				})
			}
			mints, _ := gh.counts()
			require.Equal(t, mintsBefore, mints, "an installation that cannot be used must not be minted for")
		})

		t.Run("Scenario5_TheMarkerIsOnEveryStatusAndOnlyOnTheRoute", func(t *testing.T) {
			// 200, 404 and 409 are asserted marked above. The rest:
			for name, raw := range map[string]string{
				"not json":         `lease_owner=` + ownerA,
				"empty lease":      `{"lease_owner": ""}`,
				"blank lease":      `{"lease_owner": "   "}`,
				"no lease field":   `{}`,
				"empty body":       ``,
				"wrong value type": `{"lease_owner": 42}`,
			} {
				status, headers, body := post(t, server.URL, tokenPath(jobA), raw)
				require.Equal(t, http.StatusBadRequest, status, "%s: body=%s", name, body)
				requireMarked(t, headers)
				require.JSONEq(t, `{"error":"bad_request"}`, body, name)
			}

			gh.setFailMint(true)
			status, headers, body := post(t, server.URL, tokenPath(jobA), leaseBody(ownerA))
			gh.setFailMint(false)
			require.Equal(t, http.StatusBadGateway, status, "body=%s", body)
			requireMarked(t, headers)
			require.JSONEq(t, `{"error":"github_unavailable"}`, body)

			// Anything the route did not answer is UNMARKED. This is what
			// lets the worker tell "the lease is gone" from "I am talking to
			// the wrong thing", and it holds on this listener too: a wrong
			// path here must not look like a refused lease.
			for _, path := range []string{
				tokenPath(jobA) + "/extra",
				"/internal/jobs/" + jobA,
				"/internal/jobs/",
				"/health",
				"/api/admin/jobs/" + jobA,
			} {
				status, headers, body := post(t, server.URL, path, leaseBody(ownerA))
				require.Equal(t, http.StatusNotFound, status, path)
				require.Empty(t, headers.Get(internalapi.MarkerHeader), "%s must be unmarked", path)
				require.Equal(t, "404 page not found\n", body, path)
			}
			req, err := http.NewRequest(http.MethodGet, server.URL+tokenPath(jobA), nil)
			require.NoError(t, err)
			resp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			resp.Body.Close()
			require.Equal(t, http.StatusMethodNotAllowed, resp.StatusCode)
			require.Empty(t, resp.Header.Get(internalapi.MarkerHeader), "chi's 405 is unmarked")
		})

		t.Run("Scenario6_NoTokenReachesTheLog", func(t *testing.T) {
			// ⚠ ASSERTED ON CAPTURED LOG OUTPUT, not on any function we
			// control: a test that reads no log record cannot catch a
			// logging bug (21-06). Both token shapes, both outcomes.
			for _, token := range []string{longToken, shortToken} {
				t.Run(fmt.Sprintf("token length %d", len(token)), func(t *testing.T) {
					gh.mu.Lock()
					gh.token = token
					gh.mu.Unlock()
					t.Cleanup(func() {
						gh.mu.Lock()
						gh.token = longToken
						gh.mu.Unlock()
					})

					logs.Reset()
					status, _, body := post(t, server.URL, tokenPath(jobA), leaseBody(ownerA))
					require.Equal(t, http.StatusOK, status, "body=%s", body)
					require.Contains(t, body, token, "premise: the token DID travel, in the response")
					captured := logs.String()
					require.Contains(t, captured, "repository token minted", "premise: the mint was logged")
					require.Contains(t, captured, jobA, "the line names the job")
					require.Contains(t, captured, orgA.ID, "the line names the organization")
					require.Contains(t, captured, "1103353668", "the line names the GitHub repository id")
					require.NotContains(t, captured, token)
					require.NotContains(t, captured, "ghs_")
					require.NotContains(t, captured, ownerA, "the lease owner is a credential too")

					logs.Reset()
					gh.setFailMint(true)
					status, _, body = post(t, server.URL, tokenPath(jobA), leaseBody(ownerA))
					gh.setFailMint(false)
					require.Equal(t, http.StatusBadGateway, status, "body=%s", body)
					require.NotContains(t, body, "ghs_", "the 502 body must not echo GitHub's echo")
					captured = logs.String()
					require.Contains(t, captured, "mint failed", "premise: the failure was logged")
					require.Contains(t, captured, "[REDACTED]", "premise: GitHub's echo reached the log, redacted")
					require.NotContains(t, captured, token)
					require.NotContains(t, captured, "ghs_")
					require.NotContains(t, captured, "eyJ", "the App JWT GitHub echoed must not reach the log")
					require.NotContains(t, captured, ownerA)
				})
			}
		})

		t.Run("Scenario7_ThePublicRouterDoesNotServeTheRoute", func(t *testing.T) {
			deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				t.Errorf("the RAG service must not be called; got %s", r.URL.Path)
				http.Error(w, "unexpected", http.StatusInternalServerError)
			}))
			t.Cleanup(deadRAG.Close)

			// The degraded shape (no GitHub client), which is what every
			// other router test builds: the route's absence does not depend
			// on the App being configured.
			public := httptest.NewServer(api.NewRouterWithValidatorAndAdmin(
				pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
				api.Config{LogLevel: slog.LevelWarn, InstallStates: noStates{}},
			))
			t.Cleanup(public.Close)

			mintsBefore, _ := gh.counts()
			status, headers, body := post(t, public.URL, tokenPath(jobA), leaseBody(ownerA))
			require.Equal(t, http.StatusNotFound, status, "body=%s", body)
			require.Empty(t, headers.Get(internalapi.MarkerHeader),
				"the public router's 404 is chi's, unmarked — which is what the worker keys on")
			require.Equal(t, "404 page not found\n", body)

			// With a valid tenant token too: authentication does not reveal it.
			req, err := http.NewRequest(http.MethodPost, public.URL+tokenPath(jobA), strings.NewReader(leaseBody(ownerA)))
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer "+testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner"))
			resp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			resp.Body.Close()
			require.Equal(t, http.StatusNotFound, resp.StatusCode)
			require.Empty(t, resp.Header.Get(internalapi.MarkerHeader))

			mints, _ := gh.counts()
			require.Equal(t, mintsBefore, mints)
		})
	})
}
