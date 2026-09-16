package handlers_test

// Tests for POST /api/repositories past its first authorization check —
// the project join, the upsert, and the twelve-column scan.
//
// None of that had ever been executed. TestRepositoriesIsolation always
// runs with no GitHub credentials, so every path through it stops at the
// installation lookup, and the handler's concrete *github.Client had an
// unexported baseURL that no test could redirect. The stub below plus
// api.Config.GitHubRepositories is that seam; the first thing it found was
// H2, a 500 on the reconnect flow the API docs tell clients to expect.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
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

// stubLister stands in for the GitHub App client.
//
// `beforeReturn` runs while the handler is between its two transactions,
// which is the only way to reach the window where the installation can
// disappear underneath a connect.
//
// It is mutex-guarded because 21-03's concurrency tests fire several
// connects at one server, and `pkg/api/...` runs under `-race` in CI.
type stubLister struct {
	mu           sync.Mutex
	repos        []github.Repository
	err          error
	calls        int
	beforeReturn func()
}

func (s *stubLister) ListInstallationRepositories(
	_ context.Context, _ int64,
) ([]github.Repository, error) {
	s.mu.Lock()
	s.calls++
	repos, err, before := s.repos, s.err, s.beforeReturn
	s.mu.Unlock()

	if before != nil {
		before()
	}
	return repos, err
}

// setRepos replaces what the stub reports, safely for a test that has
// already started a server.
func (s *stubLister) setRepos(repos []github.Repository) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.repos = repos
}

const stubRepoID = int64(4242000)

func stubRepo(name, cloneURL string) github.Repository {
	return github.Repository{
		ID:            stubRepoID,
		Name:          name,
		FullName:      "someone/" + name,
		Private:       true,
		Visibility:    "private",
		SizeKB:        75,
		DefaultBranch: "main",
		CloneURL:      cloneURL,
	}
}

// newConnectServer builds the real router with a stubbed GitHub client.
func newConnectServer(t *testing.T, pool *pgxpool.Pool, lister handlers.InstallationRepositoryLister) string {
	t.Helper()
	deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("repository endpoints must never call the RAG service; got %s", r.URL.Path)
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(deadRAG.Close)

	router := api.NewRouterWithValidatorAndAdmin(
		pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
		api.Config{LogLevel: slog.LevelWarn, GitHubRepositories: lister},
	)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	return server.URL
}

func connectBody(installationID string) string {
	return fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, stubRepoID, installationID)
}

// countRepositories counts everything visible in org's tenant scope.
func countRepositories(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg) int64 {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)
	var n int64
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `SELECT count(*) FROM repositories`).Scan(&n)
	}))
	return n
}

// repoStateOf reads a repository's row directly, inside orgA's scope.
func repoStateOf(t *testing.T, pool *pgxpool.Pool, orgID, repoID string) (installation *string, syncState, name string) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), orgID)
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(),
			`SELECT installation_id::text, sync_state, name FROM repositories WHERE id = $1`,
			repoID).Scan(&installation, &syncState, &name)
	}))
	return installation, syncState, name
}

