package handlers

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/render"
	"github.com/go-playground/validator/v10"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

// SearchHandler handles search requests
type SearchHandler struct {
	ragClient *client.RAGClient
	validate  *validator.Validate
}

// NewSearchHandler creates a new SearchHandler
func NewSearchHandler(ragClient *client.RAGClient, validate *validator.Validate) *SearchHandler {
	return &SearchHandler{
		ragClient: ragClient,
		validate:  validate,
	}
}

// SearchRequestBody represents the request body for search
type SearchRequestBody struct {
	Query        string `json:"query" validate:"required,min=1,max=1000"`
	RepositoryID string `json:"repository_id" validate:"required,uuid"`
	TopK         int    `json:"top_k" validate:"omitempty,min=1,max=50"`
}

// Bind implements render.Binder for SearchRequestBody
func (sr *SearchRequestBody) Bind(r *http.Request) error {
	// Set default TopK if not provided
	if sr.TopK == 0 {
		sr.TopK = 10
	}
	return nil
}

// SearchResponseBody wraps the client SearchResponse for API response
type SearchResponseBody struct {
	Results      []client.ChunkResult   `json:"results"`
	QueryID      string                 `json:"query_id,omitempty"`
	TotalResults int                    `json:"total_results"`
	Metadata     map[string]interface{} `json:"metadata,omitempty"`
}

// Render implements render.Renderer for SearchResponseBody
func (sr *SearchResponseBody) Render(w http.ResponseWriter, r *http.Request) error {
	return nil
}

// Search handles POST /api/search requests
func (h *SearchHandler) Search(w http.ResponseWriter, r *http.Request) {
	// 1. Decode request body
	var req SearchRequestBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}

	// Call Bind to set defaults
	if err := req.Bind(r); err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}

	// 2. Validate with go-playground/validator
	if err := h.validate.Struct(req); err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}

	// 3. Extract tenant from context (set by TenantMiddleware) and forward
	// it to the RAG service so the Python side scopes retrieval to this
	// org. Without this, cross-tenant chunks leak into results.
	ctx := r.Context()
	orgID, ok := ctx.Value(auth.OrgIDKey).(string)
	if !ok || orgID == "" {
		render.Render(w, r, ErrInternal(errors.New("tenant context missing from request; middleware chain misconfigured")))
		return
	}

	searchReq := client.SearchRequest{
		Query:          req.Query,
		RepositoryID:   req.RepositoryID,
		OrganizationID: orgID,
		TopK:           req.TopK,
	}

	result, err := h.ragClient.Search(ctx, searchReq)
	if err != nil {
		render.Render(w, r, ErrServiceUnavailable(err))
		return
	}

	// 4. Return results using render.Render
	response := &SearchResponseBody{
		Results:      result.Results,
		QueryID:      result.QueryID,
		TotalResults: result.TotalResults,
		Metadata:     result.Metadata,
	}

	render.Render(w, r, response)
}
