# GitHub webhooks

What we receive, what we do with it, and what it puts on the ingestion
queue.

Related: [`api-github-install.md`](api-github-install.md),
[`api-repositories.md`](api-repositories.md),
[`api-ingestion-jobs.md`](api-ingestion-jobs.md) — the queue these handlers
write to, and every state a job can reach,
[`github-app-setup.md`](github-app-setup.md).

---

## `POST /webhooks/github`

Public. GitHub holds no token of ours, so **the HMAC signature is the
entire authentication story** — everything downstream is only as
trustworthy as that check.

| Header | Use |
|---|---|
| `X-Hub-Signature-256` | `sha256=<64 hex>`, HMAC over the **raw body**. Verified before any parsing. |
| `X-GitHub-Event` | Dispatch. |
| `X-GitHub-Delivery` | A UUID. The idempotency key; a request without one is refused. |

`X-Hub-Signature` (the legacy SHA-1 header) is deliberately ignored.
Accepting either would let a caller choose the weaker one.

| Status | When |
|---|---|
| 202 | Handled, ignored, or a duplicate. All three are success. |
| 400 | No delivery id, or a signed body that is not JSON. |
| 401 | Signature missing, malformed, or wrong. |
| 413 | Body over 25MB. |
| 500 | We could not record or process it. GitHub will redeliver — see below. |

**Unrecognised events answer 202, not 4xx.** Filling the App's delivery
log with red for events nobody subscribed to makes the first place anyone
looks when webhooks seem broken useless.

---

## Idempotency

`X-GitHub-Delivery` is recorded in `github_webhook_deliveries` with a
UNIQUE constraint, and the insert is the lock: concurrent redeliveries of
the same event race, one wins, the rest answer 202 and do nothing.

GitHub redelivers on failure, and the **Redeliver** button in the App's
Advanced settings is a normal part of development, so duplicates are
routine rather than exceptional.

**A delivery that did not finish CAN be redelivered**, under two
different rules:

- **`failed`** — re-claimable immediately. The attempt is provably over,
  so there is no live worker to collide with.
- **`processing`** — re-claimable only once the row is **older than five
  minutes**. A `processing` row usually means a worker is still on it,
  and re-claiming that is how a burst of concurrent redeliveries all end
  up processing the same event. Five minutes is far beyond any real
  handler here (the route carries a 30-second timeout) and short enough
  to recover from a crash.

Only a finished delivery is a duplicate outright.

**A redelivery therefore RUNS THE HANDLER AGAIN**, which is what makes
re-entrancy a property the handlers have to hold rather than a paragraph.
Every producer call they make is safe to repeat: the enqueue is an upsert
against `(repository_id) WHERE state IN ('queued','running')`, so a second
run joins the live job instead of creating another one; a supersede of an
already-superseded job matches nothing and says so; and every other write
is keyed rather than incremental.

**Do not rely on this to recover a lost event on its own.** It recovers
one *if* GitHub redelivers, *and* redelivers again after the five-minute
mark, *and* reuses `X-GitHub-Delivery`. The last of those is recorded as
unverified in ISS-019, and the plan for this phase warned against
designing around a vendor's retry behaviour. A delivery that fails and is
never redelivered stays lost until the repository is pushed to or
reconnected.

That is a correction to an earlier design which treated any existing row
as a duplicate, on the reasoning that replaying a partially-applied event
is worse than recording it as failed. **No handler here can be partially
applied** — each does all of its writes in one tenant transaction and
each is an idempotent update by key — so that reasoning bought nothing
and cost a great deal: a transient database blip, a deploy restart or a
panic mid-handler silently dropped an uninstall or a suspend forever.

The five-minute rule is the correction to the correction: allowing any
`processing` row to be re-claimed reintroduced the concurrency bug the
delivery table exists to prevent.

That table is **not tenant-scoped**, unlike everything else this phase
touches — a delivery arrives before we know whose it is, and
`installation.deleted` concerns a tenant that is going away. It also
**grows forever**; migration 000012 carries the pruning statement.

`github_installation_tenants` is the other table here without row-level
security, for the same reason: it is the index the receiver consults to
*discover* a tenant, so it cannot be scoped by the answer. It is
maintained by a trigger on `github_installations` and is never written
directly.

---

## What each event does

