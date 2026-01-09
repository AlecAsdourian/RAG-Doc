# Phase 7: Code Parsing & Chunking - Context

**Gathered:** 2026-01-08
**Status:** Ready for planning

<vision>
## How This Should Work

Build a code parsing and chunking engine that processes local code files (no repository integration yet - that comes later). The system should be intelligent about how it breaks code apart:

**Hybrid chunking strategy:** Start with semantic parsing to understand code structure (functions, classes, modules), creating chunks at natural boundaries. Fall back to fixed-size chunking when semantic parsing fails or for unparseable code.

**Context-enriched chunks:** Each chunk includes its parent scope plus high-signal docstrings and comments. The goal is to make chunks understandable in isolation without flooding embeddings with noise. Include only comments that add semantic value.

**Structured metadata:** Store the full ancestor chain (namespace → module → class → function) as structured metadata. Also generate a human-readable "breadcrumb" string (e.g., "UserService.authenticate") for quick reference.

**Two-level summaries:**
- File-level: Generate overview chunks describing each file's purpose, main components, and responsibilities
- Class/module-level: For large or complex classes/modules, generate summary chunks that describe their role and key functionality

**Combined with embeddings:** This phase combines parsing/chunking AND embedding generation (merging what would have been Phase 7 + Phase 8). Parse code, create chunks with metadata, generate embeddings, and store everything.

**Architecture:** Python workers handle the heavy lifting (tree-sitter parsing, embedding generation). Go backend orchestrates the process. Clean separation of concerns.

</vision>

<essential>
## What Must Be Nailed

- **Accurate semantic boundaries** - This is the priority. Chunk boundaries must be clean: functions are complete, classes aren't split awkwardly, context is preserved, no orphaned code. If semantic parsing produces bad boundaries, the whole RAG pipeline suffers.

- **Context quality** - Chunks must be findable and understandable. The combination of breadcrumbs, ancestor metadata, and high-signal comments should make it clear what each chunk does and where it lives in the codebase.

- **Semantic-first, fallback-ready** - The hybrid approach must work gracefully: try semantic parsing, fall back cleanly when needed, never lose code.

</essential>

<boundaries>
## What's Out of Scope

- **Repository integration** - No GitHub/GitLab connection in this phase. Process local files only. Repository sync, webhooks, and OAuth come later (Phase 6 when we get to it).

- **Real-time updates** - No watching for file changes, no incremental updates. Batch processing only for v1.

- **Language-specific optimizations** - Start with good general support. Advanced language-specific features (understanding framework patterns, detecting API endpoints, etc.) can come later.

- **UI for browsing chunks** - Just create chunks and store them. Browsing/visualization comes in later phases.

</boundaries>

<specifics>
## Specific Ideas

- **Tree-sitter for parsing** - Use tree-sitter for language-agnostic AST parsing. It's proven, fast, supports many languages (Go, Python, JavaScript, TypeScript, Rust, etc.), and has good Python bindings.

- **Python workers** - Keep all parsing and embedding work in the Python workers service. Python has better ML/NLP libraries and tree-sitter bindings. Go backend just orchestrates.

- **Start with 3-4 languages** - Focus on Go, Python, JavaScript/TypeScript initially. Prove the approach works, then expand language support.

- **Store chunks in Postgres** - Use the existing chunks table from Phase 2. Populate it with parsed chunks, metadata (JSONB column for ancestor chain), and link to ingestion runs.

- **Store embeddings in Qdrant** - Use the vector database from Phase 3. Link chunks to embeddings via chunk_id.

- **OpenAI ada-002 for embeddings** - Start with OpenAI's text-embedding-ada-002 (1536 dimensions, matches our Qdrant setup). Consider alternatives later for cost optimization.

</specifics>

<notes>
## Additional Context

**Dependency adjustment:** This phase was originally dependent on Phase 6 (Repository Integration), but we're building it independently to avoid blocking on the auth/API/repo chain. By processing local files, we can build and test the chunking engine thoroughly, then integrate it with the repository pipeline later.

**Combining phases:** Originally Phase 7 (parsing/chunking) and Phase 8 (embeddings) were separate. We're combining them for faster iteration - parse, chunk, embed, and store in one pipeline.

**Priority:** Accuracy over speed. Better to chunk slowly and correctly than quickly and poorly. The RAG pipeline's quality depends entirely on chunk quality.

**Testing strategy:** Should be able to test with a small sample codebase (maybe this project's own code) to verify chunk boundaries, metadata, and embeddings before scaling up.

</notes>

---

*Phase: 07-code-parsing-chunking*
*Context gathered: 2026-01-08*