func TestRepositoriesConnect(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 555000)

		t.Run("PersistsWhatGitHubReported", func(t *testing.T) {
			lister := &stubLister{repos: []github.Repository{
				stubRepo("api-navigator", "https://github.com/someone/api-navigator.git"),
			}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			var got handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, "api-navigator", got.Name)
			require.Equal(t, "https://github.com/someone/api-navigator.git", got.GitURL)
			require.Equal(t, "main", got.DefaultBranch)
			require.NotNil(t, got.GitHubRepoID)
			require.Equal(t, stubRepoID, *got.GitHubRepoID)
			require.NotNil(t, got.SizeKB)
			require.Equal(t, int64(75), *got.SizeKB, "size is KILOBYTES, as GitHub reports it")
			require.NotNil(t, got.Visibility)
			require.Equal(t, "private", *got.Visibility)
			require.Equal(t, "pending", got.SyncState)
			require.NotNil(t, got.InstallationID)
			require.Equal(t, instA, *got.InstallationID)

			// And it is really there, through a second endpoint.
			s, b := doRepoRequest(t, url, http.MethodGet, "/api/repositories/"+got.ID, tokenA, "")
			require.Equal(t, http.StatusOK, s, "body=%s", b)
		})

		t.Run("ReconnectAfterUninstallRelinksTheSameRow", func(t *testing.T) {
			// The flow docs/api-repositories.md tells clients to expect:
			// uninstalling the App sets installation_id to NULL (we keep
			// what was ingested), reinstalling should recover.
			//
			// It used to raise 23505 against UNIQUE (project_id, git_url)
			// and surface as a 500, because the upsert keyed on the
			// installation — the one thing that had just changed. Nothing
			// else relinks the row, so the documented recovery was a dead
			// end. Migration 000011 keys on (project_id, github_repo_id).
			lister := &stubLister{repos: []github.Repository{
				stubRepo("orphan-repo", "https://github.com/someone/orphan-repo.git"),
			}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)
			var first handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &first))

			// Uninstall, and mark it synced so the re-queue is observable.
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				_, err := tx.Exec(context.Background(),
					`UPDATE repositories SET installation_id = NULL, sync_state = 'synced' WHERE id = $1`,
					first.ID)
				return err
			}))

			status, body = doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status,
				"reconnecting after a reinstall must recover, not 500; body=%s", body)

			var second handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &second))
			require.Equal(t, first.ID, second.ID, "the orphaned row must be relinked, not duplicated")

			installation, sync, _ := repoStateOf(t, pool, orgA.ID, first.ID)
			require.NotNil(t, installation)
			require.Equal(t, instA, *installation)
			require.Equal(t, "pending", sync,
				"a repository reached through a new installation has to be fetched again")
		})

		t.Run("PlainReconnectRefreshesMetadataWithoutRequeueing", func(t *testing.T) {
			// The other half of the rule above: when the installation did
			// NOT change, a re-connect must not stomp the sync state. A
			// 'syncing' row re-queued mid-run would be fetched twice.
			lister := &stubLister{repos: []github.Repository{
				stubRepo("steady-repo", "https://github.com/someone/steady-repo.git"),
			}}
			url := newConnectServer(t, pool, lister)

			_, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			var first handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &first))

			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				_, err := tx.Exec(context.Background(),
					`UPDATE repositories SET sync_state = 'failed' WHERE id = $1`, first.ID)
				return err
			}))

			lister.setRepos([]github.Repository{
				stubRepo("steady-repo-renamed", "https://github.com/someone/steady-repo.git"),
			})
			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			_, sync, name := repoStateOf(t, pool, orgA.ID, first.ID)
			require.Equal(t, "steady-repo-renamed", name, "metadata must be refreshed")
			require.Equal(t, "failed", sync,
				"an unchanged installation must not re-queue; the state belongs to Phase 21")
		})

		t.Run("AdoptsALegacyRowWithTheSameURLInsteadOfDuplicating", func(t *testing.T) {
			// A row with no GitHub id holding the same clone URL — the
			// shape of anything connected before this API existed. It used
			// to abort the connect with a 500; fixing that stopped the
			// error but produced a SECOND row that could never be synced,
			// because nothing else ever sets github_repo_id.
			//
			// The earlier version of this test asserted only the status
			// code, so it could not see the duplicate — and, sharing one
			// github_repo_id with every other subtest, it was really
			// re-running the UPDATE path rather than the one it named.
			const shared = "https://github.com/someone/legacy-adopt.git"
			const ghID = int64(4243000)
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)

			var legacyID string
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				return tx.QueryRow(context.Background(), `
					INSERT INTO repositories (project_id, name, git_url, sync_state)
					VALUES ($1, 'legacy', $2, 'never_synced')
					RETURNING id::text`, orgA.ProjectID, shared).Scan(&legacyID)
			}))

			before := countRepositories(t, pool, orgA)
			lister := &stubLister{repos: []github.Repository{{
				ID: ghID, Name: "legacy-adopt", Visibility: "private",
				DefaultBranch: "main", CloneURL: shared, SizeKB: 12,
			}}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, ghID, instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			var got handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, legacyID, got.ID, "the legacy row must be adopted, not duplicated")
			require.Equal(t, before, countRepositories(t, pool, orgA),
				"adopting must not add a row")

			_, sync, _ := repoStateOf(t, pool, orgA.ID, legacyID)
			require.Equal(t, "pending", sync,
				"a row linked to an installation for the first time has never been fetched")
		})

		t.Run("AdoptsARowFromANonDefaultProjectInTheSameOrganization", func(t *testing.T) {
			// Migration 000011's unique index is per-PROJECT, and an
			// organization may hold several. Without an org-wide lookup the
			// same repository gets a second row and Phase 21 ingests it
			// twice — doubling chunks and duplicating every search hit.
			const ghID = int64(4244000)
			const cloneURL = "https://github.com/someone/second-project.git"
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)

			var otherProjectID, existingID string
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				if err := tx.QueryRow(context.Background(), `
					INSERT INTO projects (organization_id, name, slug, is_default)
					VALUES ($1, 'Archive', 'archive', false)
					RETURNING id::text`, orgA.ID).Scan(&otherProjectID); err != nil {
					return err
				}
				return tx.QueryRow(context.Background(), `
					INSERT INTO repositories
					  (project_id, installation_id, github_repo_id, name, git_url, sync_state)
					VALUES ($1, $2, $3, 'second-project', $4, 'synced')
					RETURNING id::text`,
					otherProjectID, instA, ghID, cloneURL).Scan(&existingID)
			}))

			before := countRepositories(t, pool, orgA)
			lister := &stubLister{repos: []github.Repository{{
				ID: ghID, Name: "second-project-renamed", Visibility: "private",
				DefaultBranch: "main", CloneURL: cloneURL, SizeKB: 30,
			}}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, ghID, instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			var got handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, existingID, got.ID,
				"a repository already connected in another project of this org must be reused")
			require.Equal(t, before, countRepositories(t, pool, orgA),
				"the same GitHub repository must not appear twice in one organization")

			_, sync, name := repoStateOf(t, pool, orgA.ID, existingID)
			require.Equal(t, "second-project-renamed", name)
			require.Equal(t, "synced", sync,
				"the installation did not change, so nothing should be re-queued")
		})

		t.Run("PrefersARealIDMatchOverALegacyURLMatch", func(t *testing.T) {
			// Both adoptable rows in one project. Adopting the legacy one
			// would make the UPDATE collide with
			// idx_repositories_project_github_repo — a reachable 500 — so
			// the ORDER BY that puts a real id match first is load-bearing.
			const ghID = int64(4245000)
			const cloneURL = "https://github.com/someone/both-shapes.git"
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)

			var realID, legacyID string
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				if err := tx.QueryRow(context.Background(), `
					INSERT INTO repositories
					  (project_id, installation_id, github_repo_id, name, git_url, sync_state)
					VALUES ($1, $2, $3, 'both-real', 'https://github.com/someone/both-real.git', 'synced')
					RETURNING id::text`, orgA.ProjectID, instA, ghID).Scan(&realID); err != nil {
					return err
				}
				// Created LATER, so only the id-before-url ordering saves us.
				return tx.QueryRow(context.Background(), `
					INSERT INTO repositories (project_id, name, git_url, sync_state)
					VALUES ($1, 'both-legacy', $2, 'never_synced')
					RETURNING id::text`, orgA.ProjectID, cloneURL).Scan(&legacyID)
			}))

			lister := &stubLister{repos: []github.Repository{{
				ID: ghID, Name: "both-shapes", Visibility: "private",
				DefaultBranch: "main", CloneURL: cloneURL, SizeKB: 5,
			}}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, ghID, instA))
			require.Equal(t, http.StatusCreated, status,
				"adopting the legacy row here would be a unique-violation 500; body=%s", body)

			var got handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, realID, got.ID,
				"a real github_repo_id match must win over a git_url match")
		})

		t.Run("AdoptingARowThatNeverHadAGitHubIDQueuesIt", func(t *testing.T) {
			// The `OR github_repo_id IS NULL` half of the re-queue rule.
			// A row whose installation_id is ALREADY correct but which has
			// no github_repo_id is schema-legal; without that clause it
			// would be adopted and left at its old sync_state, so nothing
			// would ever fetch it.
			const ghID = int64(4246000)
			const cloneURL = "https://github.com/someone/linked-but-unidentified.git"
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)

			var rowID string
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				return tx.QueryRow(context.Background(), `
					INSERT INTO repositories
					  (project_id, installation_id, name, git_url, sync_state)
					VALUES ($1, $2, 'linked-but-unidentified', $3, 'synced')
					RETURNING id::text`, orgA.ProjectID, instA, cloneURL).Scan(&rowID)
			}))

			lister := &stubLister{repos: []github.Repository{{
				ID: ghID, Name: "linked-but-unidentified", Visibility: "private",
				DefaultBranch: "main", CloneURL: cloneURL, SizeKB: 7,
			}}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, ghID, instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			var got handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &got))
			require.Equal(t, rowID, got.ID)

			_, sync, _ := repoStateOf(t, pool, orgA.ID, rowID)
			require.Equal(t, "pending", sync,
				"a row that has just been given a github_repo_id has never been fetched as one")
		})

		t.Run("InstallationDeletedMidConnectIs404Not500", func(t *testing.T) {
			// The GitHub call sits between two transactions deliberately
			// (a network round-trip must not pin a pooled connection), so
			// this window is real. From the caller's side "your
			// installation is gone" is the same answer as "not yours".
			gone := seedInstallation(t, pool, orgA.ID, 556000)
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)

			lister := &stubLister{
				repos: []github.Repository{stubRepo("vanishing", "https://github.com/someone/vanishing.git")},
				beforeReturn: func() {
					require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
						_, err := tx.Exec(context.Background(),
							`DELETE FROM github_installations WHERE id = $1`, gone)
						return err
					}))
				},
			}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(gone))
			require.Equal(t, http.StatusNotFound, status, "body=%s", body)
		})

		t.Run("WithoutCredentialsAvailabilityIsStillCheckedAfterAuthorization", func(t *testing.T) {
			// No lister at all — a degraded deployment. The 503 must come
			// only AFTER the installation was resolved, or the pair of
			// statuses tells a caller which installation ids exist.
			url := newConnectServer(t, pool, nil)

			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusServiceUnavailable, status, "body=%s", body)

			unknown, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				connectBody("99999999-9999-9999-9999-999999999999"))
			require.Equal(t, http.StatusNotFound, unknown,
				"an id the caller cannot see must 404 before availability is considered; body=%s", body)
		})

		t.Run("TrailingGarbageAfterTheJSONObjectIsRejected", func(t *testing.T) {
			url := newConnectServer(t, pool, &stubLister{})
			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				connectBody(instA)+` <<<GARBAGE>>>`)
			require.Equal(t, http.StatusBadRequest, status, "body=%s", body)
		})

		t.Run("NonCanonicalUUIDsAreNotFoundRatherThan500", func(t *testing.T) {
			// uuid.Parse is a parser, not a validator: it accepts the URN
			// and brace forms, and Postgres accepts one of those and
			// rejects the other. Passing its input through unexamined made
			// the URN form an unhandled 22P02.
			//
			// Uppercase is a DELIBERATE behaviour change: Postgres accepts
			// an uppercase UUID literal, so `GET /api/repositories/AABB…`
			// used to return 200. It is a 404 now. One spelling of an id
			// works — the one this API emits — and the alternative is a
			// rule that admits some non-canonical forms and 500s on others.
			url := newConnectServer(t, pool, &stubLister{})
			for _, id := range []string{
				"urn:uuid:" + orgA.RepoID,
				"{" + orgA.RepoID + "}",
				strings.ToUpper(orgA.RepoID),
				strings.ReplaceAll(orgA.RepoID, "-", ""),
			} {
				for _, method := range []string{http.MethodGet, http.MethodDelete} {
					status, body := doRepoRequest(t, url, method, "/api/repositories/"+id, tokenA, "")
					require.Equalf(t, http.StatusNotFound, status,
						"%s %s must be a 404, never a 500; body=%s", method, id, body)
				}
			}
		})

		t.Run("MalformedCursorIsBadRequestRatherThan500", func(t *testing.T) {
			url := newConnectServer(t, pool, &stubLister{})
			stamp := time.Now().UTC().Format(time.RFC3339Nano)
			for _, id := range []string{
				"urn:uuid:" + orgA.RepoID,
				"{" + orgA.RepoID + "}",
				"not-a-uuid",
			} {
				cursor := base64.RawURLEncoding.EncodeToString([]byte(stamp + "|" + id))
				status, body := doRepoRequest(t, url, http.MethodGet,
					"/api/repositories?cursor="+cursor, tokenA, "")
				require.Equalf(t, http.StatusBadRequest, status,
					"cursor carrying %q must be a 400, as documented; body=%s", id, body)
			}
		})
	})
}

