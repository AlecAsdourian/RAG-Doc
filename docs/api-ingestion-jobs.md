# Ingestion jobs

The durable queue behind repository indexing: what a job is, every state it
can be in, how a job ends, and the one endpoint that reads it.

Written at the end of Phase 21, which built the queue and left it empty on
purpose, and **brought up to date by 22-05, which turned the worker on**:
`full_ingest` and `incremental` are registered, the handler fetches, parses,
embeds and stores a repository, and every state below is reachable in
production. What changed, and why, is [the Phase 22
hand-off](#the-phase-22-hand-off) at the end.

Related: [`api-repositories.md`](api-repositories.md),
[`api-github-webhooks.md`](api-github-webhooks.md),
[`internal-api.md`](internal-api.md) (the token route the handler calls),
[`isolation.md`](isolation.md).

---

## The shape of the thing

Three concerns that used to share one column now have three homes:

| Concern | Home | Meaning |
|---|---|---|
| the work item | **`ingestion_jobs`** | something to do, with a lease and an attempt counter |
| the record of a run | `ingestion_runs` | what happened; `chunks` foreign-key to it |
| status for the UI | `repositories.sync_state` | **derived** — written by the job, read by the frontend, **never a queue** |

Conflating the first and the third is what **ISS-016** was: a status column
has no owner, no lease and no attempt counter, so two writers could each
believe they owned the same repository and whichever finished last wrote the
final state.

**One live job per repository, enforced by the schema.**
`idx_ingestion_jobs_one_live_per_repo` is
`UNIQUE (repository_id) WHERE state IN ('queued','running')`, so two live
jobs for one repository are *unrepresentable* rather than merely unlikely.

**`ingestion_jobs` has no row-level security, deliberately.** A worker
claims a job *before* it knows the tenant — `organization_id` is on the row
it is trying to claim — so scoping the claim by the answer would be
circular. The consequence is the whole reason the endpoint below needs care:
**`organization_id` on this table is an authorization input that nothing in
the database applies for you.**

---

## The five states

There are five, not six. **There is no `failed` state**, and its absence is a
decision (O2), not an oversight: it would have had no edge back into the
claimable set and no place in the partial unique index, so a failed job could
neither be retried nor stop a second live job for the same repository.

| State | Meaning | Live? |
|---|---|---|
| `queued` | waiting to be claimed, from `run_after` onwards | **yes** |
| `running` | claimed, with a lease | **yes** |
| `completed` | the work is done | no |
| `dead` | out of attempts; the only failure terminal | no |
| `superseded` | replaced or stood down; nothing failed | no |

**"Currently retrying" is `state = 'queued' AND attempts > 0`**, with
`run_after` in the future. A failed attempt does not get a state of its own —
it goes back to `queued` with the backoff applied and `last_error` recorded.

### Every transition

```
                 ┌──────────── producer enqueues ───────────┐
                 │                                          ▼
   (nothing) ────┴──────────────────────────────────────► queued ◄────┐
                                                            │         │
                                 claim (attempts += 1)      │         │
                                                            ▼         │
                        ┌────────────────────────────── running       │
                        │                                  │ │        │
   installation dead ──►│ abandon                   fail   │ │ defer  │
   (uninstalled, or     │                       (attempts  │ │ (attempt
    no installation)    ▼                        remain)   │ │  handed back)
                   superseded ◄── relink/uninstall         │ └─────────┘
                        ▲          supersedes a live job   │
                        │                                  ▼
                        │                        completed / dead
                        └────── (terminal; never claimed again) ──────
```

| From | Trigger | To | `attempts` | Written by |
|---|---|---|---|---|
| — | a producer enqueues | `queued` | 0 | `pkg/jobs.Enqueue` |
| `queued` | a worker claims it | `running` | **+1** | `claim` |
| `running` | a worker claims a job whose lease expired | `running` | **+1** | `claim` (the reclaim branch) |
| `running` | the handler returns | `completed` | — | `complete` |
| `running` | the handler raises, below `max_attempts` | `queued`, `run_after` pushed out | — | `fail` |
| `running` | the handler raises, at `max_attempts` | `dead` | — | `fail` |
| `running` | the handler raises `Rejected` — a U6 hard cap (22-05) | `dead`, **at once** | unchanged | `reject` |
| `queued` / `running` | out of attempts and nobody wrote the terminal state | `dead` | — | `sweep` |
| `running` | the installation is suspended (at claim time, or mid-run since 22-05), or the worker is shutting down part-way | `queued` | **−1** (handed back) | `defer` |
| `running` | the installation is uninstalled or gone (at claim time, or mid-run since 22-05) | `superseded` | unchanged | `abandon` |
| `queued` / `running` | a relink, an uninstall, or a repository removed from the installation | `superseded` | unchanged | `pkg/jobs.SupersedeLive` |
| `completed` | `needs_rerun` was set while it ran | a **new** `incremental` job at `queued` | 0 | `complete`, in the same transaction |

What a handler raises decides which of these rows it takes; that mapping is
[How a job ends](#how-a-job-ends), below.

Five of those rows carry a decision worth stating.

**Reclaim is a retry.** The claim increments `attempts` on the reclaim branch
too, so a job that reliably kills its worker eventually dead-letters instead
of looping forever.

**`defer` hands the attempt back, and that is what makes it safe.** A week of
suspension cannot walk a healthy repository towards `dead`, and neither can an
operator's restarts: `defer` writes `attempts - 1`, undoing the claim's
increment. It is the only ending that neither consumes an attempt nor moves
`run_after`, so it is tied to the two conditions that bound it — a suspended
installation (which defers a whole hour) and a shutdown (which happens once
per process). Anything else that stops part-way is a `fail`.

**`abandon` writes `superseded`, never `failed`.** Nothing went wrong and
nothing should retry: the App was uninstalled. It matches what the uninstall
webhook writes, and it takes the job out of the live set so a reinstall can
enqueue freely.

**`reject` writes `dead` in one attempt (22-05).** A U6 cap — 500 MB of
archive or of expansion, 20,000 indexable files, 100,000 chunks — is
deterministic: the same repository trips the same cap every time. As a
`fail` it would re-download up to 500 MB five times over about 81 minutes to
reach the `dead` it was always going to reach, so U6's "ends `dead`" is
written at once, with a plain reason in `last_error`. It is fenced like
every terminal write, and it is **never** used for something that heals: a
suspended installation is not a cap.

**The rerun follow-up is `incremental`, never `full_ingest`**, and it is
enqueued **after** the completion write, in the same transaction. The reverse
order raises no error — it flags `needs_rerun` on the job that is about to
leave the live set, and the repository ends with no live job at all. Silent
loss, which is worse than an error.

### The lease, and why every terminal write is fenced

A lease is **5 minutes**, extended by a heartbeat every **60 seconds** (one
fifth, so four consecutive beats may be lost before the job becomes
reclaimable). The same five minutes as the webhook receiver's
`abandonedProcessingAfter`, deliberately: both answer "how long may a dead
process hold work before something else may take it".

Every terminal write carries

```sql
WHERE id = $1 AND lease_owner = $2 AND state = 'running'
```

**and both halves do work.** A supersede deliberately leaves the lease
attached to the row — it is the only record of which worker was running when
it was replaced — so `lease_owner` alone still matches; `state = 'running'` is
what turns a supersede into zero rows. Without the fence, a worker whose lease
expired would clobber the new attempt's result, and a superseded worker
writing `queued` on its way out would collide with the replacement job.

A worker that finds its write matched nothing logs it and moves on. Its
results are rolled back with it.

---

## How a job ends

**This table is the authority for handler endings** (22-05; it replaces the
"three endings" Phase 21 documented). The exception type a handler raises is
the contract, and `workers/jobs/runtime.py` (its "THE ENDINGS" section, which
keeps no copy of the table), `workers/ingest/handler.py` and `workers/fetch`
point here. Every ending that already existed at claim time is reused mid-run,
so an installation that changes state while a job runs ends exactly as it
would have at the claim.

| The handler… | Ending | The job row | `sync_state` | Why |
|---|---|---|---|---|
| returns `write_results` | `complete` | `completed`; the results commit in the same transaction | `synced`, `last_synced_at` | done |
| raises `Unfinished` **during a shutdown** | `defer`, no delay | `queued`, attempt handed back | unchanged | the job is still ours; an operator's restart must not cost an attempt |
| raises `Unfinished` at any other time | `fail` | attempt consumed, backoff | `failed` | outside a shutdown it is a handler bug, and `defer(0)` would re-claim without bound (measured: 193 times in 6 s) |
| raises **`Rejected`** — a U6 cap; the fetcher's `FetchRejected` is one | **`reject`** | **`dead` in one attempt**, `last_error` the plain reason | `failed` | a cap is deterministic; five retries would re-download 500 MB five times to reach the same `dead` |
| raises **`InstallationSuspended`** — the token route's `409 installation_suspended` | `defer` **60 minutes** | `queued`, attempt handed back, `run_after` an hour out, a reason | **unchanged** (reads `syncing`; see below) | the claim-time policy: a suspension heals on its own and must never dead-letter a healthy repository |
| raises **`InstallationUninstalled`** — the route's `409 installation_uninstalled` | `abandon` | `superseded` | `never_synced` | the claim-time policy: nothing to do, nothing failed |
| raises **`LeaseLost`** — `should_abort()`, or a refused token (the route's **marked** 404) | **nothing** | untouched: `running` under the old lease until someone reclaims it | untouched | the job is someone else's; any write would clobber theirs |
| raises anything else — including `InternalApiMisrouted` (an **unmarked** answer from the token route) and **`ParseFailed`** (every indexable file raised in the chunker) | `fail` | attempt consumed, backoff, `dead` at 5 | `failed` | an ordinary, retried failure, with `last_error` saying why |

Three rules sit behind that table, each learned the hard way:

- **A bare `return` after stopping early writes `completed` over work that
  did not happen.** That is not a style point: PR #42's review produced a row
  reading `state=completed last_stage=parse sync_state=synced` with
  `last_synced_at` stamped and the partial unique index freed, so nothing
  would re-queue it. `Unfinished` exists so an early stop can say so.
- **A job that is not ours raises `LeaseLost`; it never returns.**
  `COMPLETE_SQL`'s fence is `lease_owner` and `state`, not the lease's
  expiry, and the heartbeat only notices a lost lease on its next tick — so
  an expired but unreclaimed lease reached `complete()`. The token route
  refuses exactly such a lease, which is why a refused token is `LeaseLost`
  (22-05, fact-check c2).
- **A misrouted token route fails loudly.** The route marks every response
  it writes (`X-Rag-Internal`, [`internal-api.md`](internal-api.md)); an
  unmarked 404 — chi's, when `INTERNAL_API_URL` points at the public router —
  is `InternalApiMisrouted`, a plain failure with the host in `last_error`.
  Read as a lost lease, it would let the job expire five times with
  `last_error` NULL (fact-check c3).

**`write_results` raising is settled the same way.** It runs inside
`complete()`'s transaction, so an exception rolls back the results and the
completion together, and the runtime then writes the ending that exception
calls for — normally `fail` — rather than leaving the job `running` until its
lease expires.

**A parse that produced nothing from files that exist never stores.** One
file whose chunking raises is skipped and counted (`parse_errors`). When
**every** indexable file raises, the handler raises `ParseFailed` instead of
reaching `store`, because `write_results` *replaces* the repository's chunks:
carrying on would delete a good index, insert nothing and read `synced`
(PR #58's review). A tree with nothing indexable is not that case — nothing
raised, and an empty index is then the truth about the repository.

### Stages and progress

A handler reports coarse progress with `report_progress(stage, progress)`,
which writes `last_stage` and `progress` on the job row, fenced like every
other write. 21-07's endpoint returns both.

**The stage vocabulary is `fetch|parse|embed|store`** (22-05). It was
`clone|parse|embed|store`; there is no clone any more (U5 — the worker
downloads an archive). Migration 000014's `-- clone|parse|embed|store` column
comment is **historical, not schema**: `last_stage` is bare `TEXT` with no
`CHECK` (21-07), and this document is the authority for its values.

| Stage | Reported | What the stage does |
|---|---|---|
| `fetch` | on entry | ask the internal route for a token; fetch the archive at the default branch's exact SHA; revoke the token |
| `parse` | on entry | chunk every indexable file; more than 100,000 chunks is `Rejected` |
| `embed` | on entry | one vector per distinct chunk text, in slices of 1,000 |
| `store` | **last, just before the handler returns** | `write_results` runs in `complete()`'s transaction |

A stage is reported **on entry**, after a checkpoint, so `last_stage` is the
stage the job was in when it stopped (a shutdown while embedding leaves
`embed`). `store` is reported last because `report_progress` cannot run
inside `write_results` — it uses the same connection, and psycopg2's
re-entrancy guard refuses it — and `COMPLETE_SQL` does not touch
`last_stage`. **A completed job therefore reads `last_stage = 'store'`.**

**⚠ `progress` is replaced whole on every report, so every report carries
every count so far.** `PROGRESS_SQL` overwrites the column, and a report
with no payload writes NULL, so a `store` report of `{"chunks_stored": n}`
alone would erase what `fetch` found. The handler keeps one running
dictionary and sends the whole of it each time; each stage adds its keys and
removes none:

| Key | First reported at | Meaning |
|---|---|---|
| `files_indexable` | `parse` | files the fetch kept |
| `skipped` | `parse` | skip reason → count, e.g. `{"secret": 1, "unsupported": 12}`. **Counts, never paths** |
| `files_parsed` | `embed` | files chunked |
| `parse_errors` | `embed` | files whose chunking raised; skipped and counted rather than failing the job — unless **every** file raised, which is `ParseFailed` and never reaches `embed` |
| `chunks` | `embed` | chunks produced |
| `chunks_embedded` | `store` | chunks with their vector (duplicates share one) |
| `chunks_stored` | `store` | chunks the store stage writes; true of a `completed` row |

A `completed` row's `progress` therefore still holds `skipped` — the count
of secret-looking files that were never sent to OpenAI. 22.1-03 turns this
into the documented progress contract (P12).

---

## The `sync_state` projection

`repositories.sync_state` is what the UI reads. It is written **by** the job,
and it is never read to decide what work to do.

| Transition | `sync_state` written |
|---|---|
| a producer creates a **new** job | `pending` |
| a producer joins a job already live | **unchanged** |
| **claim** | **not written** |
| **mark_started** (`running`) | `syncing` |
| **complete** | `synced`, plus `last_synced_at = NOW()` |
| **fail**, retrying (`queued`, `attempts > 0`) | `failed` |
| **fail**, `dead` | `failed` |
| **reject** (a U6 cap), `dead` | `failed` |
| **defer** (suspended installation, or a shutdown) | **unchanged** |
| **abandon** (uninstalled, or no installation) | `never_synced` |
| superseded by a producer | **not written** |

Three of those rows are the ones people get wrong.

- **The claim writes nothing**, because the installation check comes first. A
  job under a dead installation must be abandoned to `never_synced` without
  ever having told the UI it was `syncing`.
- **`defer` leaves it alone.** Writing `failed` would call a healthy
  repository broken; writing `pending` would flicker it every hour the App
  stayed suspended.
- **`abandon` writes `never_synced`, never `failed`** — nothing failed, and a
  `failed` repository reads as "retrying" to a user.

### ⚠ `sync_state = 'syncing'` is NOT evidence of a live worker

This is the rule that matters most for Phase 23's UI, and it is the one a
reasonable person assumes the other way round.

`mark_started` projects `syncing`, and **`defer` writes no projection at
all** — by design, because the claim-time suspended-installation path must
not show `syncing` for an hour of waiting. So all four of these leave
`syncing` behind with nobody working:

- a worker that **crashed** mid-job;
- a worker whose **connection died** mid-job;
- a job **deferred part-way through a shutdown**, which is claimable that
  instant but has not been claimed yet;
- **since 22-05, a job whose installation was suspended MID-RUN**, which
  reads `syncing` **for up to an hour**. `mark_started` had already projected
  `syncing` when the token route answered `installation_suspended`, and
  `DEFER_SQL` — frozen since 21-05 — projects nothing, so the deferral
  leaves it there until the job is claimed again. That is the class 21-06's
  second review accepted for a shutdown deferral; for an hour it is
  user-visible, and it is known and documented rather than changed. **The job
  row is the truth:** `state = 'queued'`, `run_after` about an hour out,
  `last_error` saying the installation was suspended, `lease_expires_at`
  NULL, and `stalled` false.

A UI that reads `syncing` as "a worker is on it" will show a spinner forever
for a repository nothing is touching.

**The evidence is on the job row:** `state`, `lease_expires_at`, `updated_at`
and `attempts`. The predicate for *stalled* is

```sql
state = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
```

which is character-for-character the **`running`-with-a-dead-lease branch
that `claimSQL` and the sweeper share**.

**⚠ `stalled` does NOT mean "it will be picked up again".** That branch is
only half of each statement: *reclaimable* is this predicate **plus**
`attempts < max_attempts`, and the sweeper's dead-letter condition is this
predicate **plus** `attempts >= max_attempts`. They partition on a column
this expression does not read. So a stalled job at
`attempts >= max_attempts` is not waiting for a worker — the claim refuses
it and the next sweep writes `dead`. **Compare `attempts` with
`max_attempts` to tell the two apart**, and do not render "retrying shortly"
off `stalled` alone. (Measured on PR #43's review: `state = 'running'`,
`attempts = 5`, `max_attempts = 5`, lease expired → `stalled: true`, and the
job is one sweep from `dead`.)

**The `lease_expires_at IS NULL` half is not decoration:** `NULL < NOW()` is
NULL rather than true, so a version without it reports a null-lease row
healthy — and that row is invisible to a naive liveness check while still
occupying the partial unique index, blocking every future job for its
repository.

The endpoint below computes that predicate for you, in SQL, against the
database clock, and returns it as `stalled`. Use it rather than re-deriving
it, and never infer liveness from `sync_state`.

---

## `GET /api/admin/jobs/{id}`

One job by id.

**Who may read it: any member of the job's organization.** There is no role
gate — it shows the caller's own repository's indexing status. It needs a
`Bearer` access token from Supabase carrying an `organization_id` claim, like
every other tenant-scoped route.

```jsonc
{
  "id": "…",                         // the job, a UUID
  "repository_id": "…",
  "job_type": "full_ingest",         // or "incremental"
  "state": "running",                // queued|running|completed|dead|superseded
  "attempts": 1,
  "max_attempts": 5,
  "run_after": "2026-09-16T18:04:11.201Z",   // the backoff target
  "lease_expires_at": "2026-09-16T18:09:11.201Z",  // null unless leased
  "stalled": false,                  // see the rule above
  "last_stage": "embed",             // fetch|parse|embed|store (see "Stages and progress")
  "progress": {                      // CUMULATIVE: every count so far, or null
    "files_indexable": 3, "skipped": { "secret": 1 },
    "files_parsed": 3, "parse_errors": 0, "chunks": 9
  },
  "needs_rerun": false,
  "last_error": "FetchFailed: resolving acme/widgets@main: api.github.com answered 502",  // redacted; null if none
  "ingestion_run_id": null,
  "created_at": "2026-09-16T18:03:55.118Z",
  "updated_at": "2026-09-16T18:04:11.201Z"
}
```

**Timestamps are RFC 3339 with an offset, not necessarily `Z`.** Parse them;
do not string-compare them.

**⚠ There is no way to discover a job id from a repository yet.** Nothing in
the repositories API returns one — a connect logs the job id server-side and
its response carries only the repository. So this endpoint is usable by
anything that already holds an id, and not by a UI starting from a repository.
Phase 23 needs either a `job_id` on the repository response or a
list-by-repository endpoint; neither is built. **Filed as ISS-034**, because
choosing between the two is an API-contract decision with its own doc, test
and isolation surface — not something to settle in the plan that shipped the
reader.

### What it never returns, and why

| Column | Why not |
|---|---|
| `lease_owner` | A worker id is infrastructure identity, not status. The question a caller has is "is anything working on this right now", and `lease_expires_at` plus `stalled` answer it without handing out the value every terminal write is fenced on. |
| `payload` | Producer-supplied job parameters. It carries no credentials by design, but it is *input to the worker*, not status for a reader, and nothing in this contract should suggest it is a safe place to put something. |

`last_error`, `last_stage` and `progress` **are** returned, and they are safe
to return because the worker redacted them before they reached the column:
NUL bytes stripped, GitHub tokens (all six prefixes), fine-grained PATs,
OpenAI keys, JWTs and PEM private-key blocks replaced, then capped at 2,000
characters — values, nested values and dictionary **keys** alike.

**Two shapes are deliberately NOT redacted, and this is the endpoint that
makes that a trade rather than a detail**, because it puts the column on the
wire to any member of the organization with no role gate:

| Shape | Why it is left alone | What it costs |
|---|---|---|
| a bare 40-hex run | it is indistinguishable from a **git commit SHA**, which is the useful half of a clone or checkout failure — redacting it would blind this endpoint to *which commit* failed | the GitHub App **client secret** has the same shape. Not reachable today: no worker holds it, and `grep` for `client_secret` over `services/workers` finds nothing |
| a generic `://user:password@` DSN arm | psycopg2's connection errors do not quote the password, the clone-URL case is already covered by the `ghs_` arm, and a generic arm would blank the visible half of a credential-free URL for nothing | a credential arriving in a DSN shape from some future source would pass through |

Both were declined in 21-05 with those reasons recorded beside the pattern.
**Whoever gives a worker either value has to revisit them** — the cost of
widening before a path exists is one regular expression; the cost of widening
afterwards is whatever was written to the column in between.

### One 404 for every miss

| Status | When |
|---|---|
| 401 | No `Authorization: Bearer <token>`, or the token is not valid. |
| 403 | The token is valid but carries no `organization_id` claim. Send the user through `POST /api/user/select-organization`; never treat this as "signed out". |
| **404** | No such job, **or** the job belongs to another organization, **or** the id is not a canonical UUID. **Byte-identical in all three cases.** |
| 500 | A server-side fault. Retry and report. |

**The 404 is deliberately ambiguous and you must not try to disambiguate
it.** Telling "not yours" apart from "does not exist" would make this endpoint
an oracle answering "does this job id exist?" for every tenant in the system.
An id in any spelling other than the canonical lowercase `8-4-4-4-12` form
counts as not found too, so echo ids back exactly as they were given to you.

The isolation test for this endpoint asserts all three responses are
**byte-identical**, not merely all 404.

### ⚠ For anyone editing this endpoint

Two safety nets that exist elsewhere in this codebase are **absent here**, and
both absences are deliberate:

1. **No row-level security.** `WHERE id = $1 AND organization_id = $2` is the
   only tenant guard. Deleting the predicate returns any tenant's job with no
   error and nothing in the database to stop it.
2. **No CI gate.** `scripts/ci/check-isolation-tests.py` matches
   POST/PUT/PATCH/DELETE only, so a `GET` over this table passes the ratchet
   with no isolation test at all.

So `pkg/api/handlers/jobs_isolation_test.go` is written deliberately rather
than by ratchet, and its cross-tenant case is mutation-checked: neutering the
organization filter must fail it.

**`claimSQL` and `sweepSQL` must never appear in a request handler.** Both are
queue-wide and cross-tenant *by construction* — no organization filter, and no
row-level security to supply one. `TestClaimAndSweepNeverReachARequestHandler`
in `pkg/jobs` is a text gate over `pkg/api` for exactly that paste.

### ISS-012 applies here as on every tenant route

A revoked membership does not revoke the organization claim, so a removed
member could keep reading their former organization's job status until
whatever ships membership removal rewrites the claim. Nothing removes
memberships today.

---

## Retries, backoff and dead-lettering

`max_attempts` is **5**. After attempt *n* fails:

```
min(60s × 4^(n-1), 60 minutes) × U[0.5, 1.0)
```

| n | uncapped | capped | actual range |
|---|---|---|---|
| 1 | 60s | 60s | 30–60s |
| 2 | 240s | 240s | 120–240s |
| 3 | 960s | 960s | 480–960s |
| 4 | 3840s | **3600s** | 1800–3600s |
| 5 | 15360s | **3600s** | *(never used — attempt 5 writes `dead`)* |

**Worst case across five attempts: about 81 minutes.**

**The jitter multiplies; it does not add.** An added jitter on a capped
exponential pushes the tail *above* the cap, so the cap stops being one.
Multiplying keeps every delay inside `[cap/2, cap)` and still spreads a herd.

**Why a factor of 4 rather than 2:** with only five attempts, doubling puts
the last retry about sixteen minutes out in total — shorter than a single
large ingest, so it would retry a transient outage while it was still
happening.

**Three ways to reach `dead`.** `fail` writes it directly on the final
attempt. `reject` writes it on the first, for a U6 cap (22-05), because a
cap cannot heal. The **sweeper** is the backstop for the two cases where
nobody can: a worker that died holding a `running` job at `attempts >=
max_attempts`, and a job that failed *cleanly* on its last attempt and so
sits at `queued` with the claim refusing it on `attempts < max_attempts`. It
runs on the heartbeat's schedule from inside each worker's claim loop.

**A `dead` job is not retried.** It is outside the live set, so the next push
or reconnect enqueues a fresh job — which is the recovery path today.
**ISS-023** covers an explicit retry endpoint; the state machine makes it
possible, and nothing exposes it yet.

---

## Suspended and uninstalled installations

`ingestion_jobs.payload` carries **no credentials and no installation id**, on
purpose: two racing reconnects produce one job, and a job that had snapshotted
the loser's installation would silently use stale credentials. So the worker
reads the repository's **current** installation immediately after it claims,
under the job's tenant scope, and branches:

| What the worker finds | Action | `sync_state` | `attempts` |
|---|---|---|---|
| `installation_id IS NULL` | `abandon` → `superseded` | `never_synced` | left alone |
| the installation row is not visible under this tenant | `abandon` → `superseded` | `never_synced` | left alone |
| `uninstalled_at` set | `abandon` → `superseded` | `never_synced` | left alone |
| `suspended_at` set | `defer` **60 minutes** | **unchanged** | **handed back** |
| otherwise | `mark_started`, then run the handler | `syncing` | — |

**The same check happens again mid-run (22-05).** The backend's token route
reads the installation when the handler asks for its repository token, and
answers `409 installation_suspended` or `409 installation_uninstalled`; the
handler raises the matching exception and the runtime takes the same ending
as the table above — defer an hour with the attempt handed back, or abandon.
The one difference is the projection: by then `mark_started` has projected
`syncing`, so a mid-run suspension reads `syncing` for up to the hour (see
the rule above; the job row is the truth).

- **`mark_started` runs after the check, never before it.** That ordering is
  why the claim deliberately writes no projection: a doomed job must be
  abandoned without ever having said it was `syncing`.
- **`uninstalled_at` is tested before `suspended_at`,** because a reinstall
  clears both — a row carrying each of them is uninstalled, not suspended.
- **Sixty minutes for a suspension** means an unsuspend is noticed within the
  hour with no webhook work needed. GitHub does send
  `installation.unsuspend`, but the queue deliberately does not depend on it:
  a missed delivery would strand the repository forever.

A producer can still create a job under an installation that was uninstalled a
moment earlier — two statements in two transactions with no shared lock can
always interleave (**ISS-033**). The claim-time check above makes that cost
one round trip and a briefly wrong `sync_state`, rather than a wrong terminal
state.

---

## Pruning

**`ingestion_jobs` grows forever**, like `github_webhook_deliveries`. The
documented `DELETE`, recorded in the table's own `COMMENT`:

```sql
DELETE FROM ingestion_jobs
WHERE state IN ('completed','dead','superseded')
  AND updated_at < NOW() - INTERVAL '30 days';
```

Only terminal rows, and only by `updated_at`. **Phase 24 owns the schedule**;
nothing runs this today.

---

## The Phase 22 hand-off

Phase 21 left the worker refusing to start — an empty
`workers.jobs.handlers.REGISTRY`, exit 2, before any configuration was read —
and listed here what would turn it on. **22-05 did, and this section records
what it did.** It stays the authority: `ROADMAP.md` and `workers/__main__.py`
point here rather than keeping lists of their own, because three files with
three different lists is the failure mode `21-CONTEXT.md` opens by naming.

| Phase 21 asked for | What Phase 22 did |
|---|---|
| **Register the handlers** — the two keys fixed by 000014's `CHECK (job_type IN ('full_ingest','incremental'))` | 22-05: both keys run `workers.ingest.handler`'s full ingest (fetch → parse → embed → store) until 22.1-02 makes `incremental` re-parse only what changed. The empty-registry refusal still exists and is tested with the registry cleared; a second refusal (no `DATABASE_URL`) became reachable and has its own test, and a third refuses a missing `INTERNAL_API_URL` or `OPENAI_API_KEY` before anything is claimed |
| **`DATABASE_URL` for the compose `workers` service** | 22-05: given, with `OPENAI_API_KEY` and `INTERNAL_API_URL=http://backend:8081`, and the service put **behind the `ingest` profile**, so a plain `docker compose up` never starts it (the backend does not start under compose today, and a worker that could not reach the token route would dead-letter every queued job). It **never** carries the App key; `tests/test_compose_environment.py` asserts that by variable name |
| **Set `max_job_duration`** | 22-05: **two hours**, provisional until 22.1-05 (P16), override `WORKER_MAX_JOB_DURATION_SECONDS`. `__main__` always sets it, so the unset-bound WARNING cannot appear from a deployed worker |
| **Write handlers against the endings** | 22-05: the endings grew from three to the table in [How a job ends](#how-a-job-ends) — `Rejected` (`dead` in one attempt) for a U6 cap, the claim-time defer and abandon reused for a mid-run suspension or uninstall, a refused token as `LeaseLost` so nothing is written, and a misrouted token route as a loud `fail` |
| **Measure the pool** | 22-05 set it **provisionally**: two worker processes (four connections), the compose service's `deploy.replicas`, override `WORKER_REPLICAS`. 22.1-05 measures per-stage timings and replaces all three P16 numbers |
| `statement_timeout` on the heartbeat connection | 22-05: **fifteen seconds**, a quarter of the beat, override `WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS`, so a beat that blocks on a lock raises `57014` and counts as a failure. It is **merged into the DSN's own `options`** — never passed as a replacing `options=` keyword, which would silently drop the DSN's `-c role=…` and connect as whoever the DSN authenticates as |
| **Wire the pipeline to runs** | 22-05: `write_results` resolves the run (`resolve_ingestion_run`, so a retry of the same commit reuses it) and attaches it to the job (fenced), inside `complete()`'s transaction |
| **Make chunk writes idempotent per run** (ISS-027) | 22-05, for the full ingest: `write_results` **replaces** the repository's chunks — `DELETE … WHERE repository_id`, then the insert — in the same transaction, so a retry, a rerun or a second push leaves one copy. Retrievals citing a replaced chunk are **left dangling, by design** (P17, U9), and a test pins it. Per-file currency for incremental ingest is 22.1-02's |
| **Revisit the re-parent question** | Not re-opened: 21-07 ruled it settled, and 22-02's composite keys block drift on every normal write (the drift query stays, because replica mode bypasses the keys) |

**What a deployed worker needs**, all read by `workers/__main__` at startup
and refused with exit 2 when missing: `DATABASE_URL`, `INTERNAL_API_URL` (the
backend's **internal** listener, never the public API) and `OPENAI_API_KEY`.
**`DATABASE_URL` must log in as a `NOSUPERUSER NOBYPASSRLS` role**, as every
process in 22-05's live proof did (`rag_doc_app`): a superuser bypasses
row-level security, so the tenant checks beneath the worker's writes would go
unexercised. Compose's `coderag` is a superuser, so tenant isolation is **not**
exercised under compose; the composite keys and `trg_assert_tenant` still bind
it, but RLS does not.
`WORKER_WORKDIR` (default: the system temporary directory's
`rag-doc-worker`) needs about **1 GB free per worker process**: the archive
(up to 500 MB) is kept until extraction ends, and the extracted tree can reach
U6's 500 MB plus one file (1 MB) before the expansion counters stop it. The
worker **never** holds the GitHub App key (U4); it gets a one-repository,
read-only, one-hour token per job, and **revokes it** (`DELETE
/installation/token`) as soon as the fetch ends, because the lease gates a
token's issuance, not its hour of validity ([`internal-api.md`](internal-api.md)).
