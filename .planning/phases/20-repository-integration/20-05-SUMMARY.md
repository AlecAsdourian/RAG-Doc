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
  - "Migration 000012 — github_webhook_deliveries, uninstalled_at, and the github_installation_owner tenant-discovery function"
  - "docs/api-github-webhooks.md — including what Phase 21's queue must consume"
affects: [21 (the queue reads sync_state = 'pending'), 22 (ingestion)]

tech-stack:
  added: []
  patterns:
    - "Verify the signature over the RAW body before parsing: parsing attacker-controlled JSON is work done for someone who has not proved who they are"
    - "A webhook has no tenant — it DISCOVERS one. That single lookup needs a narrow SECURITY DEFINER function; everything done with the answer goes through a normal tenant transaction"
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
  - "A webhook never creates a repository row. The payload's repository shape has no default_branch, which is NOT NULL, so creating one means inventing it — and adding a repository nobody asked to connect."
  - "github_webhook_deliveries has no RLS, deliberately: a delivery arrives before we know whose it is, and installation.deleted concerns a tenant that is going away."
  - "A failed delivery is NOT retried into success. The delivery row is written first, so GitHub's redelivery is treated as a duplicate. Replaying a partially-applied event is worse than recording it as failed; recovery is a resync."
  - "Migration numbered 000012, not the plan's 000011 — that number was taken by 20-03, which was written after the plan. golang-migrate keys on the integer."

issues-created: [ISS-019]
issues-closed: []

duration: ~2 hours
completed: 2026-09-09
---

# Phase 20 Plan 05: the webhook receiver

**Closes Phase 20.** Everything here records intent; Phase 21 builds the queue that acts on it.

## The bug that took the longest to find

The first draft used the raw pool for every write, with a comment saying *"where it touches RLS tables it builds a scope explicitly — see `withInstallationTenant`"*.

`withInstallationTenant` did not exist. I had written a comment describing a function I never wrote, and every `UPDATE` silently matched zero rows — which is exactly what an unscoped write to a `FORCE ROW LEVEL SECURITY` table does. Five tests failed with a 500 before it surfaced.

**The underlying problem is real and worth stating plainly: a webhook has no tenant, it has to discover one.** `github_installations` is FORCE RLS, so even the *lookup* is filtered — verified, not assumed: as `rag_doc_app` with no tenant set, a direct `SELECT` returns **0 rows** while the new function returns the row.

So migration 000012 adds `github_installation_owner(BIGINT)`, a `SECURITY DEFINER` function that maps a GitHub installation id to its organization. It is the one sanctioned crossing of the tenant boundary in this phase, it is narrow (one integer in, two ids out, `search_path` pinned), and **everything done with its answer goes through a normal tenant transaction**.

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

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `gofmt` | clean |
| `go test -p 1 ./...` | all pass, container rebuilt from scratch |
| `TestGitHubWebhook` | 17/17 |
| `migrate up` → `down 1` → `up` on a scratch database | clean each time |
| CI isolation scanner | PASS, with `POST /webhooks/github` reported as **skipped** with a reason — not silently absent |

`-race` was not run locally (no gcc; it needs cgo). CI runs it.

### Mutation testing

| Mutation | Result |
|---|---|
| Signature check removed | both signature scenarios fail |
| Signature verified AFTER parsing | `Signature_IsCheckedOverTheRawBodyBeforeParsing` fails, only it |
| Duplicate deliveries reprocessed | both idempotency scenarios fail |
| `hmac.Equal` → `==` | **no functional test fails, and none can** — caught by the source-level guard instead |

## Notes for what comes next

- **Phase 21's work item is a `repositories` row with `sync_state = 'pending'`.** There is no queue table; inventing one was Phase 21's decision to make, not this plan's. The query and the three traps are in `docs/api-github-webhooks.md`.
- **ISS-016 should be settled before the queue is built.** `sync_state` is a status column used as a queue, with no lease or owner, and this phase adds a second writer to it.
- **ISS-019** — capture real `push` and `installation_repositories` deliveries and replace the documentation-derived fixtures.
- **The delivery table grows forever.** Migration 000012 carries the pruning statement; Phase 24 owns scheduling it.

---
*Phase: 20-repository-integration — COMPLETE*
*Completed: 2026-09-09*