// =====================================================================
// 21-03: connecting creates a real work item
// =====================================================================
//
// What `sync_state` used to be asked to mean is now a row in
// `ingestion_jobs`. These tests read that table DIRECTLY, filtered by
// `organization_id` where tenancy is the point: it has no row-level
// security by design (21-CONTEXT L5), so a query against it says nothing
// about tenancy unless the filter is written out.

func TestRepositoriesConnect_ANewRepositoryGetsOneQueuedFullIngest(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 557000)
		url := newConnectServer(t, pool, &stubLister{repos: []github.Repository{
			stubRepo("fresh-connect", "https://github.com/someone/fresh-connect.git"),
		}})

		status, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		require.Equal(t, http.StatusCreated, status, "body=%s", body)

		var got handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &got))
		require.Equal(t, "pending", got.SyncState,
			"the response must carry the projection this transaction committed")

		queue := jobsFor(t, pool, got.ID)
		require.Len(t, queue, 1, "connecting must create exactly one work item")
		require.Equal(t, "queued", queue[0].State)
		require.Equal(t, "full_ingest", queue[0].JobType)
		require.False(t, queue[0].NeedsRerun)
		require.Equal(t, orgA.ID, queue[0].OrganizationID)
		require.Equal(t, 0, queue[0].Attempts)
		require.Nil(t, queue[0].LeaseOwner, "a queued job has no owner yet")
		require.Equal(t, "pending", syncStateOfRepo(t, pool, orgA.ID, got.ID))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// A plain re-connect is a metadata refresh, not a retry. It must not touch
