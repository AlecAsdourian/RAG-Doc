package handlers_test

// A query containing U+0000, or any other C0 control character except tab,
// newline and carriage return, is a 400 on /api/search and /api/chat/stream,
// before anything is sent to the RAG service.
//
// Without the check, a NUL query failed keyword search in the RAG service
// (Postgres text cannot hold U+0000), and the backend reported it as 503
// "Service unavailable": an outage that no retry fixes (PR #34 review).
// Handlers are called directly with the organization in the context, as in
// rag_errors_test.go.

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/go-playground/validator/v10"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

var disallowedQueries = map[string]string{
	"NUL":                 "marmalade\x00",
	"NUL alone":           "\x00",
	"SOH":                 "marmalade\x01recipe",
	"vertical tab":        "marmalade\vrecipe",
	"form feed":           "marmalade\frecipe",
	"escape":              "\x1b[31mmarmalade",
	"unit separator":      "marmalade\x1f",
	"NUL after a newline": "line one\nline two\x00",
}

var allowedQueries = map[string]string{
	"tab":             "func\tmarmalade()",
	"newline":         "func marmalade() {\n\treturn nil\n}",
	"carriage return": "line one\r\nline two",
}

// queryBody JSON-encodes a request body, so control characters travel as
// \u00XX escapes exactly as a real client would send them.
func queryBody(t *testing.T, query string) string {
	t.Helper()
	body, err := json.Marshal(map[string]string{"query": query, "repository_id": ragTestRepoID})
	require.NoError(t, err)
	return string(body)
}

// countingRAGService records every request it receives and the query it
// carried, and answers with an empty search result or an empty done frame.
func countingRAGService(t *testing.T, calls *atomic.Int32, lastQuery *atomic.Value) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var req struct {
			Query string `json:"query"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		lastQuery.Store(req.Query)
		if r.URL.Path == "/chat/stream" {
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, `data: {"type":"done"}`+"\n\n")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"results":[],"total_results":0}`)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestSearchHandler_RejectsControlCharactersInQuery(t *testing.T) {
	for name, query := range disallowedQueries {
		t.Run(name, func(t *testing.T) {
			var calls atomic.Int32
			var lastQuery atomic.Value
			rag := countingRAGService(t, &calls, &lastQuery)

			h := handlers.NewSearchHandler(client.NewRAGClient(rag.URL), validator.New())
			rec := httptest.NewRecorder()
			h.Search(rec, ragRequest("/api/search", queryBody(t, query)))

			require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
			require.JSONEq(t, `{"status":"error","error":"query contains invalid characters"}`, rec.Body.String())
			require.Zero(t, calls.Load(), "the query must not reach the RAG service")
		})
	}
}

func TestStreamChatHandler_RejectsControlCharactersInQuery(t *testing.T) {
	for name, query := range disallowedQueries {
		t.Run(name, func(t *testing.T) {
			var calls atomic.Int32
			var lastQuery atomic.Value
			rag := countingRAGService(t, &calls, &lastQuery)

			h := handlers.NewChatHandler(client.NewRAGClient(rag.URL), validator.New())
			rec := httptest.NewRecorder()
			h.StreamChat(rec, ragRequest("/api/chat/stream", queryBody(t, query)))

			// A real 400, not a 200 event stream carrying an error frame.
			require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())
			require.NotEqual(t, "text/event-stream", rec.Header().Get("Content-Type"))
			require.JSONEq(t, `{"status":"error","error":"query contains invalid characters"}`, rec.Body.String())
			require.Zero(t, calls.Load(), "the query must not reach the RAG service")
		})
	}
}

func TestQueryHandlers_AcceptTabNewlineAndCarriageReturn(t *testing.T) {
	for name, query := range allowedQueries {
		t.Run("search/"+name, func(t *testing.T) {
			var calls atomic.Int32
			var lastQuery atomic.Value
			rag := countingRAGService(t, &calls, &lastQuery)

			h := handlers.NewSearchHandler(client.NewRAGClient(rag.URL), validator.New())
			rec := httptest.NewRecorder()
			h.Search(rec, ragRequest("/api/search", queryBody(t, query)))

			require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
			require.EqualValues(t, 1, calls.Load())
			require.Equal(t, query, lastQuery.Load(), "the query must be forwarded unchanged")
		})

		t.Run("chat/"+name, func(t *testing.T) {
			var calls atomic.Int32
			var lastQuery atomic.Value
			rag := countingRAGService(t, &calls, &lastQuery)

			h := handlers.NewChatHandler(client.NewRAGClient(rag.URL), validator.New())
			rec := httptest.NewRecorder()
			h.StreamChat(rec, ragRequest("/api/chat/stream", queryBody(t, query)))

			require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
			require.EqualValues(t, 1, calls.Load())
			require.Equal(t, query, lastQuery.Load(), "the query must be forwarded unchanged")
		})
	}
}
