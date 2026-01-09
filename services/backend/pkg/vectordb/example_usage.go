package vectordb

// Example usage (not meant to be executed, just documentation):
//
// func main() {
//     // 1. Create client
//     cfg := vectordb.Config{
//         URL: os.Getenv("QDRANT_URL"), // "http://qdrant:6333"
//     }
//     client, err := vectordb.NewClient(cfg)
//     if err != nil {
//         log.Fatalf("failed to create vector DB client: %v", err)
//     }
//     defer client.Close()
//
//     ctx := context.Background()
//
//     // 2. Create collection (one-time setup)
//     err = client.CreateCollection(ctx, "code_embeddings")
//     if err != nil {
//         log.Fatalf("failed to create collection: %v", err)
//     }
//
//     // 3. Upsert embeddings with metadata
//     vectors := [][]float32{
//         generateEmbedding("func main() { ... }"), // Your embedding function
//     }
//     metadata := []vectordb.VectorMetadata{
//         {
//             ChunkID:      "chunk-uuid-here",
//             RepositoryID: "repo-uuid-here",
//             FilePath:     "cmd/main.go",
//             Language:     "go",
//         },
//     }
//     err = client.UpsertVectors(ctx, "code_embeddings", vectors, metadata)
//     if err != nil {
//         log.Fatalf("failed to upsert vectors: %v", err)
//     }
//
//     // 4. Search for similar code
//     queryVector := generateEmbedding("How to start the server?")
//     results, err := client.SearchSimilar(
//         ctx,
//         "code_embeddings",
//         queryVector,
//         10,                  // top K results
//         "repo-uuid-here",   // optional: scope to repository
//     )
//     if err != nil {
//         log.Fatalf("failed to search: %v", err)
//     }
//
//     for _, result := range results {
//         log.Printf("Found: %s (score: %.4f)", result.FilePath, result.Score)
//     }
//
//     // 5. Delete embeddings when chunks are removed
//     err = client.DeleteByChunkID(ctx, "code_embeddings", "chunk-uuid-here")
//     if err != nil {
//         log.Fatalf("failed to delete: %v", err)
//     }
// }
