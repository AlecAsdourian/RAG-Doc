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
    - "A uniqueness rule the schema cannot express — one that spans a join — belongs in the handler, and the migration should say so rather than claim the index covers it"
    - "A down migration that can be made impossible by the data its own up migration legalises must assert that first, with the recovery in the message. Relying on a later statement to fail gives the operator an error naming neither the rows nor the way out."

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

issues-created: [ISS-015, ISS-016, ISS-017]
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

The GitHub call sits **between** two transactions rather than inside one. A network round-trip inside a transaction pins a pooled connection for its duration; the cost is that the two halves are not atomic, and the upsert on `(project_id, github_repo_id)` is what makes that safe. (This paragraph said "the unique index on `(installation_id, github_repo_id)`" until migration 000011 dropped that index — the same stale claim review found in the code comment beside it, in a second place.)

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

What remains is ISS-015: matching is still method-blind.

## Second review round: the scanner fix had two more holes, and the rollback was broken

The first fix closed the nested-block hole and left two others open, both found by review.

**H1 — `r.With(mw).Post(...)` was still invisible.** The receiver pattern required an identifier before the dot, and a middleware-wrapped call has `)` there. `router.go` writes `r.With(middleware.Timeout(…)).Route(…)` twice, so this was not hypothetical — a destructive route guarded by an admin check is exactly the shape most likely to be written that way, and it would have passed with no test. `r.Method("POST", …)` and `MethodFunc` were invisible for the same reason. Fixed, and the receiver is now `[\w)\]]`.

**H2 — a new route nested under an already-tested prefix was covered for free.** Matching the leading static piece meant `POST /api/repositories/{id}/resync` reduced to `/api/repositories/`, which the existing DELETE test already contains. Phase 21's "sync now" endpoint is precisely that shape. The path is now split on every parameter and **all** its static segments must appear, so `/resync` has to show up too.

**H3 — `migrate down 1` failed partway and left the version table dirty.** 000011 legalises rows 000010 forbade, so recreating the old keys can be impossible. The down file relied on those recreations to fail, with a comment claiming the constraint was "recreated last" so it would refuse rather than discard — wrong twice: golang-migrate runs the file in one transaction, so statement order decides nothing, and the operator got a bare "could not create unique index". There is now an assertion first, naming the offending groups and the recovery. Measured: refuses cleanly, nothing applied, `migrate force 11` restores `11 | f` and the database is usable.

The verification table previously said `migrate up → down 1 → up | clean each time`. That was true **only on an empty database**, which is what I had tested.

**M1/M2 — the same repository could still land twice in one organization.** 000011's index is per-project, and an organization may hold several projects; the migration's comment claimed the old guarantee was "preserved wherever it matters", which was false. A duplicate would be ingested twice in Phase 21. `Connect` now resolves an existing repository across **all** of the organization's projects before inserting, and also adopts a pre-API row with no GitHub id whose `git_url` matches — that one used to become a permanent duplicate that could never be synced.

**M3 — the cascade test could not see an over-broad delete.** `WithTwoOrgs` gives orgA one repository, so `DELETE ... WHERE project_id = (…)` passed every assertion. orgA now has a second repository with its own chain, and it has to survive.

**M5** — a comment in `repositories.go` still credited the `(installation_id, github_repo_id)` index that this commit's own migration drops.

**M4 → ISS-016.** A relink re-queues a repository that is mid-`syncing`. There is no better answer available here (the in-flight run holds a token for an uninstalled App and will fail anyway), and no lease column to hand off with. The comment claiming it "must not stomp a 'syncing' run" was describing an intention, not the code. Phase 21 owns the fix.

### A correction to this file's own mutation claim

The line "Eight scanner mutations, each killed by exactly its own test" was not reproducible as written, and review measured that: four natural mutations survived the suite, and three of the eight killed four or five tests rather than one. Both halves of the claim were wrong.

The survivors are now covered — a stack that never pops, prefixes recorded in the wrong order, test files not excluded from scanning, and a marker on a group opener reaching the routes inside it. Chasing the ordering one turned up a real bug: a complete one-line `r.Route("/x", func(r chi.Router) { r.Post("/y", h) })` was getting no prefix at all.

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

## Third review round: an apostrophe was enough to open a hole

