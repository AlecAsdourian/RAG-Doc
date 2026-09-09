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

### Mutation testing

| Mutation | Result |
|---|---|
| Disable RLS on `github_installations` | `AreTenantIsolated` fails (all 3 subtests) |
| Remove the `trg_assert_installation_tenant` trigger | `RepositoryCannotReferenceAnotherTenantsInstallation` fails |
| Remove `trg_assert_tenant` from `github_installations` | the `protectedTables` ratchet fails |
| Delete the default-project insert from provisioning | `CreateOrganizationForUser_CreatesADefaultProject` fails (all 3 subtests) |

**A correction to an earlier version of this table.** It claimed "remove RLS → all three isolation tests fail". That was wrong, and reviewer-measured: disabling RLS fails one test, because the other two are held up by a UNIQUE constraint and by the trigger respectively. The claim was written from expectation rather than from a run. Fourth time in this project that a summary has asserted a property the code did not have.

The `pkg/github` tests cover the credential handling the way `supabase_admin.go` learned to: a stub server echoes the `Authorization` header into an error body, and the test asserts neither an App JWT nor a `ghs_` token survives into the error. Redirects are refused outright. Both were reviewer findings on the Supabase client; here they are built in rather than retrofitted.

## Reviewer round (post-review, same branch)

One blocker-class finding, one process failure, and three doc claims the code did not have.

**H1 — a repository could point at another tenant's installation.** `github_installations` is scoped by `organization_id`; `repositories` is scoped through `projects.organization_id`; `repositories.installation_id` crosses between them and nothing made the two agree. **Foreign key validation runs with RLS bypassed**, so the referenced installation did not have to be visible. Demonstrated as the non-superuser app role: orgA `SELECT`s orgB's installation → 0 rows, then `UPDATE repositories SET installation_id = <that id>` → `UPDATE 1`.

The consequence is worse than a bad row: a sync job following that link mints an installation token for orgB's installation while acting for orgA — read access to another customer's private source. And `ON DELETE SET NULL` meant orgB deleting its *own* installation wrote into orgA's repository row, a cross-tenant write orgB could not perform directly.

Closed with `trg_assert_installation_tenant`, a trigger asserting the installation's organization matches the repository's. Not `SECURITY DEFINER`: under the caller's own RLS another tenant's installation is invisible, so the lookup returns NULL and the comparison refuses the write — same answer without elevated reads. Pinned by a test that fails when the trigger is removed.

The migration comment claiming the `UNIQUE` on `github_installation_id` "enforces one installation serves one organization ... so a repository's owner cannot be ambiguous" was half true. It constrains which organization owns an installation; it said nothing about which repositories may point at one. Corrected to say so.

**H2 — I skipped step 3 of `docs/isolation.md`'s own four-step recipe** for adding a tenant-scoped table: registering it in the harness's `protectedTables`. Migration 000009's header gives the same instruction, and the list calls itself "the ratchet".

The gap was invisible because my own test could not see it: RLS's `WITH CHECK` refuses an unscoped INSERT with the *same* SQLSTATE 42501 the trigger uses, and the test asserted only the code — so dropping `trg_assert_tenant` left every test green, while the test's doc comment claimed to be confirming the trigger was attached. Registered now; `requireTenantViolation` asserts on the message, which is what separates the two.

**M1 — the `ON CONFLICT` target was wrong in both directions.** It named `(organization_id, slug)` while asserting that the partial unique index made a duplicate impossible — backwards, since that index is the constraint that raises. An organization with a default under a different slug (exactly what this migration's backfill produces) would abort the whole transaction; one with a non-default project on slug `default` would end with zero defaults. Now targets the partial index.

**M2 — the default-project invariant test asserted the fixture's own constant.** It ran against `WithTwoOrgs`, whose fixture sets `is_default = true` as a literal. Deleting the insert from `CreateOrganizationForUser` left the entire suite green. There is now a test on the production path, including that a repository can actually be created under the resolved project.

**M3 — the token cache did not deduplicate concurrent mints.** Measured at 25 concurrent callers producing 25 mint requests, last writer winning — while the comment claimed the cache existed so a burst would not mint each time. Per-installation mint locks with a re-check after acquiring.

**M4 — pagination truncated silently at 10,000 repositories** and returned a nil error, reporting a partial list as complete. Now an error.

**Nits applied:** a token response with no `expires_at` now errors rather than re-minting forever (it would have been visible only as unexplained API volume).

**Not done:** `sync_state` has no CHECK constraint (L6) and `redactSecrets` covers only `ghs_`/`ghu_` prefixes — 20-04's OAuth flow produces `gho_`. Both are small; flagged for 20-04 rather than widened into this plan.

## Notes for what comes next

- **20-03** takes `DefaultProjectID` and the `TenantScoper`. `router.go` has `_ = githubClient` and `_ = tenantScoper` marking both spots.
- **The client caches installation tokens in memory only.** They live about an hour. Nothing should persist one.
- **`ListInstallationRepositories` is bounded at 100 pages.** A pagination bug on either side would otherwise be an unbounded loop.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-08*
