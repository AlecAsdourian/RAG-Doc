---
phase: 20-repository-integration
plan: 02
subsystem: github

requires:
  - phase: 20-01
    provides: db.TenantScoper (the new table's isolation tests use it)
  - phase: 19-02
    provides: CreateOrganizationForUser, extended here
provides:
  - "Migration 000010 — github_installations (RLS + tenant trigger), repositories GitHub columns, one default project per organization"
  - "pkg/github.Client — App JWT, installation tokens, repository listing"
  - "UserProvisioner.DefaultProjectID — how 20-03 resolves a repository's project"
  - "The VERIFIED CONTRACT block in 20-02-PLAN.md: what GitHub actually sends"
affects: [20-03 (CRUD builds on the schema and DefaultProjectID), 20-04 (installation flow uses GetInstallation), 20-05 (webhooks)]

tech-stack:
  added:
    - "GitHub App API via a hand-rolled client (no SDK) — the surface needed is four calls, and an SDK would be more dependency than code"
  patterns:
    - "A migration must not reference a role that only exists in tests"
    - "Fixtures must establish every invariant provisioning establishes, or tests run against a shape production never has"
    - "Name a field for its unit when the unit is not obvious (SizeKB, size_kb)"

key-files:
  created:
    - services/backend/migrations/000010_github_integration.up.sql / .down.sql
    - services/backend/pkg/github/client.go, client_test.go
    - services/backend/pkg/db/github_installations_isolation_test.go
  modified:
    - services/backend/pkg/auth/provisioning.go (default project; DefaultProjectID)
    - services/backend/pkg/testing/isolation/fixtures.go (fixture projects are now default)
    - services/backend/pkg/api/router.go (constructs the GitHub client)

key-decisions:
  - "Default project per organization (option A), not re-parenting repositories to organizations (option B). B would rewrite the RLS policies for repositories, chunks and ingestion_runs in the same migration that introduces a new tenant-scoped table — changing the isolation boundary and extending it at once."
  - "github_installations.github_installation_id UNIQUE is the tenancy boundary, not a dedup convenience. An org may hold several installations; an installation never fans out to several orgs."
  - "installation_id ON DELETE SET NULL, not CASCADE. Uninstalling the App means access was lost, not that the user asked us to delete what we ingested."
  - "size_kb, not size_bytes. GitHub reports kilobytes (verified: 75 for a real repository)."
  - "No retry logic in the GitHub client. Phase 24 owns rate limiting, and a naive retry against a secondary rate limit makes throttling worse."

issues-created: []
issues-closed: []

duration: ~2 hours
completed: 2026-09-08
---

# Phase 20 Plan 02: GitHub schema and client

**Migration 000010, a GitHub App client, and the contract both are built against — verified before either was written.**

## Task 1 was already done

The verification ran on 2026-09-08 against the live App, before any of this code existed. It is recorded in `20-02-PLAN.md` under VERIFIED CONTRACT, and it changed three specs:

- **`size` is kilobytes.** The plan said `size_bytes`. A real repository reported `size=75`; 75 bytes is not a possible git repo. Every repository would have been under-reported by ~1000× with nothing ever erroring.
- **The OAuth callback cannot sit behind JWT auth** — a browser following GitHub's redirect sends no `Authorization` header. Corrected in 20-04.
- **Webhook payloads carry a reduced repository shape** — id, name, full_name, private, and nothing else. A webhook cannot populate a `repositories` row on its own. Corrected in 20-05.

Doing this first is the 19-03 pattern applied deliberately. It cost about twenty minutes and saved three wrong implementations.

## The repository-to-project decision

`repositories.project_id` is NOT NULL and references `projects`, but **nothing in production had ever created a project** — the only inserts were test helpers. So a user who signed up had an organization they could not connect a repository to.

Chose **A: a default project per organization**, over **B: re-parenting repositories to organizations directly**.

B is the tempting one, because "connect a repo to my organization" is the product concept and the project layer is unused. It was rejected because the tenancy machinery is built on the current shape: the RLS policies reach the tenant via `repositories → projects → organization_id`, and so do the policies for `chunks` and `ingestion_runs` which join through `repositories`. B means rewriting those policies and the 000009 trigger's tenant derivation **in the same migration that introduces a new tenant-scoped table** — changing the isolation boundary and extending it at the same time.

A leaves the boundary untouched, is reversible, and keeps a grouping layer the product may still want. If projects turn out to be genuinely unwanted, B is still available later as a migration that changes one thing.

Enforced by a partial unique index rather than convention: `UNIQUE (organization_id) WHERE is_default`. Code has to pick "the" default project, so the schema should guarantee there is one to pick.

## What the tests caught

**A `GRANT` to a role that does not exist.** The migration ended with `GRANT ... ON github_installations TO rag_doc_app`, on the reasoning that the harness connects as that role. Two things wrong with it: `rag_doc_app` does not exist in production at all, and in the harness it is created by `ensureAppRole` which runs **after** migrations.

It passed the scratch-database check because I had created the role by hand there first. The testcontainer, which does not, failed — and failed in a way worth noting: `Dirty database version 10`, from every isolation test at container setup, saying nothing about grants. The harness's own `GRANT ... ON ALL TABLES` covers the new table anyway.

**Fixture organizations had no default project.** `WithTwoOrgs` inserts organizations directly rather than going through provisioning, so the invariant provisioning establishes did not hold for them. Fixed in the fixture: an organization without a default project is not a realistic organization, and 20-03 resolves a repository's project through it.

**A dirty container survives a mutated migration.** Mutation-testing the RLS line left migration 10 half-applied in the reused container, and the next run failed with a message pointing at the dirty state rather than the mutation. Worth knowing: `docker rm -f rag-doc-isolation-tests` after mutating a migration file.

## Verification

| Check | Result |
|---|---|
| `migrate up` → `down 1` → `up` on a scratch database | clean each time |
| `go build ./...`, `go vet ./...` | clean |
| `go test -p 1 ./...` | all pass |
| `TestGitHubInstallations_*` | 3 tests, 5 assertions |
| **Mutation: remove RLS from `github_installations`** | all three isolation tests fail |

The `pkg/github` tests cover the credential handling the way `supabase_admin.go` learned to: a stub server echoes the `Authorization` header into an error body, and the test asserts neither an App JWT nor a `ghs_` token survives into the error. Redirects are refused outright. Both were reviewer findings on the Supabase client; here they are built in rather than retrofitted.

## Notes for what comes next

- **20-03** takes `DefaultProjectID` and the `TenantScoper`. `router.go` has `_ = githubClient` and `_ = tenantScoper` marking both spots.
- **The client caches installation tokens in memory only.** They live about an hour. Nothing should persist one.
- **`ListInstallationRepositories` is bounded at 100 pages.** A pagination bug on either side would otherwise be an unbounded loop.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-08*
