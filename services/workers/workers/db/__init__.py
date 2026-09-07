"""Database access primitives for workers.

Every worker that touches a tenant-scoped Postgres table must obtain its
cursor via `require_tenant`. See workers.db.tenant.
"""

from workers.db.tenant import require_tenant

__all__ = ["require_tenant"]
