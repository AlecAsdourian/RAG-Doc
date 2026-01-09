---
phase: 07-code-parsing-chunking
plan: 04
type: execute
---

<objective>
Integrate OpenAI embeddings API for chunk embedding generation.

Purpose: Generate 1536-dimensional embeddings for all chunks (code + summaries) using OpenAI's text-embedding-ada-002 model. Embeddings enable semantic similarity search in the RAG pipeline.
Output: Embedding generator with OpenAI API integration, batch processing, and cost optimization.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/07-code-parsing-chunking/7-CONTEXT.md
@.planning/phases/07-code-parsing-chunking/7-03-SUMMARY.md
@services/workers/workers/chunker/models.py

**From Phase 3 (Vector Database):**
- Qdrant configured for 1536-dimensional vectors
- Cosine similarity distance metric
- Metadata schema: chunk_id, repository_id, file_path, language

**From PROJECT.md - Cost Constraint:**
"LLM API costs at scale are a concern. Architecture must be cost-efficient from the start - consider caching, prompt optimization, and strategic use of LLM calls."

**From CONTEXT.md:**
- OpenAI text-embedding-ada-002 (1536 dimensions)
- Batch processing for cost optimization
- Consider alternatives later for cost optimization

**OpenAI Embeddings Pricing (as of 2024):**
- text-embedding-ada-002: $0.0001 per 1K tokens (~$0.10 per 1M tokens)
- Batching improves throughput
- Max batch size: 2048 embeddings per request
</context>

<tasks>

<task type="auto">
  <name>Task 1: Set up OpenAI API client</name>
  <files>services/workers/requirements.txt, services/workers/.env.example, services/workers/workers/embeddings/__init__.py, services/workers/workers/embeddings/openai_client.py</files>
  <action>
Add dependencies to requirements.txt:
- `openai>=1.0.0` - Official OpenAI Python library (v1.0+ has better async support)
- `tiktoken>=0.5.0` - Token counting for cost estimation

Create .env.example:
```
# OpenAI API Configuration
OPENAI_API_KEY=sk-...your-key-here

# Embedding Configuration
EMBEDDING_MODEL=text-embedding-ada-002
EMBEDDING_BATCH_SIZE=100
```

Create workers/embeddings/openai_client.py:

**OpenAIEmbeddingClient** class:
- `__init__(api_key: str, model: str = "text-embedding-ada-002")` constructor
- `generate_embedding(text: str) -> List[float]` method for single text
- `generate_embeddings_batch(texts: List[str]) -> List[List[float]]` method for batch
- Handle API errors gracefully:
  - Rate limiting (429) → retry with exponential backoff
  - Invalid API key (401) → clear error message
  - Token limit exceeded (400) → truncate text to max tokens
- Token counting with tiktoken:
  - Log token count for cost tracking
  - Warn if text exceeds 8191 tokens (ada-002 limit)

DO NOT store API key in code - use environment variable.
DO implement retry logic with exponential backoff (3 retries, 1s/2s/4s delays).
DO log all API calls for cost tracking (token count, batch size).
DO handle edge cases (empty text, None values).
  </action>
  <verify>
```bash
cd services/workers
# Verify dependencies installable
pip install -r requirements.txt

# Test client instantiation (without real API call)
python -c "
from workers.embeddings.openai_client import OpenAIEmbeddingClient

client = OpenAIEmbeddingClient(api_key='test-key-12345')
print('✓ OpenAI client instantiated')
"
```
  </verify>
  <done>OpenAI API client configured with error handling, retry logic, and token counting</done>
</task>

<task type="auto">
  <name>Task 2: Implement batch embedding generator with cost optimization</name>
  <files>services/workers/workers/embeddings/embedding_generator.py, services/workers/workers/embeddings/test_embeddings.py</files>
  <action>
Create embedding_generator.py:

**EmbeddingGenerator** class:
- `generate_embeddings_for_chunks(chunks: List[Chunk]) -> Dict[str, List[float]]` method
  - Process chunks in batches of 100 (configurable)
  - For each chunk, prepare text for embedding:
    - Include chunk content
    - Prepend breadcrumb for context: f"{breadcrumb}\n\n{content}"
    - Include docstring if present
    - DO NOT include raw metadata (JSON overhead)
  - Call OpenAIEmbeddingClient.generate_embeddings_batch()
  - Return dict mapping chunk content_hash → embedding vector
  - Log batch progress and token counts

