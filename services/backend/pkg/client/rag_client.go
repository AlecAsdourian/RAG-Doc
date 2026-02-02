package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// RAGClient is an HTTP client for the Python RAG service
type RAGClient struct {
	baseURL          string
	httpClient       *http.Client
	streamingClient  *http.Client // No timeout for SSE streaming
}

// NewRAGClient creates a new RAG client with the given base URL
func NewRAGClient(baseURL string) *RAGClient {
	return &RAGClient{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
		streamingClient: &http.Client{
			// No timeout - SSE streams can be long-lived
			// Context cancellation handles cleanup
		},
	}
}

// Search executes a semantic search against the RAG service
func (c *RAGClient) Search(ctx context.Context, req SearchRequest) (*SearchResponse, error) {
	// Marshal request body
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal search request: %w", err)
	}

	// Create POST request with context
	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/search", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	// Execute request
	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("search request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	// Check response status
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Include body snippet for debugging
		bodySnippet := string(respBody)
		if len(bodySnippet) > 200 {
			bodySnippet = bodySnippet[:200] + "..."
		}
		return nil, fmt.Errorf("RAG service returned status %d: %s", resp.StatusCode, bodySnippet)
	}

	// Decode response
	var result SearchResponse
	if err := json.Unmarshal(respBody, &result); err != nil {
		// Include raw body for debugging
		bodySnippet := string(respBody)
		if len(bodySnippet) > 200 {
			bodySnippet = bodySnippet[:200] + "..."
		}
		return nil, fmt.Errorf("failed to decode search response: %w (body: %s)", err, bodySnippet)
	}

	return &result, nil
}

// Chat executes an answer generation request against the RAG service
func (c *RAGClient) Chat(ctx context.Context, req ChatRequest) (*ChatResponse, error) {
	// Marshal request body
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal chat request: %w", err)
	}

	// Create POST request with context
	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/chat", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	// Execute request
	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("chat request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	// Check response status
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		bodySnippet := string(respBody)
		if len(bodySnippet) > 200 {
			bodySnippet = bodySnippet[:200] + "..."
		}
		return nil, fmt.Errorf("RAG service returned status %d: %s", resp.StatusCode, bodySnippet)
	}

	// Decode response
	var result ChatResponse
	if err := json.Unmarshal(respBody, &result); err != nil {
		bodySnippet := string(respBody)
		if len(bodySnippet) > 200 {
			bodySnippet = bodySnippet[:200] + "..."
		}
		return nil, fmt.Errorf("failed to decode chat response: %w (body: %s)", err, bodySnippet)
	}

	return &result, nil
}

// StreamChat executes a streaming chat request against the RAG service
// Returns a channel that emits ChatChunk events as they arrive via SSE
func (c *RAGClient) StreamChat(ctx context.Context, req ChatRequest) (<-chan ChatChunk, error) {
	// Marshal request body
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal chat request: %w", err)
	}

	// Create POST request with context
	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/chat/stream", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")

	// Execute request using streaming client (no timeout)
	resp, err := c.streamingClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("stream chat request failed: %w", err)
	}

	// Check response status before starting stream
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		bodySnippet := string(respBody)
		if len(bodySnippet) > 200 {
			bodySnippet = bodySnippet[:200] + "..."
		}
		return nil, fmt.Errorf("RAG service returned status %d: %s", resp.StatusCode, bodySnippet)
	}

	// Create channel for streaming chunks
	ch := make(chan ChatChunk)

	// Start goroutine to read SSE stream
	go func() {
		defer close(ch)
		defer resp.Body.Close()

		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			// Check for context cancellation (client disconnect)
			select {
			case <-ctx.Done():
				return
			default:
			}

			line := scanner.Text()

			// SSE format: "data: {...json...}"
			if !strings.HasPrefix(line, "data: ") {
				continue
			}

			// Parse JSON payload
			jsonData := strings.TrimPrefix(line, "data: ")
			var chunk ChatChunk
			if err := json.Unmarshal([]byte(jsonData), &chunk); err != nil {
				// Send error chunk and continue
				select {
				case ch <- ChatChunk{Type: "error", Error: fmt.Sprintf("failed to parse SSE data: %v", err)}:
				case <-ctx.Done():
					return
				}
				continue
			}

			// Send chunk to channel
			select {
			case ch <- chunk:
			case <-ctx.Done():
				return
			}

			// Stop reading after done or error
			if chunk.Type == "done" || chunk.Type == "error" {
				return
			}
		}

		// Check for scanner errors
		if err := scanner.Err(); err != nil {
			select {
			case ch <- ChatChunk{Type: "error", Error: fmt.Sprintf("stream read error: %v", err)}:
			case <-ctx.Done():
			}
		}
	}()

	return ch, nil
}

// HealthCheck checks if the RAG service is healthy
func (c *RAGClient) HealthCheck(ctx context.Context) error {
	httpReq, err := http.NewRequestWithContext(ctx, "GET", c.baseURL+"/health", nil)
	if err != nil {
		return fmt.Errorf("failed to create health check request: %w", err)
	}

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("health check request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("RAG service unhealthy: status %d", resp.StatusCode)
	}

	return nil
}
