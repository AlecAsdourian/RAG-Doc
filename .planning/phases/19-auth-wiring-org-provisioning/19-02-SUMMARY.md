---
phase: 19-auth-wiring-org-provisioning
plan: 02
subsystem: auth

requires:
  - phase: 19-01
    provides: signature-verified webhook, disposable-email guard, fail-closed constructor
  - phase: 17-01
    provides: SetupTestDB, WithTwoOrgs, AssertNoCrossTenantLeak
provides:
  - Provisioning persists the real Supabase user id (was generating a random UUID)
  - Idempotent user provisioning under sequential AND concurrent webhook replay
  - Atomic org + owner-membership creation with slug-collision recovery
  - Repaired org name/slug generators
  - pkg/auth test helpers repaired for the 17-03 trigger
affects: [19-03 (Auth Hook looks up users by supabase_user_id — was structurally impossible before this fix), 19-04 (multi-org assumes memberships exist)]

tech-stack:
  added: []
  patterns:
    - "Bare ON CONFLICT DO NOTHING (no column list) catches conflicts on either unique constraint; the follow-up SELECT prefers a supabase_user_id match over an email match"
    - "Deterministic slug base + random suffix on collision, rather than a random slug up front — keeps slugs readable and tests deterministic"
    - "Org + owner membership commit in one transaction"

key-files:
  created:
    - services/backend/pkg/auth/provisioning_isolation_test.go
    - .planning/phases/19-auth-wiring-org-provisioning/19-02-SUMMARY.md
  modified:
    - services/backend/pkg/auth/provisioning.go (identity fix, idempotency, transaction, slug retry)
    - services/backend/pkg/auth/webhook.go (rewrote emailLocalPart/firstNameFragment/sanitizeSlugFragment and both generators)
    - services/backend/pkg/auth/webhook_test.go (fixed payload schema drift; expanded generator cases)
    - services/backend/pkg/auth/testing.go (CreateTestRepository tenant scope; CleanupTestDB trigger handling + error surfacing)
    - services/backend/pkg/auth/isolation_test.go (skipped — superseded by 17-01)

key-decisions:
  - "ProvisionOAuthUser now uses the supabaseUserID parameter it was already accepting. The prior code called uuid.New() instead and discarded the argument, so users.supabase_user_id never matched the real Supabase identity."
  - "Skipped rather than deleted pkg/auth/isolation_test.go. Removing a test file is a planner/user call; skipping with a documented rationale makes the supersession explicit and is reversible."
  - "CleanupTestDB disables the 000009 trigger around its cross-tenant deletes rather than looping per-tenant. Cleanup legitimately spans every tenant; the test connection is the container superuser so RLS is not an obstacle, only the trigger."

patterns-established:
  - "Any helper in pkg/auth that writes a tenant-scoped table derives the tenant from its parent row rather than taking it as a new parameter, so existing call sites keep working."
  - "Cleanup helpers surface failures via t.Logf instead of discarding errors — a silent cleanup failure is what let the 17-03 breakage rot undetected for two phases."

issues-created: []

duration: ~75 min
completed: 2026-09-07
---

# Phase 19 Plan 02: Provisioning identity, idempotency, and generator repair

**Provisioning now persists the real Supabase user id, survives replay (sequential and concurrent), and commits the org + owner membership atomically. Both long-broken generators are fixed. As collateral, `pkg/auth` has a fully green test suite for the first time this milestone.**

## The bug that mattered most

`ProvisionOAuthUser` accepted a `providerUserID` parameter and **threw it away**, calling `uuid.New()` to populate `users.supabase_user_id`. Every provisioned user therefore carried a random id unrelated to their actual Supabase identity.

This would have silently broken 19-03. The Auth Hook's whole job is:

```sql
SELECT om.organization_id, om.role
FROM public.users u
JOIN public.organization_memberships om ON om.user_id = u.id
WHERE u.supabase_user_id = <jwt sub>
```

That lookup could never match. The JWT would ship with no `organization_id` claim, `TenantMiddleware` would 403 every request, and the failure would have looked like an Auth Hook problem rather than a provisioning one. Caught here because 19-02 read the function before extending it.

## Accomplishments

- `supabaseUserID` is now load-bearing; `uuid.Parse` validates it and the value lands in the column verbatim
- Idempotency via bare `ON CONFLICT DO NOTHING` + a preference-ordered follow-up SELECT (supabase_user_id beats email, so a user who changed their Supabase email resolves to their original row rather than a stranger who since claimed the address)
- `CreateOrganizationForUser` runs in one transaction — the prior version could leave an orphaned, memberless organization if the membership insert failed
- Slug collisions recover with a random hex suffix (up to 3 attempts) instead of erroring; the deterministic base is preserved so tests stay readable
- Both generators rewritten around a shared `emailLocalPart` helper that actually handles the empty-string case
- 5 provisioning isolation tests including a concurrent-replay race (8 goroutines, exactly one `created=true`)

## Task Commits

Three atomic commits:

1. `45ec586` — **fix(19-02):** Supabase identity, idempotency, generators
2. `811e760` — **fix(19-02):** pkg/auth test-helper repair + supersession skips
3. (this) — **test(19-02):** provisioning isolation suite

## Deviations from Plan

### 1. Provisioning was already wired; the work was correctness, not wiring

Plan Task 2 was framed as "wire provisioning into the webhook handler with idempotency." The 19-01 audit had already established the wiring existed. The real work turned out to be the identity bug above plus the transaction and collision handling.

### 2. Generator bugs were both worse and simpler than described

Root cause for both `TestGenerateOrgSlugFromEmail` and `TestGenerateOrgNameFromEmail`: `strings.Split("", "@")` returns `[""]` — a one-element slice — not an empty slice. The `if len(parts) == 0` guard was unreachable for **every** input, so `""` fell through to produce `"-org"` and `"'s Organization"`.

The name generator had a second bug the tests already encoded but nobody had fixed: `bob.smith@company.io` produced `Bob.smith's Organization` because it capitalized the first letter of the whole local part rather than taking the first name fragment.

Both fixed, with edge cases added: empty local part, all-punctuation local part, leading underscores, `+tag` addressing, case normalization, consecutive-separator collapsing, and length truncation that re-trims a trailing dash.

### 3. Unplanned: repaired `pkg/auth` test helpers broken by Phase 17-03

Not in the plan. Discovered while running the suite: **migration 000009's trigger (Phase 17-03) broke two helpers in `pkg/auth/testing.go` and nobody noticed for two phases.**

- `CreateTestRepository` did a bare `INSERT INTO repositories` → SQLSTATE 42501.
- `CleanupTestDB` issued bare cross-tenant `DELETE`s → same refusal. Worse, it discarded the error from every `db.Exec`, so cleanup silently stopped deleting anything. Rows leaked between tests and the next test using a hardcoded slug hit a `organizations_slug_key` unique violation.

The 17-03 reviewer verified `pkg/testing/isolation` but not this older second helper file. The failures stayed invisible because these tests also require a live Redis and a `DATABASE_TEST_URL` that CI wasn't providing.

Fixed both: `CreateTestRepository` derives the tenant from its project and wraps in a scoped transaction; `CleanupTestDB` disables the trigger around its deletes and surfaces failures via `t.Logf`.

### 4. Unplanned: skipped the Phase-4 isolation suite

`pkg/auth/isolation_test.go` is the original Phase-4 tenant-isolation suite. Phase 17-01's SUMMARY named it as the "reference implementation" to extend — but the extension went into the new `pkg/testing/isolation` package and this file was never retired.

It cannot pass as written: every test connects via `SetupTestDB`, which uses the container **superuser**, and superusers bypass RLS even under `FORCE ROW LEVEL SECURITY`. Its core assertions ("cross-tenant read returns zero rows") are structurally unsatisfiable — precisely the problem 17-01 solved by introducing the `rag_doc_app` NOSUPERUSER role.

**Skipped, not deleted.** Removing a test file is a planner/user call, not a worker's mid-plan decision. Each test now calls `supersededByHarness(t)`, whose doc comment explains why and points at the three suites that cover the same properties correctly. **Recommended follow-up: delete the file.** Flagging for the reviewer to weigh in.

## Verification

| Check | Result |
|---|---|
| `go vet ./pkg/auth/...` | clean |
| `go test ./pkg/auth/...` | **all pass** (first fully green run this milestone) |
| `TestProvisioningIsolation_*` | 5/5 pass, including 8-goroutine concurrent replay |
| `TestGenerateOrgSlugFromEmail` / `TestGenerateOrgNameFromEmail` | pass with 11 and 10 cases (was failing since Phase 4) |
| `TestWebhookHandler_UserCreatedEvent` | passes (payload schema drift fixed) |
| `go test ./pkg/api/... ./pkg/testing/...` | all pass — no 17-series regression |
| CI isolation scanner | PASS |

Local runs used `DATABASE_TEST_URL` pointed at the 17-01 testcontainers Postgres plus a throwaway Redis container. See "Next Phase Readiness" for why that's still a papered-over gap.

## Issues Encountered

- **The `provider` parameter is still unused.** `ProvisionOAuthUser` accepts it but the schema has no provider column. Kept for call-site clarity with a doc note rather than removed — Phase 20's GitHub App work may want per-provider handling.
- **`SetupTestDB` in `pkg/auth/testing.go` still defaults to docker-compose port 5434.** ISS-006 was closed by 17-01 with testcontainers, but only for `pkg/testing/isolation`. This second, older harness never migrated, so `pkg/auth` tests need `DATABASE_TEST_URL` set by hand and a Redis on 6379. Not fixed here (out of scope, and it works once the env var is set), but it's the reason two phases of breakage went unseen.

## Next Phase Readiness

