"""Tenant-scoping primitive for worker DB access.

`require_tenant(conn, tenant_id)` is the sole entry point workers use to
touch tenant-scoped Postgres tables. It sets `app.current_tenant` for the
duration of a transaction, so:

1. RLS policies from migration 000008 silently filter SELECTs to this org.
2. The assert_tenant_scoped trigger from migration 000009 loudly refuses
   INSERT/UPDATE/DELETE on tenant-scoped tables when the caller forgot to
   set it.

Cross-language contract: this is the Python counterpart of Go's
`pkg/testing/isolation.TenantScope` (defined for tests) and the future
production Go request-scoped tx primitive (ISS-008). Any Python DB access
that reads or writes chunks, ingestion_runs, queries, retrievals, or
feedback MUST go through this.

v2 breadcrumb: when v2 introduces graph traversal helpers, the same
primitive wraps those DB calls too — the trigger and RLS coverage will
extend to the new tenant-scoped tables (see migration 000009 header for
the extension recipe), and require_tenant keeps working unchanged.
"""

from contextlib import contextmanager
from typing import Any, Iterator, Optional
from uuid import UUID

from psycopg2.extensions import TRANSACTION_STATUS_IDLE


@contextmanager
def require_tenant(
    conn: Any,
    tenant_id: Any,
    cursor_factory: Optional[Any] = None,
) -> Iterator[Any]:
    """Yield a psycopg2 cursor inside a tenant-scoped transaction.

    On successful exit the transaction commits; on any exception it rolls
    back and re-raises. The transaction's `app.current_tenant` is
    discarded automatically by Postgres at end-of-tx.

    Usage:

        with require_tenant(conn, org_id) as cur:
            cur.execute("INSERT INTO chunks (...) VALUES (...)", (...,))

        with require_tenant(conn, org_id, cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT ...")
            for row in cur.fetchall():
                ...

    Preconditions:
        `conn` MUST have no in-progress transaction on entry. psycopg2's
        `with conn:` idiom does not open a nested transaction — it just
        uses whatever transaction is already open — so calling
        `require_tenant` mid-transaction would silently commit (or
        rollback) the caller's outer work at the inner scope's boundary,
        and discard whatever tenant state the outer scope had set. The
        precondition is asserted at entry (RuntimeError) so a caller
        that violates it fails loudly rather than corrupting state.

    Args:
        conn: An open, idle psycopg2 connection. Its autocommit state is
              temporarily forced to False for the duration of the block
              and restored on exit.
        tenant_id: The organization id to scope this transaction to.
                   May be a `uuid.UUID` or a string; strings are validated
                   as UUIDs before interpolation because Postgres does
                   not accept bind parameters for GUC values in `SET`.
        cursor_factory: Optional psycopg2 cursor factory (e.g.
                        `RealDictCursor`). Passed through to
                        `conn.cursor(cursor_factory=...)`.

    Yields:
        A psycopg2 cursor bound to the tenant-scoped transaction.

    Raises:
        ValueError: If `tenant_id` is not a valid UUID.
        RuntimeError: If `conn` is already inside a transaction on entry.
    """
    tenant_str = str(tenant_id)
    # Validate — Postgres does not accept bind params for SET LOCAL, so
    # the id is interpolated after validation. This mirrors Go's
    # TenantScope pattern (services/backend/pkg/testing/isolation/tenants.go).
    UUID(tenant_str)

    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE:
        raise RuntimeError(
            "require_tenant must be entered on an idle connection; "
            "caller has an in-progress transaction that would be silently "
            "committed by psycopg2's `with conn:` idiom. Commit or rollback "
            "the outer transaction before opening a tenant scope."
        )

    # Save + toggle autocommit inside the try so a signal (KeyboardInterrupt)
    # between saving and toggling still enters the finally and restores the
    # original value. A signal that fires between the save and the toggle
    # itself would leave prev_autocommit unbound; the try/except NameError
    # below no-ops the restore in that specific race.
    try:
        prev_autocommit = conn.autocommit
        conn.autocommit = False
        with conn:  # begins tx, commits on clean exit, rollbacks on raise
            # SET LOCAL scopes to the outer transaction rather than to any
            # particular cursor, so a throwaway setup cursor is fine — the
            # yielded cursor below sees the GUC because it shares the tx.
            with conn.cursor() as setup_cur:
                setup_cur.execute(f"SET LOCAL app.current_tenant = '{tenant_str}'")
            if cursor_factory is None:
                with conn.cursor() as cur:
                    yield cur
            else:
                with conn.cursor(cursor_factory=cursor_factory) as cur:
                    yield cur
    finally:
        try:
            conn.autocommit = prev_autocommit
        except NameError:
            # Signal fired before `prev_autocommit = conn.autocommit`
            # executed; nothing to restore.
            pass
