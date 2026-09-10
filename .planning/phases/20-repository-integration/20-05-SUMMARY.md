---
phase: 20-repository-integration
plan: 05
subsystem: api

requires:
  - phase: 20-02
    provides: the verified webhook payload shapes and the captured deliveries
  - phase: 20-01
    provides: db.TenantScoper — every RLS write here goes through it
provides:
  - "POST /webhooks/github — HMAC-verified, idempotent by delivery id"
  - "Migration 000012 — github_webhook_deliveries, github_installation_tenants (the trigger-maintained discovery index), uninstalled_at"
  - "docs/api-github-webhooks.md — including what Phase 21's queue must consume"
affects: [21 (the queue reads sync_state = 'pending'), 22 (ingestion)]

tech-stack:
  added: []
  patterns:
    - "Verify the signature over the RAW body before parsing: parsing attacker-controlled JSON is work done for someone who has not proved who they are"
    - "A webhook has no tenant — it DISCOVERS one. Do that with a trigger-maintained table that has no RLS, not a SECURITY DEFINER function: FORCE RLS applies to the table owner too, so the function works only where the app and the tables have different owners. Everything done with the answer goes through a normal tenant transaction."
    - "Verify in the shape you DEPLOY, not the shape you test. A privilege split that exists only in the harness turns a verified property into a false one."
    - "Record intent, never start work. A goroutine begun in a webhook dies with the process and takes the only record of the work with it"
    - "An unrecognised event is 202, not 4xx — the delivery log is the first place anyone looks when webhooks seem broken"
    - "A constant-time comparison cannot be mutation-tested functionally; == and hmac.Equal agree on every input and differ only in timing"

key-files:
  created:
    - services/backend/pkg/api/handlers/github_webhook.go
    - services/backend/pkg/api/handlers/github_webhook_events.go
    - services/backend/pkg/api/handlers/github_webhook_isolation_test.go
    - services/backend/pkg/api/handlers/github_webhook_constanttime_test.go
    - services/backend/migrations/000012_webhook_deliveries.up.sql / .down.sql
    - docs/api-github-webhooks.md
  modified:
    - services/backend/pkg/api/router.go
    - services/backend/pkg/api/handlers/main_test.go
    - docs/api-repositories.md, docs/github-app-setup.md
    - .planning/ROADMAP.md, .planning/STATE.md

key-decisions:
  - "An installation we do not recognise is NOT adopted. Linking it to whoever acted most recently hands one customer's GitHub account to another — 20-04's review demonstrated that exact attack in its non-webhook form, and nothing in the payload identifies one of our users."
  - "installation.deleted keeps the row and the repositories. An uninstall means access was lost, not that the user asked us to forget what we ingested; repositories.installation_id is ON DELETE SET NULL, so deleting the installation would silently orphan everything under it."
  - "A webhook never creates a repository row. Not because the schema refuses it — default_branch is NOT NULL DEFAULT 'main', so a half-row would be accepted and would quietly claim the wrong branch — but because connecting a repository is a deliberate act, and a permission-scope change is not that act."
  - "github_webhook_deliveries has no RLS, deliberately: a delivery arrives before we know whose it is, and installation.deleted concerns a tenant that is going away."
  - "Only a FINISHED delivery is a duplicate. One left 'processing' or 'failed' is re-claimed on redelivery, because no handler here can be partially applied — each writes in one tenant transaction and is idempotent by key."
  - "Migration numbered 000012, not the plan's 000011 — that number was taken by 20-03, which was written after the plan. golang-migrate keys on the integer."
  - "Tenant discovery uses a trigger-maintained table with no RLS, not a SECURITY DEFINER function. FORCE RLS applies to the table owner, so the function only worked in the harness — and EXECUTE-to-PUBLIC made it an enumeration primitive for any database role."
  - "A delivery left 'processing' or 'failed' is re-claimable. No handler here can be partially applied, so the usual argument for refusing a replay does not apply, and refusing one silently dropped events forever."

issues-created: [ISS-019]
issues-closed: []

duration: ~2 hours
completed: 2026-09-09
---

# Phase 20 Plan 05: the webhook receiver

**Closes Phase 20.** Everything here records intent; Phase 21 builds the queue that acts on it.

## Tenant discovery: two wrong answers before the right one

