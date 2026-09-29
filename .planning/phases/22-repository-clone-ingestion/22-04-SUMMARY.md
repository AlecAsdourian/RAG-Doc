---
phase: 22-repository-clone-ingestion
plan: 04
subsystem: backend + workers
tags: [github-app, tokens, internal-api, tenancy, archive, tarfile, security, mutation-testing, redaction]

requires:
  - phase: 20-02
    provides: "the GitHub App client, its App JWT, its installation tokens and their measured shape (ghs_ plus 383 characters, not fixed)"
  - phase: 21-02
    provides: "ingestion_jobs, the lease, and the terminal-write fence `id + lease_owner + state = 'running'`"
  - phase: 21-05
    provides: "sanitize_error and the redaction patterns the worker's messages go through"
  - phase: 21-06
    provides: "the claim-time installation read this route repeats, and the 'assert on captured log output' practice"
  - phase: 21-07
    provides: "the one-byte-identical-404 rule, and lease_owner deliberately absent from every API response"
provides:
  - "github.Client.RepositoryToken: a one-repository, contents:read, one-hour token, minted through a body-carrying request path that shares do()'s redaction, and checked against the scope GitHub reports back"
  - "github.ScopedToken (token excluded from JSON and %v), github.WithBaseURL, github.NewClientFromEnv; the client is built once in main.go and shared by both listeners"
  - "pkg/internalapi: POST /internal/jobs/{id}/repository-token on a second listener (INTERNAL_ADDR, default 127.0.0.1:8081), marked responses, one byte-identical 404, two distinct 409s"
  - "workers.fetch.client: request_token, RepositoryToken (redacted renderings), TokenRefused / InternalApiMisrouted / InstallationSuspended / InstallationUninstalled / TokenRequestFailed"
  - "workers.fetch.archive: resolve_head, download_archive, extract_archive, collect_tree, fetch_repository (a context manager), sweep_stale_workdirs, Limits, FetchRejected / FetchFailed, FetchedTree"
  - "workers.fetch.filters: the U7 deny-list, the vendored/generated/lockfile rules, the extension map, the generated-Go header"
  - "docs/internal-api.md"
