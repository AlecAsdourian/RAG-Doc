---
phase: 01-project-setup
plan: 04
type: execute
---

<objective>
Set up linting, formatting, and testing frameworks for all three services.

Purpose: Establish good developer experience and code quality from the start. Catch errors early, maintain consistency.
Output: Working linters and formatters for Go, Python, and TypeScript. Test framework foundations in place.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/01-project-setup/1-CONTEXT.md
@.planning/phases/01-project-setup/1-01-SUMMARY.md
@.planning/phases/01-project-setup/1-02-SUMMARY.md
@.planning/phases/01-project-setup/1-03-SUMMARY.md

**Vision:** Tooling and DX - linting, formatting, testing frameworks for Go, Python, and frontend.

**Essential:** Good developer experience from the start - code quality tools configured properly.
</context>

<tasks>

<task type="auto">
  <name>Task 1: Set up Go tooling (linting, formatting, testing)</name>
  <files>services/backend/.golangci.yml, services/backend/Makefile</files>
  <action>Configure Go development tools:

**Install golangci-lint** (linting):
- Create .golangci.yml with sensible defaults (enable: gofmt, govet, staticcheck, gosimple)
- No need to install globally - document in README to install via: `go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest`

**Create Makefile** for common tasks:
```makefile
.PHONY: lint fmt test

lint:
	golangci-lint run

fmt:
	gofmt -w .

test:
	go test ./... -v
```

**Testing:** Go's built-in testing package is sufficient. No additional framework needed now.

Update backend/README.md with:
- `make fmt` - format code
- `make lint` - run linter
- `make test` - run tests

Keep it minimal - standard Go tooling is excellent.</action>
  <verify>cd services/backend && make lint succeeds (or reports issues to fix), make fmt runs successfully, make test runs (passes even with no tests)</verify>
  <done>Go linting and formatting configured, Makefile provides convenient commands, testing ready</done>
</task>

<task type="auto">
  <name>Task 2: Set up Python tooling (linting, formatting, testing)</name>
  <files>services/workers/pyproject.toml, services/workers/.flake8, services/workers/Makefile</files>
  <action>Configure Python development tools:

**Add to requirements.txt** (dev dependencies):
```
pytest>=7.4.0
black>=23.0.0
flake8>=6.0.0
mypy>=1.5.0
```

**Update pyproject.toml** with Black config:
```toml
[tool.black]
line-length = 100
target-version = ['py311']
```

**Create .flake8** for flake8 config:
```ini
[flake8]
max-line-length = 100
extend-ignore = E203, W503
```

**Create Makefile:**
```makefile
.PHONY: lint fmt test

lint:
	flake8 workers/
	mypy workers/

fmt:
	black workers/

test:
	pytest
```

Update workers/README.md with commands and purpose of each tool.</action>
  <verify>cd services/workers && make fmt runs successfully, make lint passes (or reports issues), make test runs pytest (passes with no tests)</verify>
  <done>Python linting (flake8, mypy), formatting (black), and testing (pytest) configured and working</done>
</task>

<task type="auto">
  <name>Task 3: Set up frontend tooling (linting, formatting, testing)</name>
  <files>services/frontend/.eslintrc.cjs, services/frontend/.prettierrc, services/frontend/package.json, services/frontend/vitest.config.ts</files>
  <action>Configure frontend development tools:

**Install dev dependencies:**
```bash
npm install -D eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin prettier eslint-config-prettier vitest @testing-library/react @testing-library/jest-dom jsdom
```

**Create .eslintrc.cjs:**
```javascript
module.exports = {
  parser: '@typescript-eslint/parser',
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'prettier'
  ],
  plugins: ['@typescript-eslint'],
  env: { browser: true, es2020: true, node: true }
};
```

**Create .prettierrc:**
```json
{
  "semi": true,
  "singleQuote": true,
  "tabWidth": 2,
  "trailingComma": "es5"
}
```

**Add to package.json scripts:**
```json
"lint": "eslint src --ext ts,tsx",
"fmt": "prettier --write \"src/**/*.{ts,tsx,css}\"",
"test": "vitest"
```

**Create vitest.config.ts:** Basic config extending vite.config.ts for testing.

Update frontend/README.md with npm run lint, fmt, test commands.</action>
  <verify>cd services/frontend && npm run lint passes (or reports issues), npm run fmt runs successfully, npm run test runs vitest (passes with no tests)</verify>
  <done>TypeScript linting (ESLint), formatting (Prettier), and testing (Vitest) configured and integrated</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] Backend: `cd services/backend && make lint && make fmt && make test` all succeed
- [ ] Workers: `cd services/workers && make lint && make fmt && make test` all succeed
- [ ] Frontend: `cd services/frontend && npm run lint && npm run fmt && npm run test` all succeed
- [ ] All three service READMEs document tooling commands
- [ ] Phase 1 complete: project structure + dev environment + tooling all working
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- Linting catches common issues in all three languages
- Formatting ensures consistency
- Test frameworks ready for TDD in future phases
- Developer experience is excellent
- **Phase 1 complete** - foundation is solid
</success_criteria>

<output>
After completion, create `.planning/phases/01-project-setup/1-04-SUMMARY.md`:

# Phase 1 Plan 4: Tooling & DX Summary

**Linting, formatting, and testing configured for all services.**

## Accomplishments

- Go: golangci-lint + gofmt + built-in testing
- Python: flake8 + mypy + black + pytest
- Frontend: ESLint + Prettier + Vitest
- Makefile conventions for backend and workers
- npm scripts for frontend
- Documentation for all tooling commands

## Files Created/Modified

- `services/backend/.golangci.yml` - Go linter config
- `services/backend/Makefile` - Go dev commands
- `services/workers/pyproject.toml` - Python Black config
- `services/workers/.flake8` - Flake8 config
- `services/workers/Makefile` - Python dev commands
- `services/workers/requirements.txt` - Added dev dependencies
- `services/frontend/.eslintrc.cjs` - ESLint config
- `services/frontend/.prettierrc` - Prettier config
- `services/frontend/vitest.config.ts` - Test config
- `services/frontend/package.json` - Added scripts and dev deps

## Decisions Made

- Standard tooling for each ecosystem (no exotic choices)
- Makefile pattern for Go and Python (idiomatic)
- npm scripts for frontend (standard Node.js convention)
- Vitest over Jest (faster, better Vite integration)

## Issues Encountered

None expected - standard tooling setup.

## Next Phase Readiness

**Phase 1 Complete:** Project foundation is solid. All essential pieces in place:
✓ Clean monorepo structure
✓ Docker-first development environment
✓ All three services initialized
✓ Tooling and DX configured

Ready for Phase 2: Database Setup
</output>