**First wrong answer.** The initial draft used the raw pool for every write, with a comment saying *"where it touches RLS tables it builds a scope explicitly — see `withInstallationTenant`"*. That function did not exist. Every `UPDATE` silently matched zero rows, because that is what an unscoped write to a `FORCE ROW LEVEL SECURITY` table does.

The problem underneath is real: **a webhook has no tenant, it has to discover one**, and `github_installations` is FORCE RLS so even the lookup is filtered.

**Second wrong answer, and this one shipped in the first commit.** I added a `SECURITY DEFINER` function and wrote "verified rather than assumed" next to it. It *was* verified — in the test harness, where migrations run as a superuser and the application connects as a separate non-superuser role, so the function inherited a privilege the caller lacked.

`FORCE ROW LEVEL SECURITY` applies policies **to the table owner too**, and `SECURITY DEFINER` only switches `current_user` to the function's owner. In the deployment shape this repo actually documents — application and tables owned by the same role — the function is filtered exactly like a direct read and returns nothing. The receiver would have answered `202` to every event while doing nothing at all: no error, no warning, a plausible outcome in the delivery log, and no test in the suite able to see it.

Review reproduced it on a database owned by a `NOSUPERUSER NOBYPASSRLS` role. This is the same failure as the morning's `.env` problem, one layer down: **I verified in the shape we test, not the shape we deploy.**

**The right answer stops depending on privileges.** `github_installation_tenants` is an ordinary table with no RLS, holding only the installation-to-tenant mapping, maintained by a trigger so it cannot drift. It behaves identically whoever owns it and whoever connects — and it is strictly *less* exposed than the function was, because `EXECUTE` defaults to `PUBLIC`: any database role at all could call the old one and enumerate the whole map by guessing small sequential ids. Verified under a production-shaped role: direct read 0 rows, discovery table returns the tenant, a grant-less role gets `permission denied`.

## What the plan asked for that cannot be done

The plan's verification list says: *"Mutation-check the signature verification: replace `hmac.Equal` with `==` and confirm a test fails."*

I ran that mutation. **No test failed, and none could.** `==` and `hmac.Equal` return the same answer for every input; they differ only in how long they take. The property is timing, not behaviour.

A timing test would be the behavioural equivalent and would be flaky on a shared CI runner — and a test that fails randomly gets deleted, after which the property is unguarded for real. So `TestSignatureComparisonIsConstantTime` reads the source and asserts `hmac.Equal` is used. That is a weaker kind of test, it is the strongest one available for this property, and the file says so rather than implying it proved something behavioural.

## Decisions the plan asked to be made and written down

**An orphan installation is not adopted.** A user can install from GitHub's directory without passing through our flow, so `installation.created` routinely arrives for an installation belonging to no organization of ours. Adopting it — linking to whoever acted most recently — is the same cross-tenant bug 20-04's review demonstrated. Nothing in the payload identifies one of *our* users; `sender` is a GitHub login and mapping those would be an authorization decision made from an unauthenticated request. The installation stays live-and-unlinked until the user completes the flow from inside the app, which is a normal state.

**Where the full repository fetch happens: not here.** The payload's repository shape is reduced — no `default_branch` (which is `NOT NULL`), no `size`, `visibility` or `archived`. Fetching inline would make webhook processing depend on GitHub being reachable at delivery time. So the webhook re-points and re-queues rows that already exist and creates none; `POST /api/repositories` remains the only thing that connects a repository.

## Fixture provenance, stated because it matters

The `installation` tests run against **real captured deliveries** — envelope, headers and body, from the live App on 2026-09-08. Those confirmed `sha256=` + 64 hex, a UUID delivery id, and the reduced repository shape.

`push` and `installation_repositories` have **no captured payload**; no such delivery has ever reached a capture server. Their tests are built from GitHub's documentation and are named `UNVERIFIED_*` so nobody mistakes them for evidence about the payload shape. They hold the handler logic honestly and say nothing trustworthy about what GitHub actually sends.

This is not a formality. Capturing the installation payloads in 20-02 corrected three specs, including a size field wrong by ~1000×. **ISS-019** records capturing these two as real work.

One limitation of the captured fixtures worth knowing: the capture server stored the body **parsed**, not as raw bytes, so the real signatures cannot be replayed — re-serialising JSON does not reproduce GitHub's exact bytes. Signature tests therefore sign their own payloads with a test secret. The *shapes* and *headers* are real; the *signatures* in the fixtures are unusable.

## Review round: the fix that only worked in the harness

Approved on everything but one blocker, and the blocker was the tenant-discovery function above. Alongside it:

**The `EXECUTE` grant made the function an enumeration primitive.** Review demonstrated a role with *zero* table grants reading the complete installation-to-organization map by calling it across a range of ids. GitHub installation ids are small and sequential. Gone with the function.

**`installation_repositories.added` had no positive test.** The only test asserted a negative — that no repository row is created — which a handler doing *nothing at all* satisfies perfectly, and review showed exactly that no-op surviving the suite. The one path the event exists for was uncovered. Now asserted, along with the two second-layer filters that also survived.

**A delivery that died mid-flight was poisoned forever.** The first design treated any existing row as a duplicate, justified by "replaying a partially-applied event is worse than one recorded as failed". Review pointed out that **no handler here can be partially applied** — each does all its writes in one tenant transaction and each is an idempotent update by key. So the justification was false and the cost was real: a panic, an OOM or a deploy restart silently dropped an uninstall forever. `processing` and `failed` are now re-claimable; finished deliveries are still duplicates.

**`github_webhook_deliveries.organization_id` was described in the migration and written by nothing.** Now populated when the tenant is discovered.

**Four doc claims corrected**, all of them the failure mode Task 4 exists to prevent: `default_branch` was said to be `NOT NULL` and to block a half-row insert (it is `NOT NULL DEFAULT 'main'`, so the insert would *succeed* and quietly claim the wrong branch — the decision is right, the stated mechanism was not); stand-down was described as matching what an uninstall does (it is the opposite — uninstall keeps the link); `installation.deleted` was said to stand down repositories, without the qualifier that a `synced` one keeps its state; and `GITHUB_WEBHOOK_SECRET` became mandatory at startup with no runbook mentioning it.

**One mutation still survives, and it is honest that it does.** Dropping the `p.organization_id = $2` predicate from the `added` update changes nothing observable, because RLS already restricts the rows. It is a second layer, and a second layer cannot be measured while the first one works. Recorded rather than dressed up as covered — the same call as PR #22's equivalent.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `gofmt` | clean |
| `go test -p 1 ./...` | all pass, container rebuilt from scratch |
| `TestGitHubWebhook` | 22/22 |
| `migrate up` → `down 1` → `up` on a scratch database | clean each time |
| CI isolation scanner | PASS, with `POST /webhooks/github` reported as **skipped** with a reason — checked in the JSON, not just the exit code |
| Tenant discovery under a `NOSUPERUSER NOBYPASSRLS` owner | direct read 0 rows; discovery table returns the tenant; grant-less role denied |

`-race` was not run locally (no gcc; it needs cgo). CI runs it.

### Mutation testing

| Mutation | Result |
|---|---|
| Signature check removed | both signature scenarios fail |
| Signature verified AFTER parsing | `Signature_IsCheckedOverTheRawBodyBeforeParsing` fails, only it |
| Duplicate deliveries reprocessed | both idempotency scenarios fail |
| `hmac.Equal` → `==` | **no functional test fails, and none can** — caught by the source-level guard instead |

Review round:

| Mutation | Result |
|---|---|
| `recordAddedRepositories` does nothing | `UNVERIFIED_RepositoriesAdded…Repoints…`, `AddedOnlyTouchesTheOwningOrganization` |
| Installation filter dropped from `standDownRepositories` | `UNVERIFIED_RepositoriesRemoved…`, `RemovedOnlyTouchesTheNamedInstallation` |
| `recordOutcome` disabled | four subtests, including `DeliveryRecordsItsOutcomeAndTenant` |
| Unfinished deliveries treated as duplicates again | `AnUnfinishedDeliveryCanBeReprocessed`, only it |
| `p.organization_id` predicate dropped | **survives** — RLS already covers it; see above |

## Notes for what comes next

- **Phase 21's work item is a `repositories` row with `sync_state = 'pending'`.** There is no queue table; inventing one was Phase 21's decision to make, not this plan's. The query and the three traps are in `docs/api-github-webhooks.md`.
- **ISS-016 should be settled before the queue is built.** `sync_state` is a status column used as a queue, with no lease or owner, and this phase adds a second writer to it.
- **ISS-019** — capture real `push` and `installation_repositories` deliveries and replace the documentation-derived fixtures.
- **The delivery table grows forever.** Migration 000012 carries the pruning statement; Phase 24 owns scheduling it.

---
*Phase: 20-repository-integration — COMPLETE*
*Completed: 2026-09-09*
