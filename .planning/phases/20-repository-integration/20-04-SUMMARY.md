---
phase: 20-repository-integration
plan: 04
subsystem: api

requires:
  - phase: 20-02
    provides: the verified installation payload shape and the GitHub App client
  - phase: 20-01
    provides: db.TenantScoper — the callback writes through it
provides:
  - "GET /api/github/install, GET /api/github/callback (PUBLIC), GET /api/github/installations, GET /api/github/installations/{id}/repositories"
  - "StateStore.StoreStateValue / ConsumeState — payload-carrying, atomically single-use"
  - "github.Client.ListInstallationRepositoriesPage — page-wise, for pass-through pagination"
  - "docs/api-github-install.md — the redirect-result contract Phase 23 implements"
affects: [20-05 (the webhook writes github_installations too), 23 (frontend implements the redirect results)]

tech-stack:
  added: []
  patterns:
    - "A credential that survives a round trip through a third party carries its own authority: bind the tenant to the state token server-side, never re-read it from the request or the caller's current claim"
    - "Single-use means atomic. EXISTS-then-DEL is check-then-act and lets a replay win the race."
    - "Ownership is proven BEFORE the third-party call, not after — refusing afterwards still spends the credential and still leaks existence through timing"
    - "A config value that only ever appears in a redirect URL must fail at construction; a wrong one is a 302 to somebody else's 404, with nothing in our logs"

key-files:
  created:
    - services/backend/pkg/api/handlers/github_install.go
    - services/backend/pkg/api/handlers/github_install_isolation_test.go
    - docs/api-github-install.md
  modified:
    - services/backend/pkg/auth/state_store.go (payload + atomic consume)
    - services/backend/pkg/auth/handlers.go, oauth.go (GitLab deleted)
    - services/backend/pkg/github/client.go (page-wise listing; gho_ redaction)
    - services/backend/pkg/api/router.go (mounts; the ISS-011 probe removed)
    - services/backend/.env.example, docs/github-app-setup.md

key-decisions:
  - "The organization is bound to the state token at install time and read from nowhere else. A user who switches organizations mid-flow still links the installation to the one they started from — and on the callback, which is unauthenticated, there is no current claim to fall back on anyway."
  - "A collision on github_installation_id is a normal result (already_connected), not a 500. Both organizations are logged for operators; neither is named to the user, because naming the other one confirms it exists and uses this product."
  - "The callback answers with a redirect carrying github_result, not JSON. Its caller is a browser following GitHub's redirect chain, not a fetch()."
  - "GitHub's numeric installation id is never returned to clients. A UI addresses installations by our uuid; the number is what someone would need to talk to GitHub about an installation that is not theirs."
  - "The ISS-011 StateStore probe is deleted rather than kept. 20-04 gave the state store a real consumer, so the probe's only remaining effect was a full Redis connect timeout on every router construction — 2.1s per test."

issues-created: []
issues-closed: []

duration: ~3 hours
completed: 2026-09-09
---

# Phase 20 Plan 04: the installation flow

**Where a GitHub-side fact becomes a tenant-side fact.** Four endpoints, one of them deliberately unauthenticated.

## The ordering is the security argument

`Callback` does four things and the order is the whole design:

1. **Consume the state token.** Atomic get-and-delete, so a replay loses the race rather than winning it. Invalid, expired and already-used are one answer.
2. **Read the organization out of the token** — not from the request, not from the caller's claim.
3. **Ask GitHub whether the installation is real**, before anything is written. `installation_id` is an attacker-supplied integer until GitHub says otherwise.
4. **Persist**, and treat a collision as a comprehensible situation rather than a fault.

Doing (4) before (1)-(3) is an open door. Doing (2) from the request is the subtler mistake, and it is the one a test had to catch: see below.

## Two things the plan called out, both verified rather than assumed

**The callback is mounted outside `JWTAuthMiddleware`.** A browser following GitHub's redirect sends no `Authorization` header, so a callback inside the authenticated group returns 401 to every real installation — it fails 100% of the time, not intermittently. Scenario 3 is the regression guard: it asserts the route answers *something other than 401* without a token.

**A missing `state` is refused, never defaulted.** Installing from GitHub's own "Install App" button produces a redirect with `installation_id` and `setup_action` and no `state`. Defaulting to "the caller's current organization" would link an installation to whoever happened to be logged in — and on a public route there is no caller, so the fallback would have to invent one.

## What the plan did not anticipate

