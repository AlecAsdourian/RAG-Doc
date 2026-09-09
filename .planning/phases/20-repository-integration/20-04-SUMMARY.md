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
    - "Authenticating as the APP proves a third-party object is real, never that the caller controls it. Those are different questions and only the second is authorization."

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
  - "The ISS-011 StateStore probe is deleted rather than kept: 20-04 gave the state store a real consumer, so the flow reports availability instead. It halves the handlers package runtime; it does not eliminate the dial, which still happens per router construction."
  - "The install callback FAILS CLOSED without the App's client credentials. Linking without the user-authorization leg is the vulnerability, so an unconfigured deployment refuses rather than falling back."

issues-created: [ISS-018]
issues-closed: []

duration: ~3 hours
completed: 2026-09-09
---

# Phase 20 Plan 04: the installation flow

**Where a GitHub-side fact becomes a tenant-side fact.** Four endpoints, one of them deliberately unauthenticated.

## The ordering is the security argument

`Callback` does five things and the order is the whole design:

1. **Consume the state token.** Atomic get-and-delete, so a replay loses the race rather than winning it. Invalid, expired and already-used are one answer.
2. **Read the organization out of the token** — not from the request, not from the caller's claim.
3. **Prove the CALLER controls this installation**, via the user-authorization code. Added in review; see below.
4. **Ask GitHub whether the installation is real**, before anything is written.
5. **Persist**, and treat a collision as a comprehensible situation rather than a fault.

Doing (5) before the rest is an open door. Doing (2) from the request is the subtler mistake, and a test had to catch it. **Omitting (3) entirely was a cross-tenant read of private source code**, and it shipped in the first version of this file.

## Two things the plan called out, both verified rather than assumed

**The callback is mounted outside `JWTAuthMiddleware`.** A browser following GitHub's redirect sends no `Authorization` header, so a callback inside the authenticated group returns 401 to every real installation — it fails 100% of the time, not intermittently. Scenario 3 is the regression guard: it asserts the route answers *something other than 401* without a token.

**A missing `state` is refused, never defaulted.** Installing from GitHub's own "Install App" button produces a redirect with `installation_id` and `setup_action` and no `state`. Defaulting to "the caller's current organization" would link an installation to whoever happened to be logged in — and on a public route there is no caller, so the fallback would have to invent one.

## The hole review found: installation takeover

The first version of this file proved an installation was **real** and called that authorization. It is not.

`GET /app/installations/{id}` authenticates as the **App**, so it succeeds for every installation of our App and says nothing whatsoever about who is asking. `installation_id` arrives in a query string. The state token says which of the *attacker's own* organizations to write into — which it did faithfully.

The exploit, reproduced end to end by the reviewer against the real router and database:

1. A victim installs the App from GitHub's own button, Marketplace, or the Configure flow. That redirect carries no `state`, so we answer `missing_state` — **and the installation is now live on GitHub and unlinked here.** The behaviour this plan documented as correct is the enabling condition.
2. An attacker starts a legitimate flow of their own and gets a genuine state token bound to their organization.
3. The attacker completes the callback with their own token and the victim's `installation_id` — visible to the victim at `github.com/settings/installations/<id>`, and in 20-05's webhook payloads.
4. `connected`. The attacker's organization now owns the link, and 20-03's `POST /api/repositories` will ingest the victim's private source through it, legitimately, because the row really is theirs now.

**The fix is the leg GitHub provides for exactly this.** With *Request user authorization (OAuth) during installation* enabled, the setup redirect also carries a `code`. It is exchanged for a user-to-server token, and the installation must appear in that user's own `GET /user/installations` before anything is written. Step 3 runs **before** the app-level lookup, so a stranger's probe never reaches it.

**It fails closed.** Without `GITHUB_APP_CLIENT_ID` / `GITHUB_APP_CLIENT_SECRET` the callback refuses to link at all, and `GET /api/github/install` returns 503 rather than sending someone to GitHub for an installation it could not finish — which would manufacture exactly the unlinked state the check exists to protect.

**An uncomfortable detail.** This PR had already added `gho_` to `redactSecrets`, with a comment saying 20-04 "put an OAuth-shaped flow in front of the App". There was no OAuth flow and no code exchange, so a `gho_` token could never have occurred. The redaction anticipated precisely the control that was missing — the artifact of a security measure shipped without the measure.

**Requires a GitHub UI change**, recorded in `docs/github-app-setup.md`: tick the authorization box, generate a client secret. Until that is done the flow correctly refuses to work.

