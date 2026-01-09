---
phase: 07-code-parsing-chunking
plan: 05
type: execute
---

<objective>
Store chunks in Postgres and embeddings in Qdrant, then test end-to-end with sample codebase.

Purpose: Complete the parsing/chunking/embedding pipeline by persisting data to databases. Verify the entire system works with real code from this project.
Output: Full pipeline operational - parse code → chunk → embed → store in Postgres + Qdrant. Verified with this project's own codebase.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/07-code-parsing-chunking/7-CONTEXT.md
@.planning/phases/07-code-parsing-chunking/7-04-SUMMARY.md
@.planning/phases/02-database-setup/2-02-SUMMARY.md
@.planning/phases/03-vector-database/3-01-SUMMARY.md
@services/backend/migrations/000002_create_ingestion_runs.up.sql
@services/backend/migrations/000003_create_chunks.up.sql
@services/backend/pkg/vectordb/client.go

**From Phase 2:**
- ingestion_runs table: repository_id, commit_sha, status, chunks_processed
- chunks table: id (UUID), ingestion_run_id, repository_id, file_path, start_line, end_line, content, content_hash, language, chunk_type, metadata (JSONB)

**From Phase 3:**
- Qdrant client in Go: CreateCollection(), UpsertVectors(), SearchSimilar()
- VectorMetadata: chunk_id, repository_id, file_path, language

**From Plan 7-04:**
- EmbeddingGenerator produces 1536-dim vectors
- Dict mapping content_hash → embedding

**Pipeline flow:**
1. Parse local files → chunks
2. Generate embeddings → vectors
3. Create ingestion_run record
4. Insert chunks into Postgres
5. Insert embeddings into Qdrant (linking by chunk_id)
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create storage integration module</name>
  <files>services/workers/workers/storage/__init__.py, services/workers/workers/storage/postgres_writer.py, services/workers/workers/storage/qdrant_writer.py, services/workers/requirements.txt</files>
  <action>
Add dependencies to requirements.txt:
- `psycopg2-binary>=2.9.0` - PostgreSQL adapter for Python
- `qdrant-client>=1.7.0` - Qdrant Python client

Create workers/storage/postgres_writer.py:

**PostgresWriter** class:
- `__init__(connection_string: str)` - Connect to Postgres
- `create_ingestion_run(repository_id: UUID, commit_sha: str, branch: str) -> UUID` method
  - Insert into ingestion_runs with status='processing'
  - Return ingestion_run_id
- `insert_chunks(chunks: List[Chunk], ingestion_run_id: UUID, repository_id: UUID) -> Dict[str, UUID]` method
  - Batch insert chunks (use executemany for performance)
  - Calculate SHA256 content_hash for each chunk
  - Populate all fields: file_path, start/end_line, content, content_hash, language, chunk_type
  - Store metadata as JSONB (ancestor_chain, breadcrumb, etc.)
  - Return dict mapping content_hash → chunk_id (UUID)
- `complete_ingestion_run(ingestion_run_id: UUID, chunks_count: int)` method
  - Update status='completed', chunks_processed=count

Create workers/storage/qdrant_writer.py:

**QdrantWriter** class:
- `__init__(url: str, collection_name: str = "code_embeddings")` - Connect to Qdrant
- `ensure_collection_exists()` method - Create collection if needed (1536-dim, cosine)
- `upsert_embeddings(chunk_embeddings: Dict[UUID, List[float]], chunk_metadata: Dict[UUID, Dict]) -> int` method
  - Batch upsert to Qdrant
  - Map chunk_id → embedding vector
  - Include metadata: chunk_id (as string), repository_id, file_path, language
  - Return count of vectors inserted

DO NOT duplicate collection creation logic - reuse from Phase 3 if possible.
DO use batch inserts for performance (both Postgres and Qdrant).
DO handle connection errors gracefully.
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.storage.postgres_writer import PostgresWriter
from workers.storage.qdrant_writer import QdrantWriter

