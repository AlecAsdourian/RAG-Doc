"""Python isolation-test harness.

Mirror of Go's services/backend/pkg/testing/isolation package. Same
primitives, same semantics: an ephemeral Postgres container per pytest
session with all migrations applied, a two-org fixture (with_two_orgs),
and an assert_no_cross_tenant_leak helper. Any Python test that verifies
tenant boundaries lives here.

The tenant primitive itself (require_tenant) lives in workers/db/tenant.py
so production code can import it without pulling test infrastructure.
"""
