package vectordb

import (
	"context"
	"testing"
)

// TestNewClient tests client creation with valid and invalid configs
func TestNewClient(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Config
		wantErr bool
	}{
		{
			name:    "valid config",
			cfg:     Config{URL: "http://localhost:6333"},
			wantErr: false,
		},
		{
			name:    "empty URL",
			cfg:     Config{URL: ""},
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client, err := NewClient(tt.cfg)
			if (err != nil) != tt.wantErr {
				t.Errorf("NewClient() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !tt.wantErr && client == nil {
				t.Error("NewClient() returned nil client")
			}
			if client != nil {
				_ = client.Close()
			}
		})
	}
}

// TestVectorMetadata tests metadata struct serialization
func TestVectorMetadata(t *testing.T) {
	metadata := VectorMetadata{
		ChunkID:      "chunk-123",
		RepositoryID: "repo-456",
		FilePath:     "src/main.go",
		Language:     "go",
	}

	if metadata.ChunkID != "chunk-123" {
		t.Errorf("ChunkID = %v, want chunk-123", metadata.ChunkID)
	}
	if metadata.RepositoryID != "repo-456" {
		t.Errorf("RepositoryID = %v, want repo-456", metadata.RepositoryID)
	}
	if metadata.FilePath != "src/main.go" {
		t.Errorf("FilePath = %v, want src/main.go", metadata.FilePath)
	}
	if metadata.Language != "go" {
		t.Errorf("Language = %v, want go", metadata.Language)
	}
}

// TestUpsertVectorsValidation tests input validation
func TestUpsertVectorsValidation(t *testing.T) {
	client := &Client{} // Mock client for validation testing

	tests := []struct {
		name     string
		vectors  [][]float32
		metadata []VectorMetadata
		wantErr  bool
	}{
		{
			name:     "length mismatch",
			vectors:  [][]float32{{0.1, 0.2}},
			metadata: []VectorMetadata{{}, {}},
			wantErr:  true,
		},
		{
			name:     "empty input",
			vectors:  [][]float32{},
			metadata: []VectorMetadata{},
			wantErr:  false, // Empty is valid (no-op)
		},
		{
			name: "matching lengths",
			vectors: [][]float32{
				{0.1, 0.2},
				{0.3, 0.4},
			},
			metadata: []VectorMetadata{
				{ChunkID: "1"},
				{ChunkID: "2"},
			},
			wantErr: false, // Would fail on actual upsert, but validation passes
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := client.UpsertVectors(context.Background(), "test", tt.vectors, tt.metadata)
			if (err != nil) != tt.wantErr {
				t.Errorf("UpsertVectors() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// TestSearchSimilarValidation tests query vector validation
func TestSearchSimilarValidation(t *testing.T) {
	client := &Client{} // Mock client for validation testing

	tests := []struct {
		name        string
		queryVector []float32
		wantErr     bool
	}{
		{
			name:        "invalid dimension",
			queryVector: make([]float32, 100),
			wantErr:     true,
		},
		{
			name:        "valid dimension",
			queryVector: make([]float32, 1536),
			wantErr:     false, // Would fail on actual search, but validation passes
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := client.SearchSimilar(context.Background(), "test", tt.queryVector, 10, "")
			if (err != nil) != tt.wantErr {
				t.Errorf("SearchSimilar() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// Note: Integration tests requiring a running Qdrant instance would go in a separate
// file (e.g., client_integration_test.go) with build tags to avoid running during
// standard unit tests.
