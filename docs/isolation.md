# Tenant Isolation

**How this project keeps one organization's data from ever showing up in another organization's response.**

The isolation guarantee has three walls. All three must hold. Every new
endpoint, worker, and background job goes through the same pattern —
this document is the canonical spec.

## Overview: the three walls

**1. Middleware / primitive (application layer).**
On the Go side, `TenantMiddleware` reads the current organization from
the **signature-verified JWT claim** `app_metadata.organization_id` and
stashes it on the request context; handlers pull it out via
`auth.OrgIDKey`. On the Python side, `require_tenant(conn, tenant_id)`
opens a transaction with `SET LOCAL app.current_tenant`, scoping every DB
access inside the block. This is the fast path. Handlers and workers
written correctly never bypass it.

The word "claim" is load-bearing. Until Phase 19-03 this read an
`X-Organization-ID` header, which meant any authenticated user could name
any organization and be given it. Tenant identity now comes from a value
only Supabase can mint, and there is no fallback — a token with no
organization claim is refused with 403 rather than defaulted anywhere.
The one endpoint that changes a caller's organization
(`POST /api/user/select-organization`) validates membership server-side
before writing the new claim; see
[`auth-frontend-contract.md`](auth-frontend-contract.md).

### Which mechanism a Go handler uses

Reading the claim off the context is not enough to make a query safe — the
database also has to be told. Two patterns, and picking the wrong one is
the most likely way to introduce a leak in Phase 20+:

**Touching a table with RLS** — `repositories`, `ingestion_runs`,
`chunks`, `queries`, `retrievals`, `feedback` (migration 000008)? The
handler is constructed with a **`*db.TenantScoper`** and runs every query
inside it:

```go
type RepositoriesHandler struct {
    scoper *db.TenantScoper   // deliberately NOT a *pgxpool.Pool
}

func (h *RepositoriesHandler) List(w http.ResponseWriter, r *http.Request) {
    err := h.scoper.InTenantTx(r.Context(), func(tx pgx.Tx) error {
        // every query on tx runs with app.current_tenant set
    })
}
```

`InTenantTx` takes the tenant from the request context and from nowhere
else. There is no parameter through which a caller can name one — that is
the `X-Organization-ID` vulnerability wearing a different hat.

**Touching only `users`, `organizations`, or `organization_memberships`?**
Use the pool and scope by the caller's `sub`. Those tables have no RLS,
and `pkg/api/handlers/user_orgs.go` is the worked example. Forcing them
through a tenant transaction would be wrong twice: it would require an
organization claim that a user recovering their account does not have, and
it would imply protection those tables do not carry.

**Why the handler holds a scoper instead of a pool.** An unscoped query
against an RLS table does not fail in a way you can rely on noticing. It
returns **zero rows with no error** on a connection that has never been
scoped, and **fails with SQLSTATE 22P02** on one that has — because a
committed `SET LOCAL` leaves the setting as an empty string, and
`""::uuid` is invalid. Same query, same pool, different outcome depending
on which connection you get. A handler that has no pool cannot make that
mistake at all. See ISS-013, and
`pkg/db/tenant_isolation_test.go`, which demonstrates both halves.

**2. DB trigger (database layer).**
Migration 000009 attaches `assert_tenant_scoped()` as a
`BEFORE INSERT/UPDATE/DELETE` trigger to every tenant-scoped table
(repositories, ingestion_runs, chunks, queries, retrievals, feedback).
If a caller writes without setting `app.current_tenant`, Postgres
refuses the write with SQLSTATE `42501` and a message identifying the
operation and table. This catches raw SQL, debug scripts, and any future
worker written before it discovers the pattern.

**3. Isolation tests + CI gate.**
Every mutation endpoint has a test that exercises the full router or
worker path against two isolated tenants, asserting cross-tenant reads
return empty and cross-tenant writes are refused. The
`isolation-check` GitHub Action scans every PR and fails if a new
mutation endpoint arrives without a matching test.

The three walls are independent. If the middleware is misconfigured,
the trigger catches it. If the trigger is dropped by a future
migration, the tests catch it. If a test is faked, the reviewer catches
it (see `.planning/fleet/reviewer-session-prompt.md`, hard-check
rules).

## Writing a tenant-scoped handler (Go)

Handlers under `/api` get `auth.OrgIDKey` set on the request context by
the middleware chain in `pkg/api/router.go`. Read it, pass it to the
RAG client and any downstream call:

```go
func (h *SearchHandler) Search(w http.ResponseWriter, r *http.Request) {
    var req SearchRequestBody
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        render.Render(w, r, ErrInvalidRequest(err))
        return
    }
    // ...validation...

    ctx := r.Context()
    orgID, ok := ctx.Value(auth.OrgIDKey).(string)
    if !ok || orgID == "" {
        render.Render(w, r, ErrInternal(errors.New(
            "tenant context missing; middleware chain misconfigured")))
        return
    }

    result, err := h.ragClient.Search(ctx, client.SearchRequest{
        Query:          req.Query,
        RepositoryID:   req.RepositoryID,
        OrganizationID: orgID, // MUST forward
        TopK:           req.TopK,
    })
    // ...respond...
}
```

