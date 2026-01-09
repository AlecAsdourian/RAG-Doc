---
phase: 07-code-parsing-chunking
plan: 02
type: execute
---

<objective>
Implement semantic chunking engine with context-enriched metadata.

Purpose: Create intelligent code chunking that respects natural boundaries (functions, classes) while preserving context through ancestor chains and breadcrumbs. This is the core of Phase 7 - accurate chunk boundaries are critical for RAG quality.
Output: Semantic chunker that produces context-rich chunks with full ancestor metadata, breadcrumbs, and graceful fixed-size fallback.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/07-code-parsing-chunking/7-CONTEXT.md
@.planning/phases/07-code-parsing-chunking/7-01-SUMMARY.md
@services/workers/workers/parser/tree_sitter_parser.py

**From Plan 7-01:**
- TreeSitterParser extracts functions and classes with positions
- Python, Go, TypeScript parsing working
- Tree-sitter queries for robust node extraction

**From CONTEXT.md - Essential:**
- **Accurate semantic boundaries** - Priority #1
- **Context-enriched chunks**: parent scope + high-signal comments
- **Structured metadata**: Full ancestor chain (namespace → module → class → function)
- **Breadcrumbs**: Human-readable string like "UserService.authenticate"
- **Hybrid approach**: Semantic first, fixed-size fallback

**Chunk metadata structure (JSONB):**
```json
{
  "ancestor_chain": ["namespace", "module", "class", "function"],
  "breadcrumb": "UserService.authenticate",
  "parent_scope": "class UserService",
  "docstring": "Authenticates user with email and password",
  "chunk_type": "function"
}
```
</context>

<tasks>

<task type="auto">
  <name>Task 1: Implement semantic chunker with natural boundaries</name>
  <files>services/workers/workers/chunker/__init__.py, services/workers/workers/chunker/semantic_chunker.py, services/workers/workers/chunker/models.py</files>
  <action>
Create workers/chunker/ package with:

**models.py:**
```python
@dataclass
class Chunk:
    content: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    chunk_type: str  # "function", "class", "module", "file"
    metadata: Dict[str, Any]  # ancestor_chain, breadcrumb, etc.
```

**semantic_chunker.py:**
- `SemanticChunker` class that uses TreeSitterParser
- `chunk_file(file_path: str, content: str, language: str) -> List[Chunk]` method
- Extract chunks at natural boundaries:
  - **Functions** - entire function including signature + body
  - **Classes** - entire class including all methods (may be large, will chunk methods separately too)
  - **Top-level statements** - imports, constants, top-level code
- Include parent scope in chunk content (class name for methods, module context for top-level functions)
- Include high-signal docstrings/comments (function docstrings, class docstrings, NOT inline comments)
- Calculate accurate line numbers from tree-sitter byte positions

Chunk boundaries:
- Function: Start at decorator/signature, end at last line of body
- Class: Start at decorator/class keyword, end at last line
- Method: Chunk independently WITH class context prepended

DO NOT split functions across chunks (preserve completeness).
DO NOT include low-signal comments (TODOs, license headers, commented code).
DO include docstrings and module-level comments that explain purpose.
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.chunker.semantic_chunker import SemanticChunker
from workers.chunker.models import Chunk

chunker = SemanticChunker()
chunks = chunker.chunk_file('test.py', '''
class UserService:
    def authenticate(self, email: str) -> bool:
        \"\"\"Authenticates user with email.\"\"\"
        return True
''', 'python')

assert len(chunks) >= 1
assert any('authenticate' in c.content for c in chunks)
print(f'✓ Created {len(chunks)} chunks')
"
```
  </verify>
  <done>Semantic chunker creates chunks at function/class boundaries with accurate line numbers and chunk types</done>
</task>

<task type="auto">
  <name>Task 2: Extract ancestor chains and generate breadcrumbs</name>
  <files>services/workers/workers/chunker/semantic_chunker.py, services/workers/workers/chunker/metadata_builder.py</files>
  <action>
Create metadata_builder.py with:

**MetadataBuilder** class:
- `build_ancestor_chain(node: Node, tree: Tree) -> List[str]` method
  - Walk up the AST from current node to root
  - Collect: namespace → module → class → function
  - Handle language differences:
    - Python: module → class → function
    - Go: package → struct → method OR package → function
    - TypeScript: namespace → class → method OR module → function
- `generate_breadcrumb(ancestor_chain: List[str]) -> str` method
  - Join ancestors with dots: "UserService.authenticate"
  - Skip redundant ancestors (file name same as module)
  - Keep it concise (max 3-4 levels)
- `extract_parent_scope(node: Node, tree: Tree) -> str` method
  - Get immediate parent context (class signature for methods)
- `extract_docstring(node: Node, tree: Tree) -> Optional[str]` method
  - Find associated docstring/comment
  - Language-specific extraction:
    - Python: triple-quoted strings after function/class def
    - Go: comment block before function
    - TypeScript: JSDoc block before function

Update SemanticChunker to use MetadataBuilder and populate chunk.metadata with:
- `ancestor_chain`: List of ancestor names
- `breadcrumb`: Dot-separated path
- `parent_scope`: Immediate parent signature
- `docstring`: High-signal documentation

DO NOT include file path in breadcrumb (stored separately in chunk.file_path).
DO handle cases where no docstring exists (leave None).
DO preserve accuracy - wrong breadcrumbs worse than no breadcrumbs.
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.chunker.semantic_chunker import SemanticChunker

chunker = SemanticChunker()
chunks = chunker.chunk_file('test.py', '''
class UserService:
    \"\"\"Handles user operations.\"\"\"
    def authenticate(self, email: str) -> bool:
        \"\"\"Authenticates user.\"\"\"
        return True
''', 'python')

method_chunk = next(c for c in chunks if 'authenticate' in c.content)
assert 'ancestor_chain' in method_chunk.metadata
assert 'UserService' in method_chunk.metadata['breadcrumb']
assert method_chunk.metadata['docstring'] is not None
print(f'✓ Breadcrumb: {method_chunk.metadata[\"breadcrumb\"]}')
"
```
  </verify>
  <done>Chunks include full ancestor chain, human-readable breadcrumb, parent scope, and docstring in metadata</done>