**Task 3's endpoint was unreachable as specified.** It takes an installation id in its path, and nothing in the phase gave a UI one: the only place an id appeared was `repositories.installation_id`, so a user who had just installed the App — and therefore had no repositories — could not reach the picker that exists to help them add their first one. The flow did not compose.

Recorded as a REVISION NOTICE in `20-04-PLAN.md` rather than improvised, per the 19-03 pattern. Two additions: `GET /api/github/installations`, and the success redirect now carries `installation_id` so the immediate case needs no extra round trip.

## Things found in passing

**`ValidateState` was check-then-act.** `EXISTS` then `DEL`, two round trips, and it returned `true` when the delete failed — with a comment reasoning that reuse-once beat blocking a valid user. A defensible trade for a login button; the wrong one for a token that authorises linking a tenant to a GitHub installation. Now `GETDEL`, and the legacy entry point is a wrapper over it, so the direct-OAuth handlers inherit the fix.

**`generateSecureToken` ignores `rand.Read`'s error**, which on a failing entropy source yields 32 zero bytes — a predictable CSRF token, which is no CSRF token. The new flow uses its own generator that returns the error. The old one is untouched and still used by the unmounted OAuth handlers.

**`docs/github-app-setup.md` claimed `.env.example` documented the App variables "as of 20-02".** It did not — 20-02 added the code that reads them and never added them to the template, so anyone following the runbook found nothing matching. Both fixed.

**The ISS-011 StateStore probe cost 2.1 seconds per router construction.** It dialled Redis solely to log whether Redis was reachable, with five retries. Its stated justification was that "Phase 20's GitHub App flow will want it" — which is now true, so the flow reports it and the probe is gone. The handlers package went from 17s to 0.3s.

**GitLab is deleted** (Task 4): both handlers, the OAuth config entry, the `.env.example` keys, and a stale comment in `webhook.go`. `go build` clean, no references remain.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `gofmt` | clean |
| `go test -p 1 ./...` | all pass, container rebuilt from scratch |
| `TestGitHubInstallFlow` | 10/10 |
| `TestStateStore_*` | 8/8, including the new payload and concurrency cases |
| CI isolation scanner | PASS (no new mutation endpoints — all four routes are GET) |

`-race` was not run locally (this machine has no gcc; `go test -race` needs cgo). CI runs it.

### Mutation testing

| Mutation | Result |
|---|---|
| Missing `state` falls through instead of refusing | scenario 3 fails, only it |
| State consumed AFTER GitHub is called | scenario 4 fails, only it |
| **Organization read from the query string** | **scenario 9 fails, only it** |
| Collision guard removed from the `DO UPDATE` | scenario 7 fails, only it |
| Ownership checked after GitHub is called (list) | scenario 6 fails, only it |
| Install puts the organization in the redirect URL | scenario 1 fails, only it |
| Success redirect drops `installation_id` | scenario 10 fails, only it |
| `row_security = off` inside the listing transaction | scenario 10 fails, only it |
| `ConsumeState` back to `EXISTS`-then-`DEL` (5ms window) | `TestStateStore_ConsumeStateIsSingleUseUnderConcurrency` fails |

**Scenario 9 did not exist until the mutation testing demanded it.** Reading the organization from a query parameter instead of the token — the single most important property in this file — passed the entire suite. The scenario now sends a callback carrying orgB's id as a query parameter *and* a valid orgB bearer token against a state token minted by orgA, and asserts the row lands in orgA. A status code cannot show this; only the resulting row can.

The `ConsumeState` mutation is recorded honestly: a first attempt reported the test surviving, and re-running it with the mutated source verified in place showed the test failing as it should. The first run's mutation had not applied.

## Notes for what comes next

- **20-05 writes `github_installations` too**, from the `installation` and `installation_repositories` webhooks. It must use the same `ON CONFLICT … WHERE organization_id = $1` guard, or a webhook will silently move an installation between tenants — the exact thing scenario 7 forbids through the API.
- **Uninstall is not handled here.** 20-05 owns the `installation.deleted` event; `repositories.installation_id` is `ON DELETE SET NULL` (000010) and `docs/api-repositories.md` documents the recovery.
- **`GET /api/github/installations` is unpaginated.** Fine today; noted in the contract.
- **Phase 23 implements `docs/api-github-install.md`'s result table.** The `already_connected` row is the one to get right — the message is safe to display verbatim and must not be embellished with a lookup.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-09*
