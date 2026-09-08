package vectordb

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"

	"github.com/google/uuid"
	qdrant "github.com/qdrant/go-client/qdrant"
)

// Config holds vector database client configuration.
type Config struct {
	// URL is the Qdrant endpoint. Both a bare host ("qdrant",
	// "localhost:6334") and a full URL ("http://qdrant:6333") are
	// accepted; see NewClient for how it is interpreted.
	URL string
}

// defaultGRPCPort is Qdrant's gRPC port. The Go client speaks gRPC, not
// the REST API on 6333 — a distinction that has bitten this package
// before, because every piece of configuration in the repo names 6333.
const defaultGRPCPort = 6334

// parseEndpoint turns a Config.URL into the host and port the Qdrant SDK
// wants.
//
// This exists because qdrant.Config takes a bare Host and a separate Port,
// while everything in this repo — QDRANT_URL in docker-compose, the
// example in example_usage.go, the old doc comment — supplies a full URL.
// Handing "http://localhost:6333" straight through produced a dial target
// of "http://localhost:6333:6334", and the failure surfaced only at the
// first RPC ("too many colons in address"), long after NewClient had
// returned a non-nil client and no error.
//
// Note the port default: a URL naming Qdrant's REST port (6333) is
// redirected to the gRPC port (6334), because the Go client cannot speak
// to 6333 at all. Passing 6333 explicitly and getting a connection to
// 6334 is surprising, so it is logged by the caller rather than done
// silently... except there is no logger here, so it is documented instead
// and pinned by TestParseEndpoint.
func parseEndpoint(raw string) (host string, port int, useTLS bool, err error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", 0, false, fmt.Errorf("vector database URL is required")
	}

	// A bare "host" or "host:port" has no scheme; url.Parse would read
	// "localhost:6334" as scheme "localhost", opaque "6334".
	if !strings.Contains(raw, "//") {
		if h, p, splitErr := net.SplitHostPort(raw); splitErr == nil {
			n, convErr := strconv.Atoi(p)
			if convErr != nil {
				return "", 0, false, fmt.Errorf("invalid port %q in %q", p, raw)
			}
			return h, n, false, nil
		}
		return raw, defaultGRPCPort, false, nil
	}

	u, parseErr := url.Parse(raw)
	if parseErr != nil {
		return "", 0, false, fmt.Errorf("parse vector database URL %q: %w", raw, parseErr)
	}
	if u.Hostname() == "" {
		return "", 0, false, fmt.Errorf("vector database URL %q has no host", raw)
	}

	useTLS = u.Scheme == "https"
	port = defaultGRPCPort
	if p := u.Port(); p != "" {
		n, convErr := strconv.Atoi(p)
		if convErr != nil {
			return "", 0, false, fmt.Errorf("invalid port %q in %q", p, raw)
		}
		// 6333 is the REST port. The Go SDK is gRPC-only, so honouring it
		// would guarantee a connection that never works.
		if n != 6333 {
			port = n
		}
	}
	return u.Hostname(), port, useTLS, nil
}

// Client wraps the Qdrant client with application-specific operations
type Client struct {
	qdrant *qdrant.Client
}

// VectorMetadata represents metadata stored with each embedding
type VectorMetadata struct {
	ChunkID      string `json:"chunk_id"`
	RepositoryID string `json:"repository_id"`
	FilePath     string `json:"file_path"`
	Language     string `json:"language,omitempty"`
}

// SearchResult represents a single search result
type SearchResult struct {
	ChunkID      string
	RepositoryID string
	FilePath     string
	Language     string
	Score        float32
}

