# Phase 20: Repository Integration Backend — Context

## Objective

Let an organization connect GitHub repositories and manage them through
our API. This is the first phase where the product does something with
someone else's code, and the first where a Go handler reads a
tenant-scoped table directly — which turns a deferred issue into a
blocker (see below).

By the end of Phase 20:

- A GitHub App exists, is registered, and its contract is **verified
  against the live API** rather than assumed.
- An organization can install the App and have that installation linked
  to exactly one of our organizations.
- `repositories` CRUD works end-to-end, tenant-scoped, with the
  isolation tests the 17-05 gate requires.
- GitHub webhooks arrive, are signature-verified, and are turned into
  work items — the queue that consumes them ships in Phase 21.

## The blocker this phase inherits

**ISS-008 must be resolved first, and it is genuinely blocking.**

The issue was filed in 17-02 with the note "must resolve before any
Phase 20+ handler reads a tenant-scoped table directly from Go."
Verified 2026-09-08 that this is now the case:

- `repositories` is RLS-scoped (migration 000008) and carries the
  `assert_tenant_scoped` trigger (000009).
- The only Go handler that touches the database today is `user_orgs.go`,
  and it reads `users` / `organizations` / `organization_memberships` —
  none of which have RLS.

So no Go handler has ever read an RLS-scoped table. `GET /api/repositories`
is the first. Without a request-scoped tenant transaction it will either
return zero rows (RLS filtering everything, because
`app.current_tenant` was never set) or fail outright on write with
SQLSTATE 42501. `TenantMiddleware` still carries `_ = db` reserved for
exactly this work.

This is why the phase is five plans rather than the roadmap's four.

## Locked decisions

Decided by the user 2026-09-08, before planning:

**1. Register the real GitHub App first, then build.**
The App gets created and its contracts verified — installation flow,
webhook payload shapes, signature format, token exchange — before the
schema is designed around them. This is the ISS-002 pattern, and it is
here because of what it caught in 19-03: a plan built entirely on a
Supabase Auth Hook that turned out to be architecturally impossible,
found only because the contract was checked against the live project
first. Repeating that mistake against GitHub would be more expensive,
not less.

**2. GitHub only.** No GitLab. The schema names GitHub explicitly rather
than pretending to be provider-agnostic — a fake abstraction over one
implementation is worse than an honest one, and the migration to add a
provider column later is trivial compared to the cost of maintaining an
abstraction nobody has validated against a second provider.

Consequence: the dead GitLab OAuth handlers in `pkg/auth/handlers.go`
should be deleted in this phase (they were unmounted in the ISS-011
cleanup and are now aspiration, not code).

**3. One installation serves exactly one organization.** Enforced by a
UNIQUE constraint on `github_installations.github_installation_id`.

An organization may hold several installations (they might connect two
different GitHub orgs), but an installation never fans out to multiple
tenants. This keeps a repository's tenant unambiguous: repo →
installation → organization, one path, no joins that could pick the
wrong row. The flexible alternative was considered and rejected because
it reintroduces exactly the class of ambiguity Phases 17 and 19 spent
their entire budget eliminating.

**4. Record size and shape; enforce nothing.** Phase 20 stores
`visibility`, `size_bytes`, and `default_branch` and rejects no
repository for being large, LFS-using, or submodule-laden. Limits belong
in Phase 22, where cloning actually happens and the cost is real. Phase
20 is an API surface, not a policy engine.

## Essential deliverables

1. **Request-scoped tenant transaction (ISS-008).** A middleware or
   handler-level primitive that opens a transaction, sets
   `app.current_tenant` from the verified JWT claim, and makes it
   available to handlers. Every subsequent deliverable depends on it.
2. **GitHub App registered and its contract verified.** A runbook the
   user follows once, plus recorded findings about what GitHub actually
   sends — not what the docs say it sends.
3. **Schema.** `github_installations` (new, tenant-scoped, RLS +
   trigger) and new columns on `repositories`.
4. **Repositories CRUD.** Create, list, get, delete. Tenant-scoped,
   paginated, isolation-tested.
5. **Installation flow.** Redirect to GitHub's install URL, handle the
   callback, link the installation to the caller's organization.
6. **Webhook receiver.** `POST /webhooks/github` with HMAC-SHA256
   verification, handling `push`, `installation`, and
   `installation_repositories`, recording work for Phase 21's queue to
   pick up.

## Boundaries

**In scope:** schema, API surface, installation lifecycle, webhook
ingest, isolation tests for everything that mutates.

**Out of scope:**
- Cloning repositories (Phase 22).
- The job queue itself (Phase 21) — Phase 20 records intent to sync;
  something else will consume it.
- Parsing, chunking, embedding (Phase 22).
- Frontend (Phase 23).
- Rate limiting against GitHub's API budget (Phase 24), though the
  design should not make it impossible.

## Corrections to the roadmap sketch

The ROADMAP's plan list was written before Phase 19 executed. Two items
in it are wrong:

- **`webhook_secret` on `repositories` is wrong.** A GitHub App has ONE
  webhook secret, configured on the App itself, not per repository or
  per installation. It belongs in configuration, alongside
  `SUPABASE_WEBHOOK_SECRET`.
- **`github_installation_id` on `repositories` is the wrong shape.** An
  installation is an organization-level fact, not a repository-level
  one. Denormalizing it onto every repository row invites the two to
  disagree. It gets its own table, and `repositories` references it.

## Open questions for execution

- **Repository → project mapping.** `repositories.project_id` is NOT
  NULL and references `projects`, but nothing in the product creates
  projects yet, and the user-facing concept is "connect a repo to my
  organization". Either provisioning gains a default project per
  organization, or `repositories` moves to referencing the organization
  directly. This needs deciding in 20-02 and affects the migration.
- **What GitHub actually sends on `installation_repositories`.** To be
  answered by verification, not assumption.
- **Whether an org can delete a repository that has ingested chunks.**
  Cascade behaviour is already defined at the FK level; whether the API
  should refuse is a product call.
