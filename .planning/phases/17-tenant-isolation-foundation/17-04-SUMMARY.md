---
phase: 17-tenant-isolation-foundation
plan: 04
subsystem: workers

requires:
  - phase: 17-01
    provides: Go harness pattern (WithTwoOrgs, TenantScope, AssertNoCrossTenantLeak) — mirrored 1:1 here
  - phase: 17-03
    provides: assert_tenant_scoped trigger (migration 000009) — verified firing on Python writes
provides:
  - workers/db/tenant.py — sync `require_tenant(conn, tenant_id, cursor_factory=None)` context manager
  - tests/isolation/ — pytest harness with with_two_orgs fixture and assert_no_cross_tenant_leak helper
  - Auditied + refactored PostgresWriter, FTSRetriever, QueryEngine, AnswerGenerator, IngestionPipeline — every DB call goes through require_tenant
  - Python API models (SearchRequest, ChatRequest) now require organization_id
affects: [Phase 22 (ingestion orchestration) — process_files now requires organization_id, v2 graph traversal will use require_tenant unchanged]

tech-stack:
  added:
    - testcontainers[postgres] >=4.0.0 (dev)
  patterns:
    - "Sync require_tenant matching current psycopg2 codebase; async variant deferred until an async worker is actually written"
    - "Session-scoped Postgres container per pytest session (no cross-session reuse — testcontainers-python doesn't expose WithReuseByName)"
    - "Shared migrations dir: pytest reads services/backend/migrations/*.up.sql — single source of truth for schema across Go and Python"
    - "Cross-language trigger firing verified: Python raw INSERT without require_tenant raises SQLSTATE 42501 from migration 000009"

key-files:
  created:
    - services/workers/workers/db/__init__.py
    - services/workers/workers/db/tenant.py
    - services/workers/tests/__init__.py
    - services/workers/tests/isolation/__init__.py
    - services/workers/tests/isolation/conftest.py
    - services/workers/tests/isolation/fixtures.py
    - services/workers/tests/isolation/test_harness.py
    - services/workers/tests/isolation/test_postgres_writer_isolation.py
    - services/workers/tests/isolation/test_query_engine_isolation.py
    - .planning/phases/17-tenant-isolation-foundation/17-04-SUMMARY.md
  modified:
    - services/workers/requirements.txt (added testcontainers[postgres])
    - services/workers/workers/storage/postgres_writer.py (all writes wrapped in require_tenant, organization_id added to every public method)
    - services/workers/workers/retrieval/fts_retriever.py (search + _get_latest_run_id wrapped in require_tenant, organization_id added)
    - services/workers/workers/retrieval/query_engine.py (organization_id threaded through query and _enrich_results_with_metadata)
    - services/workers/workers/generation/answer_generator.py (organization_id threaded through generate)
    - services/workers/workers/pipeline/ingestion_pipeline.py (organization_id threaded through process_files)
    - services/workers/api/models.py (SearchRequest, ChatRequest — organization_id: UUID field added)
    - services/workers/api/routes.py (search, chat, chat/stream pass organization_id to engine/generator)
    - services/workers/workers/pipeline/test_pipeline.py (mocks updated for new positional arg)

key-decisions:
  - "Sync require_tenant (Option A). The plan template assumed asyncpg; actual worker code is sync psycopg2 across every file that touches Postgres (postgres_writer, fts_retriever, query_engine._enrich_results_with_metadata). Rewriting to asyncpg would have refactored the whole retrieval/generation stack including the ThreadPoolExecutor-based parallel search in QueryEngine — clearly out of 17-04 scope. An async variant lands when an async worker actually needs it, following the same public shape."
  - "require_tenant's cursor_factory kwarg. Original signature yielded a plain cursor, but FTSRetriever's SELECT needs RealDictCursor for the dict-mapping code path. Rather than open a nested cursor inside require_tenant (which fights the transaction lifecycle), the primitive now accepts an optional cursor_factory, yields a cursor of that shape, and does the SET LOCAL on a throwaway setup cursor bound to the same tx."
  - "Test QueryEngine's read path via FTSRetriever directly, not via a live QueryEngine. Constructing QueryEngine requires Qdrant and OpenAI at __init__ (VectorRetriever tries to connect). The Postgres-touching part of the pipeline is FTSRetriever plus QueryEngine._enrich_results_with_metadata; both go through require_tenant. Testing FTSRetriever directly covers what matters."
  - "Session-scoped Postgres container per pytest session, not cross-session. Go's harness reuses container across `go test` invocations via Ryuk-disable + WithReuseByName; testcontainers-python exposes neither. ~5s startup per pytest session is tolerable."