</task>

<task type="auto">
  <name>Task 3: Add fixed-size fallback for unparseable code</name>
  <files>services/workers/workers/chunker/semantic_chunker.py, services/workers/workers/chunker/fixed_size_chunker.py</files>
  <action>
Create fixed_size_chunker.py:

**FixedSizeChunker** class:
- `chunk_by_lines(content: str, file_path: str, language: str, chunk_size: int = 50) -> List[Chunk]` method
  - Split into chunks of ~50 lines
  - Overlap of 5 lines between chunks (for context continuity)
  - Try to break at blank lines if possible (within ±5 line range)
  - Generate simple metadata:
    - `chunk_type`: "fixed_size"
    - `breadcrumb`: f"{file_path}:{start_line}-{end_line}"
    - `ancestor_chain`: [file_path]
    - No parent_scope or docstring (not available)

Update SemanticChunker:
- Wrap semantic chunking in try/except
- If tree-sitter parsing fails OR language not supported:
  - Log warning with file_path and error
  - Fall back to FixedSizeChunker
- If semantic chunking succeeds but produces zero chunks (rare edge case):
  - Fall back to FixedSizeChunker
  - Log warning about empty parse result

Fallback scenarios:
- Syntax errors in source code (parser fails)
- Unsupported language (e.g., Rust not yet supported)
- Corrupted files
- Binary files accidentally passed (should be filtered earlier, but handle gracefully)

DO NOT fail silently - log every fallback with reason.
DO ensure fixed-size chunks never lose code (all lines accounted for).
DO prefer semantic over fixed-size (only fall back when necessary).
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.chunker.semantic_chunker import SemanticChunker

chunker = SemanticChunker()

# Test semantic success
semantic_chunks = chunker.chunk_file('test.py', 'def foo(): pass', 'python')
assert len(semantic_chunks) > 0
assert semantic_chunks[0].chunk_type == 'function'

# Test fallback on unsupported language
fallback_chunks = chunker.chunk_file('test.rb', 'def foo\n  puts \"hi\"\nend\n' * 20, 'ruby')
assert len(fallback_chunks) > 0
assert fallback_chunks[0].chunk_type == 'fixed_size'

print('✓ Semantic chunking works, fallback works')
"
```
  </verify>
  <done>Hybrid chunking working: semantic parsing with graceful fixed-size fallback for unparseable code</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] Semantic chunker respects function/class boundaries
- [ ] Chunks include accurate line numbers
- [ ] Metadata has ancestor_chain, breadcrumb, parent_scope, docstring
- [ ] Fixed-size fallback works for unsupported languages
- [ ] All tests pass
- [ ] No code is lost (all lines accounted for in chunks)
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- Semantic chunking produces context-enriched chunks
- Ancestor chains and breadcrumbs generated correctly
- Fixed-size fallback prevents failures on unparseable code
- **Priority achieved: Accurate semantic boundaries**
- Foundation ready for summary generation in Plan 7-03
  </success_criteria>

<output>
After completion, create `.planning/phases/07-code-parsing-chunking/7-02-SUMMARY.md`:

# Phase 7 Plan 2: Semantic Chunking Engine Summary

**Context-enriched semantic chunking with hybrid fallback strategy.**

## Accomplishments

- Semantic chunker respects function and class boundaries
- Full ancestor chain extraction (namespace → module → class → function)
- Human-readable breadcrumbs ("UserService.authenticate")
- Parent scope and docstring extraction
- Fixed-size fallback for unparseable code
- Hybrid strategy works gracefully

## Files Created/Modified

- `services/workers/workers/chunker/__init__.py` - Package initialization
- `services/workers/workers/chunker/models.py` - Chunk data model
- `services/workers/workers/chunker/semantic_chunker.py` - Core chunking logic
- `services/workers/workers/chunker/metadata_builder.py` - Metadata extraction
- `services/workers/workers/chunker/fixed_size_chunker.py` - Fallback strategy

## Decisions Made

- Chunk size for fixed fallback: 50 lines with 5-line overlap
- Breadcrumb format: Dot-separated without file path
- Include docstrings, exclude low-signal comments
- Methods chunked independently WITH class context
- Log all fallbacks for observability

## Issues Encountered

[Document any language-specific parsing challenges or edge cases]

## Next Step

Ready for Plan 7-03: Summary Generation
</output>