# Test instantiation (without real connections)
# Real integration test in Task 3
print('✓ Storage modules importable')
"
```
  </verify>
  <done>Postgres and Qdrant writer modules created with batch insert capabilities</done>
</task>

<task type="auto">
  <name>Task 2: Create end-to-end pipeline orchestrator</name>
  <files>services/workers/workers/pipeline/__init__.py, services/workers/workers/pipeline/ingestion_pipeline.py, services/workers/workers/pipeline/test_pipeline.py</files>
  <action>
Create workers/pipeline/ingestion_pipeline.py:

**IngestionPipeline** class that orchestrates the full flow:

```python
class IngestionPipeline:
    def __init__(
        self,
        postgres_conn: str,
        qdrant_url: str,
        openai_api_key: str
    ):
        self.chunker = SemanticChunker()
        self.summarizer = FileSummaryGenerator()
        self.embedding_gen = EmbeddingGenerator(openai_api_key)
        self.postgres = PostgresWriter(postgres_conn)
        self.qdrant = QdrantWriter(qdrant_url)

    def process_files(
        self,
        files: List[Tuple[str, str, str]],  # (file_path, content, language)
        repository_id: UUID,
        commit_sha: str = "local",
        branch: str = "main"
    ) -> Dict[str, Any]:
        # 1. Create ingestion run
        # 2. Chunk all files
        # 3. Generate embeddings
        # 4. Insert chunks into Postgres
        # 5. Insert embeddings into Qdrant
        # 6. Complete ingestion run
        # 7. Return stats
```

**Method: process_files()**
1. Create ingestion run record (status='processing')
2. Parse and chunk all files:
   - For each file: chunker.chunk_file()
   - Generate file summary
   - Generate class summaries for large classes
   - Collect all chunks
3. Generate embeddings:
   - embedding_gen.generate_embeddings_for_chunks(chunks)
4. Store in Postgres:
   - postgres.insert_chunks(chunks, ingestion_run_id, repository_id)
   - Get mapping of content_hash → chunk_id
5. Store in Qdrant:
   - Map content_hash → chunk_id for embeddings
   - qdrant.upsert_embeddings(chunk_id → embedding, metadata)
6. Complete ingestion run:
   - postgres.complete_ingestion_run(ingestion_run_id, len(chunks))
7. Return stats: {files_processed, chunks_created, embeddings_generated, duration_seconds}

Error handling:
- If any step fails, mark ingestion_run status='failed' with error_message
- Log errors but don't raise (allow partial success)
- Return stats even on partial failure

Create test_pipeline.py:
- Test with mocked dependencies
- Verify flow logic
- Verify error handling

DO NOT process files in parallel yet (keep it simple).
DO log progress at each stage.
DO calculate and return comprehensive stats.
  </action>
  <verify>
```bash
cd services/workers
python -m pytest workers/pipeline/test_pipeline.py -v
# Tests pass with mocked dependencies
```
  </verify>
  <done>End-to-end pipeline orchestrator processes files from parsing to storage</done>
</task>

<task type="auto">
  <name>Task 3: Test pipeline with this project's codebase</name>
  <files>services/workers/scripts/test_ingestion.py, services/workers/README.md</files>
  <action>
Create scripts/test_ingestion.py - integration test script:

**Test with this project's code:**
1. Create test repository record in Postgres (or use existing if present)
2. Select sample files from this project:
   - services/backend/pkg/vectordb/client.go (Go code)
   - services/workers/workers/chunker/semantic_chunker.py (Python code)
   - .planning/PROJECT.md (documentation - should handle gracefully with fixed-size fallback)
3. Read file contents
4. Run IngestionPipeline.process_files()
5. Verify results:
   - Check Postgres: SELECT COUNT(*) FROM chunks WHERE repository_id = ?
   - Check Qdrant: Query collection, verify vectors exist
   - Print detailed stats and sample chunks
6. Query Qdrant with test search:
   - Generate embedding for "vector database client"
   - Search for similar chunks
   - Verify results make sense (should find vectordb/client.go chunks)

Manual verification steps:
```bash
# 1. Start services
docker-compose up -d postgres qdrant

# 2. Run migrations
cd services/backend && make migrate-up

# 3. Run test ingestion
cd services/workers
export OPENAI_API_KEY=sk-...
python scripts/test_ingestion.py
```

Expected output:
```
Processing 3 files...
✓ Created ingestion run: <uuid>
✓ Parsed and chunked 3 files → 47 chunks
✓ Generated 47 embeddings (cost: ~$0.002)
✓ Stored 47 chunks in Postgres
✓ Stored 47 vectors in Qdrant
✓ Completed ingestion run