// the job that is already there — the ISS-016 scenario read from the other
// side.
func TestRepositoriesConnect_ReconnectingToTheSameInstallationCreatesNoJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 558000)
		lister := &stubLister{repos: []github.Repository{
			stubRepo("steady", "https://github.com/someone/steady.git"),
		}}
		url := newConnectServer(t, pool, lister)

		_, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		var first handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &first))
		before := jobsFor(t, pool, first.ID)
		require.Len(t, before, 1)

		// A worker has picked it up in the meantime.
		claimJob(t, pool, before[0].ID, "worker-1")

		lister.setRepos([]github.Repository{
			stubRepo("steady-renamed", "https://github.com/someone/steady.git"),
		})
		status, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		require.Equal(t, http.StatusCreated, status, "body=%s", body)

		after := jobsFor(t, pool, first.ID)
		require.Len(t, after, 1, "an unchanged installation must not enqueue anything")
		require.Equal(t, before[0].ID, after[0].ID)
		require.Equal(t, "running", after[0].State, "the in-flight job is untouched")
		require.False(t, after[0].NeedsRerun,
			"and it is not even flagged: nothing new was asked for")
		require.Equal(t, "syncing", syncStateOfRepo(t, pool, orgA.ID, first.ID),
			"the projection belongs to the running job")

		_, _, name := repoStateOf(t, pool, orgA.ID, first.ID)
		require.Equal(t, "steady-renamed", name, "metadata is still refreshed")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// The ISS-016 scenario itself: relinking a repository whose run is IN