**If you write raw SQL outside the middleware chain** (a background job,
a CLI script, a Celery-style worker), wrap it in `TenantScope`:

```go
tx, err := isolation.TenantScope(ctx, pool, orgID)
if err != nil {
    return err
}
defer tx.Rollback(ctx)
// ...tx.Exec / tx.Query...
return tx.Commit(ctx)
```

`TenantScope` (from `pkg/testing/isolation` today, will move to a
production primitive under ISS-008) validates the org id, begins a
transaction, sets `SET LOCAL app.current_tenant`, and hands you the
`pgx.Tx`. Every write inside is caught by RLS + the trigger.

## Writing a tenant-scoped worker (Python)

Every function that reads or writes a tenant-scoped table takes an
`organization_id` argument and calls `require_tenant`:

```python
from workers.db import require_tenant

def insert_chunks(conn, organization_id, chunks, repository_id, ingestion_run_id):
    """Batch-insert chunks under the caller's tenant scope."""
    with require_tenant(conn, organization_id) as cur:
        cur.executemany(
            "INSERT INTO chunks (id, ingestion_run_id, repository_id, "
            "file_path, start_line, end_line, content, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(...) for c in chunks],
        )
```

Read paths look the same — `require_tenant` yields a cursor bound to a
tenant-scoped transaction; RLS silently filters `SELECT` results to
this tenant. For a dict-style cursor:

```python
from psycopg2.extras import RealDictCursor

with require_tenant(conn, organization_id, cursor_factory=RealDictCursor) as cur:
    cur.execute("SELECT * FROM chunks WHERE repository_id = %s", (repo_id,))
    rows = cur.fetchall()
```

**Preconditions:** `conn` must be idle (no in-progress transaction) on
entry. The primitive asserts this and raises `RuntimeError` otherwise.
See `workers/db/tenant.py` for the full docstring.

## Writing an isolation test

Every mutation endpoint needs one. The pattern is the same on both
sides: build two independent tenants, drive real traffic through the
middleware chain, assert cross-tenant paths return empty (or 4xx), same
tenant path returns real data.

### Go

```go
func TestFooIsolation(t *testing.T) {
    pool := isolation.SetupTestDB(t)

    isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
        router := api.NewRouterWithValidator(pool, ragClient,
            testjwt.NewValidator(), api.Config{LogLevel: slog.LevelWarn})
        server := httptest.NewServer(router)
        t.Cleanup(server.Close)

        orgAToken := testjwt.Sign(orgA.OwnerID, orgA.ID, "owner")
        orgBToken := testjwt.Sign(orgB.OwnerID, orgB.ID, "owner")

        t.Run("OrgB_cannot_read_OrgA_data", func(t *testing.T) {
            body := fmt.Sprintf(`{"query":"x","repository_id":%q}`, orgA.RepoID)
            status, resp := doPost(t, server.URL+"/api/foo", orgBToken, orgB.ID, body)
            require.Equal(t, http.StatusOK, status)
            require.Empty(t, resp.Results, "OrgB must not see OrgA's data")
        })
    })
}
```

Key elements:
- `SetupTestDB` (from `pkg/testing/isolation`) gets you a testcontainers
  Postgres with migrations applied and the app role set up.
- `WithTwoOrgs` builds two full tenant scaffolds (owner/admin/member,
  project, repo) — never fabricate tenant UUIDs in isolation tests; use
  the fixture.
- `NewRouterWithValidator` + `httptest.NewServer` exercises the real
  middleware chain. Do NOT call the handler function directly — you'd
  miss the auth + tenant middleware entirely.

### Python

```python
def test_writer_isolation(dsn, db_conn, with_two_orgs):
    org_a, org_b = with_two_orgs

    # Write under org A.
    writer = PostgresWriter(dsn)
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()
    run_id = writer.create_ingestion_run(
        organization_id=org_a.id,
        repository_id=org_a.repo_id,
    )
    writer.insert_chunks(
        organization_id=org_a.id,
        chunks=[Chunk(content="orange marmalade", ...)],
        ingestion_run_id=run_id,
        repository_id=org_a.repo_id,
    )
    writer.close()

    # Read under org B — must see nothing.
    with require_tenant(db_conn, org_b.id) as cur:
        cur.execute("SELECT COUNT(*) FROM chunks WHERE content = %s",
                    ("orange marmalade",))
        assert cur.fetchone()[0] == 0
```

Key elements:
- `with_two_orgs` (from `tests/isolation/conftest.py`) provides the
  same two-tenant scaffold as the Go side.
- Every DB access — writer, reader, cleanup — goes through
  `require_tenant`. If you find yourself opening a bare psycopg2
  cursor in a test, ask why.

## CI gate

`scripts/ci/check-isolation-tests.py` scans each PR's diff for added
mutation endpoints (`.Post/Put/Patch/Delete(...)` in Go,
`@router.post/put/patch/delete(...)` in Python) and looks for a
matching isolation test file that references the endpoint path. The
`isolation-check` GitHub Action runs it on every PR to `main`.

