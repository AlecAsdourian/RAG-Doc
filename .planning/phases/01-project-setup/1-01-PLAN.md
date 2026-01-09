---
phase: 01-project-setup
plan: 01
type: execute
---

<objective>
Create the monorepo directory structure and initialize Go backend and Python workers.

Purpose: Establish the foundational layout that all subsequent development builds upon. Clear separation of services in a monorepo structure.
Output: Root directory with services/, Go module initialized, Python project structure in place.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/01-project-setup/1-CONTEXT.md

**Vision:** Polyglot monorepo with Go backend (API/orchestration), Python workers (ML/RAG), and frontend. Docker-first development.

**Essential:** Clean project structure that scales - folder layout and module organization done right from day one.
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create monorepo directory structure</name>
  <files>services/backend/, services/workers/, services/frontend/, README.md, .gitignore</files>
  <action>Create root directory structure:
  - services/backend/ (Go API and orchestration)
  - services/workers/ (Python ML/RAG workers)
  - services/frontend/ (React/Vue UI)
  - Root README.md explaining monorepo structure and components
  - .gitignore for root (ignore IDE files, OS files, .env)

  Each service directory gets its own README.md stub describing its purpose. Keep it simple - detailed docs come later.</action>
  <verify>ls -la shows services/ directory with backend, workers, frontend subdirs. README.md exists and describes structure.</verify>
  <done>Directory structure in place, root README documents component purposes, .gitignore present</done>
</task>

<task type="auto">
  <name>Task 2: Initialize Go backend module</name>
  <files>services/backend/go.mod, services/backend/main.go, services/backend/.gitignore</files>
  <action>Initialize Go module in services/backend/:
  - Run `go mod init github.com/yourusername/smart-docs-platform/services/backend` (use placeholder org name)
  - Create main.go with basic structure (package main, empty main func, TODO comments for API server)
  - Create backend-specific .gitignore (ignore binaries, vendor/ if used)
  - Add backend/README.md explaining this is the Go API server

  No actual server code yet - just module initialization and skeleton.</action>
  <verify>go.mod exists with correct module path, go mod tidy succeeds without errors, main.go compiles with `go build`</verify>
  <done>Go module initialized, main.go compiles, backend structure ready for implementation</done>
</task>

<task type="auto">
  <name>Task 3: Initialize Python workers project</name>
  <files>services/workers/pyproject.toml, services/workers/__init__.py, services/workers/.gitignore, services/workers/requirements.txt</files>
  <action>Initialize Python project in services/workers/:
  - Create pyproject.toml with project metadata (name: smart-docs-workers, Python >=3.11)
  - Create workers/__init__.py (package marker)
  - Create requirements.txt (empty for now - dependencies added in later phases)
  - Create workers-specific .gitignore (ignore __pycache__/, *.pyc, venv/, .pytest_cache/)
  - Add workers/README.md explaining this handles ML/RAG processing

  Use modern Python packaging (pyproject.toml), no setup.py. No dependencies yet - pure structure.</action>
  <verify>pyproject.toml valid, directory structure follows Python package conventions, .gitignore includes standard Python artifacts</verify>
  <done>Python project initialized with modern structure, package importable, ready for worker implementation</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] services/ directory exists with backend, workers, frontend subdirectories
- [ ] Go module in backend/ compiles: `cd services/backend && go build`
- [ ] Python package in workers/ is valid: `cd services/workers && python -c "import workers"`
- [ ] All three service READMEs exist
- [ ] Root README explains monorepo structure
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- No errors in Go or Python initialization
- Monorepo structure is clear and navigable
- Each service directory has purpose documented
</success_criteria>

<output>
After completion, create `.planning/phases/01-project-setup/1-01-SUMMARY.md`:

# Phase 1 Plan 1: Monorepo Structure Summary

**Monorepo initialized with Go backend, Python workers, and frontend structure.**

## Accomplishments

- Root directory structure with services/ organization
- Go module initialized in services/backend/
- Python project initialized in services/workers/
- Documentation stubs for each component

## Files Created/Modified

- `services/backend/go.mod` - Go module definition
- `services/backend/main.go` - Entry point skeleton
- `services/workers/pyproject.toml` - Python project metadata
- `services/workers/__init__.py` - Package marker
- `README.md` - Monorepo structure documentation
- `.gitignore` - Root ignore patterns

## Decisions Made

None - followed standard project initialization patterns.

## Issues Encountered

None expected - straightforward directory and module initialization.

## Next Step

Ready for 1-02-PLAN.md (Frontend Scaffold)
</output>
