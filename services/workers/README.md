# Workers Service (Python)

Code ingestion and RAG processing pipeline for the Smart Documentation Platform.

## Purpose

- Code parsing with tree-sitter (Python, Go, TypeScript/JavaScript)
- Semantic chunking with context enrichment
- Summary generation for files and classes
- Embedding generation with OpenAI ada-002
- Storage in Postgres (chunks) and Qdrant (embeddings)
- Vector search and retrieval for RAG

## Tech Stack

- Python 3.11+
- Tree-sitter for AST parsing
- OpenAI API for embeddings
- PostgreSQL for chunk metadata
- Qdrant for vector storage

## Architecture

The ingestion pipeline processes code files through these stages:

1. **Parsing** (`workers.parser`): Tree-sitter AST parsing
2. **Chunking** (`workers.chunker`): Semantic boundaries (functions, classes)
3. **Summary Generation** (`workers.chunker.summary_generator`): File and class overviews
4. **Embedding** (`workers.embeddings`): OpenAI ada-002 1536-dim vectors
5. **Storage** (`workers.storage`): Postgres (metadata) + Qdrant (vectors)
6. **Pipeline** (`workers.pipeline`): End-to-end orchestration

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

# Database connections
DATABASE_URL=postgresql://coderag:coderag@localhost:5432/coderag
QDRANT_URL=http://localhost:6333
```

### Running Tests

```bash
# Run unit tests
make test

# Run integration test (requires databases + OpenAI API key)
python scripts/test_ingestion.py
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

## Integration Testing

The integration test validates the end-to-end pipeline with real code from this project.

### Prerequisites

1. Start databases:
   ```bash
   docker-compose up -d postgres qdrant
   ```

2. Run migrations:
   ```bash
   cd services/backend && make migrate-up
   ```

3. Set OpenAI API key:
   ```bash
   export OPENAI_API_KEY=sk-...
   ```

### Run Integration Test

```bash
cd services/workers
python scripts/test_ingestion.py
```

Expected output:
- Processes 3 sample files (Go, Python, Markdown)
- Creates ~40-50 chunks
- Generates embeddings (costs ~$0.002)
- Stores in Postgres and Qdrant
- Tests semantic search with "vector database client"

### Manual Verification

Check Postgres:
```bash
psql "$DATABASE_URL" -c "
  SELECT language, chunk_type, COUNT(*)
  FROM chunks
  GROUP BY language, chunk_type;
"
```

Check Qdrant:
```bash
curl http://localhost:6333/collections/code_embeddings
```

## Usage Example

```python
from uuid import uuid4
from workers.pipeline import IngestionPipeline

# Initialize pipeline
pipeline = IngestionPipeline(
    postgres_conn="postgresql://...",
    qdrant_url="http://localhost:6333",
    openai_api_key="sk-..."
)

# Process files
files = [
    ("main.py", "def hello(): pass", "python"),
    ("utils.go", "package main...", "go"),
]

stats = pipeline.process_files(
    files=files,
    repository_id=uuid4(),
    commit_sha="abc123",
    branch="main"
)

print(f"Processed {stats['chunks_created']} chunks")
```

## Status

Phase 7 complete - Full ingestion pipeline operational:
- Tree-sitter parsing for Python, Go, TypeScript/JavaScript
- Semantic chunking with metadata enrichment
- Summary generation for files and classes
- OpenAI embedding generation with batching and caching
- Postgres and Qdrant storage
- End-to-end tested with project codebase
