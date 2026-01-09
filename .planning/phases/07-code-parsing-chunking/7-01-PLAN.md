---
phase: 07-code-parsing-chunking
plan: 01
type: execute
---

<objective>
Set up Python workers with tree-sitter for language-agnostic AST parsing.

Purpose: Establish the foundation for semantic code parsing. Tree-sitter provides robust, fast AST parsing for multiple languages with a unified API.
Output: Python environment with tree-sitter installed, language grammars configured (Go, Python, TypeScript), and basic parser module that can extract code structures.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/STATE.md
@.planning/phases/07-code-parsing-chunking/7-CONTEXT.md
@services/workers/README.md
@services/workers/requirements.txt

**From Phase 2 (Database Setup):**
- Chunks table schema with file_path, start_line, end_line, content, language, chunk_type, metadata (JSONB)
- Ingestion runs table for tracking parsing jobs
- Repository table for linking chunks to codebases

**From Phase 3 (Vector Database):**
- Qdrant vector DB ready for embeddings
- 1536-dimensional vectors (OpenAI ada-002 compatible)
- Metadata schema: chunk_id, repository_id, file_path, language

**From CONTEXT.md:**
- Tree-sitter for language-agnostic AST parsing
- Python workers handle parsing + embedding
- Start with Go, Python, JavaScript/TypeScript
- Priority: Accurate semantic boundaries over speed

**Tech stack available:**
- Python 3.11+ with pytest, black, flake8, mypy
- services/workers/ directory structure established
</context>

<tasks>

<task type="auto">
  <name>Task 1: Install tree-sitter and language grammars</name>
  <files>services/workers/requirements.txt, services/workers/Makefile</files>
  <action>
Add tree-sitter dependencies to requirements.txt:
- `tree-sitter>=0.20.0` - Core tree-sitter library with Python bindings
- `tree-sitter-python>=0.20.0` - Python language grammar
- `tree-sitter-go>=0.20.0` - Go language grammar
- `tree-sitter-javascript>=0.20.0` - JavaScript/TypeScript grammar

Update Makefile to add `make install` target that creates venv and installs dependencies.

DO NOT use `tree_sitter_languages` package - it's outdated. Use individual language packages.
DO NOT compile grammars manually - the packages come pre-compiled.
  </action>
  <verify>
```bash
cd services/workers
pip list | grep tree-sitter
# Should show tree-sitter, tree-sitter-python, tree-sitter-go, tree-sitter-javascript
```
  </verify>
  <done>requirements.txt updated with tree-sitter dependencies, Makefile has install target, dependencies installable</done>
</task>

<task type="auto">
  <name>Task 2: Create parser module with tree-sitter integration</name>
  <files>services/workers/workers/parser/__init__.py, services/workers/workers/parser/tree_sitter_parser.py, services/workers/workers/parser/test_parser.py</files>
  <action>
Create workers/parser/ package with:

**tree_sitter_parser.py:**
- `TreeSitterParser` class that loads language grammars (Python, Go, TypeScript)
- `parse_file(content: str, language: str) -> Tree` method
- `extract_functions(tree: Tree) -> List[Dict]` method that finds function definitions with:
  - function name
  - start/end byte positions (convertible to line numbers)
  - docstring if present
- `extract_classes(tree: Tree) -> List[Dict]` method (same structure)
- `get_node_text(node: Node, content: bytes) -> str` helper

Handle TypeScript AND JavaScript with same parser (tree-sitter-javascript supports both).

Use tree-sitter query language for robust node extraction:
```python
# Example for Python functions
function_query = language.query("(function_definition name: (identifier) @name)")
```

DO NOT use string parsing or regex - use tree-sitter queries exclusively.
DO NOT try to parse every language at once - focus on Python, Go, TypeScript.

**test_parser.py:**
Basic tests with sample code snippets:
- Test Python function extraction
- Test Go function extraction
- Test TypeScript function extraction
- Verify line number calculation from byte positions
  </action>
  <verify>
```bash
cd services/workers
python -m pytest workers/parser/test_parser.py -v
# All tests pass
```
  </verify>
  <done>TreeSitterParser class extracts functions and classes from Python, Go, and TypeScript files with accurate positions</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] `pip install -r requirements.txt` succeeds
- [ ] `python -m pytest workers/parser/ -v` passes
- [ ] Parser can extract functions from sample Python, Go, TypeScript files
- [ ] Extracted positions are accurate (line numbers match source)
</verification>

<success_criteria>

- All tasks completed
- All verification checks pass
- Tree-sitter installed with 3 language grammars
- Parser module extracts functions and classes with correct positions
- Tests demonstrate parsing works for all 3 languages
- Foundation ready for semantic chunking in Plan 7-02
  </success_criteria>

<output>
After completion, create `.planning/phases/07-code-parsing-chunking/7-01-SUMMARY.md`:

# Phase 7 Plan 1: Python Workers Setup & Tree-sitter Parsing Summary

**Tree-sitter integrated for language-agnostic AST parsing in Python workers.**

## Accomplishments

- Tree-sitter library installed with Python bindings
- Language grammars configured: Python, Go, JavaScript/TypeScript
- Parser module created with function/class extraction
- Tests verify parsing accuracy across all 3 languages
- Byte position → line number conversion working

## Files Created/Modified

- `services/workers/requirements.txt` - Added tree-sitter dependencies
- `services/workers/Makefile` - Added install target
- `services/workers/workers/parser/__init__.py` - Package initialization
- `services/workers/workers/parser/tree_sitter_parser.py` - Core parser implementation
- `services/workers/workers/parser/test_parser.py` - Parser tests

## Decisions Made

- Individual language packages over tree_sitter_languages (more up-to-date)
- Tree-sitter queries over regex (more robust)
- JavaScript grammar handles TypeScript too (same parser)
- Start with functions and classes (most important boundaries)

## Issues Encountered

[Document any issues with tree-sitter setup or language grammar compatibility]

## Next Step

Ready for Plan 7-02: Semantic Chunking Engine
</output>