affects: [22-05 (wires request_token and fetch_repository into full_ingest, maps the exceptions onto Phase 21's endings, adds the compose wiring, proves the token route live), 24 (INTERNAL_ADDR is never published)]

tech-stack:
  added: ["httpx (explicit; it arrived only through openai before)"]
  patterns:
    - "A queue-wide read that must exist lives on its own listener, so 'never on the public router' is a matter of network reachability rather than middleware order"
    - "A response the client will act on silently (a 404 read as 'the lease is gone') carries a marker; anything unmarked fails loudly"
    - "One HTTP path in a client, so there is one place a response body becomes an error string and one redaction on it"
    - "Fail closed on what the upstream REPORTS a credential carries, not on what was requested"
    - "Judge an archive member by its header before writing it; apply the name filters before the write so a secret never touches the disk; then judge the tree as it exists on disk"
    - "Count the bytes a stream expands to as they are read, not the sizes the headers claim"
    - "Re-raise transport exceptions `from None`: a chained traceback prints the original's message, which is where the URL is"
    - "Every 'never logged' claim is asserted on captured log output with the exception logged exc_info=True — which is how this plan found httpx logging the download link"

key-files:
  created:
    - services/backend/pkg/internalapi/repository_token.go
    - services/backend/pkg/internalapi/repository_token_isolation_test.go
    - services/backend/pkg/github/scoped_token_test.go
    - services/workers/workers/fetch/__init__.py
    - services/workers/workers/fetch/client.py
    - services/workers/workers/fetch/archive.py
    - services/workers/workers/fetch/filters.py
    - services/workers/tests/fetch/test_fetch_client.py
    - services/workers/tests/fetch/test_archive_hostile.py
    - services/workers/tests/fetch/test_filters.py
    - docs/internal-api.md
  modified:
    - services/backend/pkg/github/client.go
    - services/backend/pkg/api/router.go
    - services/backend/main.go
    - services/workers/requirements.txt
    - .github/workflows/backend-ci.yml

key-decisions:
  - "The lease owner is the credential, and it is treated as one on the worker's side too: never in a message, never in a log (asserted)"
  - "The marker is set by the handler, not by router middleware, so a wrong path on the internal listener is unmarked like any other misroute"
  - "A malformed request body is a marked 400, not the 404: disguising a worker bug as a lost lease would let the job die quietly"
  - "RepositoryToken refuses a token GitHub reports as wider than asked for (more than the one repository, contents other than read, anything beyond metadata) — an inferred reading of the contract, validated live in 22-05"
  - "The name filters run before the write, so a committed .env never lands on the worker's disk; the walk applies them again to what is on disk"
  - "The expansion counter counts the tar stream, headers included: the strictest reading of 'the bytes the archive expands to', and it catches a header bomb"
  - "The file cap is applied at extraction to the files that pass the name filters (bounding directory entries) and again after the content filters; the conservative reading"
  - "httpx's and httpcore's loggers are held at WARNING by the fetcher, because httpx logs every request URL at INFO, redirect link and ?token= included (measured)"

issues-closed: []
issues-updated: [ISS-037 (filed)]
review: "PR #52 — reviewer A (security, token route): APPROVE WITH NITS; M1–M8 re-run and killed, no cross-serving path, byte-identical 404 with no timing oracle (516.7µs vs 516.8µs medians); three lows and five nits. Reviewer B (fetcher, tests, docs): CHANGES REQUESTED; two highs in the extractor (H1 sparse members bypass the expansion cap, H2 a full disk yields a truncated tree indexed as complete), a medium (a NUL past the probe), a lifecycle medium (job directories keyed on the id alone), nine lows; mealie reproduced with every figure identical, tests/fetch 141/141 on 3.12 and 3.11. Everything applied 2026-09-29 in 4787148 (A) and fbf1362 (B); each high reproduced in a container before the fix and measured after. Section 14."
duration: ~9h, plus ~5h applying the two reviews
completed: 2026-09-29
---

# Phase 22 Plan 04: fetching a repository safely

**The App's private key stays in the backend. The worker gets a one-hour,
one-repository, `contents: read` token — only while it holds a live lease on
a `running` job — from a listener that is never published; it fetches the
repository as an archive at an exact commit, extracts it streaming under the
U6 caps, never writes a secret-looking file to disk, and never logs the token
or the download link.** Every guard is mutation-checked; two of the checks
found real problems on their first run (below).

Locked decision implemented: **P10** (U4 token, U5 archive, U6 caps with the
user's 2026-09-17 lock that 500 MB applies to both the download and the
expansion, U7 filters).

---

## 1. The internal route and its listener

**`POST /internal/jobs/{id}/repository-token`**, body `{"lease_owner": "…"}`,
served by `pkg/internalapi`'s router on a **second `http.Server`** bound to
`INTERNAL_ADDR` (default `127.0.0.1:8081`). `main.go` starts it only when
GitHub App credentials are present (WARN otherwise, as the public side
degrades) and shuts it down with the public server. The GitHub client is now
built **once**, in `main.go` (`github.NewClientFromEnv`), and handed to both
routers through `api.Config.GitHubClient`; the public router no longer reads
the App environment.

The route, in order:

1. **The lease** — one unscoped statement on the pool, the terminal-write fence
   plus a live lease:
   `WHERE id = $1 AND lease_owner = $2 AND state = 'running' AND lease_expires_at > NOW()`.
   The organization comes *out* of this statement; that is the pre-tenant read
   the separate listener exists for.
2. **The installation** — in a tenant transaction for that organization
   (`auth.ContextWithOrgID` + `db.TenantScoper`): `github_repo_id`, the
   installation's numeric id, `suspended_at`, `uninstalled_at`.
   `uninstalled_at` first, because a reinstall clears both.
3. **The mint** — `github.Client.RepositoryToken`.
4. **One log line** — job, organization, repository, GitHub repository id,
   installation, name, expiry. Never the token, never the lease owner.

| Status | Body | When |
|---|---|---|
| 200 | `{"token","expires_at","full_name","default_branch"}` | live lease, usable installation, GitHub minted |
| **404** | `{"error":"no_live_lease"}` | every miss (section 3) |
| 409 | `{"reason":"installation_suspended"}` | suspended |
| 409 | `{"reason":"installation_uninstalled"}` | uninstalled, missing, or repository not linked |
| 400 | `{"error":"bad_request"}` | body not `{"lease_owner": "<non-empty>"}` |
| 502 | `{"error":"github_unavailable"}` | the mint failed |

**Every response the route writes carries `X-Rag-Internal: repository-token/1`**
(set first thing in the handler, so a timeout or a recovered panic carries it
too). Anything the route did not answer — chi's 404 for a wrong path on this
listener, chi's 405, the public router's 404 — is unmarked. The worker treats
a 404 as "the lease is not mine" only when marked **and** the body is the fixed
one; anything unmarked is `InternalApiMisrouted`, a plain exception that fails
the job loudly with the host in `last_error`. Scenario7 proves the public
router answers the path with an unmarked `404 page not found`.

`docs/internal-api.md` records the contract, the lease-as-credential
argument, the residual risk (a process with the worker's database access can
read every running job's lease and obtain tokens for those repositories —
still one repository, read-only, an hour) and Phase 24's requirement that the
address is never published.

**Since the review:** the listener **refuses to bind every interface**
(`:8081`, `0.0.0.0`, `::`, the `inet_aton` shorthands) unless
`INTERNAL_ADDR_ALLOW_ALL_INTERFACES=true` is set, and then warns on every
start; compose binds the service name (`INTERNAL_ADDR=backend:8081`).
`internalapi.CheckListenAddr` judges it and `listen_addr_test.go` pins the
spellings. The body is parsed **exactly** — one object, one `lease_owner`, one
non-empty string, no repeated key, no trailing bytes (`parseLeaseBody`,
`lease_body_test.go`, four more cases in Scenario5). The listener logs through
the same `slog` handler `LOG_FORMAT` gives the public side. And **the lease
gates issuance, not validity** — measured by reviewer A: after the lease
expires the route refuses, but a token it already issued stays valid for
GitHub's hour — so 22-05 revokes it (`DELETE /installation/token`, authenticated
by the token itself) when the fetch ends and on `LeaseLost`. The worker's
token client is built with `trust_env=False`: `httpx` would otherwise route the
lease owner and the token through an `HTTP_PROXY` from the environment, and a
forward proxy relays the marker, so nothing would fail loudly
(`test_the_token_client_ignores_proxy_environment_variables`, against a real
recording proxy).

## 2. The token's scoping, as observed

`RepositoryToken` posts, through the new body-carrying path (`doJSON` →
`request`; `do` is now `request` with no body, so there is one redaction on
one path):

```json
{"repository_ids":[1103353668],"permissions":{"contents":"read"}}
```

`TestRepositoryToken_ScopesToOneRepositoryReadOnly` and Scenario1 decode the
body the fake GitHub received and assert the key **set** is exactly
`{repository_ids, permissions}`, `repository_ids` decodes to exactly the one
id, and `permissions` to exactly `{"contents":"read"}` — not the presence of a
substring. The mint is signed with the App JWT; the follow-up
`GET /repositories/{id}` (by numeric id, so it survives a rename) carries the
**new** token, and its `full_name` / `default_branch` are what the worker gets.
**No cache**: three asks are three mints (`TestRepositoryToken_NeverCaches`).

**Beyond the plan, fail-closed on what GitHub reports back:** the response
must list exactly the requested repository, `contents` must be `read`, and no
permission beyond `contents` and `metadata` may appear (GitHub always adds
`metadata: read`). Eight refusals in `TestRepositoryToken_RefusesAScopeWiderThanAsked`,
each naming its own cause — since the review, "no `repositories` field" and
"no `permissions` object" are distinct from "0 repositories" and
`contents=""`, so 22-05's live proof can read which happened — and a refused
token is never used for the lookup. **Inferred**, from GitHub's documentation
of the access-tokens response; verified against the fake; 22-05's live proof
is what confirms it against the real API. Reviewer A confirmed the check would
refuse a valid real response only if GitHub omitted `permissions` entirely,
omitted `repositories` despite `repository_ids`, or added an implicit
permission other than `metadata` — all loud first-mint failures naming the
cause. The lookup error is redacted **by value as well as by prefix**, and the
by-value rule redacts any run of eight or more characters that is a prefix of
the token: `request()` keeps 300 bytes of an upstream body, so an echoed
387-character token is truncated, and the test for this found an exact-match
redaction letting 270 characters through
(`TestRepositoryToken_LookupErrorRedactsTheTokenByValue`,
`TestRedactValues_CoversATruncatedEcho`). `GET /repositories/{id}` is the
undocumented by-id alias; the fallback, `GET /repos/{full_name}` from the mint
reply's `repositories[0].full_name`, is noted beside it for 22-05.

`ScopedToken` excludes the token from JSON (`json:"-"`) and from `%v`, `%+v`,
`%s`, `%#v`, `Sprint` and `String()`; the route copies it into its own
response struct explicitly.

## 3. The byte-identical 404

Scenario3 asserts one body across thirteen causes: wrong owner; the other
organization's owner; unknown id; superseded (lease still attached — which is
why `state = 'running'` does its own work); expired lease; completed; queued
with no lease; `running` with a NULL lease; and five malformed spellings
(`not-a-uuid`, upper-hex, braced, undashed, URN). None reaches GitHub (the
fake counts mints). Scenario2 is the cross-tenant case: A's lease owner with
B's job id gets the same 404, GitHub receives no mint request, and B's own
`(id, owner)` then gets 200 — so a handler that refused everything cannot pass.

