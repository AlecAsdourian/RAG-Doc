# The internal API

The backend's **second listener**: routes for the worker process, on an
address that is never published. Built in 22-04 (decision P10, the user's
answer U4). One route today.

Related: [`api-ingestion-jobs.md`](api-ingestion-jobs.md) (the queue, the
lease, and the rule that queue-wide SQL never reaches a public handler),
[`github-app-setup.md`](github-app-setup.md) (the App and its private key),
[`isolation.md`](isolation.md).

---

## Why it exists

The worker fetches a repository's code. To do that it needs a GitHub
credential, and the only credential the platform holds is the **App's
private key**, which can open every customer's repository. The user decided
(U4) that the key **never enters the process that parses untrusted code**.
Instead the backend, which holds the key, mints a token that is:

| Property | Value | Why |
|---|---|---|
| repositories | **one**: the job's | a worker reads one repository at a time |
| permission | **`contents: read`** (GitHub adds `metadata: read` itself) | it reads an archive; it never writes |
| lifetime | **one hour**, fixed by GitHub | a job asks once per run; nothing caches it |
| who may ask | **the holder of a live lease on a `running` job** | the lease is already the credential every terminal write is fenced on |

The analogy in `22-CONTEXT.md`: a hotel front desk issuing a key card for one
room for one night, rather than handing every cleaner the master key.

## Why it is a separate listener

The route finds a job by its id and lease owner **before it knows the tenant**
— the organization is on the row it is looking for. That is a queue-wide read
over `ingestion_jobs`, a table with **no row-level security** by decision
(21-CONTEXT L5), and `api-ingestion-jobs.md` keeps the rule that such SQL
never reaches a public handler. On the public router that rule would be a
matter of middleware ordering; on a separate listener it is a matter of
**network reachability**, which a deployment can enforce and audit.

