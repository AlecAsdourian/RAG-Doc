package handlers_test

// Isolation tests for the repository CRUD surface — the first API in this
// project whose handlers read and write an RLS-scoped table.
//
// The two that matter most are scenario 3 (delete) and scenario 4
// (connect through another tenant's installation). Both are cross-tenant
// WRITES, and both are written to assert on the resulting ROW rather than
// on a status code: under RLS a cross-tenant write does not error, it
// simply matches nothing, so "no error" and "did the wrong thing" look
// identical from the outside.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

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

// seedInstallation creates a github_installations row for org and returns
// its internal id.
func seedInstallation(t *testing.T, pool *pgxpool.Pool, orgID string, ghID int64) string {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	var id string
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(context.Background(), orgID),
		func(tx pgx.Tx) error {
			return tx.QueryRow(context.Background(), `
				INSERT INTO github_installations
				  (organization_id, github_installation_id, account_login,
				   account_type, repository_selection)
				VALUES ($1, $2, 'someone', 'User', 'selected')
				RETURNING id::text
			`, orgID, ghID).Scan(&id)
		}))
	return id
}

func TestRepositoriesIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("repository endpoints must never call the RAG service; got %s", r.URL.Path)
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

		instA := seedInstallation(t, pool, orgA.ID, 111000)
		instB := seedInstallation(t, pool, orgB.ID, 222000)

		t.Run("Scenario1_ListReturnsOnlyYourOwnRepositories", func(t *testing.T) {
			// WithTwoOrgs gives each org exactly one repository.
			resp := listRepositories(t, server.URL, tokenA, "")

			require.Len(t, resp.Repositories, 1)
			require.Equal(t, orgA.RepoID, resp.Repositories[0].ID)
			for _, r := range resp.Repositories {
				require.NotEqual(t, orgB.RepoID, r.ID,
					"cross-tenant leak: orgA listed orgB's repository")
			}
		})

		t.Run("Scenario2_CannotGetAnotherTenantsRepositoryByID", func(t *testing.T) {
			status, body := doRepoRequest(t, server.URL, http.MethodGet,
				"/api/repositories/"+orgB.RepoID, tokenA, "")
			require.Equal(t, http.StatusNotFound, status, "body=%s", body)

			// 404 and not 403: the two must be indistinguishable, or the
			// endpoint becomes an existence oracle for other tenants' ids.
			nonexistent, _ := doRepoRequest(t, server.URL, http.MethodGet,
				"/api/repositories/99999999-9999-9999-9999-999999999999", tokenA, "")
			require.Equal(t, nonexistent, status,
				"'another tenant's repository' and 'no such repository' must look the same")
		})

		t.Run("Scenario3_CannotDeleteAnotherTenantsRepository", func(t *testing.T) {
			status, body := doRepoRequest(t, server.URL, http.MethodDelete,
				"/api/repositories/"+orgB.RepoID, tokenA, "")
			require.Equal(t, http.StatusNotFound, status, "body=%s", body)

			// Assert on the ROW. A cross-tenant DELETE under RLS matches
			// nothing rather than erroring, so a status-only assertion
			// would pass against a handler that deleted the row.
			resp := listRepositories(t, server.URL, tokenB, "")
			require.Len(t, resp.Repositories, 1,
				"orgA deleted orgB's repository")
			require.Equal(t, orgB.RepoID, resp.Repositories[0].ID)
		})

		t.Run("Scenario4_CannotConnectThroughAnotherTenantsInstallation", func(t *testing.T) {
			// The most important test here. orgA names orgB's installation.
			// If this succeeded, a later sync would mint a GitHub token for
			// orgB's account while acting for orgA.
			body := fmt.Sprintf(`{"github_repo_id":12345,"installation_id":%q}`, instB)
			status, respBody := doRepoRequest(t, server.URL, http.MethodPost,
				"/api/repositories", tokenA, body)

			require.Equal(t, http.StatusNotFound, status,
				"orgA connected a repository through orgB's installation; body=%s", respBody)

			// And nothing was written.
			resp := listRepositories(t, server.URL, tokenA, "")
			require.Len(t, resp.Repositories, 1,
				"a refused connect must not create a repository")
		})

		t.Run("Scenario5_ClaimlessTokenIsRefused", func(t *testing.T) {
			claimless := testjwt.SignWithoutOrg(orgA.OwnerSupabaseID)
			status, body := doRepoRequest(t, server.URL, http.MethodGet,
				"/api/repositories", claimless, "")
			require.Equal(t, http.StatusForbidden, status,
				"a token with no organization claim must not reach tenant data; body=%s", body)
		})

		t.Run("Scenario6_PaginationDoesNotWalkIntoAnotherTenant", func(t *testing.T) {
			// Fill both orgs past one page, then page orgA to exhaustion
			// and confirm every id belongs to orgA.
			seedRepositories(t, pool, orgA, instA, 5)
			seedRepositories(t, pool, orgB, instB, 5)

			ownedByA := map[string]bool{orgA.RepoID: true}
			scoper := db.NewTenantScoper(pool)
			require.NoError(t, scoper.InTenantTx(
				auth.ContextWithOrgID(context.Background(), orgA.ID),
				func(tx pgx.Tx) error {
					rows, err := tx.Query(context.Background(), `SELECT id::text FROM repositories`)
					if err != nil {
						return err
					}
					defer rows.Close()
					for rows.Next() {
						var id string
						if err := rows.Scan(&id); err != nil {
							return err
						}
						ownedByA[id] = true
					}
					return rows.Err()
				}))

			seen := 0
			cursor := ""
			for page := 0; page < 20; page++ {
				resp := listRepositories(t, server.URL, tokenA, cursor+"&limit=2")
				for _, repo := range resp.Repositories {
					require.Truef(t, ownedByA[repo.ID],
						"pagination walked into another tenant: repository %s", repo.ID)
					seen++
				}
				if resp.NextCursor == nil {
					break
				}
				cursor = "cursor=" + *resp.NextCursor + "&"
			}

			require.Equal(t, 6, seen,
				"orgA should page through exactly its own 6 repositories (1 fixture + 5 seeded)")
		})
	})
}

// seedRepositories inserts n repositories for org, under its installation.
func seedRepositories(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, installationID string, n int) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)

	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		for i := 0; i < n; i++ {
			if _, err := tx.Exec(context.Background(), `
				INSERT INTO repositories
				  (project_id, installation_id, github_repo_id, name, git_url, sync_state)
				VALUES ($1, $2, $3, $4, $5, 'pending')
			`, org.ProjectID, installationID, int64(900000+i),
				fmt.Sprintf("seeded-%d", i),
				fmt.Sprintf("https://example.test/%s-%d.git", org.Slug, i)); err != nil {
				return err
			}
		}
		return nil
	}))
}

func doRepoRequest(t *testing.T, baseURL, method, path, token, body string) (int, string) {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, baseURL+path, reader)
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(raw)
}

func listRepositories(t *testing.T, baseURL, token, query string) handlers.RepositoryListResponse {
	t.Helper()
	url := baseURL + "/api/repositories"
	if query != "" {
		url += "?" + strings.TrimSuffix(query, "&")
	}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode, "body=%s", raw)

	var out handlers.RepositoryListResponse
	require.NoError(t, json.Unmarshal(raw, &out), "body=%s", raw)
	return out
}
