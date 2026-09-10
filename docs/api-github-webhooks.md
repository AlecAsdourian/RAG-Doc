# GitHub webhooks

What we receive, what we do with it, and what Phase 21 is expected to
pick up.

Related: [`api-github-install.md`](api-github-install.md),
[`api-repositories.md`](api-repositories.md),
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

**A delivery that did not finish CAN be redelivered.** The row is written
before processing with `outcome = 'processing'`, and a redelivery of
anything still `processing` or `failed` is re-claimed and processed
again. Only a finished delivery is treated as a duplicate.

That is a correction to an earlier design which treated any existing row
as a duplicate, on the reasoning that replaying a partially-applied event
is worse than recording it as failed. **No handler here can be partially
applied** — each does all of its writes in one tenant transaction and
each is an idempotent update by key — so that reasoning bought nothing
and cost a great deal: a transient database blip, a deploy restart or a
panic mid-handler silently dropped an uninstall or a suspend forever.

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
process and takes the only record of the work with it.

### `installation`

| Action | Effect |
|---|---|
| `created`, already linked | Refreshes account name, type and repository selection. **Never** changes which organization owns it. |
| `created`, unknown | **Nothing.** See below. |
| `deleted` | Marks `uninstalled_at`. Repositories are **kept**; those that were `pending` or `syncing` are stood down to `never_synced`. One already `synced` keeps that state — it is simply not re-synced until the App returns. |
| `suspend` / `unsuspend` | Sets or clears `suspended_at`. |

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
| `added` | Repositories we already track are re-pointed at the installation and queued. New ones are **not** created. |
| `removed` | `installation_id` cleared, `sync_state` set to `never_synced`. Rows are **not** deleted. |

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

Marks the repository `pending`. **Default branch only** — ingesting every
feature branch is not the product, and the comparison uses the branch
GitHub reports on the payload rather than a stored value, so a repository
whose default branch changed is handled without us noticing the change.

A repository already `syncing` is left alone (ISS-016).

A push to a repository nobody connected is ignored.

### `ping`

Answered 202. GitHub sends one when the App is created, and a failure
there is the first red line in the delivery log.

---

## What Phase 21 inherits

**The work item is a row in `repositories` with `sync_state = 'pending'`.**
There is no queue table; this phase deliberately did not invent one,
because the queue's shape is Phase 21's decision.

To find work:

```sql
SELECT id, project_id, installation_id, github_repo_id, default_branch
FROM repositories
WHERE sync_state = 'pending' AND installation_id IS NOT NULL;
```

`idx_repositories_sync_state` is a partial index on
`sync_state <> 'synced'`, which covers exactly this.

Three things the queue must handle, none of which this phase solved:

1. **`sync_state` is a status column being used as a queue.** It has no
   lease, owner or attempt counter, so two workers can believe they own
   the same repository. **ISS-016**, and it should be settled before the
   queue is built rather than after.
2. **`installation_id IS NULL` means unsyncable, not failed.** Those rows
   are waiting for a reinstall and must not be retried.
3. **A suspended installation cannot mint a token.** Check
   `github_installations.suspended_at` and `uninstalled_at` before
   attempting a sync, or the failure arrives as an opaque 403 from
   GitHub.