- It binds **`INTERNAL_ADDR`**, default `127.0.0.1:8081`.
- **It refuses to bind every interface.** `:8081`, `0.0.0.0:8081`, `[::]:8081`
  and the `inet_aton` shorthands (`0:8081`) stop the process at startup, because
  on a platform that publishes whatever a process listens on they are a public
  token route (PR #52's review, L3). Bind loopback, or the compose service name
  (`INTERNAL_ADDR=backend:8081`, which resolves to the container's own address),
  or set `INTERNAL_ADDR_ALLOW_ALL_INTERFACES=true` and accept a WARN on every
  start. `internalapi.CheckListenAddr` is the judge and `listen_addr_test.go`
  pins the spellings.
- In compose it is exposed on the compose network only. **It is never in
  `ports:`.** (22-05 adds the compose wiring; 22-04 adds none.)
- **Phase 24 carries "keep the token route internal-only" as a deployment
  requirement.** Publishing this address would let anyone who can read a
  `lease_owner` mint repository tokens.
- **Without GitHub App credentials the listener does not start**, and
  `main.go` says so at WARN, exactly as the public side degrades.
- The public router does not serve `/internal/…`. A request for it there gets
  chi's plain `404 page not found`, **without the marker below**, and
  `TestRepositoryTokenIsolation/Scenario7` pins that.

---

## `POST /internal/jobs/{id}/repository-token`

**Request body:** `{"lease_owner": "<the worker's lease owner for this job>"}`.

**Every response the route writes carries the header
`X-Rag-Internal: repository-token/1`**, whatever the status. Section
[The marker](#the-marker) says why.

| Status | Body | When |
|---|---|---|
| **200** | `{"token", "expires_at", "full_name", "default_branch"}` | the job is `running` under this lease, the lease is live, the installation is usable, GitHub minted |
| **404** | `{"error":"no_live_lease"}` — **byte-identical** | unknown id, malformed id, wrong lease owner, expired lease, or a job that is not `running` (superseded, completed, dead, queued) |
| **409** | `{"reason":"installation_suspended"}` | the repository's installation is suspended |
| **409** | `{"reason":"installation_uninstalled"}` | the installation is uninstalled, missing, or the repository is not linked to GitHub |
| 400 | `{"error":"bad_request"}` | the body is not `{"lease_owner": "<non-empty string>"}` |
| 502 | `{"error":"github_unavailable"}` | GitHub refused or failed the mint |
| 500 | `{"error":"internal"}` | a database fault |

`full_name` and `default_branch` come from a lookup made **with the new
token, by numeric repository id**, so they are current after a rename and the
token is proven to reach the repository before the worker is handed it.

### What the route checks, in order

1. **The lease.** One unscoped statement, on the pool:

   ```sql
   SELECT organization_id, repository_id FROM ingestion_jobs
   WHERE id = $1 AND lease_owner = $2 AND state = 'running'
     AND lease_expires_at > NOW()
   ```

   This is the terminal-write fence every transition carries, plus a **live**
   lease. A job whose lease has expired is someone else's to reclaim, so its
   old owner gets nothing. `state = 'running'` matters on its own: a supersede
   deliberately leaves the lease attached to the row, so `lease_owner` alone
   would still match.

2. **The installation**, inside a tenant transaction for the organization the
   row named: the repository's `github_repo_id` and installation, and the
   installation's `suspended_at` / `uninstalled_at`. `uninstalled_at` is
   tested first, because a reinstall clears both. The worker checked this at
   claim time (21-06); this closes the window between then and now.

3. **The mint**, through `github.Client.RepositoryToken`: a `POST` to
   GitHub's access-tokens endpoint with
   `{"repository_ids":[<id>],"permissions":{"contents":"read"}}`. The client
   then **fails closed on what GitHub reports back**: the token must list
   exactly the one repository and carry `contents: read` and nothing beyond
   `metadata: read`. A token minted wider than asked for is refused, not
   handed out. (This reading of GitHub's contract is verified against the
   fake in tests; 22-05's live proof validates it against the real API.)

4. **One log line per mint**: job, organization, repository, GitHub
   repository id, installation, name and expiry. **Never the token, never
   the lease owner.**

### The 404 is one 404

"No such job", "not your job", "your lease expired", "the job was superseded"
and "that is not a UUID" are indistinguishable, by status and by byte. Telling
them apart would make this route an oracle over every tenant's job ids, and a
caller holding a stale lease has no business learning what happened to the
job. The isolation test asserts the bodies are equal across thirteen causes,
not merely all 404.

### The marker

The worker treats a 404 as "the lease is not mine" — and 22-05 turns that into
`LeaseLost`, which **writes nothing** — **only when the marker is present and
the body is the fixed one**. Any response without the marker, a 404 included,
is `InternalApiMisrouted`: a plain exception, so the job fails loudly, an
attempt is consumed, and `last_error` names the host the request reached.

Without that rule, `INTERNAL_API_URL` pointing at the public router (or a
proxy, or a wrong path on this listener) would turn every chi `404 page not
found` into a "lost lease": the job would die after five lease expiries with
`last_error` NULL and nothing to say why. The marker is set by the handler,
not by router middleware, so a wrong path on the internal listener is
unmarked too.

The two 409 reasons are distinct on purpose and map onto Phase 21's endings:
a suspension **defers** an hour with the attempt handed back; an uninstall
**abandons**. Neither may ever end a job `dead`.

---

## The lease as a credential, and the residual risk

`lease_owner` is a UUID4 the worker generated when it claimed the job. No
API returns it: 21-07 deliberately left it out of the job response. A caller
that presents the right `(job id, lease owner)` pair for a running job with a
live lease is the worker running that job.

**The residual risk, stated (P10):** a process with the worker's database
access can read every running job's `lease_owner`, and could therefore ask for
a token for any **currently running** job's repository. The tokens it could
obtain are still one repository, read-only and gone within the hour — far
narrower than the App key, which never leaves the backend. Phase 24's
deployment keeps the listener unreachable from anything but the workers, which
is what bounds the risk to that process.

**The lease gates issuance, not validity.** Measured in PR #52's review (L1):
once the lease expires the route refuses, but a token it already issued stays
valid for GitHub's full hour, and nothing on this side can shorten it. So
**22-05 revokes the token** — `DELETE /installation/token`, authenticated by
the token being revoked, no App key involved — when the fetch ends and on
`LeaseLost`, so the credential's life is the fetch rather than the hour.

**What the network position is worth.** On a private compose network, the
lease owner plus reachability is the whole authentication, and the review ruled
that sufficient for v1. If the worker and the backend ever sit on different
hosts, the route needs a bearer secret or mTLS in front of it; Phase 24 carries
that requirement.

---

## The worker's side

`workers.fetch.client.request_token(internal_api_url, job_id, lease_owner)`
returns a `RepositoryToken` whose `repr` and `str` never include the token,
or raises one of: `TokenRefused` (marked 404), `InternalApiMisrouted`
(unmarked anything), `InstallationSuspended`, `InstallationUninstalled`, or
`TokenRequestFailed` (a marked response the client does not accept, or a
transport failure — an ordinary, retried failure). Messages carry the internal
API's host and a status, never the lease owner, never a token.

**The token client ignores proxy variables.** `httpx` honours `HTTP_PROXY` /
`HTTPS_PROXY` / `ALL_PROXY` by default; this request is plain HTTP on the
compose network and carries the lease owner in its body and the token in its
reply, and a forward proxy relays headers, so the marker would survive and
nothing would fail loudly (PR #52's review, L2). The client is built with
`trust_env=False`, and `test_the_token_client_ignores_proxy_environment_variables`
measures it against a real recording proxy. The fetcher's GitHub client keeps
the default on purpose: an egress proxy for GitHub is a legitimate deployment,
and that traffic is HTTPS through a CONNECT tunnel the proxy cannot read.

`workers.fetch.archive.fetch_repository(token, job_id=…, workdir=…)` then
resolves the default branch to a full SHA, downloads the tarball of **that
commit**, extracts it under the U6 caps, applies the U7 filters and yields the
tree. The download link GitHub redirects to may carry a credential of its
own; it is never logged. **The fetcher holds `httpx`'s logger at WARNING**
because, measured, `httpx` logs every request URL at INFO — the redirect link
included.

---

## Testing it

`pkg/internalapi/repository_token_isolation_test.go` runs a real
`github.Client` against a fake GitHub (through `github.WithBaseURL`) so it can
decode what the mint request carried. Its cross-tenant scenario is
**mutation-checked**: neutering `AND lease_owner = $2` to
`AND $2::text IS NOT NULL` must fail it, and the run is recorded in
`22-04-SUMMARY.md` with the other mutations.

The CI isolation scanner asks for that file because the route is a POST. What
makes it necessary is the absence of row-level security on `ingestion_jobs`:
the statement's own predicate is the whole tenant boundary.
