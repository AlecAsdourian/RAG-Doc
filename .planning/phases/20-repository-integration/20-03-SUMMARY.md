---
phase: 20-repository-integration
plan: 03
subsystem: api

requires:
  - phase: 20-01
    provides: db.TenantScoper — this is its first production consumer
  - phase: 20-02
    provides: schema, github.Client, the default-project invariant
provides:
  - "GET/POST /api/repositories, GET/DELETE /api/repositories/{id} — tenant-scoped, cursor-paginated"
  - "docs/api-repositories.md — the contract Phase 23 builds against"
  - "Migration 000011 — (project_id, github_repo_id) is a repository's identity; sync_state CHECK"
  - "api.Config.GitHubRepositories — the seam that makes Connect testable"
  - "A CI isolation scanner that can see routes inside nested chi.Route blocks"
affects: [20-05 (the webhook writes the same rows), 21 (sync_state is the queue's input), 23 (frontend)]

tech-stack:
  added: []
  patterns:
    - "Authorization is checked before service availability — the reverse order turns a 503 into an enumeration oracle"
    - "A cross-tenant write is asserted on the resulting ROW, never on the status code — RLS makes it match nothing rather than error"
    - "Key a row's identity on what does not change. Keying on the installation broke the documented reinstall recovery, because the installation is exactly what a reinstall changes."
    - "A handler that talks to an external service takes an interface, not the concrete client — otherwise its success path is untestable and ships unexecuted"
    - "uuid.Parse is a parser, not a validator: canonicalise or refuse before a UUID from outside reaches Postgres"

key-files:
  created:
    - services/backend/pkg/api/handlers/repositories.go
    - services/backend/pkg/api/handlers/repositories_isolation_test.go
    - services/backend/pkg/api/handlers/repositories_connect_test.go
    - services/backend/migrations/000011_repository_identity.up.sql / .down.sql
    - docs/api-repositories.md
  modified:
    - services/backend/pkg/api/router.go
    - scripts/ci/check-isolation-tests.py, test_check_isolation.py, README.md
    - .github/workflows/isolation-check.yml

key-decisions:
  - "DELETE genuinely deletes, cascading to ingestion_runs, chunks, retrievals AND user-written feedback. A disconnected repository whose contents stayed searchable is the wrong answer for this product, and worse if it was disconnected because it should not have been indexed. The response reports all three counts so a client can say what was lost."
  - "POST takes github_repo_id, not a git URL. GitHub's numeric id is stable across renames and transfers."
  - "404 covers 'not yours', 'does not exist' and 'not visible to that installation', identically — otherwise the endpoint is an existence oracle for other tenants' ids."
  - "Connect is split across two transactions with the GitHub call between them, rather than holding a pooled connection across a network round-trip. The upsert makes a duplicate connect harmless."
  - "Re-connecting refreshes metadata and returns 201 rather than erroring. It does NOT reset sync_state unless the installation changed — a metadata refresh must not restart a run already in flight, but a repository reached through a new credential has to be fetched again."
  - "git_url stops being a key for GitHub-sourced rows (000011). For those it is DERIVED from github_repo_id, and a rename frees the old URL, so two rows can briefly hold the same stored URL. That must not be a 500 on an unrelated connect."
  - "The isolation scanner was fixed rather than worked around. Flattening the routes or planting a `/{id}` literal in the test would have turned the check green while leaving POST /api/repositories unchecked."

issues-created: [ISS-015]
issues-closed: []

duration: ~1 hour
completed: 2026-09-08
---

# Phase 20 Plan 03: repositories CRUD

**Four endpoints, tenant-scoped, cursor-paginated. The first production consumer of `db.TenantScoper`.**

## The bug the tests caught

Scenario 4 — orgA connecting through orgB's installation — expected 404 and got **503**.

The handler checked "is the GitHub App configured?" *before* resolving the installation. With credentials missing, a caller naming a **real** installation id belonging to another tenant got 503, and a made-up one got 404. The difference tells them which ids exist.

So availability is now checked after authorization. The general form: **a check that can distinguish valid from invalid inputs must not run before the check that decides whether the caller may ask at all.** Cheap to get wrong, because refusing early reads as defensive.

## Two guards on the cross-tenant connect

`POST /api/repositories` resolves the installation with a scoped read inside a tenant transaction, so another tenant's installation is simply invisible and the caller gets 404. Migration 000010's `trg_assert_installation_tenant` is the backstop underneath. It is re-read inside the persisting transaction as well, so the window around the GitHub call is covered.

Two layers because they fail differently: the scoped read gives a clean 404 the client can act on, and the trigger catches anything that reaches the database by another route. The trigger alone would surface as a 500.

## What the endpoints do

`GET /api/repositories` — cursor-paginated on `(created_at, id)`, not OFFSET. **The set shifts while you page through it**: 20-05's webhook inserts repositories with no user action, and offset pagination skips and duplicates already-visible rows when that happens.

It does **not** guarantee you see every new row, and an earlier version of this summary said it did — in four places, including the handler comment and the client contract. `created_at DEFAULT NOW()` is transaction-start time while visibility begins at commit, so a write that started before your cursor and committed after it sits permanently behind you. Demonstrated by the reviewer end-to-end. Postgres exposes no commit-order column, so the contract now tells clients to re-poll page one rather than trusting a held cursor.

`POST /api/repositories` — takes `github_repo_id` and `installation_id`. Verifies the installation is the caller's, asks GitHub whether that installation can actually see the repository, then persists what GitHub reported. Re-connecting refreshes metadata and returns 201, so a retry is harmless.

The GitHub call sits **between** two transactions rather than inside one. A network round-trip inside a transaction pins a pooled connection for its duration; the cost is that the two halves are not atomic, and the unique index on `(installation_id, github_repo_id)` is what makes that safe.

`DELETE /api/repositories/{id}` — deletes, cascading to everything ingested. The response reports how many chunks, ingestion runs **and pieces of feedback** went with it, so a client can show what was lost instead of a bare 204. Feedback is the only one of the three a user cannot get back by re-ingesting, and it was being destroyed silently.

## Review round: the CI check was red, and this file said it was green

`check-isolation` failed on PR #21 with a bot comment. This summary's verification table claimed `CI isolation scanner | PASS`. **Fifth time a summary in this project has asserted a property the code did not have**, and the first where a required check was visibly red while the claim sat next to it.

The scanner had a real bug underneath, and it was worse than the failure it produced.

`_iter_logical_added_lines` joins consecutive added lines while parentheses are unbalanced, so a wrapped `r.Post(\n "/x",\n)` is still detected. But `r.Route("/repositories", func(r chi.Router) {` also has an unclosed paren — until the `})` four routes later. So the **entire block collapsed into one logical line**, and an anchored pattern with a greedy `.*` reported only the **last** registration in it. Result:

- `DELETE` was reported as path `/{id}` at the line of the `r.Route(` opener — a path no test can contain, from the wrong line.
- `POST /api/repositories` — the endpoint scenario 4 exists for — **was never checked at all**.
- And `r.Post("/", …)` alone would have matched vacuously anyway: the old coverage test was `"/" in text`, true of every Go file.

So the gate had been verifying nothing for either mutation endpoint on this PR, and would do the same for any route added inside `r.Route("/api", …)` — which is every protected route in this codebase.

Fixed rather than worked around. Flattening the routes or planting a `/{id}` literal in the test would have turned the check green and left the hole.

| Fix | Effect |
|---|---|
| A block-opening line ends the join | each route is its own logical line again, with its own line number and its own skip-marker window |
| Every match on a logical line is yielded, not just the last | a shape the opener check does not recognise degrades to over-reporting, not to silence |
| Enclosing `Route`/`Mount` prefixes resolved from the file on disk | `DELETE /api/repositories/{id}`, not `/{id}` — the opener is usually a context line, so the prefix is not in the diff |
| Coverage matches the full path or its static prefix | a test building `"/api/repositories/" + id` counts; a path that resolves to `/` never counts |

Eight scanner mutations, each killed by exactly its own test. What remains is ISS-015: matching is still method-blind.

## Review round: seven more findings

**H2 — `Connect` 500'd whenever `git_url` collided, and the documented recovery was a dead end.** `repositories` carried two keys that both claimed to identify a repository: `UNIQUE (project_id, git_url)` from 000001 and `UNIQUE (installation_id, github_repo_id)` from 000010. The upsert could only name one. Uninstalling the App nulls `installation_id`, which drops the row out of the partial index, so reconnecting after a reinstall fell through to the `git_url` key and raised 23505 — and nothing else relinks the installation. `docs/api-repositories.md` told clients to expect exactly that flow.

Migration 000011 picks one identity: `(project_id, github_repo_id)`. The general form — **key on what does not change**. The installation is a credential, and a reinstall is precisely the event that changes it.

**M1 — `uuid.Parse` again.** `decodeCursor` validated with it and then forwarded the raw string, so `urn:uuid:…` parsed in Go, was rejected by Postgres, and surfaced as a 500 where the docs promise 400. `pkg/db/tenant.go` documents this trap forty lines from here. Fixed as a class, not an instance: one `canonicalUUID` helper, used by `Get`, `Delete` and the cursor — `Get`/`Delete` had the same latent 500 and the review had only measured the cursor.

**M3 — `Connect`'s success path had never been executed by anything.** The handler held a concrete `*github.Client` with an unexported `baseURL`, so no test could reach past the first check; the isolation suite runs with no credentials and stops at the 503. The project join, the upsert and the twelve-column scan all shipped unrun — which is how H2 survived. Now an `InstallationRepositoryLister` interface plus `api.Config.GitHubRepositories`, the same seam `auth.TokenValidator` already provides. Nine new subtests.

The nil handling is deliberate: a nil `*github.Client` assigned to an interface makes the interface **non-nil**, which would defeat the handler's own guard and panic in a degraded deployment. `router.go` only assigns when there is really a client.

**M4 — 500 where 404 belongs.** The installation can be deleted in the window between the two transactions. `Connect` re-reads it in the second transaction now and returns the same 404 as "not yours"; "your organization has no default project" gets its own error rather than sharing that 500.

**M5 — doc claims that were not true.** `sync_state` on a re-connect (it is not reset — now stated, and made conditional on the installation changing), the cascade chain (`feedback` goes too, and `chunks` hang off `repositories` directly), `created_at` shown as `Z` when it carries an offset, and no 401/403/500 anywhere in the document.

**M6 — scenario 6 could not see the pagination off-by-one.** Deleting `repos = repos[:limit]` left all six scenarios green while clients received `limit+1` rows and overlapping pages. One assertion added.

**L7 — trailing garbage after the JSON object was accepted.** `Decode` reads one value and stops; `dec.More()` closes it. The same shape exists in `user_orgs.go` — left alone as a sweep rather than widened into this plan.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...` | clean |
| `go test -p 1 ./...` | all pass, from a container rebuilt from scratch |
| `TestRepositoriesIsolation` | 6/6 |
| `TestRepositoriesConnect` | 9/9 |
| `TestDeleteReportsTheWholeCascade` | pass |
| `migrate up` → `down 1` → `up` on a scratch database | clean each time |
| `pytest scripts/ci/test_check_isolation.py` | 18 pass |
| CI isolation scanner | PASS — `POST /api/repositories` and `DELETE /api/repositories/{id}`, both now actually resolved and matched |

### Mutation testing

Original three:

| Mutation | Result |
|---|---|
| DELETE ignores `RowsAffected` (reports success for a cross-tenant delete) | scenario 3 fails, only 3 |
| Connect skips the scoped installation check | scenario 4 fails, only 4 |
| Get returns 500 rather than 404 for another tenant's repository | scenario 2 fails, only 2 |

Review round:

| Mutation | Result |
|---|---|
| Upsert keyed on `(installation_id, github_repo_id)` again | 4 connect subtests fail — the arbiter no longer matches any index, so every connect breaks |
| `DO UPDATE` stops relinking `installation_id` | `ReconnectAfterUninstall…`, `PlainReconnect…` fail |
| Never re-queue on relink | `ReconnectAfterUninstallRelinksTheSameRow` fails, only it |
| Always re-queue on any reconnect | `PlainReconnectRefreshesMetadataWithoutRequeueing` fails, only it |
| `canonicalUUID` degraded to a bare `uuid.Parse` | `NonCanonicalUUIDs…`, `MalformedCursor…` fail |
| `dec.More()` removed | `TrailingGarbage…` fails, only it |
| Installation vanishing mid-connect is a 500 again | `InstallationDeletedMidConnectIs404Not500` fails, only it |
| Feedback goes uncounted | `TestDeleteReportsTheWholeCascade` fails |
| Page not truncated to `limit` | scenario 6 fails, only it |

Scenarios 3 and 4 assert on the resulting **row**, not the status code. Under RLS a cross-tenant write matches nothing rather than erroring, so a status-only assertion passes against a handler that did the wrong thing — the trap this project has fallen into before.

Scenario 4 now runs against a **working** stubbed GitHub client. It previously passed with the client nil, meaning the endpoint was switched off rather than refusing; the only thing that can refuse it now is the tenant scope.

The cascade test asserts orgB's chunks, runs and feedback are untouched rather than counting orgA's own rows afterwards. A scoped count of orgA's feedback post-delete cannot tell deletion from invisibility — the RLS path runs through the repository that just went. (Writing that check the naive way tripped ISS-013 live: an unscoped read of `feedback` returned SQLSTATE 22P02.)

## Notes for what comes next

- **20-05 writes these same rows** from webhooks. Its inserts must go through a tenant scope too; the webhook has no request context, so it resolves the tenant from the installation and builds one. It also upserts on `(project_id, github_repo_id)` — 000011's key, not 000010's.
- **`sync_state` is Phase 21's input.** Everything connected here starts `pending` and nothing moves it yet. It now has a CHECK constraint (closes 20-02's L6).
- **Repositories connected before this API may not be in the default project.** `docs/api-repositories.md` says so; nothing should assume one project per organization.
- **`redactSecrets` still misses `gho_`** (carried from 20-02). 20-04's OAuth flow produces those.
- **ISS-015** — the scanner's coverage match is method-blind.

---
*Phase: 20-repository-integration*
*Completed: 2026-09-08*
