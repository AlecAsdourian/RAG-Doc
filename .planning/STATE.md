# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-08; product-vision reframe recorded in memory at `product_vision.md`, 2026-09-03)

**Core value:** Persistent, shared, code-aware memory substrate for parallel AI coding agents. v1.0 ships as a smart docs platform (hybrid RAG over connected code repos) — the foundation the substrate is built on.
**Current focus:** Milestone v1.0 MVP — first shippable version. In flight: Phase 19 (Auth Wiring & Org Provisioning).

## Current Position

Milestone: v1.0 MVP (9 phases: 17-25)
Phase: 20 COMPLETE — Repository Integration. All five plans merged. Phase 21 (Job Infrastructure) is researched and has locked context; plans not yet broken out.
Plan: Phases 17, 19 and 20 closed. Phase 21 researched 2026-09-10 (`21-RESEARCH.md`, `21-CONTEXT.md`); six decisions locked (L1-L6); ISS-016 settled in that context, closes when the phase ships.
Status: Phase 17 closed 2026-09-06. Phase 18 deprioritized. Phase 19 closed 2026-09-08. The GitHub App is registered and its contract verified against the live API (20-02). Repositories now have a tenant-scoped CRUD API (20-03).
Last activity: 2026-09-10 — v2 substrate design + research + decisions D1-D4 (PR #24, awaiting review); Phase 21 research and context written

**v2 substrate work, 2026-09-10.** `.planning/v2-substrate/` holds `DESIGN.md`
(the RAG redesign and 21 fleet proposals), `RESEARCH.md` (R-A through R-G, all
complete) and `DECISIONS.md` (D1-D4, settled with schemas). D1-D4 gate Phase 22
because that phase writes the first real rows. Two findings changed the plan:
SCIP indexers must execute untrusted build commands, which pulls a sandbox
forward; and RLS makes every vector query the filtered-search case pgvector is
worst at, which added HASH partitioning by organization to D2. The pruning
question was measured rather than assumed — 15 of 16 partitions pruned from the
RLS predicate alone, recall 8/10 unpartitioned vs 10/10 partitioned, and
`hnsw.iterative_scan` changed nothing (correcting R-B). See `DECISIONS.md` § 5.

**Phase 21 is decided but not planned.** `21-CONTEXT.md` locks six decisions.
The headline reversal: the ROADMAP's tentative Redis Streams pick is rejected in
favour of a Postgres `ingestion_jobs` table claimed with `FOR UPDATE SKIP
LOCKED` — because D2 moves vectors into Postgres, which makes chunk-write and
job-completion a single transaction, and because a Redis queue would re-create
the dual-write hazard D2 exists to delete. Breaking 21 into plans is the next
planning task.

Progress: v1.0 MVP ██░░░░░░░ 2/9 phases complete, Phase 20 at 5/5 plans (Phase 18 Observability deferred — see project_phase18_deprioritized memory)

**Note on this section's history:** 19-01 and 19-02 both shipped without updating STATE.md, so this file sat two plans stale. Brought current in 19-03 — and then went stale again through 20-01/20-02/20-03, which is why the note is worth keeping. Update this file in the same commit as the summary, not afterwards.

## Performance Metrics

**Velocity:**
- Total plans completed: — (v1.0 tracking starts here)
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| — | — | — | — |

**Recent Trend:**
- Last 5 plans: —
- Trend: —

## Accumulated Context

### Decisions (v1.0 MVP milestone)

- **Milestone version:** `v1.0 MVP` (nothing has formally shipped; v0.9 = pre-milestone initial build)
- **Sign-ups:** Public (open registration) with abuse protections in Phase 24
- **Repository integration:** GitHub App (not OAuth App) — per-installation permissions, higher rate limits, built-in webhook signing
- **Phase ordering:** Isolation → Observability → Auth → Repo → Job Infra → Ingestion → Frontend → Deploy → Launch. Cross-cutting concerns (isolation, observability) established BEFORE more endpoints ship so every new endpoint inherits them
- **Cost controls:** Per-org rate limits + LLM cost caps included in v1.0 Phase 24 (mandatory; not optional)
- **Billing:** Explicitly deferred to post-v1.0 (need traction data first)
- **v2 door-keeping:** Every design decision noted for whether it closes GraphRAG/agent/MCP doors — chunking output events (Phase 22-04), job progress payload shape, etc.
- **Reviewer session:** Live since Phase 17-01; bootstrap prompt at `.planning/fleet/reviewer-session-prompt.md`. Every code PR since has gone through it.

### Decisions (retained from v0.9)

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions still affecting current work:

- **Tailwind v4 syntax:** `@theme` directive + CSS variable syntax
- **SSE streaming:** AsyncGenerator pattern for chat responses
- **Path aliases:** `@/` import alias for frontend
- **Vector DB technology:** Qdrant (self-hosted, cosine similarity, 1536-dim)
- **LLM model choice:** GPT-4o Mini (cost efficiency)
- **Semantic cache threshold:** 0.95 cosine similarity, 1h TTL
- **Database primary keys:** UUIDs over SERIAL
- **Timestamps:** TIMESTAMPTZ
- **RLS:** Enabled + FORCED in migration 000008 (verification pending Phase 17)
- **Migration tool:** golang-migrate
- **Deduplication:** SHA256 content_hash

### Deferred Issues

- **ISS-001:** Shared type definitions for cross-phase data contracts — surface again during Phase 18 (structured logging conventions) or Phase 20 (repos API shape)
- **ISS-002:** Cross-phase verification pattern in planning workflow — template updated; apply to all v1.0 plans
- **ISS-004:** Org selection mechanism — **✅ closed 2026-09-08** across 19-03 (JWT-carried claim, header gone) and 19-04 (list + select endpoints). Frontend picker is Phase 23 work against `docs/auth-frontend-contract.md`.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today (nothing removes memberships), but **whatever ships membership removal must rewrite the claim** — short token TTLs do not bound this. See ISSUES.md.
- **ISS-005:** Supabase Native OAuth webhook handler — **scheduled: Phase 19-01**
- **ISS-006:** Test database connectivity — **✅ closed 2026-09-05** in Phase 17-01 via testcontainers-go harness (`pkg/testing/isolation`); see ISSUES.md
- **ISS-007:** JWT-carried tenant claim — **✅ closed 2026-09-08** in Phase 19-03. Tenant identity now comes only from the Supabase-signed `app_metadata.organization_id` claim; the `X-Organization-ID` path is deleted, including from CORS. Closed without the per-request membership re-check the original filing called for — reasoning in ISSUES.md and 19-03-SUMMARY.md.
- **ISS-008:** Request-scoped tenant transaction for DB-hitting endpoints — **✅ closed 2026-09-08** in Phase 20-01 (`db.TenantScoper`). Handlers touching an RLS table take the scoper and not a pool, so an unscoped query is inexpressible rather than merely discouraged.
- **ISS-013:** Unscoped access to an RLS table behaves differently depending on connection history — **filed 2026-09-08** during 20-01. Not live (the scoper makes it unreachable), but it is a heisenbug generator and the fix is now known to be a one-line `AfterConnect` sentinel rather than a six-table migration. It bit a test during 20-03.
- **ISS-014:** `pkg/db` imports `pkg/auth`, inverting the layering — **filed 2026-09-08** during 20-01.
- **ISS-015:** The isolation scanner's coverage match is method-blind — **filed 2026-09-08** during 20-03's review. The nested-block, middleware-wrapped and multi-segment holes it originally also claimed are closed and pinned.
- **ISS-016:** `sync_state` has no lease, so a relink can re-queue a run already in flight — **filed 2026-09-09** during 20-03's review. **Must be settled before Phase 21 builds the queue**, not after.
- **ISS-017:** Three residual soft edges in the isolation scanner — **filed 2026-09-09**, all LOW and none reachable today.
- **ISS-009:** `pkg/vectordb` build — **✅ closed 2026-09-08.** Qdrant client bumped v1.7.0 → v1.19.2; the pin had always predated the API the package was written against. Fixing it surfaced a panicking unit test that had never been able to run.
- **ISS-010:** Isolation harness parallel race — **✅ closed 2026-09-08.** Role setup now holds a `pg_advisory_xact_lock`; verified over 5 cold-container parallel runs.
- **ISS-011:** OAuth callback routes — **✅ closed 2026-09-08** by unmounting them (not repairing). Repairing the 500 alone would have converted a loud failure into a silently claim-less account. Handlers kept as reference; deleting them is a planner/user call.
- **ISS-012:** A revoked membership does not revoke the organization claim — **filed 2026-09-08** during 19-04. Not exploitable today; **whatever ships membership removal must rewrite the claim.** See ISSUES.md.
- **Frontend inline-style pollution** — ongoing rule, cleaned per component touched
- **Mocked repos/orgs/graph in frontend** — **replaced in Phase 23**

### Blockers/Concerns

- ~~**User action needed:** GitHub App registration~~ — **done 2026-09-08.** App id 4880866 (`rag-doc-dev`); private key lives outside the repository.
- **User action needed:** Deployment target choice in Phase 24-01 (recommend I bring back options + trade-offs at that point)
- **User action needed:** Observability stack choice in Phase 18 (self-hosted vs SaaS — cost implications)

## Session Continuity

Last session: 2026-09-09
Stopped at: Phase 20 complete. Nothing in flight.
Resume file: None

Next command suggested: plan Phase 21 (`/gsd:plan-phase 21`). It has no PLAN files yet.

**Settle ISS-016 as part of planning Phase 21, not after.** `sync_state` on
`repositories` is a status column being used as a queue: no lease, no owner,
no attempt counter, so two workers can believe they own the same repository.
Phase 20 added a second writer to it (the webhook), which makes the race
easier to hit. The queue's shape is Phase 21's decision and this is the input
to it.

**What Phase 21 inherits, concretely:** a work item is a `repositories` row
with `sync_state = 'pending'`. There is no queue table — 20-05 deliberately
did not invent one. `docs/api-github-webhooks.md` has the query and three
traps: rows with `installation_id IS NULL` are unsyncable rather than failed
and must not be retried; a suspended or uninstalled installation cannot mint
a token, so check `suspended_at` / `uninstalled_at` before attempting a sync.

**The GitHub App is fully configured** (registration 2026-09-08; user
authorization and client credentials 2026-09-09). Its contract was verified
against the live API before code was written against it, which corrected
three specs. `docs/github-app-setup.md` is the runbook if it ever has to be
redone — note it now has a REQUIRED user-authorization step, without which
the install callback fails closed.

**Two env-loading traps, hit for real on 2026-09-09** and documented in
`docs/local-development.md`: a UTF-8 BOM in `.env` makes the documented
`set -a && . ./.env && set +a` fail on line 1 and export nothing; and an
unquoted Windows path loses its backslashes when sourced, so the backend
panics naming a path that is not the one in the file. Also,
docker-compose's `backend` service passes none of the GitHub or Supabase
variables through and would panic on the missing `SUPABASE_WEBHOOK_SECRET`
— run the backend directly.

**Why ISS-008 comes first, verified rather than assumed:** `repositories` is RLS-scoped (000008) and carries the 000009 trigger. The only Go handler touching the database today is `user_orgs.go`, which reads `users`/`organizations`/`organization_memberships` — none of which have RLS. So no Go handler has ever read an RLS-scoped table, and `GET /api/repositories` is the first. Without the request-scoped tenant transaction it returns zero rows with no error.

**Environment note (new in 19-03):** local dev now has two separate Postgres instances — Supabase's (auth only) and docker-compose's on port 5434 (all application tables). They are NOT the same database, which is why 19-03 could not use a Supabase Auth Hook. Runbook: `docs/local-development.md`. The Go backend does not read `.env`; only docker-compose does.

**CI now builds and tests the Go code** (`.github/workflows/backend-ci.yml`, added 2026-09-08). `go build ./...`, `go vet ./...`, and `go test -p 1 ./...` all gate every PR. Before this, nothing in CI had ever invoked a compiler — the isolation check runs a Python script over the diff — which is how `pkg/vectordb` stayed uncompilable from Phase 3 to Phase 19.

The workflow supplies Postgres and Redis service containers for `pkg/auth`'s pre-17-01 helpers. Migrating those onto the testcontainers harness would let both be dropped; tracked in `19-02-SUMMARY.md`.

The full gate is: `go mod download`/`verify`/`tidy -diff`, migrations applied, `go build ./...`, `go vet ./...`, `go test -p 1 ./...`, the harness packages again at default parallelism, and `go test -race` on the concurrency-sensitive packages. **All of them gate** — the `-race` step shipped as `continue-on-error` because it could not be executed on the authoring machine (no cgo), and was promoted once it ran green.

**On ISS-010's regression guard:** the real one is `TestEnsureAppRoleIsConcurrencySafe` in `pkg/testing/isolation`, which releases 16 concurrent callers through a barrier and detected the missing advisory lock 8 times out of 8. The CI step that runs the harness packages at default parallelism is defense in depth only — measured at roughly one detection in eight, so a green result there proves little on its own. Do not replace the test with the step.

**Fleet handoff notes for the worker session:**
- Read the phase `-CONTEXT.md` first for vision context
- Plans lock design decisions inline — don't re-litigate them without cause
- Every commit follows `feedback_commit_convention.md`: one sentence, conventional prefix, no attribution trailers
- Every PR references its plan file in the description
- All work on a feature branch → PR → reviewer session comments → merge after approval
- When a plan's stated approach turns out to be impossible against the real system, revise the PLAN file with a REVISION NOTICE recording what was actually verified, then execute the revised version — do not silently improvise (pattern established in 19-03)

**Fleet handoff notes for the reviewer session:**
- Bootstrap prompt: `.planning/fleet/reviewer-session-prompt.md`
- Isolation-test-coverage hard rule is live as of 17-05

### Roadmap Evolution

- 2026-09-03 — Milestone v1.0 MVP created: 9 phases (17-25); roadmap restructured with milestone groupings; v0.9 disposition table records disposition of original phases 1-16 (some shipped, some superseded, some deferred to v2)
- 2026-09-03 — Product vision reframed: from "smart documentation platform" to "persistent shared memory substrate for parallel AI coding agents"; v1.0 remains RAG-over-code as the foundation, v2 adds GraphRAG + agents + MCP
- 2026-09-03 — Phase 17 planned: 3 sub-plans in ROADMAP expanded to 5 executable plans (17-01 harness, 17-02 endpoint verification, 17-03 DB trigger migration, 17-04 Python harness + workers audit, 17-05 CI gate + docs); design decisions locked inline (testcontainers, PL/pgSQL BEFORE trigger, regex-on-diff scanner)