## 4. U6 as locked, in the fetcher

| Cap | Where | How |
|---|---|---|
| 500 MB downloaded | `download_archive` | streamed to `workdir/<job_id>/archive.tar.gz`; bytes counted per chunk and the download rejected past the cap (within one chunk); a `Content-Length` above the cap is rejected before reading |
| 500 MB expanded | `extract_archive` | **Two counters against one cap.** `_CountingReader` feeds `tarfile` (stream mode, `r|`) from the gzip stream and counts **the bytes the stream expands to** — headers, padding and payload — raising within one read of the cap; a header bomb (4,000 empty members = 2 MB of headers) trips it as surely as a zero bomb. And every regular member's **declared size** is charged to `declared_bytes` before the member is written — the bytes that would **land** — because a sparse member streams a few bytes and lands its declared size (reviewer B's H1: a 366-byte archive put 10,000,000 apparent bytes on disk while the stream counter saw 98,304). Sparse members are skipped besides; `git archive` never emits one |
| 1 MB per file | `extract_archive` (header), `collect_tree` (lstat) | skipped and counted, never written |
| 20,000 indexable files | `extract_archive` (files written), `collect_tree` (files returned) | `FetchRejected("more than 20000 indexable files")` at the 20,001st file that passes the name filters, and again after the content filters |
| 100,000 chunks | — | 22-05, after chunking |

`FetchRejected` carries a plain, token-free reason and `members_seen`; 22-05
turns it into `Rejected` → `dead` in one attempt. `FetchFailed` is an ordinary,
retried failure — and since the review it is also what a **write failing for
any reason but the name** raises (reviewer B's H2: on a 300 KB tmpfs the
extractor returned normally with 5 written and 15 "unwritable", 20 files on
disk of which 15 partial, and the walk returned all 20 as complete; now
`writing the tree failed at member 6: No space left on device (errno 28)`,
the half-written file unlinked, the 5 complete ones left), what an
**unreadable archive** raises (`BadGzipFile`, `EOFError`, `ReadError` and a
member truncated mid-data all become `FetchFailed("the archive is not a
readable gzip tar: …")`), and what an archive under the **wrong top-level
directory** raises: the fetcher tells the extractor GitHub must have named it
`{owner}-{repo}-{sha7}` and the first member is checked against it. Only
name-shaped write errors (`ENAMETOOLONG`, `EINVAL`, `ENOTDIR`, `EISDIR`,
`EEXIST`, `ELOOP`) skip the member and go on. A NUL **anywhere** in a file is
binary (the 8 KB probe let a late NUL into `content`, which 22-05 writes with
psycopg2 — the one character it refuses). `Limits` is a parameter, so the
tests run the caps at 1 MB.