Everything here **records intent**. Nothing fetches from GitHub and
nothing starts work — a goroutine begun in a webhook dies with the
process and takes the only record of the work with it. What "recording
intent" produces is a row in `ingestion_jobs`, written through `pkg/jobs`
and nowhere else.

**⚠ Two of these payload shapes are unverified — ISS-019.** The
`installation` payloads were captured from real deliveries; `push` and
`installation_repositories` were written from GitHub's documentation,
because no such delivery has ever reached a capture server. Their tests
are named `UNVERIFIED_*` so nobody mistakes them for evidence, and this is
not pedantry: capturing the `installation` payloads corrected three specs,
including a size field that was out by a factor of a thousand. The handler
behaviour below is tested; the field names it reads are not.

### `installation`

| Action | Effect |
|---|---|
| `created`, already linked | Refreshes account name, type and repository selection. **Never** changes which organization owns it. |
| `created`, unknown | **Nothing.** See below. |
| `deleted` | Marks `uninstalled_at`. Repositories are **kept**; every live job under the installation is **superseded**, and the repositories that had one — plus any left `pending` or `syncing` by an older build — are stood down to `never_synced`. One already `synced` keeps that state. |
| `suspend` / `unsuspend` | Sets or clears `suspended_at`. **No job is created or cancelled** — that is the whole of the behaviour here, deliberately. The worker **defers** a job whose installation is suspended at claim time (21-06), for an hour and without consuming an attempt, so a suspension that is lifted resumes rather than having burned its retries — and the queue does not depend on `unsuspend` being delivered. |

**`deleted` supersedes and then stands down, in that order and in one
transaction.** The stand-down covers exactly the repositories whose live
job was cancelled, not a list of sync states — under the projection, a
repository whose job is *retrying* reads `failed`, and a state-based
filter would leave it looking like it is still retrying after its job was
taken away.

**An installation we do not recognise is not adopted.** A user can
install from GitHub's directory without passing through our install flow,
so this event routinely arrives for an installation belonging to no
organization of ours. The tempting fix — link it to whoever acted most
recently — hands one customer's GitHub account to another, and 20-04's
review demonstrated exactly that attack in its non-webhook form. Nothing
in the payload identifies one of *our* users.

The user completes the flow from inside the app, where their organization
comes from a verified claim. Until then the installation is live on
GitHub and unlinked here, which is a normal state.

**`deleted` keeps everything.** An uninstall means access was lost, not
that the user asked us to forget what we ingested, and uninstalling by
accident is easy. Repositories keep their rows, their link and their
ingested content; `docs/api-repositories.md` already documents that state
and how to recover from it.

### `installation_repositories`

| Action | Effect |
|---|---|
| `added` | Repositories we already track are re-pointed at the installation and get a `full_ingest` job — one call for the whole set. Any whose `installation_id` actually **changed** have their live job superseded first. New repositories are **not** created. |
| `removed` | `installation_id` cleared, `sync_state` set to `never_synced`, and any live job **superseded** — a job for a repository the App can no longer read fails on its first call anyway, so it is stopped promptly rather than left to race the stand-down. |

**`added` queues every repository, not most of them.** One
`jobs.Enqueue` over the whole set, where each row either inserts a job or
joins the live one. The design this replaced caught the unique-index
violation from a plain `INSERT` and returned success, which is correct for
two reconnects of one repository and wrong for a set: three repositories
racing a relink left two of them never queued while the handler reported a
win.

**A webhook cannot create a repository row.** Verified 2026-09-08: the
payload's repository shape is reduced to `id`, `node_id`, `name`,
`full_name`, `private` — no `default_branch`, `size`, `visibility` or
`archived`.

The reason is not that the database would refuse it. `default_branch` is
`NOT NULL DEFAULT 'main'`, so a half-row *would* be accepted and would
silently claim the default branch is `main` — being accepted is what
makes it dangerous. (An earlier version of this page said the `NOT NULL`
would stop us. It would not.)

The reason is a product one: connecting a repository is a deliberate act,
and a webhook firing because someone widened a permission scope is not
that act. Connecting stays with `POST /api/repositories`, which has an
installation token and can fetch the real shape.

### `push`

Creates an **`incremental`** job for the repository. **Default branch
only** — ingesting every feature branch is not the product, and the
comparison uses the branch GitHub reports on the payload rather than a
stored value, so a repository whose default branch changed is handled
without us noticing the change.