patterns-established:
  - "Any worker that touches a tenant-scoped Postgres table (chunks, ingestion_runs, queries, retrievals, feedback, repositories) accepts organization_id as a required parameter and calls require_tenant to open its transaction."
  - "Cross-language trigger contract: migration 000009's assert_tenant_scoped raises on Python callers too (SQLSTATE 42501, message 'tenant isolation violated'). No language-specific code required — the DB is the seam."
  - "Migration files stay a Go concern; Python imports the up.sql files at test setup time and never carries its own migration state."

issues-created: []

duration: ~90 min
completed: 2026-09-06
---

# Phase 17 Plan 04: Python isolation harness + workers DB audit

**Workers now speak the same tenant-scoping vocabulary as the backend. Every Python read and write to tenant-scoped tables goes through `require_tenant`, the assert_tenant_scoped trigger from 17-03 fires cross-language on Python callers that skip it, and the API models refuse requests without `organization_id`.**

## Accomplishments

- Sync `require_tenant(conn, tenant_id, cursor_factory=None)` context manager — the single Python primitive for tenant-scoped DB access
- Session-scoped Postgres container per pytest session, shared migrations dir, `rag_doc_app` NOSUPERUSER role so RLS actually enforces (parity with Go harness)
- Five-scenario self-test suite for the harness itself; matches Go's `fixtures_test.go` scenarios
- PostgresWriter refactored: `create_ingestion_run`, `insert_chunks`, `complete_ingestion_run` all take `organization_id` and go through `require_tenant`
- FTSRetriever refactored: `search` and `_get_latest_run_id` take `organization_id` and go through `require_tenant`
- QueryEngine refactored: `query` and `_enrich_results_with_metadata` take `organization_id`; the fresh psycopg2 connection in enrichment goes through `require_tenant` too
- AnswerGenerator, IngestionPipeline, API models and routes all propagate `organization_id`
- Isolation tests for the writer (3 scenarios) and the read path via FTSRetriever (3 scenarios); the writer suite includes the cross-language trigger firing proof — a raw psycopg2 INSERT without `require_tenant` raises SQLSTATE 42501 from migration 000009

## Task Commits

Three atomic commits:

1. `8d993f8` — **test(17-04):** harness (require_tenant, with_two_orgs, self-tests)
2. `adf7d22` — **fix(17-04):** thread organization_id through every worker DB access (writer, retriever, query engine, answer generator, ingestion pipeline, API models/routes)
3. `c0457ee` — **test(17-04):** isolation tests for PostgresWriter and FTSRetriever, including cross-language trigger firing

_This SUMMARY commits separately as `docs(17-04):`._

## Deviations from Plan

### 1. Sync `require_tenant`, not async (Option A, chosen with planner)

