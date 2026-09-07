package handlers_test

// TestSearchIsolation exercises /api/search end-to-end through the real
// router (chi + auth middleware + tenant middleware + handler + RAG
// client) against a real Postgres from the 17-01 harness. The Python RAG
// side is stubbed by an httptest.Server that itself queries the shared
// pool under isolation.TenantScope, so the RLS policy in migration 8 is
// the last line of defense inside every scenario.
//
// Scenario 5 (originally: JWT tampering) is reframed to tamper the
// X-Organization-ID header, because the current TenantMiddleware pulls
// tenant from that header rather than from the JWT claim. The JWT-carried
// claim ships in Phase 19-03; a followup in .planning/ISSUES.md re-runs
// the equivalent test against that mechanism when it lands.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

func TestSearchIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		ctx := context.Background()

		chunkA := insertSearchChunk(t, pool, orgA, "orange marmalade recipe")
		chunkB := insertSearchChunk(t, pool, orgB, "purple velvet cake")
		_ = chunkB // included so cleanup path is exercised; not asserted directly

		// Stub Python RAG service. It reads OrganizationID and RepositoryID
		// from the request body, opens a TenantScope tx against the shared
		// pool, and runs the retrieval query under RLS. This mirrors what
		// the real Python side must do: honor the tenant the Go layer
		// passes it.
		fakePython := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/search" {
				http.NotFound(w, r)
				return
			}
			var req client.SearchRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			results, err := queryChunksUnderTenant(ctx, pool, req.OrganizationID, req.RepositoryID, req.Query)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			resp := client.SearchResponse{
				Results:      results,
				TotalResults: len(results),
				QueryID:      "test-query",
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(resp)
		}))
		t.Cleanup(fakePython.Close)

		ragClient := client.NewRAGClient(fakePython.URL)
		router := api.NewRouterWithValidator(pool, ragClient, testjwt.NewValidator(), api.Config{
			LogLevel: slog.LevelWarn,
		})
		server := httptest.NewServer(router)
		t.Cleanup(server.Close)

		orgAToken := testjwt.Sign(orgA.OwnerID, orgA.ID, "owner")
		orgBToken := testjwt.Sign(orgB.OwnerID, orgB.ID, "owner")

		t.Run("Scenario1_OrgAQueriesOwnRepo_SeesOnlyOwnChunk", func(t *testing.T) {
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, resp := doSearch(t, server.URL, orgAToken, orgA.ID, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 1, resp.TotalResults, "orgA should see 1 marmalade chunk")
			require.Equal(t, chunkA, resp.Results[0].ChunkID)
			require.Contains(t, resp.Results[0].Content, "marmalade")
		})

		t.Run("Scenario2_OrgBQueriesOwnRepo_SeesOnlyOwnChunk", func(t *testing.T) {
			body := fmt.Sprintf(`{"query":"purple","repository_id":%q}`, orgB.RepoID)
			status, resp := doSearch(t, server.URL, orgBToken, orgB.ID, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 1, resp.TotalResults, "orgB should see 1 velvet-cake chunk")
			require.Contains(t, resp.Results[0].Content, "velvet")
		})

		t.Run("Scenario3_CrossTenantRepoAccess_ReturnsEmpty", func(t *testing.T) {
			// OrgB asks for orgA's repo. RLS on chunks scoped to app.current_tenant
			// means the stub's SELECT returns 0 rows. A 200 with any of orgA's
			// data would be a leak — the assertion below catches that.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, resp := doSearch(t, server.URL, orgBToken, orgB.ID, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 0, resp.TotalResults, "orgB must NOT see orgA's chunks — this is a cross-tenant leak")
			require.Empty(t, resp.Results)
		})

		t.Run("Scenario4_MissingTenantHeader_Rejected", func(t *testing.T) {
			// TenantMiddleware currently sources tenant from X-Organization-ID.
			// Omit it and the middleware must reject before the handler runs.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			req, err := http.NewRequest(http.MethodPost, server.URL+"/api/search", strings.NewReader(body))
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer "+orgAToken)
			req.Header.Set("Content-Type", "application/json")
			// Deliberately no X-Organization-ID header.
			httpResp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			defer httpResp.Body.Close()
			require.Equal(t, http.StatusBadRequest, httpResp.StatusCode,
				"middleware must reject a request with no tenant header (currently) or claim (after 19-03)")
		})

		t.Run("Scenario5_HeaderTamper_HeaderCurrentlyTrusted_TODO_1903", func(t *testing.T) {
			// Reframed from the plan's JWT-tampering scenario. The current
			// TenantMiddleware trusts X-Organization-ID blindly, so a client
			// authenticated as an orgA user CAN currently obtain orgB data by
			// setting the header to orgB's id. This test pins that behavior
			// so a change in either direction is caught:
			//
			//   - Regression (silently drop header): scenario 1/2 break.
			//   - Fix in 19-03 (validate membership from JWT claim): update
			//     this assertion to expect 403.
			//
			// Followup filed: .planning/ISSUES.md ISS-007.
			body := fmt.Sprintf(`{"query":"purple","repository_id":%q}`, orgB.RepoID)
			// JWT identifies user as orgA owner, header claims orgB.
			status, resp := doSearch(t, server.URL, orgAToken, orgB.ID, body)
			require.Equal(t, http.StatusOK, status,
				"current behavior: header wins; 19-03 must flip this to 403 or membership-check")
			require.Equal(t, 1, resp.TotalResults, "with header-trust, orgA JWT + orgB header currently reaches orgB data")
			require.Contains(t, resp.Results[0].Content, "velvet",
				"header-trusted path returned orgB's chunk to a user JWT'd for orgA")
		})
	})
}