- **19-03** — unblocked in a way it genuinely wasn't before. The Auth Hook's `WHERE u.supabase_user_id = <jwt sub>` lookup can now match, because provisioning finally writes the real id.
- **19-04** — every provisioned user has exactly one owner membership, committed atomically. The multi-org endpoints have a real starting state.
- **Recommended follow-ups (not blocking):**
  1. Delete `pkg/auth/isolation_test.go`.
  2. Migrate `pkg/auth/testing.go`'s `SetupTestDB` onto the 17-01 testcontainers harness so `pkg/auth` tests run without hand-set env vars — this is what would have surfaced the 17-03 breakage immediately.
  3. Consider a CI job that runs the full `go test ./...` with testcontainers, so "tests nobody runs" stops being a category.

## Reviewer follow-ups (post-review, same branch)

Reviewer returned four blockers and three mediums, all empirically verified against the live container rather than reasoned from docs. Option 2 applied per user decision: all four blockers plus the two substantive mediums. Two of the blockers were damage this PR's own "fixes" introduced.

**H1 — the trigger-disable cleanup was worse than the bug it fixed**
- **Was:** `CleanupTestDB` used `ALTER TABLE ... DISABLE TRIGGER`. Reviewer confirmed empirically that this writes `pg_trigger.tgenabled='D'` — durable catalog state surviving session close and process exit. Because the 17-01 container is reused across `go test` runs with Ryuk disabled, a panic or timeout between disable and re-enable would **permanently disarm tenant isolation** for every later run on that container, turning real failures into silent passes. Even on the happy path the window was global: parallel packages could observe a disabled trigger.
- **Now:** `SET LOCAL session_replication_role = replica` inside a transaction. Same trigger suppression, but transaction-scoped — it cannot outlive the tx however the process dies. Verified post-run: all six `trg_assert_tenant` triggers report `tgenabled='O'`.

**H2 — the repaired cleanup began executing unfiltered cross-tenant deletes**
- **Was:** `DELETE FROM users` etc. with no `WHERE`. They previously no-opped because every error was discarded; repairing the error handling made them live. Pointed at the shared container they wipe every row, including concurrently-running packages' in-flight fixtures.
- **Now:** `SetupTestDB` records a watermark from the database clock; every delete is scoped `WHERE created_at >= $1`. Residual caveat documented in the function doc: the watermark bounds by time window, not by ownership, so a package running concurrently *within that window* could still lose fixtures. The real fix remains follow-up 2 (migrate onto the 17-01 harness, which scopes by tenant id).

**H3 — a user could end up permanently organization-less**
- **Was:** org creation was gated on `isNewUser`. If it failed after the user row committed: 500 → Supabase retries → `created=false` → the branch is skipped → 202 returned. The user had no org, forever, and the success response stopped retries.
- **Now:** gated on actual ownership via a new `UserHasOwnerOrg`. The handler is convergent — however many times it runs, the end state is one user with one owner org. Pinned by `TestProvisioningIsolation_OrgCreationRecoversAfterFailure`.

**H4 — email-only conflict returned a different user's row**
- **Was:** Supabase identity Y signing up with an address owned by identity X got X's row back with `created=false` and no signal. No live takeover (the caller ignored the row on that path) but a loaded gun for 19-03, and Y silently never got a row while the webhook returned 202.
- **Now:** the post-conflict SELECT compares `user.SupabaseUserID` against the incoming id and returns `ErrEmailOwnedByAnotherIdentity` on mismatch. The webhook maps it to **409 Conflict** — not 5xx — so Supabase stops retrying something retries cannot fix. Pinned by `TestProvisioningIsolation_EmailOwnedByAnotherIdentityIsRefused`.

**M5 — added the write-direction isolation test**
- Reviewer verified the existing `NewTenantIsWalledOff` is not vacuous (it fails if RLS breaks) but noted it treats the new org as an opaque tenant id — a random UUID behaves identically, so it re-proves a 17-01 harness property rather than anything about provisioning. Added `TestProvisioningIsolation_CannotWriteIntoNewTenantsRepo`: gives the provisioned org a real project and repository, then attempts an `ingestion_runs` insert into that repo from orgA's scope. RLS's `WITH CHECK` must refuse it.

**M7 — un-skipped the two RLS-independent tests**
- The blanket skip of all five tests in `isolation_test.go` was over-broad, and my justification was factually wrong for two of them. `TestRoleBasedAccess` and `TestMultipleOrganizationsPerUser` touch only `organization_memberships` — no RLS, no trigger — so they pass fine, and reviewer confirmed they are the only coverage anywhere for role-value storage and multi-org membership. Both un-skipped and passing. Revised recommendation: delete the three genuinely-superseded tests, keep these two.

**Not applied (nits):** non-ASCII email local parts degrade to `my-org` (cosmetic + retry pressure); org display name isn't suffixed on slug collision; `t.Cleanup` registered after assertions in two tests; `SlugCollisionRecovers` hand-passes the colliding slug rather than deriving it.

Post-fix state: `pkg/auth` 100% green (24 tests, 3 skipped by design), `pkg/api/handlers` and `pkg/testing/...` green, CI scanner PASS, all six triggers verified enabled after a cleanup cycle.

---
*Phase: 19-auth-wiring-org-provisioning*
*Completed: 2026-09-07*