**A push arriving while a job is already live joins that job** rather than
queueing a second one, and the outcome says `joined the live job`. This is
close to all steady-state volume, not an edge case: an ingest takes
minutes and people push repeatedly. The live job covers the new commits
either way — if it has not been claimed yet it will clone at whatever HEAD
is current when it is, and if it is running, `needs_rerun` is set so that
the worker re-queues it once on completion. The completion clears the flag
and enqueues an `incremental` follow-up in the same transaction, **after**
the completion write — the reverse order silently loses the rerun (21-05).

That replaces the old behaviour, which refused to touch a repository whose
`sync_state` was `syncing` and so **dropped the push**. Both halves of that
were ISS-016: `sync_state` was the queue, stamping over `syncing` gave one
repository two writers, and not stamping lost the work.

A push to a repository nobody connected is ignored.

### `ping`

Answered 202. GitHub sends one when the App is created, and a failure
there is the first red line in the delivery log.

---

## What a webhook puts on the queue

**The work item is a row in `ingestion_jobs`** — leased, retried and
dead-lettered — and **`pkg/jobs` is the only producer**. Nothing in
`services/backend` writes that table directly, and nothing writes
`repositories.sync_state` as a way of asking for work.

**`sync_state` is a projection.** The producer writes `pending` for a
repository that got a *new* job, and the handlers write `never_synced`
when access is lost. It is what the UI reads and what
`idx_repositories_sync_state` indexes. It is not a queue, and treating it
as one is what **ISS-016** was: a status column with no owner, no lease and
no attempt counter, so two writers could each believe they owned the same
repository.

**⚠ The consumer exists and still claims nothing.** 21-05 and 21-06 built
the transitions, the claim loop, the heartbeat and the sweeper, and
`python -m workers` **refuses to start** — it finds the handler registry
empty, says so and exits 2 before reading any configuration. So the
sentences below describe code that is written and tested, and a repository
that gets a job still stays `pending` until Phase 22 registers the ingestion
handlers. See
[`api-ingestion-jobs.md`](api-ingestion-jobs.md#the-phase-22-hand-off).

To find work, read the queue, not the projection:

```sql
SELECT id, organization_id, repository_id, job_type, attempts
FROM ingestion_jobs
WHERE state = 'queued' AND run_after <= NOW()
ORDER BY run_after
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

`idx_ingestion_jobs_claimable` covers exactly that, and
`idx_ingestion_jobs_one_live_per_repo` —
`UNIQUE (repository_id) WHERE state IN ('queued','running')` — makes two
live jobs for one repository unrepresentable rather than merely unlikely.

Four things that follow, and that a reader of this page needs:

1. **`installation_id IS NULL` gets no job.** Those repositories are
   *unsyncable*, not failed: they are waiting for a reinstall, and a job
   for one would clone nothing, fail its attempts and dead-letter. The
   same goes for a repository whose installation carries `uninstalled_at`.
   Their `sync_state` is `never_synced`, so nothing tells a user that work
   is under way when none can be.

   **Enforced by migration 000015 and by the handlers, not yet by the
   producers.** `handlePush` and `recordAddedRepositories` resolve
   repositories by `installation_id` without checking `uninstalled_at`, so
   a `push` racing an `installation.deleted` can still create a job under a
   dead installation — **ISS-033**, which also records why 21-06's
   claim-time check makes it a wasted round trip rather than a wrong
   terminal state.
2. **A suspended installation is DEFERRED, not failed** (21-06). The worker
   reads `github_installations.suspended_at` when it claims a job and puts
   the job back sixty minutes later without consuming an attempt, so a
   suspension that is lifted resumes rather than having burned its retries.
   Nothing on this page cancels or re-queues a job on `suspend` or
   `unsuspend`, and that is the design rather than a gap.
3. **An uninstall supersedes and stands down.** Live jobs leave the live
   set; their repositories become `never_synced`, keeping their rows,
   their ingested content and their installation link so a reinstall can
   recover.
4. **Supersede before enqueue, always.** Both in one transaction. The
   reverse order raises no error at all through the enqueue upsert: it
   flags `needs_rerun` on the job that is about to leave the live set, the
   supersede then removes it, and the repository ends with no live job.

`services/backend/migrations/000015_backfill_ingestion_jobs.up.sql` gave a
job to every repository the pre-queue webhook path had left `pending`.