## What the plan did not anticipate

**Task 3's endpoint was unreachable as specified.** It takes an installation id in its path, and nothing in the phase gave a UI one: the only place an id appeared was `repositories.installation_id`, so a user who had just installed the App — and therefore had no repositories — could not reach the picker that exists to help them add their first one. The flow did not compose.

Recorded as a REVISION NOTICE in `20-04-PLAN.md` rather than improvised, per the 19-03 pattern. Two additions: `GET /api/github/installations`, and the success redirect now carries `installation_id` so the immediate case needs no extra round trip.

## Things found in passing

**`ValidateState` was check-then-act.** `EXISTS` then `DEL`, two round trips, and it returned `true` when the delete failed — with a comment reasoning that reuse-once beat blocking a valid user. A defensible trade for a login button; the wrong one for a token that authorises linking a tenant to a GitHub installation. Now `GETDEL`, and the legacy entry point is a wrapper over it, so the direct-OAuth handlers inherit the fix.

**`generateSecureToken` ignores `rand.Read`'s error**, which on a failing entropy source yields 32 zero bytes — a predictable CSRF token, which is no CSRF token. The new flow uses its own generator that returns the error. The old one is untouched and still used by the unmounted OAuth handlers.

**`docs/github-app-setup.md` claimed `.env.example` documented the App variables "as of 20-02".** It did not — 20-02 added the code that reads them and never added them to the template, so anyone following the runbook found nothing matching. Both fixed.

**The ISS-011 StateStore probe dialled Redis solely to log whether Redis was reachable**, with five retries, on every router construction. Its stated justification was that "Phase 20's GitHub App flow will want it" — now true, so the flow reports it and the probe is gone.

**Correction to an earlier version of this section**, measured by review: it claimed "the probe's only remaining effect was a Redis connect timeout on every router construction — the handlers package went from 17s to 0.3s". Both halves were wrong. `auth.NewStateStore()` still runs on every router construction where nothing is injected, so the dial was moved rather than removed; measured, the package is 1.86s with Redis up and 37.5s with Redis down, against 94.3s with the probe restored. Removing the probe roughly halved it. The 0.3s figure came from a run of one test function, not the package.

**GitLab is deleted** (Task 4): both handlers, the OAuth config entry, the `.env.example` keys, and a stale comment in `webhook.go`. `go build` clean, no references remain.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, `gofmt` | clean |
| `go test -p 1 ./...` | all pass, container rebuilt from scratch |
| `TestGitHubInstallFlow` | 16/16 |
| `TestStateStore_*` | 8/8; the concurrency case now warms the pool and races 5 rounds |
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
| `ConsumeState` back to `EXISTS`-then-`DEL` | `TestStateStore_ConsumeStateIsSingleUseUnderConcurrency` fails |
| **User-control check removed** (the shipped-vulnerable state) | scenario 11 fails, only it |
| Fail-closed becomes fail-open with no client credentials | scenario 12 fails, only it |
| User check runs after the app-level lookup | scenario 11 fails, only it |
| `Install` stops refusing an unconfigured App | scenario 12 fails, only it |
| Availability checked before ownership on the list path | scenario 13 fails, only it |
| Suspended-installation check removed | scenario 14 fails, only it |
| `newStateToken` returns 32 zero bytes | `TestNewStateToken_IsNotPredictable` fails |

Second review round:

| Mutation | Result |
|---|---|
| Query scrubber removed | scenario 15 fails |
| Scrubber blanks `state` but not `code` | scenario 15 fails |
| Empty-slug refusal removed | scenario 16 fails, only it |
| `has_next` forced to false | `TestListInstallationRepositoriesPage_HasNextAtTheBoundaries` fails |
| Client secret / code not redacted by value | `TestExchangeUserCode_DoesNotLeakTheClientSecret` fails |
| Empty code reaches GitHub | `TestVerifyUserControlsInstallation_RefusesAnEmptyCode…` fails |
| Page bound fails open | `TestUserHasInstallation_FailsClosedAtThePageBound` fails |

### A correction to this file's own mutation claim

The `ConsumeState` row previously said the check-then-act mutation was killed, with a paragraph asserting a first false negative had been re-verified "with the mutated source verified in place". Review measured it surviving three independent runs, and was right: the test built a fresh store and raced **once**, so it only ever measured a cold connection pool, which serialised the racers enough that even a non-atomic implementation produced one winner. Instrumented, the mutation won round 0 every time and then leaked 6–16 winners in later rounds.

