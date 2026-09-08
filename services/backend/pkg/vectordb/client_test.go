package vectordb

import (
	"context"
	"errors"
	"testing"
)

// TestParseEndpoint pins the URL-to-(host, port) mapping.
//
// This is the whole reason the package could "work" and not work at the
// same time. qdrant.Config wants a bare Host and a separate Port, but
// every piece of configuration in this repo supplies a full URL, and the
// old code passed it through untouched — producing a dial target of
// "http://localhost:6333:6334" that failed only at the first RPC, long
// after NewClient had returned a non-nil client and a nil error.
func TestParseEndpoint(t *testing.T) {
	tests := []struct {
		name     string
		in       string
		wantHost string
		wantPort int
		wantTLS  bool
		wantErr  bool
	}{
		// The form docker-compose and .env.example actually use. 6333 is
		// Qdrant's REST port and the Go SDK is gRPC-only, so honouring it
		// literally would guarantee a connection that never works.
		{"compose-style URL", "http://qdrant:6333", "qdrant", 6334, false, false},
		{"localhost REST URL", "http://localhost:6333", "localhost", 6334, false, false},
		{"explicit gRPC port", "http://localhost:6334", "localhost", 6334, false, false},
		{"non-default port is honoured", "http://qdrant:7000", "qdrant", 7000, false, false},
		{"https implies TLS", "https://qdrant.example.com", "qdrant.example.com", 6334, true, false},
		{"bare host", "qdrant", "qdrant", 6334, false, false},
		{"bare host:port", "localhost:6334", "localhost", 6334, false, false},
		// The bare form of the REST port. This regressed once: the URL
		// branch applied the 6333→6334 redirect and the bare-host branch
		// did not, so "http://qdrant:6333" worked while "qdrant:6333" —
		// the same value with the scheme dropped, and the more likely
		// thing to type — produced a client that could never connect.
		{"bare host with REST port", "qdrant:6333", "qdrant", 6334, false, false},
		{"bare localhost with REST port", "localhost:6333", "localhost", 6334, false, false},
		{"whitespace is trimmed", "  qdrant  ", "qdrant", 6334, false, false},
		{"empty is an error", "", "", 0, false, true},
		{"scheme with no host is an error", "http://", "", 0, false, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			host, port, tls, err := parseEndpoint(tt.in)
			if (err != nil) != tt.wantErr {
				t.Fatalf("parseEndpoint(%q) error = %v, wantErr %v", tt.in, err, tt.wantErr)
			}
			if tt.wantErr {
				return
			}
			if host != tt.wantHost || port != tt.wantPort || tls != tt.wantTLS {
				t.Errorf("parseEndpoint(%q) = (%q, %d, %v), want (%q, %d, %v)",
					tt.in, host, port, tls, tt.wantHost, tt.wantPort, tt.wantTLS)
			}
		})
	}
}

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
//
// EVERY exported method is covered. The first version of this test
// checked only UpsertVectors and SearchSimilar, and the guard had been
// added to only those two — so CreateCollection, DeleteByChunkID and
// Close still panicked, which is the exact failure the guard exists to
// prevent, surviving in the methods nobody happened to test.
func TestClientlessOperationsReturnErrNotConnected(t *testing.T) {
	ctx := context.Background()
	var client *Client // nil receiver, the worst case

	checks := map[string]func() error{
		"CreateCollection": func() error {
			return client.CreateCollection(ctx, "test")
		},
		"UpsertVectors": func() error {
			return client.UpsertVectors(ctx, "test",
				[][]float32{{0.1}}, []VectorMetadata{{ChunkID: "1"}})
		},
		"SearchSimilar": func() error {
			_, err := client.SearchSimilar(ctx, "test",
				make([]float32, EmbeddingDimension), 10, "")
			return err
		},
		"DeleteByChunkID": func() error {
			return client.DeleteByChunkID(ctx, "test", "chunk-1")
		},
		"Close": func() error {
			return client.Close()
		},
	}

	for name, call := range checks {
		t.Run(name, func(t *testing.T) {
			if err := call(); !errors.Is(err, ErrNotConnected) {
				t.Errorf("%s on a nil client = %v, want ErrNotConnected", name, err)
			}
		})
	}
}

// TestPublicMethodsApplyValidation is the link the earlier restructuring
// broke.
//
// Moving the validation rules into validateUpsertInput/validateQueryVector
// made them directly testable, but left nothing asserting that
// UpsertVectors and SearchSimilar still CALL them: deleting both calls
// kept the whole package green. These cases close that gap by going
// through the public API on a disconnected client and requiring the
// VALIDATION error rather than ErrNotConnected — which only holds if
// validation runs first.
func TestPublicMethodsApplyValidation(t *testing.T) {
	ctx := context.Background()
	var client *Client

	t.Run("UpsertVectors rejects a length mismatch before connecting", func(t *testing.T) {
		err := client.UpsertVectors(ctx, "test",
			[][]float32{{0.1}}, []VectorMetadata{{}, {}})
		if err == nil {
			t.Fatal("expected an error for mismatched lengths")
		}
		if errors.Is(err, ErrNotConnected) {
			t.Fatalf("got ErrNotConnected, want the validation error — "+
				"UpsertVectors is not calling validateUpsertInput: %v", err)
		}
	})

	t.Run("SearchSimilar rejects a bad dimension before connecting", func(t *testing.T) {
		_, err := client.SearchSimilar(ctx, "test", make([]float32, 100), 10, "")
		if err == nil {
			t.Fatal("expected an error for the wrong vector dimension")
		}
		if errors.Is(err, ErrNotConnected) {
			t.Fatalf("got ErrNotConnected, want the validation error — "+
				"SearchSimilar is not calling validateQueryVector: %v", err)
		}
	})

	// The empty-input path must NOT report success on a dead client. With
	// the connection check placed after the empty-input early return,
	// this returned nil — a disconnected client claiming a successful
	// write.
	t.Run("empty upsert on a dead client is not a success", func(t *testing.T) {
		if err := client.UpsertVectors(ctx, "test", nil, nil); !errors.Is(err, ErrNotConnected) {
			t.Errorf("empty UpsertVectors on a nil client = %v, want ErrNotConnected", err)
		}
	})
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