// insertSearchChunk writes one ingestion_run + one chunk under org's tenant
// scope and returns the chunk id. Uses direct TenantScope inserts rather
// than the ingestion pipeline for test speed, per plan.
func insertSearchChunk(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, content string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var runID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, $2, $3, $4) RETURNING id`,
		org.RepoID, "0000000000000000000000000000000000000000", "main", "completed",
	).Scan(&runID))

	var chunkID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO chunks (ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash)
		 VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
		runID, org.RepoID, "recipes/entry.md", 1, 10, content, fmt.Sprintf("h-%x", len(content)),
	).Scan(&chunkID))

	require.NoError(t, tx.Commit(ctx))
	return chunkID
}

// queryChunksUnderTenant simulates the retrieval query the Python side
// would run: open a tenant-scoped tx, run a naive content match, return
// matching rows. RLS filters by app.current_tenant so a mismatched tenant
// silently returns 0 rows even if the repo id matches.
func queryChunksUnderTenant(ctx context.Context, pool *pgxpool.Pool, orgID, repoID, query string) ([]client.ChunkResult, error) {
	tx, err := isolation.TenantScope(ctx, pool, orgID)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	rows, err := tx.Query(ctx,
		`SELECT id::text, content, file_path, start_line, end_line
		 FROM chunks
		 WHERE repository_id = $1::uuid AND content ILIKE '%' || $2 || '%'`,
		repoID, query,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var results []client.ChunkResult
	for rows.Next() {
		var (
			id, content, path string
			startL, endL      int
		)
		if err := rows.Scan(&id, &content, &path, &startL, &endL); err != nil {
			return nil, err
		}
		results = append(results, client.ChunkResult{
			ChunkID:        id,
			Content:        content,
			ContentPreview: content,
			FilePath:       path,
			StartLine:      startL,
			EndLine:        endL,
			Score:          1.0,
		})
	}
	return results, rows.Err()
}

// doSearch POSTs a search request through the real router and decodes
// the SearchResponseBody. Returns the raw status plus a partially decoded
// response so tests can assert on TotalResults and Results.
func doSearch(t *testing.T, baseURL, token, orgHeader, body string) (int, decodedSearch) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/api/search", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-Organization-ID", orgHeader)
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)

	var decoded decodedSearch
	decoded.raw = raw
	if resp.StatusCode == http.StatusOK {
		require.NoError(t, json.NewDecoder(bytes.NewReader(raw)).Decode(&decoded))
	}
	return resp.StatusCode, decoded
}

type decodedSearch struct {
	Results      []client.ChunkResult `json:"results"`
	TotalResults int                  `json:"total_results"`
	raw          []byte
}

// String lets require.Equal / require.Contains print the raw bytes if a
// decoded assertion fails, so failures show what the server actually sent.
func (d decodedSearch) String() string { return string(d.raw) }
