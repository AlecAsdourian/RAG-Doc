---
phase: 03-vector-database
plan: 01
type: execute
---

<objective>
Set up vector database for storing and querying code embeddings.

Purpose: Enable semantic similarity search for RAG retrieval. The vector database stores embeddings of code chunks and provides fast nearest-neighbor search to find relevant code for user queries.
Output: Running vector database with API integration, basic storage and retrieval functions, verified with sample embeddings.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
@./.claude/get-shit-done/references/checkpoints.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/02-database-setup/2-03-SUMMARY.md

**From Phase 2:**
- Postgres schema has `query_embedding_id VARCHAR(255)` reference to vector DB
- Chunks table stores code with repository_id, file_path, start_line, end_line
- Queries and retrievals tables ready for vector search integration

**From PROJECT.md - Key Constraint:**
"Cost: LLM API costs at scale are a concern...Architecture must be cost-efficient from the start"

**Tech stack available:**
- Go 1.21 backend (services/backend/)
- Docker Compose for local development
- Postgres 16 with migrations

**Roadmap research topics:**
- Compare Pinecone vs Weaviate vs Qdrant (cost, performance, features)
- API patterns
- Indexing strategies for code embeddings
</context>

<tasks>

<task type="checkpoint:decision" gate="blocking">
  <decision>Select vector database technology</decision>
  <context>
Need a vector database for storing code embeddings and performing similarity search. This is a foundational choice with long-term impact on cost, performance, and operational complexity. Cost efficiency is critical (per PROJECT.md constraints).

Three main options with different tradeoffs:
  </context>
  <options>
    <option id="qdrant">
      <name>Qdrant</name>
      <pros>
- Open source, can self-host in Docker (no vendor lock-in)
- Free for self-hosted deployment (critical for cost constraint)
- Excellent performance (Rust-based)
- Rich Go client library
- Runs alongside Postgres in docker-compose (simple local dev)
- Good for code embeddings (supports metadata filtering, hybrid search)
      </pros>
      <cons>
- You manage infrastructure and scaling
- More operational overhead than managed service
- Need to handle backups and monitoring yourself
      </cons>
    </option>
    <option id="pinecone">
      <name>Pinecone</name>
      <pros>
- Fully managed (zero ops overhead)
- Generous free tier (100k vectors, 1 pod)
- Excellent documentation and Go SDK
- Purpose-built for production vector search
- Handles scaling, backups, monitoring automatically
      </pros>
      <cons>
- Paid beyond free tier ($70/month starter)
- Vendor lock-in (proprietary service)
- May not align with cost constraint at scale
- Additional external dependency (not in docker-compose)
      </cons>
    </option>
    <option id="weaviate">
      <name>Weaviate</name>
      <pros>
- Open source, can self-host
- Docker deployment option
- Good hybrid search capabilities (vector + keyword)
- Growing ecosystem and community
      </pros>
      <cons>
- Less mature than alternatives
- Smaller community and fewer resources
- More complex setup than Qdrant
- Go client less mature than Qdrant/Pinecone
      </cons>
    </option>
  </options>
  <resume-signal>Select: qdrant, pinecone, or weaviate</resume-signal>
</task>

<task type="auto">
  <name>Task 2: Set up vector database service</name>
  <files>docker-compose.yml, services/backend/.env.example, services/backend/go.mod</files>
  <action>
Based on selected vector DB:

**If Qdrant:**
- Add qdrant service to docker-compose.yml (image: qdrant/qdrant:latest, port 6333:6333, volume for persistence)
- Add QDRANT_URL to .env.example (http://qdrant:6333)
- Add qdrant-go client to go.mod: `go get github.com/qdrant/go-client`
- Verify with health check: curl http://localhost:6333/health returns OK

**If Pinecone:**
- No docker-compose changes (managed service)
- Add PINECONE_API_KEY and PINECONE_ENVIRONMENT to .env.example (with placeholder values and instructions)
- Add pinecone-go-client to go.mod: `go get github.com/pinecone-io/go-pinecone`
- Document signup process in backend README: "Get API key from https://app.pinecone.io"

**If Weaviate:**
- Add weaviate service to docker-compose.yml (image: semitechnologies/weaviate:latest, port 8080:8080, volume for persistence)
- Add WEAVIATE_URL to .env.example (http://weaviate:8080)
- Add weaviate-go client to go.mod: `go get github.com/weaviate/weaviate-go-client/v4`
- Verify with health check: curl http://localhost:8080/v1/.well-known/ready returns true

</action>
  <verify>
- docker-compose up starts vector DB service (if self-hosted)
- go mod tidy succeeds
- Health check endpoint returns success
  </verify>
  <done>Vector database service running and accessible, Go client library installed</done>
</task>

<task type="auto">
  <name>Task 3: Create vector database client and basic operations</name>
  <files>services/backend/pkg/vectordb/client.go, services/backend/pkg/vectordb/client_test.go</files>
  <action>
Create services/backend/pkg/vectordb/client.go with:
- NewClient() function that connects to vector DB using env vars
- CreateCollection() to initialize embeddings collection with:
  - Dimension: 1536 (OpenAI ada-002 embedding size, standard for now)
  - Distance metric: Cosine similarity (standard for semantic search)
  - Metadata schema: chunk_id (UUID), repository_id (UUID), file_path (string), language (string)
- UpsertVectors() to store embeddings with metadata
- SearchSimilar() to query by vector, return top K results with scores and metadata
- DeleteByChunkID() to remove embeddings when chunks are deleted

Use dependency injection pattern - client takes config, doesn't read env directly (testable).

Create basic tests in client_test.go:
- Test connection establishment
- Test vector upsert and retrieval
- Mock/stub for tests (don't require running vector DB for unit tests)

Error handling: Return errors, don't panic. Log connection failures but allow graceful degradation.
  </action>
  <verify>
- go build ./pkg/vectordb succeeds
- go test ./pkg/vectordb passes (with mocked/stubbed tests)
- Manual test: Start services, create collection, upsert sample vector, search returns results
  </verify>
  <done>Vector DB client with CRUD operations, tests passing, manual verification successful</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] docker-compose up starts all services including vector DB (if self-hosted)
- [ ] go build ./... succeeds without errors
- [ ] go test ./pkg/vectordb passes
- [ ] Manual test: Upsert sample embedding, search returns expected results
- [ ] Health check endpoint accessible
- [ ] Client handles connection failures gracefully
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- Vector database running and accessible
- Go client library integrated
- Basic CRUD operations (create collection, upsert, search, delete) implemented and tested
- Technology choice documented in summary
- **Foundation ready for Phase 8 (Embedding Pipeline)**
  </success_criteria>

<output>
After completion, create `.planning/phases/03-vector-database/3-01-SUMMARY.md`:

# Phase 3 Plan 1: Vector Database Setup Summary

**[Vector DB technology] integrated for code embedding storage and similarity search.**

## Accomplishments

- Vector database technology selected: [choice]
- [Self-hosted/Managed] service configured
- Go client library integrated
- Basic vector operations implemented (create, upsert, search, delete)
- Collection schema designed for code embeddings with metadata
- Tests created and passing

## Files Created/Modified

- `docker-compose.yml` - [If self-hosted: Added vector DB service]
- `services/backend/.env.example` - Vector DB connection config
- `services/backend/go.mod` - Added vector DB client library
- `services/backend/pkg/vectordb/client.go` - Vector DB client with CRUD operations
- `services/backend/pkg/vectordb/client_test.go` - Client tests

## Decisions Made

- **Vector DB choice:** [Technology] - Rationale: [cost/performance/operational tradeoffs]
- **Embedding dimension:** 1536 (OpenAI ada-002 standard)
- **Distance metric:** Cosine similarity (semantic search standard)
- **Metadata schema:** chunk_id, repository_id, file_path, language for filtering
- **Error handling:** Graceful degradation, errors returned not panicked

## Issues Encountered

[None expected for standard setup, or document any auth/connectivity issues]

## Next Step

Ready for Phase 8: Embedding Pipeline (generate embeddings and populate vector DB)

Note: Phase 4-7 can proceed in parallel - vector DB is ready but won't be populated until Phase 8.
</output>
