# Ingestion jobs

The durable queue behind repository indexing: what a job is, every state it
can be in, and the one endpoint that reads it.

Written at the end of Phase 21, which built the queue and left it empty on
purpose. **Nothing claims a real job yet** — see
[the Phase 22 hand-off](#the-phase-22-hand-off) — so every state below is
reachable in tests and none of them is reachable in production until the
ingestion handlers are registered.

Related: [`api-repositories.md`](api-repositories.md),
[`api-github-webhooks.md`](api-github-webhooks.md),
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
| `queued` / `running` | out of attempts and nobody wrote the terminal state | `dead` | — | `sweep` |
| `running` | the installation is suspended, or the worker is shutting down part-way | `queued` | **−1** (handed back) | `defer` |
| `running` | the installation is uninstalled or gone | `superseded` | unchanged | `abandon` |
| `queued` / `running` | a relink, an uninstall, or a repository removed from the installation | `superseded` | unchanged | `pkg/jobs.SupersedeLive` |
| `completed` | `needs_rerun` was set while it ran | a **new** `incremental` job at `queued` | 0 | `complete`, in the same transaction |

Four of those rows carry a decision worth stating.

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
all** — by design, because the suspended-installation path must not show
`syncing` for an hour of waiting. So all three of these leave `syncing`
behind with nobody working:

- a worker that **crashed** mid-job;
- a worker whose **connection died** mid-job;
- a job **deferred part-way through a shutdown**, which is claimable that
  instant but has not been claimed yet.

They always have. A UI that reads `syncing` as "a worker is on it" will show a
spinner forever for a repository nothing is touching.

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
  "last_stage": "parse",             // conventionally clone|parse|embed|store
  "progress": { "files_parsed": 12 },// whatever the handler reported, or null
  "needs_rerun": false,
  "last_error": "clone failed: exited 128",  // redacted; null if none
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

**Two ways to reach `dead`.** `fail` writes it directly on the final attempt.
The **sweeper** is the backstop for the two cases where nobody can: a worker
that died holding a `running` job at `attempts >= max_attempts`, and a job
that failed *cleanly* on its last attempt and so sits at `queued` with the
claim refusing it on `attempts < max_attempts`. It runs on the heartbeat's
schedule from inside each worker's claim loop.

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

`python -m workers` starts, finds `workers.jobs.handlers.REGISTRY` empty, says
why and **exits 2 — before it reads a single environment variable**. That
ordering is deliberate: the compose `workers` service has no `DATABASE_URL`,
so reading configuration first would kill the container complaining about a
missing DSN, which is a true statement about the wrong problem.

**Five things turn it on**, and this list is the authority: `ROADMAP.md` and
`workers/__main__.py` point here rather than keeping counts of their own,
because three files with three different lists is the failure mode
`21-CONTEXT.md` opens by naming.

1. **Register the handlers.**
   ```python
   from workers.jobs.handlers import REGISTRY
   REGISTRY["full_ingest"] = run_full_ingest
   REGISTRY["incremental"] = run_incremental
   ```
   The two keys are fixed by the schema's
   `CHECK (job_type IN ('full_ingest','incremental'))`, not by convention.

2. **Add `DATABASE_URL` to the compose `workers` service.** It has
   `ENV=development` and nothing else today. No compose change was made in
   Phase 21, deliberately: the service exits 2 with the handler message until
   Phase 22 is ready, which is the safe ending.

3. **Set `max_job_duration`.** It defaults to `None` — no bound — and until it
   is set, a hung handler holds its lease indefinitely, nothing can reclaim
   the job, the repository sits `syncing`, and that worker's sweeper never
   runs again either. The mechanism is built and tested; the *number* was
   deliberately not invented, because it is a multiple of a typical ingest and
   nothing has ingested end to end yet. An unset bound logs a WARNING at
   startup so the hazard cannot ship silently.

4. **Write handlers against the three endings**, where the exception type is
   the contract:

   | Ending | Means | Result |
   |---|---|---|
   | `return` | done | `complete`, with the returned `write_results` callback |
   | `raise Unfinished(...)` | stopped part-way (check `ctx.is_shutting_down()`) | `defer`, attempt handed back — **only** during a shutdown; otherwise `fail` |
   | raise anything else | failed | `fail`, attempt consumed, backoff applied |

   ⚠ **A bare `return` after stopping early writes `completed` and
   `sync_state = synced` over work that did not happen.** That is not a style
   point: it is a row a review produced, reading
   `state=completed last_stage=parse sync_state=synced` with `last_synced_at`
   stamped and the partial unique index freed, so nothing would re-queue it.

5. **Measure the pool.** **One job at a time per process; scale by
   processes.** The claim is safe across any number of them — proven from both
   Go and Python with a barrier race. *How many* processes is still an open
   input: nothing has ingested end to end, so measure the incremental duration
   first and size from it. **Each worker holds two connections while a job
   runs** — the claim loop's and the heartbeat's — so a pool of N wants 2N.

Also Phase 22's, and not started:

- **Wire the pipeline to runs.** `PostgresWriter.create_ingestion_run` has no
  `ON CONFLICT`, so a repeat of the same commit raises `23505`. The shape is
  `resolve_ingestion_run(cur, …)` then `attach_ingestion_run(cur, job, …)` in
  one tenant-scoped transaction, with `PostgresWriter` taking the run id
  rather than minting one.
- **Make chunk writes idempotent per run.** The completion transaction removes
  *torn* writes, not *duplicated work*: a crash mid-embedding re-runs the job
  from the start and the retry reuses the same `ingestion_runs` row. So
  `write_results` must delete-by-run-then-insert, or upsert (**ISS-027**).
- **Revisit the re-parent question.** The tenant trigger validates at INSERT
  and on UPDATE of its own columns; it is blind to a repository's tenancy
  changing underneath it. That cannot happen today (cross-organisation
  re-parenting is forbidden), so it is latent rather than live — but it should
  be revisited rather than inherited silently.
