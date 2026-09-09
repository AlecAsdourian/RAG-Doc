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
type stubLister struct {
	repos        []github.Repository
	err          error
	calls        int
	beforeReturn func()
}

func (s *stubLister) ListInstallationRepositories(
	_ context.Context, _ int64,
) ([]github.Repository, error) {
	s.calls++
	if s.beforeReturn != nil {
		s.beforeReturn()
	}
	return s.repos, s.err
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

			lister.repos = []github.Repository{
				stubRepo("steady-repo-renamed", "https://github.com/someone/steady-repo.git"),
			}
			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status, "body=%s", body)

			_, sync, name := repoStateOf(t, pool, orgA.ID, first.ID)
			require.Equal(t, "steady-repo-renamed", name, "metadata must be refreshed")
			require.Equal(t, "failed", sync,
				"an unchanged installation must not re-queue; the state belongs to Phase 21")
		})

		t.Run("DoesNotCollideWithAGitURLAlreadyInTheProject", func(t *testing.T) {
			// A row with no GitHub id holding the same clone URL — the
			// shape of anything connected before this API existed. It used
			// to abort the connect with a 500.
			const shared = "https://github.com/someone/shared-url.git"
			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				_, err := tx.Exec(context.Background(), `
					INSERT INTO repositories (project_id, name, git_url, sync_state)
					VALUES ($1, 'legacy', $2, 'never_synced')`, orgA.ProjectID, shared)
				return err
			}))

			lister := &stubLister{repos: []github.Repository{stubRepo("shared-url", shared)}}
			url := newConnectServer(t, pool, lister)

			status, body := doRepoRequest(t, url, http.MethodPost,
				"/api/repositories", tokenA, connectBody(instA))
			require.Equal(t, http.StatusCreated, status,
				"a stored git_url must not block connecting a different repository; body=%s", body)
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
			url := newConnectServer(t, pool, &stubLister{})
			for _, id := range []string{
				"urn:uuid:" + orgA.RepoID,
				"{" + orgA.RepoID + "}",
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
	})
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
