package handlers_test

// TestSearchIsolation exercises /api/search end-to-end through the real
// router (chi + auth middleware + tenant middleware + handler + RAG
// client) against a real Postgres from the 17-01 harness. The Python RAG
// side is stubbed by an httptest.Server that itself queries the shared
// pool under isolation.TenantScope, so the RLS policy in migration 8 is
// the last line of defense inside every scenario.
//
// Tenant identity travels in the signed JWT's `app_metadata` claim.
// Phase 19-03 removed the X-Organization-ID header entirely, so these
// tests carry tenant purely via testjwt.Sign — there is no request-
// controlled input a caller could use to redirect their own scope.
//
// Scenarios 4-7 were rewritten or added in 19-03: scenario 4 previously
// asserted a missing header returned 400 and now asserts a token with no
// organization claim returns 403; scenario 5 previously PINNED the
// header-trust vulnerability as expected behavior and now asserts the
// claim is authoritative; scenarios 6 and 7 are new.
//
// Scenario 7 is the regression guard for the vulnerability this phase
// closed, and it exists because a reviewer demonstrated its absence: with
// the header path re-added to TenantMiddleware, every other scenario in
// this file still passed. A suite that cannot detect the re-introduction
// of the hole it was written for is not covering it.

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
		chunkA := insertSearchChunk(t, pool, orgA, "orange marmalade recipe")
		// chunkB is required by scenario 2 (orgB queries "purple") — the id
		// itself is not asserted, but the row must exist for that scenario
		// to see exactly one match under RLS.
		_ = insertSearchChunk(t, pool, orgB, "purple velvet cake")

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
			// Use the request's context so a client cancellation propagates
			// to the DB query — matches how the real Python side should
			// behave.
			results, err := queryChunksUnderTenant(r.Context(), pool, req.OrganizationID, req.RepositoryID, req.Query)
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
			status, resp := doSearch(t, server.URL, orgAToken, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 1, resp.TotalResults, "orgA should see 1 marmalade chunk")
			require.Equal(t, chunkA, resp.Results[0].ChunkID)
			require.Contains(t, resp.Results[0].Content, "marmalade")
		})

		t.Run("Scenario2_OrgBQueriesOwnRepo_SeesOnlyOwnChunk", func(t *testing.T) {
			body := fmt.Sprintf(`{"query":"purple","repository_id":%q}`, orgB.RepoID)
			status, resp := doSearch(t, server.URL, orgBToken, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 1, resp.TotalResults, "orgB should see 1 velvet-cake chunk")
			require.Contains(t, resp.Results[0].Content, "velvet")
		})

		t.Run("Scenario3_CrossTenantRepoAccess_ReturnsEmpty", func(t *testing.T) {
			// OrgB asks for orgA's repo. RLS on chunks scoped to app.current_tenant
			// means the stub's SELECT returns 0 rows. A 200 with any of orgA's
			// data would be a leak — the assertion below catches that.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, resp := doSearch(t, server.URL, orgBToken, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 0, resp.TotalResults, "orgB must NOT see orgA's chunks — this is a cross-tenant leak")
			require.Empty(t, resp.Results)
		})

		t.Run("Scenario4_TokenWithoutOrgClaim_Rejected", func(t *testing.T) {
			// A validly-signed token carrying no app_metadata at all — the
			// shape a user has if provisioning never ran for them, or if the
			// Supabase org-context push failed. The middleware must refuse
			// before the handler sees the request.
			//
			// Pre-19-03 this tested a missing X-Organization-ID header and
			// expected 400. That header no longer exists. Tenant comes only
			// from the signed claim, and its absence is 403 (authenticated,
			// but has no organization) rather than 400 (malformed request).
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			req, err := http.NewRequest(http.MethodPost, server.URL+"/api/search",
				strings.NewReader(body))
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer "+testjwt.SignWithoutOrg(orgA.OwnerID))
			req.Header.Set("Content-Type", "application/json")

			httpResp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			defer httpResp.Body.Close()
			require.Equal(t, http.StatusForbidden, httpResp.StatusCode,
				"a token with no app_metadata.organization_id must be refused")
		})

		t.Run("Scenario5_OrgClaimIsAuthoritative_SubIsIgnoredForScoping", func(t *testing.T) {
			// Replaces the pre-19-03 header-tamper scenario, which asserted
			// that a client authenticated as orgA could read orgB's data by
			// setting X-Organization-ID. That path is gone.
			//
			// What this proves: the organization CLAIM decides tenant scope,
			// and the `sub` claim has no say. The token below names orgA's
			// owner as the subject and orgB in the org claim, and the
			// response is orgB's data — cleanly, not a mixture, and not an
			// error.
			//
			// The name is careful on purpose. An earlier revision called this
			// "TamperedOrgClaim_CannotReachOtherTenantsData", which asserted
			// the opposite of what happens: the token DOES reach orgB's data,
			// and that is correct and intended. Only Supabase's signing key
			// can mint this token, so "tampered" was never the right word.
			//
			// What this does NOT prove, stated plainly: the middleware trusts
			// the claim wholesale. The membership check happens where the
			// claim is WRITTEN (the webhook's org-context push, and 19-04's
			// select-organization), not per request. Adding a per-request
			// membership query would be defense-in-depth against an attacker
			// who by construction already controls token issuance — a real
			// but lower-value trade against a database round-trip on every
			// request. Deliberate non-goal; revisit if token-signing ever
			// moves in-house.
			body := fmt.Sprintf(`{"query":"purple","repository_id":%q}`, orgB.RepoID)
			crossSubject := testjwt.Sign(orgA.OwnerID, orgB.ID, "owner")

			status, resp := doSearch(t, server.URL, crossSubject, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)

			// POSITIVE assertions. The previous version asserted only that no
			// result contained "marmalade" — which every broken middleware
			// also satisfies, because a broken middleware returns zero
			// results and the loop body never runs. Verified: under a
			// middleware whose claim read was replaced with a nonexistent
			// org id, five other scenarios went red and this one stayed
			// green. Requiring the orgB row to actually be there is what
			// makes it able to fail.
			require.Equal(t, 1, resp.TotalResults,
				"the org claim must scope the request to orgB's data, not to nothing")
			require.Contains(t, resp.Results[0].Content, "velvet",
				"the row returned must be orgB's, selected by the claim")
			require.NotContains(t, resp.Results[0].Content, "marmalade",
				"orgA's data must never appear under an orgB-scoped token")
		})

		t.Run("Scenario7_TenantHeaderIsIgnored_CannotRedirectScope", func(t *testing.T) {
			// The regression guard for the exact hole 19-03 closed.
			//
			// Before this phase, X-Organization-ID chose the tenant, so any
			// authenticated user could read any organization's data by
			// setting it. The header path is deleted — but nothing else in
			// this suite would notice if it came back, because no other test
			// sends the header. Verified: re-adding a header-with-claim-
			// fallback read to TenantMiddleware left all other scenarios
			// green.
			//
			// So: send orgA's token, ask for orgA's repo, and set the header
			// to orgB. If the header has any influence at all, the result
			// stops being orgA's marmalade row.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, resp := doSearch(t, server.URL, orgAToken, body, func(r *http.Request) {
				r.Header.Set("X-Organization-ID", orgB.ID)
			})
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)

			require.Equal(t, 1, resp.TotalResults,
				"the request must stay scoped to orgA — the header must not redirect it")
			require.Contains(t, resp.Results[0].Content, "marmalade",
				"orgA's own row must come back; a header-influenced scope would not return it")
		})

		t.Run("Scenario6_OrgAClaimNeverSeesOrgBData", func(t *testing.T) {
			// The complement of scenario 5, and the one that would catch a
			// middleware that ignored the claim entirely and fell back to
			// something request-controlled. OrgA's token asking for orgB's
			// repo must come back empty.
			body := fmt.Sprintf(`{"query":"purple","repository_id":%q}`, orgB.RepoID)
			status, resp := doSearch(t, server.URL, orgAToken, body)
			require.Equal(t, http.StatusOK, status, "body=%s", resp.raw)
			require.Equal(t, 0, resp.TotalResults,
				"orgA's token must not reach orgB's repository")
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
// The variadic mutators let a scenario add something to the request
// without giving the helper a parameter for it. Scenario 7 uses this to
// attach the deleted X-Organization-ID header: the header must have no
// effect, so it belongs in the one test that proves that rather than in
// the helper's signature, where its presence would imply it is part of
// the protocol.
func doSearch(t *testing.T, baseURL, token, body string, mutate ...func(*http.Request)) (int, decodedSearch) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/api/search", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	for _, m := range mutate {
		m(req)
	}

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
