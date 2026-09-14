package handlers_test

// Since ISS-030 the Python RAG service fails a search loudly. A failed
// retriever is a 503 on /search and an SSE error frame on /chat/stream,
// with a fixed message that never carries the underlying exception text.
//
// These tests pin the Go side of that contract, which needed no change:
// Search maps any RAG client error to its own generic 503, and StreamChat
// relays the Python error frame as it arrived.
//
// The handlers are called directly with the organization already in the
// context, so no database or JWT is involved. Tenant scoping on these
// routes is covered by search_isolation_test.go and chat_isolation_test.go.

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/go-playground/validator/v10"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

const (
	ragTestOrgID  = "00000000-0000-4000-8000-0000000000a1"
	ragTestRepoID = "00000000-0000-4000-8000-0000000000b1"
	// What the Python service sends when vector search fails (api/routes.py).
	ragUnavailable = "Search is temporarily unavailable (vector search failed); please retry"
)

func ragRequest(path, body string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req.WithContext(auth.ContextWithOrgID(req.Context(), ragTestOrgID))
}

func TestSearchHandler_RAGServiceFailureIsServiceUnavailable(t *testing.T) {
	for _, pythonStatus := range []int{http.StatusServiceUnavailable, http.StatusInternalServerError} {
		t.Run(http.StatusText(pythonStatus), func(t *testing.T) {
			fakePython := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(pythonStatus)
				_, _ = io.WriteString(w, `{"detail":"`+ragUnavailable+`"}`)
			}))
			t.Cleanup(fakePython.Close)

			h := handlers.NewSearchHandler(client.NewRAGClient(fakePython.URL), validator.New())
			rec := httptest.NewRecorder()
			h.Search(rec, ragRequest("/api/search", `{"query":"q","repository_id":"`+ragTestRepoID+`"}`))

			require.Equal(t, http.StatusServiceUnavailable, rec.Code, "body=%s", rec.Body.String())
			require.JSONEq(t, `{"status":"error","error":"Service unavailable"}`, rec.Body.String())
		})
	}
}

func TestStreamChatHandler_RelaysRAGErrorFrame(t *testing.T) {
	fakePython := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, `data: {"type":"error","error":"`+ragUnavailable+`"}`+"\n\n")
	}))
	t.Cleanup(fakePython.Close)

	h := handlers.NewChatHandler(client.NewRAGClient(fakePython.URL), validator.New())
	rec := httptest.NewRecorder()
	h.StreamChat(rec, ragRequest("/api/chat/stream", `{"query":"q","repository_id":"`+ragTestRepoID+`"}`))

	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, `data: {"type":"error","error":"`+ragUnavailable+`"}`+"\n\n", rec.Body.String())
}