// NewClient creates a new vector database client.
func NewClient(cfg Config) (*Client, error) {
	host, port, useTLS, err := parseEndpoint(cfg.URL)
	if err != nil {
		return nil, err
	}

	client, err := qdrant.NewClient(&qdrant.Config{
		Host:   host,
		Port:   port,
		UseTLS: useTLS,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create Qdrant client: %w", err)
	}

	return &Client{
		qdrant: client,
	}, nil
}

// CreateCollection initializes the embeddings collection with proper schema
// Dimension: 1536 (OpenAI ada-002 embedding size)
// Distance metric: Cosine similarity (standard for semantic search)
func (c *Client) CreateCollection(ctx context.Context, collectionName string) error {
	if err := c.connected(); err != nil {
		return err
	}

	// Check if collection already exists
	exists, err := c.qdrant.CollectionExists(ctx, collectionName)
	if err != nil {
		return fmt.Errorf("failed to check collection existence: %w", err)
	}

	if exists {
		return nil // Collection already exists, skip creation
	}

	// Create collection with cosine distance and 1536 dimensions
	err = c.qdrant.CreateCollection(ctx, &qdrant.CreateCollection{
		CollectionName: collectionName,
		VectorsConfig: qdrant.NewVectorsConfig(&qdrant.VectorParams{
			Size:     1536, // OpenAI ada-002 embedding dimension
			Distance: qdrant.Distance_Cosine,
		}),
	})
	if err != nil {
		return fmt.Errorf("failed to create collection: %w", err)
	}

	// Create payload index for efficient metadata filtering.
	//
	// CreateFieldIndex returns (*UpdateResult, error) and the status in
	// that result is NOT redundant with the error — see checkUpdateStatus.
	res, err := c.qdrant.CreateFieldIndex(ctx, &qdrant.CreateFieldIndexCollection{
		CollectionName: collectionName,
		FieldName:      "repository_id",
		FieldType:      qdrant.FieldType_FieldTypeKeyword.Enum(),
	})
	if err != nil {
		return fmt.Errorf("failed to create repository_id index: %w", err)
	}
	if err := checkUpdateStatus("create repository_id index", res); err != nil {
		return err
	}

	res, err = c.qdrant.CreateFieldIndex(ctx, &qdrant.CreateFieldIndexCollection{
		CollectionName: collectionName,
		FieldName:      "language",
		FieldType:      qdrant.FieldType_FieldTypeKeyword.Enum(),
	})
	if err != nil {
		return fmt.Errorf("failed to create language index: %w", err)
	}
	return checkUpdateStatus("create language index", res)
}

// checkUpdateStatus turns a non-success UpdateStatus into an error.
//
// Qdrant's write RPCs return `err == nil` alongside a status that may say
// the write did not happen: ClockRejected means the server refused it, and
// WaitTimeout means it gave up waiting. Ignoring the status therefore
// reports success for writes that were rejected or abandoned — silent data
// loss with nothing in the logs.
//
// Acknowledged is accepted deliberately. It means "queued, not yet
// applied", which is what Qdrant returns whenever the request does not set
// Wait; callers that need durability set Wait and get Completed.
func checkUpdateStatus(op string, res *qdrant.UpdateResult) error {
	if res == nil {
		// No result and no error is not a shape the SDK produces today.
		// Treat it as success rather than inventing a failure.
		return nil
	}
	switch s := res.GetStatus(); s {
	case qdrant.UpdateStatus_Completed, qdrant.UpdateStatus_Acknowledged:
		return nil
	default:
		return fmt.Errorf("qdrant refused %s: status=%s operation_id=%d",
			op, s, res.GetOperationId())
	}
}

// ErrNotConnected is returned by any operation invoked on a Client that
// has no underlying Qdrant connection — a zero-value Client, or one built
// by a constructor whose error was ignored.
//
// This exists because the alternative is a nil-pointer panic from inside
// the Qdrant SDK, several frames below the mistake. That is what the
// package's own unit tests did the first time they were ever able to run:
// `&Client{}` reached the wire call and took the whole test binary down.
var ErrNotConnected = errors.New("vectordb: client is not connected")

// connected reports whether the client can actually talk to Qdrant.
//
// Every exported method calls this. The first version of the guard
// covered only UpsertVectors and SearchSimilar, which left
// CreateCollection, DeleteByChunkID and Close still panicking on a nil
// receiver — the same failure the guard was added to prevent, in the
// three methods nobody happened to be testing.
func (c *Client) connected() error {
	if c == nil || c.qdrant == nil {
		return ErrNotConnected
	}
	return nil
}

// validateUpsertInput checks the caller-supplied arguments, independent of
// any connection.
//
// Split out so the validation rules are testable without a live Qdrant.
// The tests previously exercised them by calling UpsertVectors on a
// zero-value Client and relying on it returning before touching the
// network — which held for the rejecting cases and panicked for the
// accepting one, so the only case that proved validation *passes* was the
// one that could not run.
func validateUpsertInput(vectors [][]float32, metadata []VectorMetadata) error {
	if len(vectors) != len(metadata) {
		return fmt.Errorf("vectors and metadata length mismatch: %d vectors, %d metadata",
			len(vectors), len(metadata))
	}
	return nil
}

// UpsertVectors inserts or updates embeddings with metadata
func (c *Client) UpsertVectors(ctx context.Context, collectionName string, vectors [][]float32, metadata []VectorMetadata) error {
	if err := validateUpsertInput(vectors, metadata); err != nil {
		return err
	}

	// Connection check BEFORE the empty-input early return. With the two
	// swapped, `nilClient.UpsertVectors(ctx, "x", nil, nil)` returned nil —
	// a disconnected client reporting a successful write.
	if err := c.connected(); err != nil {
		return err
	}

	if len(vectors) == 0 {
		return nil // Nothing to upsert
	}

	// Build points for batch upsert
	points := make([]*qdrant.PointStruct, len(vectors))
	for i := range vectors {
		// Generate unique point ID
		pointID := uuid.New().String()

		// Convert metadata to Qdrant payload
		payload := map[string]*qdrant.Value{
			"chunk_id":      qdrant.NewValueString(metadata[i].ChunkID),
			"repository_id": qdrant.NewValueString(metadata[i].RepositoryID),
			"file_path":     qdrant.NewValueString(metadata[i].FilePath),
		}
		if metadata[i].Language != "" {
			payload["language"] = qdrant.NewValueString(metadata[i].Language)
		}

		points[i] = &qdrant.PointStruct{
			// NewIDUUID, not the removed NewIDString. pointID is a
			// uuid.New().String(); Qdrant point ids are either a uint64 or
			// a UUID, and this is the UUID constructor.
			Id:      qdrant.NewIDUUID(pointID),
			Vectors: qdrant.NewVectors(vectors[i]...),
			Payload: payload,
		}
	}

	// Batch upsert
	_, err := c.qdrant.Upsert(ctx, &qdrant.UpsertPoints{
		CollectionName: collectionName,
		Points:         points,
	})
	if err != nil {
		return fmt.Errorf("failed to upsert vectors: %w", err)
	}

	return nil
}

// EmbeddingDimension is the vector width the collection is created with
// (OpenAI ada-002). Queries of any other width are rejected before they
// reach Qdrant, where the failure would be a less obvious server error.
const EmbeddingDimension = 1536

// validateQueryVector checks a query vector independent of any connection.
// Split out for the same reason as validateUpsertInput.
func validateQueryVector(queryVector []float32) error {
	if len(queryVector) != EmbeddingDimension {
		return fmt.Errorf("invalid query vector dimension: expected %d, got %d",
			EmbeddingDimension, len(queryVector))
	}
	return nil
}

// SearchSimilar queries by vector and returns top K results with scores and metadata
func (c *Client) SearchSimilar(ctx context.Context, collectionName string, queryVector []float32, topK uint64, repositoryID string) ([]SearchResult, error) {
	if err := validateQueryVector(queryVector); err != nil {
		return nil, err
	}

	if err := c.connected(); err != nil {
		return nil, err
	}

	// Build filter for repository scope (optional)
	var filter *qdrant.Filter
	if repositoryID != "" {
		filter = &qdrant.Filter{
			Must: []*qdrant.Condition{
				{
					ConditionOneOf: &qdrant.Condition_Field{
						Field: &qdrant.FieldCondition{
							Key: "repository_id",
							Match: &qdrant.Match{
								MatchValue: &qdrant.Match_Keyword{
									Keyword: repositoryID,
								},
							},
						},
					},
				},
			},
		}
	}

	// Execute search
	searchResult, err := c.qdrant.Query(ctx, &qdrant.QueryPoints{
		CollectionName: collectionName,
		Query:          qdrant.NewQuery(queryVector...),
		Limit:          &topK,
		Filter:         filter,
		WithPayload:    qdrant.NewWithPayload(true),
	})
	if err != nil {
		return nil, fmt.Errorf("failed to search vectors: %w", err)
	}

	// Parse results
	results := make([]SearchResult, len(searchResult))
	for i, point := range searchResult {
		results[i] = SearchResult{
			ChunkID:      point.Payload["chunk_id"].GetStringValue(),
			RepositoryID: point.Payload["repository_id"].GetStringValue(),
			FilePath:     point.Payload["file_path"].GetStringValue(),
			Language:     point.Payload["language"].GetStringValue(),
			Score:        point.Score,
		}
	}

	return results, nil
}

// DeleteByChunkID removes embeddings when chunks are deleted
func (c *Client) DeleteByChunkID(ctx context.Context, collectionName string, chunkID string) error {
	if err := c.connected(); err != nil {
		return err
	}

	// Delete points by metadata filter
	_, err := c.qdrant.Delete(ctx, &qdrant.DeletePoints{
		CollectionName: collectionName,
		Points: &qdrant.PointsSelector{
			PointsSelectorOneOf: &qdrant.PointsSelector_Filter{
				Filter: &qdrant.Filter{
					Must: []*qdrant.Condition{
						{
							ConditionOneOf: &qdrant.Condition_Field{
								Field: &qdrant.FieldCondition{
									Key: "chunk_id",
									Match: &qdrant.Match{
										MatchValue: &qdrant.Match_Keyword{
											Keyword: chunkID,
										},
									},
								},
							},
						},
					},
				},
			},
		},
	})
	if err != nil {
		return fmt.Errorf("failed to delete vectors for chunk %s: %w", chunkID, err)
	}

	return nil
}

// Close closes the client connection
func (c *Client) Close() error {
	if err := c.connected(); err != nil {
		return err
	}
	return c.qdrant.Close()
}