// FLIGHT. It used to stamp `sync_state = 'pending'` over a `syncing` row
// and let two writers race for the outcome. Now the running job is
// superseded — it leaves the live set, keeping the lease that says which
// worker was interrupted — and its replacement is enqueued behind it.
func TestRepositoriesConnect_RelinkSupersedesARunningJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 559000)
		reinstalled := seedInstallation(t, pool, orgA.ID, 559001)
		url := newConnectServer(t, pool, &stubLister{repos: []github.Repository{
			stubRepo("relinked", "https://github.com/someone/relinked.git"),
		}})

		_, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		var first handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &first))
		original := jobsFor(t, pool, first.ID)[0].ID
		claimJob(t, pool, original, "worker-1")

		status, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(reinstalled))
		require.Equal(t, http.StatusCreated, status, "body=%s", body)

		var second handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &second))
		require.Equal(t, first.ID, second.ID, "a relink adopts the row")
		require.Equal(t, "pending", second.SyncState)

		queue := jobsFor(t, pool, first.ID)
		require.Len(t, queue, 2, "the old job is kept as the record of what was interrupted")

		byID := map[string]queuedJob{}
		for _, job := range queue {
			byID[job.ID] = job
		}
		require.Equal(t, "superseded", byID[original].State)
		require.NotNil(t, byID[original].LeaseOwner,
			"the supersede leaves the lease attached: it is the only record of which "+
				"worker was running when the job was taken away")
		require.Equal(t, "worker-1", *byID[original].LeaseOwner)

		live := liveJobsFor(t, pool, first.ID)
		require.Len(t, live, 1, "exactly one live job, which is the ISS-016 guarantee")
		require.NotEqual(t, original, live[0].ID)
		require.Equal(t, "queued", live[0].State)
		require.Equal(t, "full_ingest", live[0].JobType)
		require.Equal(t, orgA.ID, live[0].OrganizationID)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// L8: two reconnects landing together both want the same outcome, so one
// job satisfies both — and neither caller may be told it failed.
//
// Through the real handler, through the real router, over five rounds on a
// warm server. The enqueue upsert is what makes this safe: the loser flags
// the winner's job instead of colliding with it. The withdrawn
// catch-23505 design would have to turn one of these into a 500.
func TestRepositoriesConnect_ConcurrentRelinksLeaveOneLiveJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const rounds = 5

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 560000)
		reinstalled := seedInstallation(t, pool, orgA.ID, 560001)
		url := newConnectServer(t, pool, &stubLister{repos: []github.Repository{
			stubRepo("raced", "https://github.com/someone/raced.git"),
		}})

		_, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		var repo handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &repo))

		for round := 1; round <= rounds; round++ {
			// Back to the original installation, with one live job, so
			// every round is a genuine relink. Only this repository's rows
			// are touched.
			setInstallation(t, pool, orgA.ID, repo.ID, instA)
			clearJobsFor(t, pool, repo.ID)
			seedRunningJob(t, pool, orgA.ID, repo.ID, "worker-1")

			var (
				start    sync.WaitGroup
				done     sync.WaitGroup
				mu       sync.Mutex
				statuses []int
				bodies   []string
			)
			start.Add(1)
			done.Add(2)

			for i := 0; i < 2; i++ {
				go func() {
					defer done.Done()
					start.Wait() // release them together

					status, body := doRepoRequest(t, url, http.MethodPost,
						"/api/repositories", tokenA, connectBody(reinstalled))

					mu.Lock()
					defer mu.Unlock()
					statuses = append(statuses, status)
					bodies = append(bodies, body)
				}()
			}
			start.Done()
			done.Wait()

			for i, status := range statuses {
				require.Equalf(t, http.StatusCreated, status,
					"round %d: both concurrent relinks must succeed; body=%s", round, bodies[i])
			}
			live := liveJobsFor(t, pool, repo.ID)
			require.Lenf(t, live, 1,
				"round %d: two concurrent relinks must leave exactly one live job", round)
			require.Equalf(t, "pending", syncStateOfRepo(t, pool, orgA.ID, repo.ID),
				"round %d", round)
		}

		// The rounds above leave one live job and several superseded ones;
		// nothing else in the container is touched.
		_, err := pool.Exec(ctx, `DELETE FROM ingestion_jobs WHERE repository_id = $1`, repo.ID)
		require.NoError(t, err)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// ⚠ THIS IS WHAT PINS `FOR UPDATE OF r`.
