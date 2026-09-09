---
phase: 20-repository-integration
plan: 03
subsystem: api

requires:
  - phase: 20-01
    provides: db.TenantScoper — this is its first production consumer
  - phase: 20-02
    provides: schema, github.Client, the default-project invariant
provides:
  - "GET/POST /api/repositories, GET/DELETE /api/repositories/{id} — tenant-scoped, cursor-paginated"
  - "docs/api-repositories.md — the contract Phase 23 builds against"
affects: [20-05 (the webhook writes the same rows), 21 (sync_state is the queue's input), 23 (frontend)]

tech-stack:
  added: []
  patterns:
    - "Authorization is checked before service availability — the reverse order turns a 503 into an enumeration oracle"
    - "Cursor pagination on (created_at, id) wherever a background process can insert into the set being paged"
    - "A cross-tenant write is asserted on the resulting ROW, never on the status code — RLS makes it match nothing rather than error"

key-files:
  created:
    - services/backend/pkg/api/handlers/repositories.go
    - services/backend/pkg/api/handlers/repositories_isolation_test.go
    - docs/api-repositories.md
  modified:
    - services/backend/pkg/api/router.go

key-decisions:
  - "DELETE genuinely deletes, cascading to ingestion_runs, chunks and retrievals. A disconnected repository whose contents stayed searchable is the wrong answer for this product, and worse if it was disconnected because it should not have been indexed. The response reports the counts so a client can say what was lost."
  - "POST takes github_repo_id, not a git URL. GitHub's numeric id is stable across renames and transfers."
  - "404 covers 'not yours', 'does not exist' and 'not visible to that installation', identically — otherwise the endpoint is an existence oracle for other tenants' ids."
  - "Connect is split across two transactions with the GitHub call between them, rather than holding a pooled connection across a network round-trip. The unique index makes a duplicate connect harmless."
  - "Re-connecting an existing repository refreshes metadata and returns 201 rather than erroring — retry-safe."

issues-created: []
issues-closed: []

duration: ~1 hour
completed: 2026-09-08
---

# Phase 20 Plan 03: repositories CRUD

**Four endpoints, tenant-scoped, cursor-paginated. The first production consumer of `db.TenantScoper`.**

## The bug the tests caught

Scenario 4 — orgA connecting through orgB's installation — expected 404 and got **503**.

The handler checked "is the GitHub App configured?" *before* resolving the installation. With credentials missing, a caller naming a **real** installation id belonging to another tenant got 503, and a made-up one got 404. The difference tells them which ids exist.

So availability is now checked after authorization. The general form: **a check that can distinguish valid from invalid inputs must not run before the check that decides whether the caller may ask at all.** Cheap to get wrong, because refusing early reads as defensive.

## Two guards on the cross-tenant connect

`POST /api/repositories` resolves the installation with a scoped read inside a tenant transaction, so another tenant's installation is simply invisible and the caller gets 404. Migration 000010's `trg_assert_installation_tenant` is the backstop underneath.

Two layers because they fail differently: the scoped read gives a clean 404 the client can act on, and the trigger catches anything that reaches the database by another route. The trigger alone would surface as a 500.

## What the endpoints do

`GET /api/repositories` — cursor-paginated on `(created_at, id)`, not OFFSET. **The set changes while you page through it**: 20-05's webhook inserts repositories with no user action, and offset pagination skips and duplicates rows when that happens.

`POST /api/repositories` — takes `github_repo_id` and `installation_id`. Verifies the installation is the caller's, asks GitHub whether that installation can actually see the repository, then persists what GitHub reported. Re-connecting refreshes metadata and returns 201, so a retry is harmless.

The GitHub call sits **between** two transactions rather than inside one. A network round-trip inside a transaction pins a pooled connection for its duration; the cost is that the two halves are not atomic, and the unique index on `(installation_id, github_repo_id)` is what makes that safe.

`DELETE /api/repositories/{id}` — deletes, cascading to everything ingested. The response reports how many chunks and ingestion runs went with it, so a client can show what was lost instead of a bare 204.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...` | clean |
| `go test -p 1 ./...` | all pass |
| `TestRepositoriesIsolation` | 6/6 |
| CI isolation scanner | PASS (POST and DELETE are new mutation endpoints) |

### Mutation testing

| Mutation | Result |
|---|---|
| DELETE ignores `RowsAffected` (reports success for a cross-tenant delete) | scenario 3 fails, only 3 |
| Connect skips the scoped installation check | scenario 4 fails, only 4 |
| Get returns 500 rather than 404 for another tenant's repository | scenario 2 fails, only 2 |

Scenarios 3 and 4 assert on the resulting **row**, not the status code. Under RLS a cross-tenant write matches nothing rather than erroring, so a status-only assertion passes against a handler that did the wrong thing — the trap this project has fallen into before.

## Notes for what comes next

- **20-05 writes these same rows** from webhooks. Its inserts must go through a tenant scope too; the webhook has no request context, so it resolves the tenant from the installation and builds one.
- **`sync_state` is Phase 21's input.** Everything connected here starts `pending` and nothing moves it yet.
- **Repositories connected before this API may not be in the default project.** `docs/api-repositories.md` says so; nothing should assume one project per organization.
- **`sync_state` has no CHECK constraint** (carried from 20-02's review). A typo would sit in the partial index silently. Worth a small migration in 20-05.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-08*
