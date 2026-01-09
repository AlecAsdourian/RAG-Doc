# Phase 11: RAG Query Engine - Context

**Gathered:** 2026-01-09
**Status:** Ready for planning

<vision>
## How This Should Work

When a developer queries the codebase (e.g., "authentication flow" or "vector database client"), the system runs a hybrid search that combines lexical precision with semantic understanding.

Behind the scenes, two searches execute in parallel:
- **Postgres Full-Text Search (FTS)** catches exact lexical matches - identifiers, error strings, config keys, file paths, qualified names. This is the "grep on steroids" that developers trust.
- **Qdrant vector similarity search** captures semantic meaning - conceptual queries where the exact wording doesn't match the code.

Results from both searches (top 50 each) are merged, deduplicated by chunk_id, and ranked using reciprocal rank fusion (RRF). Chunks that appear in both result sets get an overlap boost - high-confidence signals that combine lexical and semantic relevance.

The final ranking applies intelligent adjustments using metadata we already have: documentation chunks get boosted, test files get slightly penalized, vendor/generated code gets heavily penalized. Queries with quoted strings or identifier-like tokens trigger exact-match boosts.

The system returns 5-10 high-precision, citeable results. Each result includes the file path, line range, breadcrumb (namespace→module→class→function), chunk type, similarity score, and full provenance (run_id + commit_sha). Fewer results, higher precision, complete traceability.

This is an "accuracy-first" approach: developers need to trust the results, and they need to be able to trace back to the exact code version that was indexed.

</vision>

<essential>
## What Must Be Nailed

- **Result relevance and quality** - The ranking must be excellent. 5-10 high-precision results beats 50 noisy ones. Developers need to trust the first few results.
- **Rich result metadata** - Every result must be fully citeable with file path, line numbers, breadcrumb, chunk type, similarity scores, and provenance (run_id + commit_sha). Developers need context and traceability.

</essential>

<boundaries>
## What's Out of Scope

- **LLM answer generation** - Phase 11 returns structured search results (chunks with metadata). Phase 12 will feed these to an LLM to generate natural language answers.
- **Multi-repository search** - Keep it simple for now. Query one repository at a time. Cross-repo aggregation can come later.
- **Advanced filtering UI** - No complex filtering by language, file type, date ranges, etc. Basic search first, refinements later.
- **Query caching and optimization** - Focus on correctness first. Performance optimizations like result caching are nice-to-haves that can come later.

</boundaries>

<specifics>
## Specific Ideas

**Hybrid Retrieval Architecture:**
- Run Postgres FTS (lexical) and Qdrant (semantic) in parallel against the latest successful ingestion run (or user-specified run_id)
- FTS prioritizes exact matches: identifiers, error strings, config keys, file paths, symbol names
- Qdrant prioritizes semantic similarity for conceptual queries
- Merge top 50 from each, deduplicate by chunk_id

**Ranking Fusion:**
- Use reciprocal rank fusion (RRF) with k=60: `score = Σ(1 / (60 + rank_i))`
- Overlap boost is automatic - chunks appearing in both lists accumulate scores from both retrievers
- Apply deterministic metadata-based adjustments:
  - **Chunk-type boosts**: docs/entrypoints/middleware/config > helpers > tests (configurable)
  - **Path/scope boosts**: Match on breadcrumb, boost paths like auth/, security/, middleware/
  - **Exact-match boosts**: Quoted strings `"AuthError"`, identifier-like tokens (camelCase, snake_case, PascalCase)
  - **Noise penalties**: generated/vendor/lockfiles/migrations (0.3-0.5x penalty)

**Query Parsing:**
- Start with simple regex-based detection for exact-match boosts
- Detect quoted strings: `"[^"]+"` or `'[^']+'`
- Detect identifiers: `[A-Z][a-zA-Z0-9_]+` (PascalCase), `[a-z_][a-z0-9_]+` (snake_case)
- Advanced query operators (AND, OR, NOT, wildcards) are out of scope

**Metadata Utilization:**
- Use existing `breadcrumb` field for scope/path matching and boosts
- Structured `qualified_name` field can be added later if needed for more precise filtering/ranking
- All chunks already have file+line provenance (citeable by default)

**Configuration:**
- Boost weights can be hardcoded initially for fast iteration
- Optional: Make them env-tunable (`BOOST_DOCS=1.5`, `PENALTY_VENDOR=0.3`, etc.)
- Full "configurable" boost system can come later

**Run Selection:**
- Default: Query the latest successful ingestion run for the given repository_id
- Optional: Accept `run_id` query parameter to search a specific ingestion run
- User-pinnable runs (persistent state) is a later feature requiring UI/state management

**Noise Reduction:**
- Prefer citeable chunks with clear file+line provenance
- Penalize generated/vendor paths: `*/node_modules/*`, `*/vendor/*`, `*/dist/*`, `*/build/*`, `*.lock`, `*-lock.json`, `*/migrations/*`, `*/__pycache__/*`, `*/.git/*`

</specifics>

<notes>
## Additional Context

**Current Architecture Alignment:**
- We're already using Qdrant for vectors (tested and working)
- The mention of "pgvector" was conceptual - actual implementation uses Qdrant for semantic search and Postgres for FTS/metadata
- All necessary fields already exist in the `chunks` table: file_path, start_line, end_line, breadcrumb, chunk_type, content
- The `ingestion_runs` table tracks repository_id, commit_sha, status, making run selection straightforward
- No schema changes needed for Phase 11

**Implementation Additions:**
1. Database migration: Add GIN index on chunks.content for full-text search
2. Query parser module: Simple regex-based exact-match detection
3. Ranking module: RRF fusion + metadata boosting logic
4. Query endpoint: Orchestrates FTS + vector search, applies fusion/ranking, returns top 5-10
5. Result format: `{chunk_id, file_path, start_line, end_line, breadcrumb, chunk_type, score, run_id, commit_sha}`

**Key Philosophy:**
Accuracy-first over coverage. Fewer results with higher precision builds developer trust. Complete traceability (run_id + commit_sha) enables debugging and reproducibility.

</notes>

---

*Phase: 11-rag-query-engine*
*Context gathered: 2026-01-09*