//
// Connect's adopt lookup joins `projects`, and a bare `FOR UPDATE` locks
// every table in the FROM clause — so it would take a row lock on the
// organization's DEFAULT PROJECT, which every repository in the
// organization shares. Nothing would error; connects would simply queue up
// behind each other one at a time, and the only symptom would be latency
// under load.
//
// The first transaction here runs handlers.ExistingRepositoryLookupSQL
// ITSELF — the production statement, not a copy — for one repository, and
// holds it. A connect for a DIFFERENT repository in the same organization
// then has to finish. Widen the production statement to a bare
// `FOR UPDATE` and this deadlocks on the shared `projects` row until the
// deadline fires.
func TestRepositoriesConnect_ConcurrentConnectsOfDifferentRepositoriesDoNotBlock(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 561000)
		reinstalled := seedInstallation(t, pool, orgA.ID, 561001)

		const (
			firstGitHubID  = int64(4247000)
			secondGitHubID = int64(4248000)
			firstURL       = "https://github.com/someone/lock-a.git"
			secondURL      = "https://github.com/someone/lock-b.git"
		)
		lister := &stubLister{repos: []github.Repository{
			{ID: firstGitHubID, Name: "lock-a", Visibility: "private",
				DefaultBranch: "main", CloneURL: firstURL, SizeKB: 4},
			{ID: secondGitHubID, Name: "lock-b", Visibility: "private",
				DefaultBranch: "main", CloneURL: secondURL, SizeKB: 4},
		}}
		url := newConnectServer(t, pool, lister)

		// Both repositories exist and sit in the same default project, so
		// the join below resolves to the same `projects` row for each.
		connect := func(ghID int64, installation string) handlers.Repository {
			t.Helper()
			status, body := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, ghID, installation))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)
			var repo handlers.Repository
			require.NoError(t, json.Unmarshal([]byte(body), &repo))
			return repo
		}
		first := connect(firstGitHubID, instA)
		second := connect(secondGitHubID, instA)
		require.NotEqual(t, first.ID, second.ID)

		// Hold the FIRST repository's row the way an in-flight connect
		// does, using the handler's own statement.
		holder, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = holder.Rollback(ctx) }()

		var lockedID string
		var lockedInstallation *string
		var lockedGitHubID *int64
		require.NoError(t, holder.QueryRow(ctx, handlers.ExistingRepositoryLookupSQL,
			orgA.ID, firstGitHubID, firstURL,
		).Scan(&lockedID, &lockedInstallation, &lockedGitHubID))
		require.Equal(t, first.ID, lockedID, "the holder must have locked the row it meant to")

		// Now relink the OTHER repository. It must not wait on the row, or
		// on the project both of them hang off.
		finished := make(chan int, 1)
		go func() {
			status, _ := doRepoRequest(t, url, http.MethodPost, "/api/repositories", tokenA,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`, secondGitHubID, reinstalled))
			finished <- status
		}()

		select {
		case status := <-finished:
			require.Equal(t, http.StatusCreated, status)
		case <-time.After(15 * time.Second):
			t.Fatal("a connect blocked on another repository's connect: a bare FOR UPDATE " +
				"locks the joined projects row and serialises every connect in the " +
				"organization; `FOR UPDATE OF r` is what keeps them independent")
		}

		require.NoError(t, holder.Rollback(ctx))
		require.Len(t, liveJobsFor(t, pool, second.ID), 1)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// `ingestion_jobs` has NO row-level security, deliberately, so a job
// carrying the wrong tenant is not something the database will hide from a
// reader — it is something the schema refuses to store. This asserts the
// result with explicit `organization_id` filters, because a query on this
// table that omits one proves nothing about tenancy.
func TestRepositoriesConnect_JobsCarryTheConnectingOrganizationOnly(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		instA := seedInstallation(t, pool, orgA.ID, 562000)
		instB := seedInstallation(t, pool, orgB.ID, 562001)
		url := newConnectServer(t, pool, &stubLister{repos: []github.Repository{
			stubRepo("tenanted", "https://github.com/someone/tenanted.git"),
		}})

		status, body := doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instA))
		require.Equal(t, http.StatusCreated, status, "body=%s", body)
		var got handlers.Repository
		require.NoError(t, json.Unmarshal([]byte(body), &got))

		require.Equal(t, 1, countJobs(t, pool, `organization_id = $1 AND repository_id = $2`,
			orgA.ID, got.ID))
		require.Zero(t, countJobs(t, pool, `organization_id = $1 AND repository_id = $2`,
			orgB.ID, got.ID), "no job may carry org B's id for org A's repository")
		require.Zero(t, countJobs(t, pool, `organization_id = $1`, orgB.ID),
			"and org B, which did nothing here, must end with no jobs at all")

		// Org A naming org B's installation: 404, and no new work for
		// either tenant. Counted per organization rather than over the
		// whole table: the harness container is shared, and this must not
		// depend on what another package is doing to it.
		beforeA := countJobs(t, pool, `organization_id = $1`, orgA.ID)
		status, body = doRepoRequest(t, url, http.MethodPost,
			"/api/repositories", tokenA, connectBody(instB))
		require.Equal(t, http.StatusNotFound, status,
			"another tenant's installation must be invisible; body=%s", body)
		require.Equal(t, beforeA, countJobs(t, pool, `organization_id = $1`, orgA.ID),
			"a refused connect must create no work")
		require.Zero(t, countJobs(t, pool, `organization_id = $1`, orgB.ID),
			"least of all for the organization whose installation was named")

		_, err := pool.Exec(ctx, `DELETE FROM ingestion_jobs WHERE repository_id = $1`, got.ID)
		require.NoError(t, err)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// --- queue helpers ------------------------------------------------------
//
// `ingestion_jobs` has no RLS and the tenant trigger only fires on writes
// that touch organization_id or repository_id, so these read and write
// through the pool directly, as a worker does.

type queuedJob struct {
	ID             string
	OrganizationID string
	RepositoryID   string
	JobType        string
	State          string
	Attempts       int
	NeedsRerun     bool
	LeaseOwner     *string
}

const queuedJobColumns = `id::text, organization_id::text, repository_id::text,
	job_type, state, attempts, needs_rerun, lease_owner`

func scanJobs(t *testing.T, pool *pgxpool.Pool, where string, args ...any) []queuedJob {
	t.Helper()
	rows, err := pool.Query(context.Background(),
		`SELECT `+queuedJobColumns+` FROM ingestion_jobs WHERE `+where+` ORDER BY created_at, id`,
		args...)
	require.NoError(t, err)
	jobs, err := pgx.CollectRows(rows, pgx.RowToStructByPos[queuedJob])
	require.NoError(t, err)
	return jobs
}

func jobsFor(t *testing.T, pool *pgxpool.Pool, repoID string) []queuedJob {
	t.Helper()
	return scanJobs(t, pool, `repository_id = $1`, repoID)
}

// liveJobsFor returns the set the partial unique index allows at most one
// of. "Exactly one live job" is the ISS-016 guarantee, in the schema.
func liveJobsFor(t *testing.T, pool *pgxpool.Pool, repoID string) []queuedJob {
	t.Helper()
	return scanJobs(t, pool, `repository_id = $1 AND state IN ('queued','running')`, repoID)
}

// countJobs reads `ingestion_jobs` ONLY. It must not join or sub-select
// `repositories`: that table has row-level security, and an unscoped read
// of it through the pool is silently empty on a fresh connection and
// SQLSTATE 22P02 on one that has committed a `SET LOCAL` (ISS-013). The
// first version of the tenant test above did exactly that and got the
// 22P02.
func countJobs(t *testing.T, pool *pgxpool.Pool, where string, args ...any) int {
	t.Helper()
	var n int
	require.NoError(t, pool.QueryRow(context.Background(),
		`SELECT count(*) FROM ingestion_jobs WHERE `+where, args...).Scan(&n))
	return n
}

// claimJob is what a worker does when it picks the job up: it makes the row
// `running` with a lease, which is the state a relink has to supersede.
func claimJob(t *testing.T, pool *pgxpool.Pool, jobID, owner string) {
	t.Helper()
	ctx := context.Background()
	tag, err := pool.Exec(ctx, `
		UPDATE ingestion_jobs
		SET state = 'running', lease_owner = $2,
		    lease_expires_at = NOW() + INTERVAL '5 minutes',
		    attempts = attempts + 1, updated_at = NOW()
		WHERE id = $1`, jobID, owner)
	require.NoError(t, err)
	require.EqualValues(t, 1, tag.RowsAffected())

	// The projection a worker writes on claim (21-05). Written here so the
	// relink tests face the state ISS-016 was about: a `syncing` row.
	scoper := db.NewTenantScoper(pool)
	var orgID, repoID string
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT organization_id::text, repository_id::text FROM ingestion_jobs WHERE id = $1`,
		jobID).Scan(&orgID, &repoID))
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx,
			`UPDATE repositories SET sync_state = 'syncing' WHERE id = $1`, repoID)
		return err
	}))
}

