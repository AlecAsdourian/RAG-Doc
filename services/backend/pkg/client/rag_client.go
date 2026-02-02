package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// RAGClient is an HTTP client for the Python RAG service
type RAGClient struct {
	baseURL    string
	httpClient *http.Client
}

// NewRAGClient creates a new RAG client with the given base URL
func NewRAGClient(baseURL string) *RAGClient {
	return &RAGClient{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
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
