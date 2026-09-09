# 20-01 Design: request-scoped tenant transactions

**Decision: Option B — an explicit scoping helper — with the pool kept out
of tenant-scoped handlers entirely, so bypassing it requires deliberately
reaching for something a handler does not have.**

## What ISS-008 asked

Two options were recorded in 17-02:

**A. Middleware opens the transaction.** `TenantMiddleware` begins a tx,
sets `app.current_tenant`, stashes it on the request context, commits on
2xx and rolls back otherwise.

**B. Handlers call a scoped helper.** Each handler that needs the database
opens its own transaction.

## Why B

**A holds a database connection for every authenticated request, including
the ones that never touch the database.** Today that is most of them:
`/api/search` and `/api/chat/stream` proxy to Python and issue no queries
at all. Under A they would each pin a pooled connection and an open
transaction for the life of the request — and `/api/chat/stream` is an SSE
endpoint whose life is measured in minutes. A pool of 10 connections and
four users reading a long stream is an outage.

That alone is close to disqualifying, and the workaround makes it worse: A
needs an opt-out list of routes that skip the transaction, which is a
second thing to forget, failing in the direction of "held a connection for
an hour" rather than "returned an error".

**A couples transaction semantics to HTTP status.** "Commit on 2xx" means
a handler that writes and then fails to marshal its response silently
commits, and a handler that writes and returns 201 through an error path
nobody expected does too. Transaction boundaries should be visible where
the work happens, not inferred from a status code set later by a different
layer.

**B's weakness is real and addressable.** A handler can forget to use the
helper.

I assumed the failure mode was "writes fail loudly with SQLSTATE 42501,
reads return zero rows silently". **Measured during implementation, and it
is worse than that.**

The RLS policies compare against
`current_setting('app.current_tenant', true)::uuid`. The `true` means
"missing is OK" — but Postgres has *two* kinds of missing on a pooled
connection:

| Connection state | `current_setting(...)` | Unscoped read |
|---|---|---|
| never scoped | `NULL` | 0 rows, **no error** |
| scoped once, then committed | `""` | **ERROR 22P02** |

A committed `SET LOCAL` leaves the GUC as an empty string on that backend
for the rest of its life; `RESET` and `SET TO DEFAULT` do not clear it.
That was established in 17-02 and re-confirmed here with a direct probe.

So an unscoped read is **silently empty OR a 500, depending on which
pooled connection it happens to get and what that connection did
earlier.** That is worse than either outcome on its own: it passes in a
fresh test process and fails intermittently in production once connections
have been reused.

This strengthens the case for B-with-no-pool rather than weakening it. A
failure mode that is merely silent can be caught by a careful test. One
that is silent *or* loud depending on connection history cannot be tested
into submission — it has to be made unreachable.

Both halves are pinned by
`TestUnscopedAccess_BehaviourDependsOnConnectionHistory`, which holds a
connection and demonstrates each in turn rather than describing them.

Filed as **ISS-013**, deliberately not fixed here: making the policies
deterministic means a migration across all six tenant-scoped tables, and
choosing between "always silent" (`NULLIF(..., '')`) and "always loud"
(drop the `missing_ok` flag) is a decision worth taking on its own terms
rather than inside a plan about something else. My inclination is "always
loud", since with `TenantScoper` an unscoped query is by definition a bug —
but that is a judgement about operational risk, not a detail.

## Closing B's hole: don't give handlers the pool

The mitigation in the plan was "B plus a lint-or-test-level guard". A
static check is fragile — it has to recognise every way a handler might
reach a pool.

Instead, make the pool unreachable. A tenant-scoped handler holds a
`*db.TenantScoper`, not a `*pgxpool.Pool`:

```go
type RepositoriesHandler struct {
    scoper *db.TenantScoper   // not *pgxpool.Pool
}

func (h *RepositoriesHandler) List(w http.ResponseWriter, r *http.Request) {
    err := h.scoper.InTenantTx(r.Context(), func(tx pgx.Tx) error {
        // every query here runs with app.current_tenant set
    })
}
```

`TenantScoper` exposes exactly one method, and that method always sets the
tenant. There is no unscoped path through it. A handler that wants to
bypass this has to be given a pool by whoever constructs it — a visible,
deliberate act in `router.go`, not an omission inside a 200-line handler.

This is the same principle as 19-03 deleting the `X-Organization-ID`
header rather than deprecating it: remove the wrong path instead of
documenting that people should not take it.

## Where the tenant comes from

`auth.OrgIDKey` on the request context, and nowhere else.

`InTenantTx` takes a `context.Context`, not a tenant id. There is
deliberately **no parameter through which a caller can supply a tenant** —
that is the header vulnerability 19-03 removed, wearing a different hat.
The value on that context came from a Supabase-signed JWT claim, was
validated as a UUID by `ExtractOrganizationID` (19-04), and is
canonicalized (19-04 round 3).

It is re-validated at the interpolation site anyway. `SET LOCAL` cannot
take a bind parameter, so the id is concatenated into SQL — the one place
in this codebase where that happens. It should be defensible reading that
function alone, not by tracing three layers up to a guarantee someone
could later weaken.

## Two patterns will coexist, and that is correct

`pkg/api/handlers/user_orgs.go` (19-04) holds a `*pgxpool.Pool` and will
keep it. It reads `users`, `organizations`, and `organization_memberships`
— **none of which have RLS** — and it scopes by the caller's `sub`, not by
tenant. Forcing it through a tenant transaction would be wrong twice: it
would require an organization claim that a claim-less user recovering
their account does not have, and it would imply RLS protection that those
tables do not carry.

The rule to write in `docs/isolation.md`:

> A handler touching a table listed in migration 000008 uses
> `TenantScoper`. A handler touching only `users`, `organizations`, or
> `organization_memberships` uses the pool and scopes by the caller.

## What this does not solve

**Multi-statement work spanning several handlers** is not a transaction.
Each `InTenantTx` call is its own. Nothing in Phase 20 needs otherwise, and
a shared cross-handler transaction is exactly the connection-lifetime
problem that ruled out Option A.

**A handler can still open a transaction and use it wrongly inside the
callback** — the scope guarantees `app.current_tenant` is set, not that the
queries are sensible. RLS and the trigger remain the enforcement; this is
the fast path, wall 1 of the three in `docs/isolation.md`.