**Cost optimization strategies:**
- Batch processing (100 chunks per API call)
- Truncate long chunks to 8000 tokens (keeps cost predictable)
- Cache embeddings by content_hash (if chunk unchanged, reuse embedding)
- Log costs: tokens used, estimated $ cost per batch

**Performance considerations:**
- Process batches sequentially for now (avoid rate limits)
- Async processing can come later if needed
- Progress logging every 100 chunks

Create test_embeddings.py:
- Test with mock OpenAI client (don't hit real API in tests)
- Verify batching logic
- Verify breadcrumb prepending
- Verify error handling

DO NOT embed metadata separately (vectors are for chunk content only).
DO truncate long chunks gracefully (keep first N tokens).
DO log all costs for transparency.
  </action>
  <verify>
```bash
cd services/workers
python -m pytest workers/embeddings/test_embeddings.py -v
# All tests pass with mocked API

# Manual test with real API (requires OPENAI_API_KEY)
python -c "
from workers.embeddings.embedding_generator import EmbeddingGenerator
from workers.chunker.models import Chunk

# Skip if no API key
import os
if not os.getenv('OPENAI_API_KEY'):
    print('⚠ Skipping real API test (no OPENAI_API_KEY)')
    exit(0)

gen = EmbeddingGenerator()
chunks = [
    Chunk(
        content='def hello(): return \"hi\"',
        file_path='test.py',
        start_line=1,
        end_line=1,
        language='python',
        chunk_type='function',
        metadata={'breadcrumb': 'test.hello'}
    )
]

embeddings = gen.generate_embeddings_for_chunks(chunks)
assert len(embeddings) == 1
assert len(list(embeddings.values())[0]) == 1536
print('✓ Generated 1536-dim embedding')
"
```
  </verify>
  <done>Batch embedding generator with cost optimization, progress logging, and 1536-dimensional vectors</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] `pip install -r requirements.txt` succeeds
- [ ] OpenAI client handles errors gracefully
- [ ] Batch embedding generation works
- [ ] Embeddings are 1536-dimensional (ada-002 standard)
- [ ] Token counting and cost logging functional
- [ ] Tests pass with mocked API
- [ ] Manual test with real API succeeds (if key available)
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- OpenAI API client integrated with retry logic
- Batch processing implemented (100 chunks per call)
- Cost optimization strategies in place
- Embeddings ready to be stored in Qdrant (Plan 7-05)
- Foundation ready for storage integration
  </success_criteria>

<output>
After completion, create `.planning/phases/07-code-parsing-chunking/7-04-SUMMARY.md`:

# Phase 7 Plan 4: OpenAI Embedding Integration Summary

**OpenAI text-embedding-ada-002 integrated for chunk embedding generation.**

## Accomplishments

- OpenAI API client with error handling and retry logic
- Batch embedding generation (100 chunks per call)
- Token counting and cost estimation
- Breadcrumb prepending for better context
- Cost optimization through batching and truncation
- 1536-dimensional vectors matching Qdrant configuration

## Files Created/Modified

- `services/workers/requirements.txt` - Added openai and tiktoken
- `services/workers/.env.example` - API key and config documentation
- `services/workers/workers/embeddings/__init__.py` - Package initialization
- `services/workers/workers/embeddings/openai_client.py` - OpenAI API client
- `services/workers/workers/embeddings/embedding_generator.py` - Batch processing
- `services/workers/workers/embeddings/test_embeddings.py` - Embedding tests

## Decisions Made

- Model: text-embedding-ada-002 (1536 dimensions)
- Batch size: 100 chunks per API call
- Truncation: 8000 tokens max per chunk
- Breadcrumb prepending: Improves context for embeddings
- Sequential processing: Avoids rate limits
- Cost logging: Track tokens and estimated costs

## Issues Encountered

[Document any API rate limiting or token limit issues]

## Next Step

Ready for Plan 7-05: Storage & End-to-end Integration
</output>