## 5. The mealie measurement (public, no token; two unauthenticated requests)

`mealie-recipes/mealie` at `84b2677f76069a6fb2bac6e2c633c9dbe7379ce4`
(the pinned `84b2677f7606`, resolved through the commits endpoint), on this
Windows host, default limits:

| | |
|---|---|
| downloaded | **23,525,683 bytes in 2.04 s**, status 200 |
| redirect | `api.github.com` → **`codeload.github.com`** (one hop) |
| query parameters on the download URL | **none** (names recorded, never values; a private repository's link is recorded the same way in 22-05, and whether it carries a credential stays unverified until then) |
| members | **1,967**; top level `mealie-recipes-mealie-84b2677` — which, re-measured after the review, **matches the `{owner}-{repo}-{sha7}` the fetcher now expects**, so the check holds against a real GitHub archive |
| expanded | **55,654,400 bytes** of tar stream; **54,218,029 bytes declared** (the second counter, re-measured after the review); 3,611,551 bytes of indexable content written |
| extraction / walk | 1.16 s / 5.66 s |
| indexable files | **979**: python 648, typescript 278, markdown 49, javascript 4 |
| skipped | unsupported 688, oversize 4, lockfile 2, symlink 1 |
| caps tripped | **none** |

Not comparable to the benchmark harness's 390 `.py` files, which counts
`mealie/` only, minus `alembic/versions` and empty files. This is the first
real data point for U6's caps: the largest benchmark repository uses 4.5 % of
the download cap and 10.6 % of the expansion cap.

## 6. The filters (U7 and the rest)

By base name, case-insensitive, checked in this order and counted under the
first reason that applies:

- **secret** — `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`,
  `*.keystore`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `.npmrc`,
  `.pypirc`, `.netrc`, `*.tfvars`, `credentials*.json`, `service-account*.json`,
  and since the review `.git-credentials` and the path rule `.aws/credentials`
  (`id_rsa*` deliberately matches `id_rsa_notes.md`: broad, because a miss
  sends key material to OpenAI). **What the list cannot do, measured by
  reviewer B and recorded in `22-CONTEXT.md` U7:** a key pasted into
  `README.md`, `secrets.py`, `config.py`, `private_key.go` or `token.js` is
  indexed. That is option A's known limitation; content-level detection is a
  question for the retrieval-quality track;
- **vendored** — any component `vendor`, `node_modules`, `dist`, `build`,
  `.git`, `third_party`;
- **generated** — `*.min.js`, `*_pb2.py`, `*.pb.go`, and a Go file whose first
  20 lines match `^// Code generated .* DO NOT EDIT\.$`;
- **lockfile** — `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`,
  `poetry.lock`, `go.sum`;
- **unsupported** — anything whose extension is not in the map
  (`.py .go .js .jsx .ts .tsx .md`).

Applied **before** a member is written (so a secret never touches the disk)
and again by the walk over the tree as it exists. Content checks in the walk:
a NUL in the first 8 KB → `binary`; a failed UTF-8 decode → `non_utf8`; a
BOM is stripped. `skipped` carries counts only, never paths.

## 7. Every hostile case

Each archive is built with `tarfile` in the test and **asserted to contain its
hostile member** (by name, type and link target) before it is fed to the
extractor; after every run the whole temporary directory is listed and only
`archive.tar.gz` and `dest/…` may exist.

| Case | Outcome |
|---|---|
| `top/../escape.py` | skipped `unsafe_path`; nothing outside `dest/` |
| `../escape.py` (no top directory) | skipped `unsafe_path` |
| `/etc/abs.py` | skipped `unsafe_path`; not under `dest/etc/` |
| `top\..\escape.py`, `C:/escape.py`, `top/./x`, `top//x`, `top/sub/../../x` | skipped `unsafe_path`, each |
| a member under a second top-level directory | skipped `unexpected_top_level` |
| symlink `link.py → /etc/passwd` | skipped `symlink`; not on disk |
| symlink `up → ..` | skipped `symlink` |
| symlink `alias.py → src/a.py` (inside the tree) | skipped `symlink`; never followed |
| hardlink `hard.py → ../../outside.py` | skipped `hardlink` |
| FIFO, character device, block device | skipped `special`, each |
| a 2 MB `.py` | skipped `oversize_file`; **never written** |
| a file of exactly 1 MB | kept |
| ten 400 KB zero-filled members under a 1 MB expansion cap | **rejected at member 3 of 10**, ≤ 3 files on disk |
| 4,000 empty members (a header bomb) under a 1 MB cap | rejected before member 4,000 |
| 20,001 indexable files (+100 more) | rejected; `members_seen == 20001` |
| exactly the cap | allowed |
| `.env`, `deploy/id_rsa`, `certs/server.pem` beside real code | never returned, **never on disk**, counted `secret: 3`; the counts contain no paths |
| a `.env` planted on disk | still refused by the walk |
| `vendor/…`, `node_modules/…`, `app.min.js`, `schema_pb2.py`, `package-lock.json`, `go.sum`, `logo.png` | skipped under their reasons; none written |
| a Go file with the generated header | skipped `generated` by the walk |
| a binary `.py` | skipped `binary` |
| a NUL after the first 8 KB | not called binary |
| Latin-1 content | skipped `non_utf8` |
| a BOM | stripped |
| a file symlink planted on disk → an outside secret | walk skips it `link` (Linux; skipped on Windows without the privilege) |
| a directory symlink planted on disk | walk does not descend (Linux) |
| `job_directory("../escape")` | `ValueError`; ids are UUIDs, canonical |
| **Added at the review** | |
| ten GNU sparse members, 1,000,000 bytes apparent each, 9 KB stored (a 366-byte archive; premise asserted: `sparse is not None`, `size == 1_000_000`) | skipped `sparse: 10`, nothing written, 10,000,000 bytes charged to the declared budget; under a 1 MB cap **rejected at member 2** |
| a disk that fills after 300,000 bytes (a quota-wrapped file object; and the real 300 KB tmpfs when `RAG_DOC_TINY_FS` names one) | `FetchFailed` naming `errno 28`, the half-written file unlinked, every remaining file complete |
| `ENAMETOOLONG` on one member | skipped `unwritable`; the extraction goes on |
| a text file named `.tar.gz`; a gzip cut in half; a gzip of random bytes; an empty gzip; a member whose data ends after 100 of 4,000 bytes | `FetchFailed("the archive is not a readable gzip tar: …")`, nothing left on disk |
| an archive under `someone-else-0000000` when `acme-widgets-<sha7>` was asked for | `FetchFailed` on the first member; nothing written |
| `.env.production`, `.npmrc`, `certs/client.p12`, `android/release.jks`, `.git-credentials`, `home/.aws/credentials` beside real code | never written, counted `secret: 6` |
| 9 KB of text then 100 NULs | `binary` (returned as text before the review) |
| `../escape.py` with the header check neutered in the test | `filter="data"` refuses it: `refused_by_filter: 1`, nothing lands — the filter's contribution made observable |
| two directories for one job, one stale | `job_directory` gives a new `<id>-<random>` each call; the sweep removes only the stale one |

## 8. The redirect-header measurement

Measured twice, not assumed:

- **through `httpx.MockTransport`** with two host names
  (`api.github.test` → `codeload.github.test?token=…`): the download request
  carried no `authorization` header; the API requests did.
- **over real sockets** (`http.server` in threads, two ports on `127.0.0.1` —
  a different port is a different origin under httpx's rule, as a different
  host is): the download server recorded the path with `?token=…` and **no
  `authorization` header**; both API requests recorded `Bearer <token>`.

The mealie download confirms the real hop is a host change
(`api.github.com` → `codeload.github.com`).

## 9. What the log-capture tests found

Two real problems, both caught by tests that read captured log output:

1. **`httpx` logs every request URL at INFO** — for the archive download that
   is the redirect link, `?token=` included. The fetcher's own lines were
   clean; the library's were not. The fetcher now holds `httpx` and `httpcore`
   at WARNING (at import and again before every download), and
   `test_the_token_never_reaches_a_log_line_during_a_successful_fetch` resets
   `httpx` to DEBUG first, so a logging configuration that lowers it cannot
   reopen the leak silently. Mutation P11 removes the guard and the test fails.
2. A chained exception (`raise FetchFailed(...) from exc`) would print the
   original's message in the traceback — for `httpx.ReadTimeout` that is the
   URL with the token. Every transport exception is re-raised `from None`;
   mutation P12 puts the chain back and the test fails.

The no-leak evidence, both sides: Go Scenario6 (a `ghs_` + 383-character token
and `ghs_x1`; captured `slog` JSON of a success names the job, organization
and GitHub repository id and contains no `ghs_` and no lease owner; of a
GitHub 500 echoing the App JWT and the token: `[REDACTED]` present, no `ghs_`,
no `eyJ`; the 502 body clean). Python
`test_the_lease_owner_and_token_never_reach_a_message_or_a_log` runs eight
response shapes, asserts on `str(exc)`, `sanitize_error(exc)` and `caplog`
with `exc_info=True`.

## 10. Mutations

Each applied to a **copy** of the committed tree by a helper that refuses
anything but exactly one match and prints "mutated present / original
absent"; each restored from the worktree and the copy diffed clean afterwards.
Predicates were **neutered**, never deleted (21-07's M1a lesson).

**Go** (`pkg/internalapi`, `pkg/github`):

| # | Mutation | Result |
|---|---|---|
| M1 | `AND lease_owner = $2` → `AND $2::text IS NOT NULL` | **killed** — Scenario2 (cross-tenant), Scenario3 (wrong owner) |
| M2 | `AND lease_expires_at > NOW()` → `AND TRUE` | **killed** — Scenario3 ("expired lease") |
| M6 | `state = 'running'` → `'running' = 'running'` | **killed** — Scenario3 (superseded, completed, queued) |
| M5 | marker header not set | **killed** — 10 failures, Scenarios 1–5 |
| M3 | `permissions` dropped from the mint body | **killed** — `TestRepositoryToken_ScopesToOneRepositoryReadOnly`, Scenario1 |
| M4 | `redactSecrets` bypassed in `request` | **killed** — 14 failures: `TestDo_NeverLeaksCredentials`, `TestRepositoryToken_NeverLeaksCredentials` (both lengths, both calls), Scenario6 (both lengths) |
| M7 | the repositories scope check removed | **killed** — 3 subcases |
| M8 | the `contents: read` check removed | **killed** — 2 subcases |

**Python** (Windows 3.13.7 locally; `python:3.12-slim` on Linux; identical
except where the host cannot create symlinks):

| # | Mutation | Result |
|---|---|---|
| P1 | `followlinks=True` | **killed on Linux** (`test_the_walk_does_not_descend_a_directory_symlink`); not observable on Windows |
| P2 | expanded-bytes check removed | **killed** — the zero bomb and the header bomb |
| P3 | `*.pem` removed | **killed** — its own case, `Server.PEM`, the beside-real-code test |
| P4a | `filter="data"` removed **alone** | survived on the first run, as designed — the header check holds. **Since the review it is killed** (PB11 below): `test_the_data_filter_alone_refuses_traversal` neuters the header check itself, so the filter's contribution is observable |
| P4b | header path check removed **alone** | 8 accounting tests fail (`refused_by_filter` instead of `unsafe_path`); **all five "nothing lands" property tests pass** — `filter="data"` holds |
| P5 | **both** removed | **killed** — `TestNothingHostileLands::test_dot_dot_traversal`: the file lands at `tmp/escape.py`. The absolute-path property still holds: the top-level strip and the one-top-level rule contain it |
| P6a | member-type check removed **alone** | 7 accounting tests fail; the five property tests pass — `filter="data"` refuses the absolute and outside links and the devices. **Corrected at the review (reviewer B, L1):** the inside symlink `alias.py → src/a.py` **lands** under P6a on Linux, because the `data` filter allows a link that stays inside the destination; the type check is the only guard for in-tree links, and its own test says so. The first version of this row claimed stream mode refused it, which was wrong |
| P6b | **both** removed | **killed** — Linux 10 failures (7 accounting + the two symlink and the hardlink properties: links land on disk; the walk still never reads through them); Windows 8 |
| P7 | walk `lstat` check removed | **killed on Linux** (`test_the_walk_skips_a_file_symlink_planted_on_disk`) |
| P8 | per-file size check removed at extraction | **killed** — the 2 MB file is written |
| P9 | file cap removed at extraction | **killed** |
| P10 | marker check removed | **killed** — the unmarked 404, the unmarked 200 and the redirect become `TokenRefused` |
| P11 | `httpx` logger left at its default | **killed** — the link with `?token=` reaches `caplog` |
| P12 | `from None` dropped | **killed** — the chained `ReadTimeout` carries the link |
| P13 | name filters not applied before writing | **killed** — `.env` lands on disk |

The defence-in-depth result the plan asked for, exactly: with either the
header check or `filter="data"` removed alone, no hostile member lands; with
both removed, `..` traversal lands. Absolute paths are contained by a third
mechanism (the top-level strip), which is recorded rather than claimed as a
guard. Reviewer B ruled the overlap a strength — the header check is strictly
broader (backslash, drive letter, NUL, surrogates, `.`/empty components, all
measured) and the type check catches the in-tree links the filter allows —
provided the second guard is observable, which PB11 now makes it.

**Mutations added at the review** (each on a copy of the committed tree,
`4787148`/`fbf1362`, proven present, restored, the copy diffed clean):

Go:

| # | Mutation | Result |
|---|---|---|
| GA1 | `redactValues` anchors on the whole value (prefix rule removed) | **killed** — `TestRedactValues_CoversATruncatedEcho`, `TestRepositoryToken_LookupErrorRedactsTheTokenByValue` |
| GA2a | `CheckListenAddr` does not flag unspecified IPs | **killed** — `TestCheckListenAddr/every-interface_spellings_are_flagged` |
| GA2b | `CheckListenAddr` does not flag an empty host | **killed** — same |
| GA3 | `parseLeaseBody` accepts a duplicate key | **killed** — `TestParseLeaseBody`, Scenario5 |
| GA4 | `parseLeaseBody` accepts trailing bytes | **killed** — `TestParseLeaseBody`, Scenario5 |
| GA5 | no distinct error for an absent `repositories` field | **killed** — `…/repositories_field_absent` |
| GA6 | no distinct error for an absent `permissions` object | **killed** — `…/permissions_object_absent` |

Python (Windows 3.13.7 and `python:3.11-slim`, identical):

| # | Mutation | Result |
|---|---|---|
| PB1 | `trust_env=True` on the token client | **killed** — `test_the_token_client_ignores_proxy_environment_variables` (the request reaches the recording proxy) |
| PB2 | sparse members not skipped | **killed** — both sparse tests (files land) |
| PB3 | declared size not charged | **killed** — both sparse tests (the bomb is not rejected) |
| PB4 | every write error skippable (ENOSPC swallowed) | **killed** — `test_a_full_disk_raises_and_leaves_no_partial_file` |
| PB5 | the half-written file not unlinked | **killed** — same test, on the "no partial file" assertion |
| PB6 | the NUL check back to an 8 KB probe | **killed** — `test_a_nul_anywhere_in_the_file_is_binary` (run through an escape-free helper, after a shared helper's `unicode_escape` turned the `\x00` in the mutation into a real NUL and refused it) |
| PB7 | expected top level not checked | **killed** — the extractor test and the fetcher test |
| PB8a | `.git-credentials` removed | **killed** — its name case and the never-written test |
| PB8b | the `.aws/credentials` path rule removed | **killed** — three path cases and the never-written test |
| PB9 | job directory keyed on the id alone | **killed** — `test_job_directory_is_unique_per_call`, `test_sweep_removes_only_the_stale_directory_of_a_job` |
| PB10 | unreadable archives not wrapped | **killed** — the four unreadable cases and the truncated member |
| PB11 | `filter="data"` removed alone | **killed** — `test_the_data_filter_alone_refuses_traversal` (P4a's survivor, now observable) |

## 11. Verification

| Check | Result |
|---|---|
| `go build ./... && go vet ./...` | ok |
| `go test ./... -count=1 -p 1` (after merging `main`) | 8 packages ok; `pkg/api/handlers` fails only `TestSignatureComparisonIsConstantTime` — the known CRLF-only local failure |
| `-race` in `golang:1.25` (go1.25.14) on `./pkg/internalapi/... ./pkg/github/...` | ok, 0 data races; both added to CI's race step |
| `go mod tidy -diff` | go.mod/go.sum byte-identical to `main`; the local diff is line ordering only (0 unpaired lines) |
| `pytest tests/ workers/ -v --tb=short` (fresh venv 3.13.7, `REDIS_URL` db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no `.env`) | **431 passed, 2 skipped** after the merge (423/2 before) |
| `pytest tests/fetch` in `python:3.12-slim` on Linux | **141 passed, 0 skipped** (the two symlink cases run there) |
| `check-isolation-tests.py --base-ref RAG-Doc/main` | PASS; `POST /internal/jobs/{id}/repository-token` **covered** |
| `grep -rn GITHUB_APP_PRIVATE_KEY services/workers` | nothing |
| compose | untouched; no service started |
| **After the review (`4787148`, `fbf1362`)** | |
| `go test ./pkg/github/... ./pkg/internalapi/...` | ok |
| `-race` in `golang:1.25` on the same two packages | ok, 0 data races |
| `pytest tests/fetch` locally (3.13.7) | **159 passed, 3 skipped** (the tiny-filesystem case and the two on-disk symlink cases) |
| `pytest tests/fetch` in **`python:3.11-slim`** (the production image), with `--tmpfs /small:size=300k` and `RAG_DOC_TINY_FS=/small` | **162 passed, 0 skipped** — the real-full-disk case included |
| `pytest tests/ workers/` as CI (fresh venv) | **451 passed, 3 skipped** |
| H1 / H2 reproduced in `python:3.12-slim` before the fix, re-run in `python:3.11-slim` after | section 4 has both sets of numbers |
| mealie re-measured after the fix | identical figures; top level matched; declared 54,218,029 bytes; no cap tripped |

`DATABASE_TEST_URL` pointed at an own scratch pgvector container (random
port), never 5434.

## 12. Measured, inferred, and not pinned

- **Measured:** everything in sections 5, 8, 9, 10 and 11.
- **Inferred:** GitHub's access-tokens response listing `repositories` and
  `permissions` when scoped (the fail-closed check depends on it); whether the
  download link of a **private** repository carries a credential (the public
  one carried none). Both are settled by 22-05's live proof.
- **Does NOT pin:** the marker's value across a future protocol change (the Go
  and Python constants point at each other, nothing checks them against each
  other); CPU time on an archive of millions of tiny non-indexable members
  (the expansion cap bounds the tar stream and `max_job_duration` bounds
  time; nothing bounds member count separately); a repository with more than
  20,000 name-indexable files that content filters would bring under the cap
  is rejected, by the conservative reading; the download cap counts
  `iter_bytes`, which is **decoded** bytes — stricter than the wire if the
  download host ever applies a content encoding, never looser (reviewer B,
  L9); a token's life after its lease — the lease gates issuance, not
  validity, and revocation is 22-05's.

## 13. Deviations and notes

- `pkg/github/scoped_token_test.go` beside `client_test.go`; a third Python
  test file, `test_filters.py`, with one case per rule.
- The fail-closed scope check (section 2) goes beyond the plan.
- The `httpx` logger guard (section 9) was not in the plan; a test found it.
- The file cap and the expansion counter take the conservative readings
  (section 4).
- The router's "GitHub App unset" WARN moved to `main.go` with the client
  construction; the router keeps its slug and client-credential panics, keyed
  on `cfg.GitHubClient != nil`.
- **Fleet-environment finding, filed as ISS-037.** The Go harness reuses its
  container **by name** across worktrees. The parallel 22-02 run migrated the
  shared container to 17 while this tree was at 16, and golang-migrate
  refused ("no migration found for version 17"). Worked around with a local,
  uncommitted rename of the constant until `main` (with 000017) was merged,
  then restored. The fix reviewer B recommended and the planner adopted —
  derive the name from the worktree by default, `ISOLATION_CONTAINER_NAME`
  as an override — is 22-05's first task, not this PR's.
- No real GitHub App call was made; the only network calls were four
  unauthenticated requests for the public mealie archive (two before the
  review, two after). No `.env` was read.

## 14. Applied from PR #52's two reviews

**Reviewer A (security, token route) — APPROVE WITH NITS, all applied in `4787148`:**

| # | Finding | What changed |
|---|---|---|
| L1 | the lease gates issuance, not validity | stated in the package doc and `docs/internal-api.md`; 22-05 revokes via `DELETE /installation/token` on fetch end and `LeaseLost` (recorded in `22-CONTEXT.md`'s 22-05 row) |
| L2 | `httpx` honours `HTTP_PROXY` by default | `trust_env=False` on the token client; `test_the_token_client_ignores_proxy_environment_variables` against a real recording proxy, with the trusting client shown reaching it first |
| L3 | `INTERNAL_ADDR=:8081` binds every interface | `CheckListenAddr`; the process refuses to start on a wildcard host unless `INTERNAL_ADDR_ALLOW_ALL_INTERFACES=true`, and then warns; `listen_addr_test.go` |
| N1 | redact the lookup error by value | done — and the by-value rule now covers a truncated echo, which the new test found leaking |
| N2 | distinguish "no permissions object" from `contents=""` | done, and "no repositories field" from "0 repositories" |
| N3 | the internal listener logged in text regardless of `LOG_FORMAT` | one `slog` handler built from `LOG_FORMAT`/`LOG_LEVEL`, set as default and handed to the listener |
| N4 | lenient JSON decoding | `parseLeaseBody`: exactly one object, one key, one string, no trailing bytes; 19 refused shapes |
| N5 | the by-id alias is undocumented | the fallback noted beside the lookup |
| ruling | network position + lease owner is sufficient for v1 on a private compose network | carried to Phase 24 (`ROADMAP.md`, `docs/internal-api.md`): a bearer secret or mTLS if the worker and the backend ever sit on different hosts |

**Reviewer B (fetcher, tests, docs) — CHANGES REQUESTED, all applied in `fbf1362`:**

| # | Finding | What changed |
|---|---|---|
| H1 | sparse members bypass the expansion cap (measured: 6,016-byte archive → 210 MB apparent) | reproduced (366 bytes → 10,000,000 apparent, walk returned all ten); sparse members skipped **and** every member's declared size charged to the budget; after: rejected at member 2 |
| H2 | a full disk yields a truncated tree indexed as complete | reproduced on a 300 KB tmpfs (5 written, 15 "unwritable", 15 partial files walked as complete); only name-shaped errnos skip, everything else unlinks the partial file and raises `FetchFailed`; after: `errno 28`, no partial file |
| H3 | a NUL past the 8 KB probe reached `content` | `if b"\x00" in raw:` over the whole file; the test that pinned the hole rewritten |
| M1 | `job_directory` keyed on the id alone | `mkdtemp(prefix=f"{job_id}-")`; the sweep keys on `<uuid>-`; two-directory tests |
| L1 | P6a's mechanism was false | corrected in section 10 |
| L2 | `filter="data"` unobserved | `test_the_data_filter_alone_refuses_traversal`; PB11 kills the P4a survivor |
| L3 | raw `BadGzipFile`/`EOFError`/`ReadError` escaped | wrapped as `FetchFailed`, five cases |
| L4 | two test names overclaimed | `…_rejected_before_reading` now asserts no chunk was pulled; `…_exactly_the_file_cap_is_allowed` |
| L5 | the top-level directory was fixed by member order | checked against `{owner}-{repo}-{sha7}` on the first member; matched by the real mealie archive |
| L6 | deny-list gaps | `.git-credentials` and `.aws/credentials` added with tests; the content-level gap recorded in `22-CONTEXT.md` U7 for the retrieval-quality track |
| L7 | the production image was never run | `python:3.11-slim` runs recorded above and preferred for Linux from now on |
| L8 | the harness collision | filed as ISS-037, 22-05's first task |
| L9 | `iter_bytes` counts decoded bytes | recorded under does-not-pin |

## 15. Hand-off to 22-05

- **Revoke the token** when the fetch ends and on `LeaseLost`:
  `DELETE /installation/token` with `Authorization: Bearer <the token>`; no
  App key involved. The lease gates issuance, not validity.
- **Compose binds the internal listener to the service name**
  (`INTERNAL_ADDR=backend:8081`), never a wildcard; the override exists for
  a platform that has no other way, and it warns.
- **ISS-037 first**: derive the harness container name from the worktree.
- Record the **private** repository's download-link query parameter
  **names** (the public one had none) and whether the link carries a
  credential.
- Confirm the mint reply's `repositories` and `permissions` against the
  real API on the first live mint; the client's refusals name the cause.
- If `GET /repositories/{id}` ever fails, the fallback is
  `GET /repos/{full_name}` from the mint reply.
- Run the Python suite in `python:3.11-slim` for Linux checks.
- **A dense bomb is rejected AT the cap, not before it** (observed in PR
  #52's re-check, no code change). The declared-size budget refuses a
  member only once the running total EXCEEDS the cap, so a bomb whose
  members sum to exactly the cap is let through by that counter and is
  stopped by the stream counter instead, when the tar stream (headers
  included) crosses it: in the re-check the declared bytes sat at exactly
  the cap when the stream crossed it. **Up to one member's worth of content
  can therefore be on disk at the moment of rejection** — which is the
  "within one member of the cap" this summary and the PR body state, not
  "nothing on disk". What bounds the consequence is `fetch_repository`'s
  `finally`, which removes the job directory on a rejection as on anything
  else; a caller that uses `extract_archive` directly, as 22-05 might for a
  resumed stage, owns that cleanup itself. Size the worker's disk for the
  cap plus one file (500 MB + 1 MB), not for the cap.