Plan example was async/asyncpg. Actual production code is sync/psycopg2 across every file that touches Postgres. Rewriting to asyncpg would drag the whole retrieval and generation stack (including QueryEngine's ThreadPoolExecutor-based parallel search) into 17-04 — well beyond scope. Sync primitive matches reality; if a future async worker needs one, the shape ports cleanly.

### 2. `test_query_engine_isolation.py` tests FTSRetriever, not QueryEngine

Plan named this file after QueryEngine. Constructing QueryEngine in a test requires a live Qdrant and an OpenAI key at __init__ (VectorRetriever tries to connect). The Postgres-touching part of the pipeline is FTSRetriever + QueryEngine._enrich_results_with_metadata; both now go through `require_tenant`. Testing FTSRetriever directly covers the tenant-scoping semantics. Filename kept as the plan specified so cross-references still resolve; the test docstring explains the substitution.

### 3. Session-scoped container, no cross-session reuse

Go's harness uses `WithReuseByName` + Ryuk-disable to keep the same Postgres container alive across `go test` invocations. testcontainers-python exposes neither knob, so each `pytest` session starts its own. Startup is ~5s cold, which is tolerable given how often the isolation suite runs.

### 4. `require_tenant` grew a `cursor_factory` kwarg

Refactoring FTSRetriever surfaced the fact that its `search` returns `RealDictCursor` results. Rather than open a nested cursor inside `require_tenant`, the primitive now accepts an optional `cursor_factory` — the caller gets a cursor of the shape they need, still bound to the same tenant-scoped transaction. Documented in the primitive's docstring.

### 5. Real leaks fixed as part of this plan (per plan directive)

- `PostgresWriter` methods did not take `organization_id` at all — every write bypassed both the RLS filter and the 17-03 trigger. Fixed.
- `FTSRetriever` methods did not take `organization_id` — searches would return zero rows on a non-superuser DB connection, or the entire org's chunks on a superuser one. Fixed.
- `QueryEngine.query` and `_enrich_results_with_metadata` opened plain psycopg2 connections and executed SELECTs with no tenant scope. Fixed.
- API `SearchRequest` and `ChatRequest` did not declare `organization_id` — the Go layer (from 17-02) sends it, but Pydantic silently dropped it because the field wasn't declared. Result: the Python side computed results with no tenant context on the wire. Fixed by adding the field as required.

### 6. Not fixed (documented, out of scope)

- Pre-existing `workers/pipeline/test_pipeline.py::TestIngestionPipeline::test_process_files_success` failure. The test's `mock_chunk.content` is a `Mock()` object; `hashlib.sha256(chunk.content.encode(...))` raises TypeError. Reproduced on `main` with my changes stashed — pre-existing bug in the test's mock setup. Same shape as the pre-existing Go webhook_test failure from 17-02.
- `datetime.utcnow()` deprecation warnings in `postgres_writer.py`. Pre-existing style; not tenant-isolation-related.
- No isolation test for the full QueryEngine orchestration (the ThreadPoolExecutor parallel FTS + vector path). Requires Qdrant test infra; deferred.

## Verification

| Check | Result |
|---|---|
| `pytest tests/isolation/ -v` | 11/11 pass (~7s warm, ~7s cold) |
| Test harness self-tests | 5/5 pass |
| Writer isolation | 3/3 pass — includes cross-language 42501 proof |
| Read path (FTSRetriever) isolation | 3/3 pass |
| Existing `workers/pipeline/test_pipeline.py` | 2/3 pass (1 pre-existing failure, see deviation 6) |
| Go-side isolation regression (17-01/02/03) | untouched, no changes to backend/ |

## Issues Encountered

- **Pydantic silently drops undeclared fields.** The Go RAG client sends `organization_id` in the body (17-02), but Python's `SearchRequest`/`ChatRequest` didn't declare it, so Pydantic ate it and downstream code had no tenant. Adding the field made it required; requests without it now fail validation. This is the intended defense: the leak surface is a missing-field-in-the-model bug, and Pydantic's validation is where you want that to fail loudly.
- **The pytest container startup on Windows was flaky the first two runs.** Second-run warm was fine (~4s), but the very first startup on this machine took ~15s the first time Docker pulled the image. Documented but not a code change — same as Go's harness.
- **Refactoring `require_tenant` after FTSRetriever needed RealDictCursor.** Discovered mid-audit that yielding a plain cursor forced a nested-cursor dance in the retriever; adding `cursor_factory` cleaned it up. Harness self-tests still 5/5 after the primitive change.

## Next Phase Readiness

- **17-05 (CI gate + docs):** the isolation test convention is now established on both sides. `docs/isolation.md` should call out (a) require_tenant as the Python entry point matching Go's TenantScope, (b) the cross-language trigger contract from 17-03, (c) the migrations dir as the single source of truth. The CI scanner should recognize `require_tenant(` as the Python-side seal on any new mutation function, mirroring `TenantScope(` on the Go side.
- **Phase 22 (ingestion orchestration):** `IngestionPipeline.process_files` now takes `organization_id` — the caller (a job runner, whichever phase adds it) has to supply it. Silently omitting is a TypeError, not a leak.
- **v2 graph traversal:** the same `require_tenant` primitive wraps any future DB call. If v2 introduces new tenant-scoped tables, migration 000009's extension pattern (one ALTER TABLE plus one line in the Go test) handles the trigger side; Python doesn't need any change beyond passing `organization_id` through.

---
*Phase: 17-tenant-isolation-foundation*
*Completed: 2026-09-06*