func seedRunningJob(t *testing.T, pool *pgxpool.Pool, orgID, repoID, owner string) {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `
			INSERT INTO ingestion_jobs
			  (organization_id, repository_id, job_type, state, attempts,
			   lease_owner, lease_expires_at)
			VALUES ($1, $2, 'full_ingest', 'running', 1, $3, NOW() + INTERVAL '5 minutes')`,
			orgID, repoID, owner)
		return err
	}))
}

// clearJobsFor removes ONE repository's jobs. Scoped on purpose: nothing in
// this package may delete a row another package created — the harness
// container is shared, and `go test ./...` runs packages in parallel.
func clearJobsFor(t *testing.T, pool *pgxpool.Pool, repoID string) {
	t.Helper()
	_, err := pool.Exec(context.Background(),
		`DELETE FROM ingestion_jobs WHERE repository_id = $1`, repoID)
	require.NoError(t, err)
}

func setInstallation(t *testing.T, pool *pgxpool.Pool, orgID, repoID, installationID string) {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx,
			`UPDATE repositories SET installation_id = $2 WHERE id = $1`, repoID, installationID)
		return err
	}))
}

func syncStateOfRepo(t *testing.T, pool *pgxpool.Pool, orgID, repoID string) string {
	t.Helper()
	_, state, _ := repoStateOf(t, pool, orgID, repoID)
	return state
}

// TestDeleteReportsTheWholeCascade pins what DELETE actually destroys.
//
// The response used to report chunks and ingestion runs only, while the
// cascade also took `retrievals` and — two edges down — user-written
// `feedback`, which is the one thing here that re-ingesting cannot bring
// back.
func TestDeleteReportsTheWholeCascade(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		url := newConnectServer(t, pool, &stubLister{})
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")

		seedIngestedContent(t, pool, orgA, 2)
		seedIngestedContent(t, pool, orgB, 1)

		// A SECOND repository for orgA, with its own chain.
		//
		// Without it the delete cannot be measured for over-breadth within
		// the tenant: `WithTwoOrgs` gives orgA exactly one repository, so
		// `DELETE ... WHERE project_id = (the row's project)` — a realistic
		// wrong-column bug — takes everything orgA has and every assertion
		// still passes.
		siblingID := seedSiblingRepository(t, pool, orgA)

		status, body := doRepoRequest(t, url, http.MethodDelete,
			"/api/repositories/"+orgA.RepoID, tokenA, "")
		require.Equal(t, http.StatusOK, status, "body=%s", body)

		var got handlers.DeleteRepositoryResponse
		require.NoError(t, json.Unmarshal([]byte(body), &got))
		require.Equal(t, int64(2), got.ChunksDeleted)
		require.Equal(t, int64(1), got.IngestionsGone)
		require.Equal(t, int64(1), got.FeedbackDeleted,
			"feedback is destroyed by the cascade and must be reported")

		// The other tenant's chain is untouched. This is what makes the
		// counts above mean "removed exactly one repository's worth"
		// rather than "removed everything" — a scoped count of orgA's own
		// feedback after the delete cannot tell deletion from invisibility,
		// because the RLS path runs through the repository that just went.
		require.Equal(t, []int64{1, 1, 1}, ingestedCounts(t, pool, orgB),
			"deleting orgA's repository must not touch orgB's chunks, runs or feedback")

		// And orgA's OTHER repository, in the same project, survives whole.
		// This is the assertion that catches a within-tenant over-broad
		// delete — a wrong WHERE column, a wrong join, a cascade that takes
		// siblings — which the cross-tenant checks above cannot see.
		require.Equal(t, []int64{1, 1, 1}, ingestedCounts(t, pool, orgA),
			"deleting one repository must not take orgA's other one with it")
		s, b := doRepoRequest(t, url, http.MethodGet, "/api/repositories/"+siblingID, tokenA, "")
		require.Equal(t, http.StatusOK, s, "orgA's sibling repository was deleted too; body=%s", b)
	})
}