**To make a failing check pass:** add a test in
`*_isolation_test.go` (Go) or `test_*_isolation.py` /
`*_isolation_test.py` (Python) that references the endpoint path
string. The scanner does a substring match on the path.

**To intentionally skip** (rare — only legitimately non-tenant-scoped
endpoints like `/health` and webhooks): add an inline marker on the
route registration line:

```go
r.Post("/health", healthHandler) // @skip-isolation-test: no tenant data
```

```python
@router.post("/health")  # @skip-isolation-test: no tenant data
```

The reason must be non-empty. `// @skip-isolation-test:` on its own
does not unlock the skip. The reviewer verifies the reason is
substantive (see reviewer prompt hard-check rules).

Run locally:

```bash
python scripts/ci/check-isolation-tests.py --base-ref main --verbose
```

The scanner's own test suite lives at
`scripts/ci/test_check_isolation.py` — see `scripts/ci/README.md`.

## Adding a new tenant-scoped table

When a phase adds a new table that carries per-tenant data:

**1. Attach the trigger.** In your migration:

```sql
CREATE TRIGGER trg_assert_tenant BEFORE INSERT OR UPDATE OR DELETE
  ON <new_table> FOR EACH ROW EXECUTE FUNCTION assert_tenant_scoped();
```

The function was created in migration 000009 and is reusable — no
per-table variant needed.

**2. Add RLS.** Follow the pattern in migration 000008:

```sql
ALTER TABLE <new_table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <new_table> FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <new_table>
  FOR ALL USING (
    <tenant-scoping predicate against
     current_setting('app.current_tenant', true)::uuid>
  );
```

**3. Register the table with the harness.** Add it to the table-driven
list in `services/backend/pkg/testing/isolation/db_assertion_test.go`
(`protectedTables` slice). CI will then verify the trigger fires on
INSERT/UPDATE/DELETE for the new table on every subsequent run.

**4. Extend the fixture.** If `WithTwoOrgs` / `with_two_orgs` needs to
populate a starter row for the new table, add it to `fixtures.go` /
`fixtures.py`. Otherwise the per-test setup handles it.

Migration 000009 header carries a shorter version of this recipe so
future contributors see it before they need it.

## Troubleshooting

**Error: `tenant isolation violated: app.current_tenant must be set for INSERT on chunks`**
You wrote to a tenant-scoped table without wrapping the call in
`TenantScope` (Go) or `require_tenant` (Python). Find the write path
and add the wrapper.

**SELECT returns empty in dev when there's clearly data in the table.**
Your session doesn't have `app.current_tenant` set, or it's set to a
UUID that no rows match. RLS silently filters — no error, just no
rows. From `psql`:

```sql
SELECT current_setting('app.current_tenant', true);
SET app.current_tenant = '<your-org-uuid>';
SELECT COUNT(*) FROM chunks;
```

Beware: once you set the GUC in a session and commit, later reads on
the same connection see the empty-string GUC even after a rollback —
Postgres does not restore it to NULL. Reset by disconnecting.

**Isolation test passes but data leaks in staging.**
The test probably calls the handler function directly instead of going
through the router. Convert it to `httptest.NewServer(router)` (Go) or
FastAPI `TestClient` (Python) so the middleware chain runs.

**CI gate flags an endpoint that shouldn't need a test.**
If the endpoint is truly non-tenant-scoped (health, webhooks, static
metadata), add the `@skip-isolation-test:` marker with a substantive
reason. If you're not sure whether an endpoint is tenant-scoped, it
probably is — add the test.

**Cross-language sanity check.**
Migration 000009's trigger fires on writes from any language, not just
Go. If a Python worker suddenly reports SQLSTATE 42501, it's the same
error surface as a Go handler that skipped `TenantScope`. Fix the same
way: wrap the DB access.

## References

- `services/backend/pkg/testing/isolation/` — Go harness primitives
  (`SetupTestDB`, `WithTwoOrgs`, `TenantScope`, `AssertNoCrossTenantLeak`)
- `services/workers/workers/db/tenant.py` — Python `require_tenant`
- `services/workers/tests/isolation/` — Python harness fixtures + self-tests
- `services/backend/migrations/000008_enable_rls_policies.up.sql` —
  RLS policies (the silent filter)
- `services/backend/migrations/000009_tenant_assertion.up.sql` —
  the trigger (the loud refuse)
- `scripts/ci/check-isolation-tests.py` — the CI scanner
- `scripts/ci/README.md` — scanner operational reference
- `.planning/fleet/reviewer-session-prompt.md` — reviewer's hard-check rule
- [`auth-frontend-contract.md`](auth-frontend-contract.md) — how a client
  reads and changes its active organization. Relevant here because the
  tenant that wall 1 scopes to comes from the JWT claim that document
  describes, and nothing on the wire can override it.

## Adding a new endpoint framework

If a future phase adds a routing framework the scanner doesn't
recognize (e.g., an MCP server, a Kafka consumer, gRPC handlers), the
extension point is `ENDPOINT_PATTERNS` in
`scripts/ci/check-isolation-tests.py`. Add one regex per line, add a
scenario to `scripts/ci/test_check_isolation.py`, done.