The test now warms the pool and races five rounds. The mutation dies without needing an artificial sleep.

**Scenario 9 did not exist until the mutation testing demanded it.** Reading the organization from a query parameter instead of the token — the single most important property in this file — passed the entire suite. The scenario now sends a callback carrying orgB's id as a query parameter *and* a valid orgB bearer token against a state token minted by orgA, and asserts the row lands in orgA. A status code cannot show this; only the resulting row can.

Scenarios 11 and 12 exist for the same reason one level up: the takeover passed every test in this file, because every one of them supplied a caller who legitimately owned what they were asking about.

## Second review round: the fix promoted two log leaks from cosmetic to serious

Approved, with two MEDIUM findings — both credential-handling gaps that **the H1 fix itself made security-relevant**, because `code` is now the thing standing between an attacker and someone's private source.

**N1 — the App client secret and the `code` survived redaction into a logged error.** The token exchange carries its credentials in the request BODY, and `redactSecrets` only knows token *prefixes*; a GitHub App client secret has none. Measured with an upstream echoing the request body — the proxy/WAF case `redactSecrets`' own doc comment cites as its reason for existing — both appeared verbatim in an error that `Callback` then logs. Added `redactValues`, which removes exact strings, applied to the client secret, client id and code.

**N2 — `code` and `state` were written to the application log in full, on every callback.** `httplog` builds its `url` field from `r.RequestURI`, query string included. This was cosmetic before the H1 fix. It is not now: the `missing_state` path refuses **before** exchanging the code, so a victim's code sits unconsumed and valid for its full lifetime — in our logs, replayable by anyone who can read them. A `scrubSensitiveQuery` middleware, registered before the logger, blanks those parameters in `RequestURI` while leaving `r.URL` intact for the handler.

Scenario 15 asserts on **captured log output**, not on the scrubbing function, because the property that must hold is "no logger renders it" — which depends on which field the logger chooses, not on anything this code controls. `LogWriter` was added to `api.Config` to make that observable.

I did **not** take the suggested mitigation of exchanging-and-discarding the code on the `missing_state` path: that route is unauthenticated, so it would let anyone force us to call GitHub.

**N3** — `ghr_` added to the redaction prefixes; the exchange returns a refresh token when "expire user authorization tokens" is enabled.

**N4** — three unpinned behaviours, now pinned: the empty-slug refusal (scenario 16 — scenario 12 tripped the credentials guard first, so the slug branch was never reached), the fail-closed answer at `userHasInstallation`'s page bound, and refusing an empty code without calling GitHub.

**N5** — missing client credentials now panic at construction, exactly like a missing slug, and for the identical reason: a deployment that has credentials but cannot use them should say so at startup. Scoped to `githubClient != nil`, so a machine with no App still boots.

**The one residual, and it is structural:** a code belonging to a *different user* would pass, because the code **is** the user's identity. The whole control rests on `code` confidentiality — which is exactly why N1 and N2 mattered enough to fix rather than file.

## Carried, not fixed

- **The state store is built once at router construction and never closed or retried** — ISS-018. Documented in `local-development.md` rather than fixed here, because a lazily-dialling store is a change to shared infrastructure rather than to this flow.
- **`inst.ID` vs the query's id.** Persisting GitHub's value rather than the caller's is defensive, and it is not pinned: a stub cannot return a different id than it was asked for without simulating a GitHub bug, so the mutation is unobservable by construction. Recorded rather than dressed up as covered.
- **`per_page=abc` and `page=abc` give differently-shaped 400 messages.** Cosmetic.

## Notes for what comes next

- **20-05 writes `github_installations` too**, from the `installation` and `installation_repositories` webhooks. It must use the same `ON CONFLICT … WHERE organization_id = $1` guard, or a webhook will silently move an installation between tenants — the exact thing scenario 7 forbids through the API.
- **Uninstall is not handled here.** 20-05 owns the `installation.deleted` event; `repositories.installation_id` is `ON DELETE SET NULL` (000010) and `docs/api-repositories.md` documents the recovery.
- **`GET /api/github/installations` is unpaginated.** Fine today; noted in the contract.
- **Phase 23 implements `docs/api-github-install.md`'s result table.** The `already_connected` row is the one to get right — the message is safe to display verbatim and must not be embellished with a lookup.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-09*
