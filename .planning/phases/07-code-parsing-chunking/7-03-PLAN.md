---
phase: 07-code-parsing-chunking
plan: 03
type: execute
---

<objective>
Generate summary chunks for files and large classes/modules.

Purpose: Create overview chunks that provide high-level context. File summaries give "what does this file do" answers, class summaries describe complex class responsibilities. These aid retrieval by providing entry points before diving into specific functions.
Output: Summary generator that creates file-level and class-level overview chunks with structured descriptions.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/07-code-parsing-chunking/7-CONTEXT.md
@.planning/phases/07-code-parsing-chunking/7-02-SUMMARY.md
@services/workers/workers/chunker/semantic_chunker.py
@services/workers/workers/chunker/models.py

**From Plan 7-02:**
- Semantic chunker creates function/class/method chunks
- Chunks have metadata with ancestor chains and breadcrumbs
- chunk_type field distinguishes chunk types

**From CONTEXT.md - Two-level summaries:**
- File-level: Describe file purpose, main components, responsibilities
- Class/module-level: For large or complex classes, describe role and key functionality

**Summary chunk characteristics:**
- chunk_type: "file_summary" or "class_summary"
- Content: Structured description (not code)
- Metadata includes what's being summarized
</context>

<tasks>

<task type="auto">
  <name>Task 1: Implement file-level summary generator</name>
  <files>services/workers/workers/chunker/summary_generator.py</files>
  <action>
Create summary_generator.py:

**FileSummaryGenerator** class:
- `generate_file_summary(file_path: str, content: str, language: str, chunks: List[Chunk]) -> Chunk` method
- Analyze file structure from chunks:
  - Count functions, classes, imports
  - Identify main exports/public API
  - Extract module-level docstring if present
  - Detect file purpose from name and structure:
    - `*_test.py` / `*.test.ts` - Test file
    - `__init__.py` - Package initialization
    - `main.go` - Entry point
    - `routes.ts` / `api.py` - API definitions
- Generate structured summary:
  ```
  File: {file_path}
  Purpose: {inferred purpose}

  Components:
  - {count} functions
  - {count} classes

  Main exports: {list of public functions/classes}

  {module docstring if present}
  ```

Summary metadata:
```json
{
  "chunk_type": "file_summary",
  "summarizes": "file",
  "file_path": "src/services/user.py",
  "breadcrumb": "user.py",
  "component_counts": {"functions": 5, "classes": 2},
  "main_exports": ["UserService", "authenticate", "create_user"]
}
```

DO NOT read file to generate summary - use already-parsed chunks (passed as parameter).
DO keep summaries concise (< 200 words).
DO focus on "what" and "why", not "how" (code chunks handle implementation).
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.chunker.semantic_chunker import SemanticChunker
from workers.chunker.summary_generator import FileSummaryGenerator

chunker = SemanticChunker()
summarizer = FileSummaryGenerator()

content = '''
class UserService:
    def authenticate(self): pass
    def create(self): pass

def helper(): pass
'''

chunks = chunker.chunk_file('services/user.py', content, 'python')
summary = summarizer.generate_file_summary('services/user.py', content, 'python', chunks)

assert summary.chunk_type == 'file_summary'
assert 'UserService' in summary.content
print(f'✓ File summary: {len(summary.content)} chars')
"
```
  </verify>
  <done>File summary generator creates structured overviews from parsed chunks</done>
</task>

<task type="auto">
  <name>Task 2: Implement class/module-level summary generator</name>
  <files>services/workers/workers/chunker/summary_generator.py</files>
  <action>
Add to summary_generator.py:

**ClassSummaryGenerator** class:
- `generate_class_summary(class_chunk: Chunk, method_chunks: List[Chunk]) -> Optional[Chunk]` method
- Determine if class needs summary:
  - Threshold: >5 methods OR >100 lines
  - Skip small utility classes
- Analyze class structure:
  - Count public vs private methods
  - Identify constructor/init method
  - Extract class docstring
  - List main responsibilities from method names
- Generate structured summary:
  ```
  Class: {class_name}
  Purpose: {class docstring or inferred from name}

  Key methods:
  - {public method 1}: {brief description from docstring}
  - {public method 2}: {brief description from docstring}
  - {count} more methods

  Usage: {inferred from init signature or common patterns}
  ```

Summary metadata:
```json
{
  "chunk_type": "class_summary",
  "summarizes": "class",
  "class_name": "UserService",
  "breadcrumb": "UserService",
  "method_count": 8,
  "public_methods": ["authenticate", "create", "update", "delete"]
}
```

Update SemanticChunker to generate summaries:
- After chunking file, generate file summary
- For each class with >5 methods, generate class summary
- Add summaries to chunk list with all other chunks

DO NOT summarize every class (only large/complex ones).
DO extract method descriptions from docstrings (don't invent).
DO keep class summaries focused on public API.
  </action>
  <verify>
```bash
cd services/workers
python -c "
from workers.chunker.semantic_chunker import SemanticChunker

chunker = SemanticChunker()

content = '''
class UserService:
    \"\"\"Manages user accounts.\"\"\"
    def __init__(self): pass
    def authenticate(self): pass
    def create(self): pass
    def update(self): pass
    def delete(self): pass
    def get(self): pass
    def list(self): pass
'''

chunks = chunker.chunk_file('services/user.py', content, 'python')

# Should have: file summary + class summary + individual method chunks
file_summaries = [c for c in chunks if c.chunk_type == 'file_summary']
class_summaries = [c for c in chunks if c.chunk_type == 'class_summary']

assert len(file_summaries) == 1
assert len(class_summaries) == 1  # Class has >5 methods
assert 'UserService' in class_summaries[0].content
print(f'✓ Generated {len(file_summaries)} file + {len(class_summaries)} class summaries')
"
```
  </verify>
  <done>Class summary generator creates overviews for large classes, integrated with semantic chunker</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] File summaries generated for all files
- [ ] Class summaries generated for large classes (>5 methods)
- [ ] Summaries are concise and structured
- [ ] Summary metadata includes component counts and main exports
- [ ] All tests pass
- [ ] Summaries don't duplicate information from function chunks
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- File-level summaries provide high-level context
- Class-level summaries describe complex classes
- Summaries integrated with semantic chunker output
- Summary chunks ready to be embedded alongside code chunks
- Foundation ready for embedding generation in Plan 7-04
  </success_criteria>

<output>
After completion, create `.planning/phases/07-code-parsing-chunking/7-03-SUMMARY.md`:

# Phase 7 Plan 3: Summary Generation Summary

**Two-level summary chunks for files and complex classes.**

## Accomplishments

- File-level summary generator analyzes structure and purpose
- Class-level summary generator for large/complex classes (>5 methods)
- Structured summary format with component counts
- Summaries integrated with semantic chunker pipeline
- Summary chunks have appropriate metadata and breadcrumbs

## Files Created/Modified

- `services/workers/workers/chunker/summary_generator.py` - File and class summary generation
- `services/workers/workers/chunker/semantic_chunker.py` - Updated to generate summaries

## Decisions Made

- Class summary threshold: >5 methods or >100 lines
- Summary max length: 200 words
- Focus on public API, not private methods
- Extract descriptions from docstrings (don't invent)
- Summaries added to same chunk list as code chunks

## Issues Encountered

[Document any challenges with summary generation or quality]

## Next Step

Ready for Plan 7-04: OpenAI Embedding Integration
</output>
