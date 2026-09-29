# Workers Service (Python)

Code ingestion and RAG processing pipeline for the Smart Documentation Platform.

## Tenant isolation

Every worker function that reads or writes a tenant-scoped table takes
`organization_id` and calls `workers.db.require_tenant`. This is not
optional — the DB-level trigger from migration 000009 refuses any write
without `app.current_tenant` set, and row-level security on every `chunks`
partition (migration 000017) returns nothing without it. See
[`docs/isolation.md`](../../docs/isolation.md) for the pattern, and
`workers/db/tenant.py` for the primitive's docstring.

## Purpose

- Code parsing with tree-sitter (Python, Go, TypeScript/JavaScript)
- Semantic chunking with context enrichment
- Summary generation for files and classes
- Embedding generation with OpenAI `text-embedding-ada-002`
- Storage of chunks **and their vectors** in Postgres, with pgvector
- Hybrid retrieval for RAG: keyword search and vector search, both in
  Postgres under the caller's tenant, fused in Python

## Tech Stack

- Python 3.11+
- Tree-sitter for AST parsing
- OpenAI API for embeddings
- PostgreSQL 16 with pgvector for chunks, vectors and full-text search
- Redis for the semantic cache

## Architecture

The ingestion pipeline processes code files through these stages:

1. **Parsing** (`workers.parser`): Tree-sitter AST parsing
2. **Chunking** (`workers.chunker`): Semantic boundaries (functions, classes)
3. **Summary Generation** (`workers.chunker.summary_generator`): File and class overviews
4. **Embedding** (`workers.embeddings`): OpenAI ada-002 1536-dim vectors
5. **Storage** (`workers.storage`): Postgres — each chunk row carries its
   tenant, its vector and the model that produced it
6. **Pipeline** (`workers.pipeline`): End-to-end orchestration

Retrieval (`workers.retrieval`) runs the keyword leg (`FTSRetriever`) and the
vector leg (`VectorRetriever`, `ORDER BY embedding <=> $query` with
`hnsw.iterative_scan = relaxed_order`, filtered to the generator's model)
in parallel under one tenant scope, fuses them with reciprocal rank fusion
and applies metadata boosts. Qdrant was retired in 22-03 after the
storage-move equivalence gate passed
(`.planning/phases/22-repository-clone-ingestion/22-03-equivalence.md`).

## Development

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Environment Setup

Create `.env` file:

```bash
# OpenAI API key for embeddings
OPENAI_API_KEY=sk-...

# Database connection (chunks, vectors and full-text search)
DATABASE_URL=postgresql://coderag:coderag@localhost:5434/coderag

# Semantic cache (optional)
REDIS_URL=redis://localhost:6379
```

### Running Tests

```bash
# The whole suite, as CI runs it (the isolation tests start their own
# Postgres through testcontainers, so Docker must be available)
pytest tests/ workers/ -q
```

### Code Quality

```bash
# Format code
make fmt

# Run linters (flake8 + mypy)
make lint
```

## Tooling

- **Black**: Code formatting (100 char line length)
- **Flake8**: Style guide enforcement
- **Mypy**: Static type checking
- **Pytest**: Testing framework

## Measuring retrieval

`scripts/rag_quality_harness.py` ingests a corpus and scores retrieval
against questions whose answers were established by reading the code. Its
docstring is the manual. In short:

```bash
# A scratch database, never compose's: --ingest and --clear refuse port 5434
# without --allow-compose.
export DATABASE_URL=postgresql://user:pass@127.0.0.1:<scratch-port>/db
export OPENAI_API_KEY=sk-...

python scripts/rag_quality_harness.py --corpus miniflux --corpora-dir ../../../rag-bench-corpora --fetch --check
python scripts/rag_quality_harness.py --corpus miniflux --corpora-dir ../../../rag-bench-corpora --ingest
python scripts/rag_quality_harness.py --corpus miniflux --corpora-dir ../../../rag-bench-corpora --measure --set holdout
```

Ingestion costs OpenAI credits; measurement is cheap. A recorded run
(`--query-vectors`, `--record`) can be judged against another with
`scripts/rag_benchmarks/compare_runs.py`, which is how 22-03's storage move
was shown to change no ranking.

### Manual Verification

```bash
psql "$DATABASE_URL" -c "
  SELECT language, chunk_type, embedding_model, COUNT(*)
  FROM chunks
  GROUP BY language, chunk_type, embedding_model;
"
```

## Usage Example

```python
from uuid import UUID
from workers.pipeline import IngestionPipeline

# Initialize pipeline
pipeline = IngestionPipeline(
    postgres_conn="postgresql://...",
    openai_api_key="sk-..."
)

# Process files, under the tenant that owns the repository
files = [
    ("main.py", "def hello(): pass", "python"),
    ("utils.go", "package main...", "go"),
]

stats = pipeline.process_files(
    files=files,
    organization_id=UUID("..."),
    repository_id=UUID("..."),
    commit_sha="abc123",
    branch="main"
)

print(f"Processed {stats['chunks_created']} chunks")
```

## Status

The ingestion pipeline and hybrid retrieval are operational on Postgres with
pgvector (Phase 22). What turns the queue into real ingestion of connected
repositories is Phase 22's remaining plans; see `.planning/ROADMAP.md`.
