package vectordb

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	qdrant "github.com/qdrant/go-client/qdrant"
)

// Config holds vector database client configuration
type Config struct {
	URL string // Qdrant server URL (e.g., "http://localhost:6333")
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

// NewClient creates a new vector database client
func NewClient(cfg Config) (*Client, error) {
	if cfg.URL == "" {
		return nil, fmt.Errorf("vector database URL is required")
	}

	client, err := qdrant.NewClient(&qdrant.Config{
		Host: cfg.URL,
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

	// Create payload index for efficient metadata filtering
	err = c.qdrant.CreateFieldIndex(ctx, &qdrant.CreateFieldIndexCollection{
		CollectionName: collectionName,
		FieldName:      "repository_id",
		FieldType:      qdrant.FieldType_FieldTypeKeyword.Enum(),
	})
	if err != nil {
		return fmt.Errorf("failed to create repository_id index: %w", err)
	}

	err = c.qdrant.CreateFieldIndex(ctx, &qdrant.CreateFieldIndexCollection{
		CollectionName: collectionName,
		FieldName:      "language",
		FieldType:      qdrant.FieldType_FieldTypeKeyword.Enum(),
	})
	if err != nil {
		return fmt.Errorf("failed to create language index: %w", err)
	}

	return nil
}

// UpsertVectors inserts or updates embeddings with metadata
func (c *Client) UpsertVectors(ctx context.Context, collectionName string, vectors [][]float32, metadata []VectorMetadata) error {
	if len(vectors) != len(metadata) {
		return fmt.Errorf("vectors and metadata length mismatch: %d vectors, %d metadata", len(vectors), len(metadata))
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
			Id:      qdrant.NewIDString(pointID),
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

// SearchSimilar queries by vector and returns top K results with scores and metadata
func (c *Client) SearchSimilar(ctx context.Context, collectionName string, queryVector []float32, topK uint64, repositoryID string) ([]SearchResult, error) {
	if len(queryVector) != 1536 {
		return nil, fmt.Errorf("invalid query vector dimension: expected 1536, got %d", len(queryVector))
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
	return c.qdrant.Close()
}
