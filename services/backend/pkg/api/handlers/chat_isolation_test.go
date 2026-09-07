package handlers_test

// TestChatIsolation exercises /api/chat/stream end-to-end. The stub Python
// side speaks the same wire protocol as the real one — a stream of SSE
// events, each carrying a ChatChunk JSON payload — and, like the search
// stub in search_isolation_test.go, runs its retrieval query under
// isolation.TenantScope so RLS is the last line of defense on every
// scenario.
//
// Scenario coverage differs from the plan in two places, documented on
// each subtest:
//
//   - Scenario 3 (cross-tenant session_id) has no code to test yet — chat
//     sessions land in a later phase. Reframed as the streaming analog of
//     search's cross-tenant repo access.
//   - Scenario 4 tests the missing X-Organization-ID header today; the
//     JWT-carried variant lands in 19-03 alongside the search reframe.

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

func TestChatIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		ctx := context.Background()

		_ = insertSearchChunk(t, pool, orgA, "orange marmalade recipe")
		_ = insertSearchChunk(t, pool, orgB, "purple velvet cake")

		// Stub Python streaming endpoint. Emits one chunk of answer text
		// and a `done` event whose Sources are the RLS-filtered rows.
		fakePython := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/chat/stream" {
				http.NotFound(w, r)
				return
			}
			var req client.ChatRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}

			results, err := queryChunksUnderTenant(ctx, pool, req.OrganizationID, req.RepositoryID, req.Query)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}

			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Cache-Control", "no-cache")
			flusher, _ := w.(http.Flusher)

			// Answer text — proves handler forwards content chunks.
			answer := "no matching context"
			if len(results) > 0 {
				answer = fmt.Sprintf("Found %d chunk(s) about %q", len(results), req.Query)
			}
			writeChunk(w, flusher, client.ChatChunk{Type: "chunk", Content: answer})

			// Sources — the tenant-scoped retrieval result.
			sources := make([]client.SourceInfo, 0, len(results))
			for i, r := range results {
				sources = append(sources, client.SourceInfo{
					Number:    i + 1,
					FilePath:  r.FilePath,
					StartLine: r.StartLine,
					EndLine:   r.EndLine,
				})
			}
			writeChunk(w, flusher, client.ChatChunk{
				Type:    "done",
				Sources: sources,
				QueryID: "test-query",
			})
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

		t.Run("Scenario1_StreamingAsOrgA_SourcesFromOwnRepoOnly", func(t *testing.T) {
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, chunks := doChatStream(t, server.URL, orgAToken, orgA.ID, body)
			require.Equal(t, http.StatusOK, status)
			done := findDoneChunk(t, chunks)
			require.Len(t, done.Sources, 1, "orgA streaming should surface 1 marmalade source")
			require.Contains(t, chunks[0].Content, "1 chunk", "answer text must reference chunk count")
		})

		t.Run("Scenario2_StreamingAsOrgB_SameQuery_NoMatchingContext", func(t *testing.T) {
			// Same query "orange" — orgB has no such chunk. Expect empty
			// sources and the "no matching context" answer path.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgB.RepoID)
			status, chunks := doChatStream(t, server.URL, orgBToken, orgB.ID, body)
			require.Equal(t, http.StatusOK, status)
			done := findDoneChunk(t, chunks)
			require.Empty(t, done.Sources, "orgB has no orange chunks; sources must be empty (no leak from orgA)")
			require.Equal(t, "no matching context", chunks[0].Content)
		})

		t.Run("Scenario3_CrossTenantRepoAccess_ReturnsEmptyStream", func(t *testing.T) {
			// Reframed from the plan's "cross-tenant session_id" — chat
			// sessions aren't a table yet. The streaming analog of search
			// scenario 3: orgB asks for orgA's repo. RLS returns 0 rows.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			status, chunks := doChatStream(t, server.URL, orgBToken, orgB.ID, body)
			require.Equal(t, http.StatusOK, status)
			done := findDoneChunk(t, chunks)
			require.Empty(t, done.Sources, "orgB must not see any of orgA's chunks — cross-tenant leak")
		})

		t.Run("Scenario4_MissingTenantHeader_AbortsBeforeStream", func(t *testing.T) {
			// TenantMiddleware rejects with 400 before the handler runs, so
			// no SSE frames are emitted at all.
			body := fmt.Sprintf(`{"query":"orange","repository_id":%q}`, orgA.RepoID)
			req, err := http.NewRequest(http.MethodPost, server.URL+"/api/chat/stream", strings.NewReader(body))
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer "+orgAToken)
			req.Header.Set("Content-Type", "application/json")
			// Deliberately no X-Organization-ID header.
			resp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			defer resp.Body.Close()

			require.Equal(t, http.StatusBadRequest, resp.StatusCode,
				"middleware must reject before opening SSE stream")

			raw, _ := io.ReadAll(resp.Body)
			require.NotContains(t, string(raw), "data:",
				"no SSE frames must be emitted on a pre-stream rejection")
		})
	})
}

// writeChunk marshals c to JSON and writes it as one SSE data line.
func writeChunk(w http.ResponseWriter, flusher http.Flusher, c client.ChatChunk) {
	data, _ := json.Marshal(c)
	fmt.Fprintf(w, "data: %s\n\n", data)
	if flusher != nil {
		flusher.Flush()
	}
}

// doChatStream POSTs a chat/stream request through the real router and
// decodes every "data: {...}\n" line into a ChatChunk. Returns the HTTP
// status and the ordered slice of decoded chunks (nil chunk slice on a
// non-200 status).
func doChatStream(t *testing.T, baseURL, token, orgHeader, body string) (int, []client.ChatChunk) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/api/chat/stream", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-Organization-ID", orgHeader)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return resp.StatusCode, nil
	}

	var chunks []client.ChatChunk
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		var c client.ChatChunk
		require.NoError(t, json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &c))
		chunks = append(chunks, c)
	}
	require.NoError(t, scanner.Err())
	return resp.StatusCode, chunks
}

func findDoneChunk(t *testing.T, chunks []client.ChatChunk) client.ChatChunk {
	t.Helper()
	for _, c := range chunks {
		if c.Type == "done" {
			return c
		}
	}
	t.Fatalf("no done chunk in stream: %+v", chunks)
	return client.ChatChunk{}
}