**N-H1 — prose apostrophes desynchronised the brace scan, and that handed a destructive route a free pass.**

The literal-blanking added in round two was a regex alternation whose rune branch, `'(?:\\.|[^'\\])*'`, had no bound and no `//` branch beside it. So an apostrophe in an English comment opened a "rune literal" that closed at the next apostrophe anywhere in the file, blanking every brace between them. `router.go` carries fifteen apostrophes inside `//` comments; it survived only because interleaved string literals happened to consume them first.

Measured on the real router: two ordinary comments — `// Don't add a sync endpoint here` and `// The stream's lifecycle …` — swallowed the `})` between them, and a top-level `r.Delete("/{id}", h.WipeEverything)` was reported as `DELETE /api/repositories/{id}` and matched by the existing repositories test. **A destructive endpoint, green, with no test.** `/*` written inside a `//` comment did the same through a second door.

Replaced with a single-pass scanner. The fix is ordering: **a comment is recognised before a literal**, so nothing inside a comment can open one — which is how Go itself reads the file. It emits two views (literals-and-comments blanked for brace counting, comments-only blanked for reading a route's path) and preserves length and newlines, which the prefix walk depends on.

**N-M2 — segments were matched independently anywhere in the file**, so the free pass survived in a narrower form: a lone `// TODO: cover /resync one day` covered the resync route. They must now appear on **one line, in order**. The cost is idiom-sensitivity — `path.Join(...)` reads as uncovered — and the failure text now says so, because a gate whose guidance does not fix its own red check gets disabled.

**N-M1 — the gate's instructions described a rule it no longer implemented.** Four places still said the leading prefix was enough, including the PR comment a failing author actually reads. `scripts/ci/README.md` had not been touched by the round-two commit at all.

**N-L7 — the widened receiver matched any library call**, so `buckets[0].Delete("tmp")` read as a route. Go paths must now start with `/`.

**N-L1 — the rollback guard was check-then-act.** A writer committing between the assertion and the ALTER put back exactly the duplicate the check had cleared. `LOCK TABLE repositories IN ACCESS EXCLUSIVE MODE` first; the ALTER takes that lock anyway.

**N-L2 / N-L3 — two load-bearing clauses were unpinned**, and mutation confirmed it: reversing the adoption `ORDER BY` (which would make an adoption collide with the new unique index — a reachable 500) and dropping the `OR github_repo_id IS NULL` half of the re-queue rule both survived the suite. Both now have tests.

**N-L4 / N-L5 / N-L6 / N-L8 — four more claims corrected.** The docs said "you will not get a duplicate" when three real gaps remain (URL spelling, pre-existing duplicates, a cross-project race); the verification table said 11/11 when ten subtests ran; a comment called the join "what scopes this" when RLS already does and the join is the second layer; and the rollback message said "resolve the rows named below" while naming only counts.

## Fourth review round: approved, with two of my explanations wrong

The reviewer approved. Two of its five LOW findings were **claims about why the fix works**, both measurably false, and both fixed here rather than filed — a maintainer told to guard the wrong invariant will reintroduce the hole.

**The stated mechanism for the N-H1 fix was wrong, in three places.** The comment, the docstring and this summary all credited *branch ordering* ("a comment is recognised before a literal"). Measured: swapping the branches changes nothing and all tests still pass, because they cannot both apply at one index — `//` starts with `/`, a literal with a quote. The real invariant is **statefulness**: `//` mode is sticky until the newline, so a quote inside a comment is never a delimiter. Corrected, and the docstring now says explicitly that the branches may be reordered but the mode must not be flattened.

**"Newlines and total length are preserved" did not mean what it said.** Length holds; line *counts* do not. `str.splitlines()` also breaks on `\v \f \x1c \x1d \x1e \x85    ` and a lone `\r`, which survive inside a literal and are blanked outside one — so the two views could disagree about which line a route is on. `route_prefixes` now uses `split("\n")`, pinned by a test carrying a genuine `\x0b` (the two-character escape proves nothing, which is how the first attempt at that test passed against the bug).

**Writing those two tests found a fourth real bug.** A comment carrying an unbalanced `(` — ordinary English prose — opened a diff join that swallowed the route lines after it, which were then reported at the comment's line number under the comment's prefix. Parens are now counted on the comment-stripped line.

Both of the tests written for this round initially **passed against the bug they were named for**, and only died once sharpened. That is the same failure as writing the conclusion before the measurement, one level down: a test that cannot fail is a claim, not evidence.

The remaining three findings are ISS-017.

## Verification

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...` | clean |
| `go test -p 1 ./...` | all pass, from a container rebuilt from scratch |
| `TestRepositoriesIsolation` | 6/6 |
| `TestRepositoriesConnect` | 12/12 |
| `TestDeleteReportsTheWholeCascade` | pass |
| `migrate up` → `down 1` → `up` on an EMPTY scratch database | clean each time, ends `11 \| f` |
| `down 1` on a database holding rows 000011 legalises | refuses by design, names the offending groups, applies nothing; `force 11` recovers |
| `pytest scripts/ci/test_check_isolation.py` | 34 pass |
| CI isolation scanner | PASS — `POST /api/repositories` and `DELETE /api/repositories/{id}`, both now actually resolved and matched |

`-race` was NOT run locally — this machine has no gcc, and `go test -race`
requires cgo. CI runs it (`backend-ci.yml`).

### Mutation testing

Original three:

| Mutation | Result |
|---|---|
| DELETE ignores `RowsAffected` (reports success for a cross-tenant delete) | scenario 3 fails, only 3 |
| Get returns 500 rather than 404 for another tenant's repository | scenario 2 fails, only 2 |
| ~~Connect skips the scoped installation check → scenario 4 fails, only 4~~ | **Stale, re-measured.** Scenario 4 now stays GREEN: the second-transaction re-read added in the first review round catches the cross-tenant case on its own. What fails instead is `WithoutCredentialsAvailabilityIsStillCheckedAfterAuthorization`. Two layers now guard that connect, which is the intent — but the row as written was no longer true. |

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

Second review round:

| Mutation | Result |
|---|---|
| No org-wide adoption lookup | both `Adopts…` subtests fail |
| Adoption matches `github_repo_id` only | `AdoptsALegacyRowWithTheSameURL…` fails, only it |
| Adoption scoped to the default project | `AdoptsARowFromANonDefaultProject…` fails, only it |
| DELETE takes every repository in the project | `TestDeleteReportsTheWholeCascade` fails (green before the sibling was seeded) |
| `canonicalUUID` degraded to a bare `uuid.Parse` | `NonCanonicalUUIDs…`, `MalformedCursor…` fail |
| Adoption `ORDER BY` reversed (legacy URL match wins) | `PrefersARealIDMatchOverALegacyURLMatch` fails, only it |
| Re-queue drops the `OR github_repo_id IS NULL` half | `AdoptingARowThatNeverHadAGitHubIDQueuesIt` fails, only it |

Third round, scanner:

| Mutation | Tests killed |
|---|---|
| Line comments not recognised at all (the original shape of N-H1) | `apostrophe_in_prose`, `block_comment_opener_inside_a_line_comment`, `double_slash_route_literal` |
| `/*` inside a `//` comment reopens block-comment mode | `block_comment_opener_inside_a_line_comment` |
| Segments matched anywhere in the file rather than on one line | `segments_must_share_one_line_in_order` |
| Segment order not required | `segments_out_of_order_on_one_line` |
| Path no longer required to start with `/` | `library_call_with_a_non_path_argument` |

Scanner, eight mutations. Three kill several tests rather than one, which is recorded here rather than rounded off:

| Mutation | Tests killed |
|---|---|
| Receiver requires an identifier again | `middleware_wrapped_routes` |
| `Method`/`MethodFunc` pattern removed | `chi_method_and_methodfunc` |
| Only the leading static segment required | `a_new_route_under_a_tested_prefix_is_not_free` |
| Literals/comments not blanked before brace counting | `braces_inside_literals…`, `double_slash_route_literal` |
| Prefix stack never pops | `braces_inside_literals…`, `route_after_a_closed_block` |
| Prefix recorded before the line's own opener | `route_sharing_its_groups_line` |
| Test files no longer excluded from scanning | `routes_registered_inside_a_test_file` |
| Block openers stop being a skip boundary | `double_slash_route_literal` |

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
