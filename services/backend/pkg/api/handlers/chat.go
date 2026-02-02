package handlers

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/go-playground/validator/v10"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

// ChatHandler handles chat/streaming requests
type ChatHandler struct {
	ragClient *client.RAGClient
	validate  *validator.Validate
}

// NewChatHandler creates a new ChatHandler
func NewChatHandler(ragClient *client.RAGClient, validate *validator.Validate) *ChatHandler {
	return &ChatHandler{
		ragClient: ragClient,
		validate:  validate,
	}
}

// ChatRequestBody represents the request body for chat
type ChatRequestBody struct {
	Query        string `json:"query" validate:"required,min=1,max=2000"`
	RepositoryID string `json:"repository_id" validate:"required,uuid"`
	TopK         int    `json:"top_k" validate:"omitempty,min=1,max=20"`
}

// Bind implements render.Binder for ChatRequestBody
func (cr *ChatRequestBody) Bind(r *http.Request) error {
	// Set default TopK if not provided
	if cr.TopK == 0 {
		cr.TopK = 5
	}
	return nil
}

// StreamChat handles POST /api/chat/stream requests using SSE
func (h *ChatHandler) StreamChat(w http.ResponseWriter, r *http.Request) {
	// 1. Decode request body
	var req ChatRequestBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeSSEError(w, fmt.Sprintf("Invalid request: %v", err))
		return
	}

	// Call Bind to set defaults
	if err := req.Bind(r); err != nil {
		writeSSEError(w, fmt.Sprintf("Invalid request: %v", err))
		return
	}

	// 2. Validate with go-playground/validator
	if err := h.validate.Struct(req); err != nil {
		writeSSEError(w, fmt.Sprintf("Validation error: %v", err))
		return
	}

	// 3. Set SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no") // Disable nginx buffering

	// Get flusher for immediate writes
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeSSEError(w, "Streaming not supported")
		return
	}

	// 4. Call RAG streaming client with request context
	ctx := r.Context()
	chatReq := client.ChatRequest{
		Query:        req.Query,
		RepositoryID: req.RepositoryID,
		TopK:         req.TopK,
	}

	chunkChan, err := h.ragClient.StreamChat(ctx, chatReq)
	if err != nil {
		writeSSEError(w, fmt.Sprintf("Failed to start stream: %v", err))
		return
	}

	// 5. Forward chunks from RAG client to SSE response
	for chunk := range chunkChan {
		// Check if client disconnected
		select {
		case <-ctx.Done():
			return
		default:
		}

		// Marshal chunk to JSON
		data, err := json.Marshal(chunk)
		if err != nil {
			writeSSEData(w, flusher, client.ChatChunk{
				Type:  "error",
				Error: fmt.Sprintf("Failed to marshal chunk: %v", err),
			})
			return
		}

		// Write SSE data line
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()

		// Stop on done or error
		if chunk.Type == "done" || chunk.Type == "error" {
			return
		}
	}
}

// writeSSEError writes an error as an SSE event and sets proper headers
func writeSSEError(w http.ResponseWriter, errMsg string) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")

	chunk := client.ChatChunk{
		Type:  "error",
		Error: errMsg,
	}
	data, _ := json.Marshal(chunk)
	fmt.Fprintf(w, "data: %s\n\n", data)

	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}

// writeSSEData writes a ChatChunk as an SSE event
func writeSSEData(w http.ResponseWriter, flusher http.Flusher, chunk client.ChatChunk) {
	data, _ := json.Marshal(chunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	flusher.Flush()
}
