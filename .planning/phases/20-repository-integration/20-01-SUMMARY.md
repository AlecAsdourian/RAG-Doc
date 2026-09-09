---
phase: 20-repository-integration
plan: 01
subsystem: db

requires:
  - phase: 19-03
    provides: auth.OrgIDKey populated from a Supabase-signed claim
  - phase: 19-04
    provides: ExtractOrganizationID returns a validated, canonical UUID
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs
  - phase: 17-03
    provides: the 000009 assert_tenant_scoped trigger
provides:
  - "db.TenantScoper — services/backend/pkg/db. THE way a Go handler reads or writes an RLS-scoped table. 20-03 onward depend on it."
  - "db.ErrNoTenant — returned when a route is mounted outside TenantMiddleware"
  - "Executable documentation of what happens when you bypass it (pkg/db/tenant_isolation_test.go)"
affects: [20-03 (repositories CRUD is its first consumer), 20-05, 21+, and every future handler touching a table in migration 000008]

tech-stack:
  added: []
  patterns:
    - "A tenant-scoped handler is constructed with a *db.TenantScoper, never a *pgxpool.Pool. Removing the wrong path beats documenting that nobody should take it."
    - "The tenant comes from the request context and from nowhere else — no parameter through which a caller can name one."
    - "A value interpolated into SQL is re-validated at the interpolation site, not trusted from upstream."

key-files:
  created:
    - services/backend/pkg/db/tenant.go
    - services/backend/pkg/db/tenant_isolation_test.go
    - .planning/phases/20-repository-integration/20-01-DESIGN.md
    - .planning/phases/20-repository-integration/20-01-SUMMARY.md
  modified:
    - services/backend/pkg/auth/middleware.go (the reserved db parameter is GONE, not ignored)
    - services/backend/pkg/api/router.go (constructs the scoper; documents why user_orgs keeps the pool)
    - docs/isolation.md (wall 1 now says which mechanism a handler must use)
    - .planning/ISSUES.md

key-decisions:
  - "Option 2 (explicit scoping helper), not option 1 (middleware opens the transaction). Option 1 would hold a pooled connection and an open transaction for the life of every authenticated request — including /api/chat/stream, an SSE endpoint — and couples commit to HTTP status."
  - "Handlers hold a *TenantScoper, not a pool. This is what closes option 2's weakness: forgetting is not an omission a handler can make, because it has nothing to forget with."
  - "TenantMiddleware's unused pool parameter was removed rather than left. An unused pool argument is an invitation to wire something into the wrong layer."
  - "ISS-013 filed, not fixed. Making the RLS policies deterministic is a migration across six tables and a judgement call about silent-vs-loud; it does not belong inside this plan."

issues-created:
  - ISS-013 (unscoped RLS access behaves differently depending on connection history)

issues-closed:
  - ISS-008

duration: ~1.5 hours
completed: 2026-09-08
---

# Phase 20 Plan 01: request-scoped tenant transactions

**Go handlers can now read and write RLS-scoped tables. Closes ISS-008, open since 17-02, and the thing every remaining plan in Phase 20 stands on.**

## Why this went first

ISS-008 was filed with the note "must resolve before any Phase 20+ handler reads a tenant-scoped table directly from Go." Verified rather than assumed:

- `repositories`, `ingestion_runs`, `chunks`, `queries`, `retrievals`, `feedback` have RLS (000008) and the 000009 trigger.
- The only Go handler that touched the database was `user_orgs.go`, reading `users` / `organizations` / `organization_memberships` — none RLS-scoped.

**No Go handler had ever read an RLS-scoped table.** `GET /api/repositories` in 20-03 is the first, and without this it would have returned an empty list with no error.

## The design, briefly

Option 2 from ISS-008 — an explicit helper — with the pool kept out of tenant-scoped handlers entirely:

```go
type RepositoriesHandler struct {
    scoper *db.TenantScoper   // not *pgxpool.Pool
}
```

`TenantScoper` exposes one method, and it always sets the tenant. There is no unscoped path through the type. Option 1 (middleware opens the transaction) was rejected because it would pin a connection and an open transaction for the life of every authenticated request — `/api/chat/stream` is an SSE endpoint measured in minutes — and because "commit on 2xx" couples transaction boundaries to a status code set by a different layer.

Full comparison in `20-01-DESIGN.md`.

## What implementation found that the design got wrong

I wrote that bypassing the scoper fails one way for writes and another for reads: writes loud (42501), reads silent (zero rows). **Measured, it is worse than that.**

The RLS policies use `current_setting('app.current_tenant', true)::uuid`. Postgres has two kinds of missing on a pooled connection:

| Connection state | `current_setting(...)` | Unscoped read |
|---|---|---|
| never scoped | `NULL` | 0 rows, **no error** |
| scoped once, then committed | `""` | **ERROR 22P02** |

A committed `SET LOCAL` leaves the GUC as an empty string on that backend for good — `RESET` and `SET TO DEFAULT` don't clear it, which 17-02 established and a direct probe re-confirmed here.

So an unscoped read is **silently empty or a 500, depending on which pooled connection it gets and what that connection did earlier.** It passes in a fresh test process and fails intermittently in production once connections have been reused.

That strengthens the design rather than weakening it. A failure mode that is merely silent can be caught by a careful reviewer; one that is silent *or* loud depending on connection history has to be made unreachable, which is exactly what not handing out the pool does.

Filed as **ISS-013**. Not fixed here — it is a migration across six tables plus a judgement about whether unscoped access should be always-silent or always-loud, and that decision deserves its own airing.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...` | clean |
| `go test -p 1 ./...` | all pass, including the new `pkg/db` |
| CI isolation scanner | PASS |
| Mutation: remove the `SET LOCAL` | **4 tests fail** — the three scoping tests plus the connection-history test |

`pkg/db/tenant_isolation_test.go` has two halves. The first proves scoping works: reads and writes see only the caller's tenant, a failed callback rolls back, a request with no tenant is refused before the callback runs, and a non-UUID tenant never reaches the interpolation. The second half proves what happens when you *don't* use it, holding a single connection to demonstrate both the silent case and the 22P02 case in turn rather than describing them.

The second half is there because someone will eventually ask why they can't just use the pool. That test is the answer.

## Deviations from plan

**Task 1 recommended "Option B plus a lint-or-test-level guard".** The guard became a type-level one instead: a static check has to recognise every way a handler might reach a pool, whereas a handler that is never given one has nothing to check. Cheaper and not fragile.

**`TenantMiddleware`'s signature changed** — it no longer takes a `*pgxpool.Pool`. The plan said to remove the `_ = db` placeholder; leaving the parameter would have left the next reader thinking the transaction belongs in the middleware, which is the option this plan rejected.

## Next

- **20-02** — schema and GitHub client. Its contract verification is already done (2026-09-08, recorded in the plan).
- **20-03** — the first consumer of `TenantScoper`. Constructing `RepositoriesHandler` with the scoper rather than `dbpool` is the whole point; `router.go` currently has `_ = tenantScoper` marking the spot.
- **ISS-013** wants a decision before Phase 21 adds more tenant-scoped tables — each one written against the current policy shape inherits the same nondeterminism.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-08*