Sample chunks:
- vectordb/client.go:15-42 (function NewClient)
- semantic_chunker.py:23-56 (function chunk_file)
- PROJECT.md:1-50 (fixed_size)

Test query: "vector database client"
Found 5 similar chunks:
1. vectordb/client.go:15-42 (score: 0.89) - NewClient function
2. vectordb/client.go:65-89 (score: 0.82) - CreateCollection function
...
```

Update README.md with testing instructions.

DO test with real OpenAI API (need key).
DO test with real Postgres and Qdrant (docker-compose).
DO verify search quality manually.
  </action>
  <verify>
Run the integration test:
```bash
cd services/workers
python scripts/test_ingestion.py
# Should complete successfully with stats output
```

Verify Postgres:
```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*), language, chunk_type FROM chunks GROUP BY language, chunk_type;"
# Should show Go, Python chunks with function/class/file_summary types
```

Verify Qdrant:
```bash
curl http://localhost:6333/collections/code_embeddings
# Should show collection with ~47 points
```
  </verify>
  <done>End-to-end pipeline tested with real code, chunks in Postgres, embeddings in Qdrant, search working</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] Postgres writer inserts chunks with all metadata
- [ ] Qdrant writer stores 1536-dim embeddings
- [ ] Pipeline processes multiple files successfully
- [ ] Integration test completes with real databases
- [ ] Chunks queryable in Postgres by file_path, language, chunk_type
- [ ] Embeddings searchable in Qdrant
- [ ] Test search returns relevant results
- [ ] No data loss (all code accounted for)
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- Chunks stored in Postgres with full metadata
- Embeddings stored in Qdrant with chunk_id linkage
- End-to-end pipeline works with this project's code
- Search returns relevant chunks
- **Phase 7 complete: Code parsing, chunking, embedding, and storage operational**
  </success_criteria>

<output>
After completion, create `.planning/phases/07-code-parsing-chunking/7-05-SUMMARY.md`:

# Phase 7 Plan 5: Storage & Integration Summary

**Full pipeline operational - code to chunks to embeddings to storage.**

## Accomplishments

- Postgres writer with batch insert capabilities
- Qdrant writer with vector upsert
- End-to-end pipeline orchestrator
- Integration tested with this project's codebase
- 47 chunks created from 3 sample files
- Embeddings stored and searchable
- Search quality verified manually

## Files Created/Modified

- `services/workers/requirements.txt` - Added psycopg2 and qdrant-client
- `services/workers/workers/storage/__init__.py` - Package initialization
- `services/workers/workers/storage/postgres_writer.py` - Postgres batch insert
- `services/workers/workers/storage/qdrant_writer.py` - Qdrant vector storage
- `services/workers/workers/pipeline/__init__.py` - Package initialization
- `services/workers/workers/pipeline/ingestion_pipeline.py` - End-to-end orchestrator
- `services/workers/workers/pipeline/test_pipeline.py` - Pipeline tests
- `services/workers/scripts/test_ingestion.py` - Integration test script
- `services/workers/README.md` - Updated with testing instructions

## Decisions Made

- Batch inserts for performance (Postgres and Qdrant)
- Sequential processing (parallel can come later)
- Error handling: Mark runs as 'failed' but return partial stats
- Test with project's own code for validation
- Commit SHA 'local' for local file processing (no git integration yet)

## Issues Encountered

[Document any database connection issues, embedding costs, or search quality concerns]

## Phase 7 Complete

**Full pipeline achieved:**
- Parse code files with tree-sitter (Python, Go, TypeScript)
- Chunk semantically with context enrichment
- Generate file and class summaries
- Create embeddings with OpenAI ada-002
- Store chunks in Postgres with full lineage
- Store embeddings in Qdrant with metadata
- Search works - relevant chunks retrieved

**What's ready:**
- Process local code files end-to-end
- Generate searchable knowledge base
- Query by semantic similarity
- Complete audit trail (chunk → file → lines)

**Next phase integration:**
- Phase 6 (Repository Integration): Add Git clone and webhook triggers
- Phase 11 (RAG Query Engine): Use stored embeddings for retrieval
- Phase 12 (LLM Answer Generation): Generate answers from retrieved chunks

**Testing coverage:**
- Unit tests for all components
- Integration test with real databases
- Manual search quality verification
- Sample codebase processed successfully
</output>