// seedSiblingRepository adds a second repository to org's default project,
// with its own ingestion chain, and returns its id.
func seedSiblingRepository(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg) string {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)
	bg := context.Background()

	var repoID string
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		if err := tx.QueryRow(bg, `
			INSERT INTO repositories (project_id, name, git_url, sync_state)
			VALUES ($1, 'sibling', $2, 'never_synced')
			RETURNING id::text`,
			org.ProjectID, "https://example.test/"+org.Slug+"-sibling.git").Scan(&repoID); err != nil {
			return err
		}
		var runID, chunkID, queryID, retrievalID string
		if err := tx.QueryRow(bg, `
			INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
			VALUES ($1, repeat('c', 40), 'main', 'completed')
			RETURNING id::text`, repoID).Scan(&runID); err != nil {
			return err
		}
		if err := tx.QueryRow(bg, `
			INSERT INTO chunks
			  (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
			VALUES ($1, $2, 'sibling.go', 1, 2, 'package main', repeat('d', 64))
			RETURNING id::text`, runID, repoID).Scan(&chunkID); err != nil {
			return err
		}
		if err := tx.QueryRow(bg, `
			INSERT INTO queries (project_id, query_text) VALUES ($1, 'sibling?')
			RETURNING id::text`, org.ProjectID).Scan(&queryID); err != nil {
			return err
		}
		if err := tx.QueryRow(bg, `
			INSERT INTO retrievals (query_id, chunk_id, rank, score)
			VALUES ($1, $2, 1, 0.5) RETURNING id::text`, queryID, chunkID).Scan(&retrievalID); err != nil {
			return err
		}
		_, err := tx.Exec(bg, `
			INSERT INTO feedback (retrieval_id, feedback_type) VALUES ($1, 'neutral')`, retrievalID)
		return err
	}))
	return repoID
}

// ingestedCounts reports {chunks, ingestion_runs, feedback} within org's
// own tenant scope.
func ingestedCounts(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg) []int64 {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)
	out := make([]int64, 3)
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			SELECT (SELECT count(*) FROM chunks),
			       (SELECT count(*) FROM ingestion_runs),
			       (SELECT count(*) FROM feedback)`).Scan(&out[0], &out[1], &out[2])
	}))
	return out
}

// seedIngestedContent builds one full chain under org's fixture
// repository: ingestion_run -> chunks -> query -> retrieval -> feedback.
func seedIngestedContent(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, chunks int) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)
	bg := context.Background()

	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		var runID string
		if err := tx.QueryRow(bg, `
			INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
			VALUES ($1, repeat('a', 40), 'main', 'completed')
			RETURNING id::text`, org.RepoID).Scan(&runID); err != nil {
			return fmt.Errorf("ingestion run: %w", err)
		}

		var chunkID string
		for i := 0; i < chunks; i++ {
			if err := tx.QueryRow(bg, `
				INSERT INTO chunks
				  (ingestion_run_id, repository_id, file_path, start_line, end_line,
				   content, content_hash)
				VALUES ($1, $2, $3, 1, 2, 'package main', repeat('b', 64))
				RETURNING id::text`,
				runID, org.RepoID, fmt.Sprintf("main%d.go", i)).Scan(&chunkID); err != nil {
				return fmt.Errorf("chunk %d: %w", i, err)
			}
		}

		var queryID string
		if err := tx.QueryRow(bg, `
			INSERT INTO queries (project_id, query_text) VALUES ($1, 'what does this do?')
			RETURNING id::text`, org.ProjectID).Scan(&queryID); err != nil {
			return fmt.Errorf("query: %w", err)
		}

		var retrievalID string
		if err := tx.QueryRow(bg, `
			INSERT INTO retrievals (query_id, chunk_id, rank, score)
			VALUES ($1, $2, 1, 0.9) RETURNING id::text`, queryID, chunkID).Scan(&retrievalID); err != nil {
			return fmt.Errorf("retrieval: %w", err)
		}

		if _, err := tx.Exec(bg, `
			INSERT INTO feedback (retrieval_id, feedback_type, feedback_text)
			VALUES ($1, 'positive', 'exactly what I needed')`, retrievalID); err != nil {
			return fmt.Errorf("feedback: %w", err)
		}
		return nil
	}))

	// Confirm the seed landed, inside the scope — an unscoped read of an
	// RLS table returns zero rows on a fresh connection and SQLSTATE 22P02
	// on a previously-scoped one (ISS-013), so it can neither verify this
	// nor fail honestly.
	require.Equal(t, []int64{int64(chunks), 1, 1}, ingestedCounts(t, pool, org),
		"seed did not produce the chain the cascade test measures")
}
