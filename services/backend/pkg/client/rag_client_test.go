package client_test

// TestRAGClient_PropagatesTenant is the audit-first check the 17-02 plan
// asked for: confirm that the Go RAG client forwards organization_id to
// the Python service on both Search and StreamChat. The Python side does
// the actual tenant-scoped retrieval; if Go ever drops the tenant on the
// wire, Python has no way to know whose data to return and every response
// leaks. This test pins the contract.
//
// The stub server captures the exact request body Python would see and
// asserts on it directly, so a rename of the JSON tag (e.g. from
// "organization_id" to "org_id") also fails the test.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

func TestRAGClient_PropagatesTenant(t *testing.T) {
	t.Run("Search request body carries organization_id", func(t *testing.T) {
		captured := make(chan map[string]any, 1)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			require.NoError(t, err)
			var payload map[string]any
			require.NoError(t, json.Unmarshal(body, &payload))
			captured <- payload

			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(client.SearchResponse{TotalResults: 0})
		}))
		t.Cleanup(srv.Close)

		c := client.NewRAGClient(srv.URL)
		_, err := c.Search(context.Background(), client.SearchRequest{
			Query:          "orange",
			RepositoryID:   "11111111-1111-1111-1111-111111111111",
			OrganizationID: "22222222-2222-2222-2222-222222222222",
			TopK:           5,
		})
		require.NoError(t, err)

		payload := <-captured
		requireStringField(t, payload, "organization_id", "22222222-2222-2222-2222-222222222222")
		requireStringField(t, payload, "repository_id", "11111111-1111-1111-1111-111111111111")
		requireStringField(t, payload, "query", "orange")
	})

	t.Run("StreamChat request body carries organization_id", func(t *testing.T) {
		captured := make(chan map[string]any, 1)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			require.NoError(t, err)
			var payload map[string]any
			require.NoError(t, json.Unmarshal(body, &payload))
			captured <- payload

			w.Header().Set("Content-Type", "text/event-stream")
			flusher, _ := w.(http.Flusher)
			done := client.ChatChunk{Type: "done"}
			data, _ := json.Marshal(done)
			fmt.Fprintf(w, "data: %s\n\n", data)
			if flusher != nil {
				flusher.Flush()
			}
		}))
		t.Cleanup(srv.Close)

		c := client.NewRAGClient(srv.URL)
		ch, err := c.StreamChat(context.Background(), client.ChatRequest{
			Query:          "purple",
			RepositoryID:   "33333333-3333-3333-3333-333333333333",
			OrganizationID: "44444444-4444-4444-4444-444444444444",
			TopK:           3,
		})
		require.NoError(t, err)
		// Drain so the stream goroutine exits.
		for range ch {
		}

		payload := <-captured
		requireStringField(t, payload, "organization_id", "44444444-4444-4444-4444-444444444444")
		requireStringField(t, payload, "repository_id", "33333333-3333-3333-3333-333333333333")
		requireStringField(t, payload, "query", "purple")
	})
}

func requireStringField(t *testing.T, payload map[string]any, key, want string) {
	t.Helper()
	got, ok := payload[key].(string)
	require.True(t, ok, "field %q missing or not a string in payload: %+v", key, payload)
	require.Equal(t, want, got, "field %q mismatch", key)
}
