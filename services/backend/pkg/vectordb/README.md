# Vector Database Client

Go client for Qdrant vector database, designed for storing and querying code embeddings.

## Architecture Decisions

**Technology: Qdrant**
- Self-hosted in Docker (zero cost, no vendor lock-in)
- Production-ready performance (Rust-based)
- Rich metadata filtering capabilities
- Mature Go client ecosystem

**Embedding Configuration:**
- Dimension: 1536 (OpenAI ada-002 standard)
- Distance metric: Cosine similarity (semantic search standard)
- Collection: `code_embeddings`

**Metadata Schema:**
- `chunk_id` (UUID): Links to Postgres chunks table
- `repository_id` (UUID): Enables repository-scoped search
- `file_path` (string): Source file location
- `language` (string): Programming language for filtering

**Indexed Fields:**
- `repository_id`: Fast repository filtering
- `language`: Fast language filtering

## API Overview

```go
// Create client
client, err := vectordb.NewClient(vectordb.Config{
    URL: "http://qdrant:6333",
})

// Initialize collection (one-time)
err = client.CreateCollection(ctx, "code_embeddings")

// Store embeddings
err = client.UpsertVectors(ctx, "code_embeddings", vectors, metadata)

// Search by similarity
results, err := client.SearchSimilar(ctx, "code_embeddings", queryVector, 10, repoID)

// Delete by chunk ID
err = client.DeleteByChunkID(ctx, "code_embeddings", chunkID)
```

See `example_usage.go` for detailed usage patterns.

## Testing

**Unit tests:** `go test ./pkg/vectordb`
- Validation logic (no Qdrant required)
- Mock-based tests

**Integration tests:** (TODO)
- Requires running Qdrant instance
- Use build tags to separate from unit tests
- Test with `docker-compose up qdrant`

## Error Handling

All methods return errors (never panic). Connection failures are logged but allow graceful degradation.

## Development

**Start Qdrant:**
```bash
docker-compose up -d qdrant
```

**Health check:**
```bash
curl http://localhost:6333/health
# Should return: {"title":"qdrant - vector search engine","version":"..."}
```

**Qdrant UI:**
Visit http://localhost:6333/dashboard for the web interface.

## Dependencies

- `github.com/qdrant/go-client` v1.7.0
- `github.com/google/uuid` (for point IDs)

## Future Enhancements

- [ ] Connection pooling configuration
- [ ] Retry logic for transient failures
- [ ] Batch size tuning for large upserts
- [ ] Integration tests with testcontainers
- [ ] Metrics/observability hooks
