package vectordb

import (
	"context"
	"errors"
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

// TestUpsertVectorsValidation tests input validation.
//
// It calls validateUpsertInput rather than UpsertVectors on a zero-value
// Client. The old form could not express its own third case: "matching
// lengths" is meant to prove validation ACCEPTS the input, but on a
// clientless Client accepted input proceeds to the wire call, which
// panicked on the nil connection. The package had never compiled, so the
// panic sat undiscovered from Phase 3 until the dependency was fixed.
func TestUpsertVectorsValidation(t *testing.T) {
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
			wantErr: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateUpsertInput(tt.vectors, tt.metadata)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateUpsertInput() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// TestClientlessOperationsReturnErrNotConnected pins the guard that
// replaced the panic. A zero-value Client is what a caller holds if they
// ignored NewClient's error, and it must fail with something legible
// rather than dereferencing nil several frames inside the SDK.
func TestClientlessOperationsReturnErrNotConnected(t *testing.T) {
	var client *Client // nil receiver, the worst case

	err := client.UpsertVectors(context.Background(), "test",
		[][]float32{{0.1}}, []VectorMetadata{{ChunkID: "1"}})
	if !errors.Is(err, ErrNotConnected) {
		t.Errorf("UpsertVectors on a nil client = %v, want ErrNotConnected", err)
	}

	_, err = client.SearchSimilar(context.Background(), "test",
		make([]float32, EmbeddingDimension), 10, "")
	if !errors.Is(err, ErrNotConnected) {
		t.Errorf("SearchSimilar on a nil client = %v, want ErrNotConnected", err)
	}
}

// TestSearchSimilarValidation tests query vector validation. Same
// restructuring as TestUpsertVectorsValidation, and for the same reason —
// its "valid dimension" case had the identical latent panic; it simply
// never ran, because the Upsert test panicked first and took the binary
// down with it.
func TestSearchSimilarValidation(t *testing.T) {
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
			queryVector: make([]float32, EmbeddingDimension),
			wantErr:     false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateQueryVector(tt.queryVector)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateQueryVector() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// Note: Integration tests requiring a running Qdrant instance would go in a separate
// file (e.g., client_integration_test.go) with build tags to avoid running during
// standard unit tests.
