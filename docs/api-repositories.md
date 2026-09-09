# Repositories API

The contract Phase 23's UI implements against.

All four endpoints require a `Bearer` access token from Supabase and are
tenant-scoped: you see your organization's repositories and nobody else's.
See [`auth-frontend-contract.md`](auth-frontend-contract.md) for how to get
a token and what the organization claim means.

Related: [`isolation.md`](isolation.md), [`github-app-setup.md`](github-app-setup.md).

---

## The states you will actually hit

Read this before the endpoint list. Most of the client-side complexity is
here, not in the request shapes.

**Every repository is `never_synced` or `pending` right now.** Ingestion
ships in Phase 22. A UI that waits for `synced` before showing anything
will show nothing indefinitely.

| `sync_state` | Meaning |
|---|---|
| `never_synced` | Connected before sync existed, or never queued. Pre-existing rows. |
| `pending` | Queued. Everything connected through this API starts here. |
| `syncing` | In progress (Phase 21+). |
| `synced` | Content is current as of `last_synced_at`. |
| `failed` | Last attempt failed. Nothing here explains why yet. |

**`installation_id` can be null.** It becomes null when the GitHub App is
uninstalled — the repository and everything ingested from it are kept
deliberately, but it cannot be re-synced until the App is reinstalled.
Show these as needing attention rather than hiding them; the user's data
is still there.

To recover, reinstall the App and `POST /api/repositories` again with the
new installation. That relinks the existing row rather than creating a
second one, and moves it back to `pending` — including when the
repository was mid-sync, so a run that was in flight against the old
installation is superseded rather than waited for.

**`archived` repositories still appear.** GitHub archived them; we do not
filter. Worth a visual marker.

---

## Errors common to every endpoint

| Status | When |
|---|---|
| 401 | No `Authorization: Bearer <token>`, or the token is expired, malformed, or not signed by Supabase. |
| 403 | The token is valid but carries no `organization_id` claim. The user is signed in with no organization selected — send them through `POST /api/user/select-organization`. Never treat this as "signed out". |
| 500 | A server-side fault. The body carries no detail; there is nothing for the client to do but retry and report. |

The per-endpoint tables below list only the statuses those endpoints add.

---

## `GET /api/repositories`

Cursor-paginated list, oldest first.

**Query parameters**

| Name | Default | Notes |
|---|---|---|
| `limit` | 25 | 1–100. Outside that range is a 400. |
| `cursor` | — | Opaque. Pass back the `next_cursor` from the previous response. |

```jsonc
{
  "repositories": [
    {
      "id": "…",                       // ours, a UUID
      "name": "ES-SC-API-Navigator",
      "git_url": "https://github.com/…/….git",
      "default_branch": "main",
      "github_repo_id": 1103353668,    // GitHub's, stable across renames
      "installation_id": "…",          // null if the App was uninstalled
      "visibility": "private",
      "size_kb": 75,                   // KILOBYTES, not bytes
      "archived": false,
      "sync_state": "pending",
      "last_synced_at": null,
      "created_at": "2026-09-08T23:24:03.942762-07:00"
    }
  ],
  "next_cursor": "MjAyNi0wOS0wOF…"     // null on the last page
}
```

**Timestamps are RFC 3339 with an offset, not necessarily `Z`.** They
carry whatever offset the database session is in. Parse them; do not
string-compare them or assume a trailing `Z`.

**Paginate by following `next_cursor` until it is null.** Do not construct
one — it encodes a position in an ordering, not an identifier, and a
client that parses it will break when the ordering changes.

Cursor rather than offset because **this list shifts while you page
through it**: the GitHub webhook (Phase 20-05) inserts repositories
without the user doing anything, and offset pagination skips and
duplicates rows that were already visible when that happens.

**A held cursor will not surface every new row, and cannot.**
`created_at` is assigned when the inserting transaction *starts*, but the
row only appears when it *commits*, so a write that began before your
cursor and committed after it lands permanently behind you. To watch for
new repositories, re-poll from the first page rather than resuming a
cursor. Cursor pagination fixes the shifting-window problem; nothing
here fixes commit-order skew.

A stale or malformed cursor is a **400** — including one carrying an id
in any spelling other than the canonical `8-4-4-4-12` lowercase form.
Restart from no cursor.

| Status | Meaning |
|---|---|
| 400 | `limit` outside 1–100 or not an integer, or a malformed cursor. |

---

## `POST /api/repositories`

Connect a repository the caller's installation can see.

```jsonc
{
  "github_repo_id": 1103353668,
  "installation_id": "…"              // from GET /api/github/installations
}
```

**`github_repo_id`, not a URL.** GitHub's numeric id is stable across
renames and transfers; a URL is not, and would let someone name a
repository their installation cannot reach.

**201** returns the repository object above. A first connect is
`sync_state: "pending"`.

**Connecting an already-connected repository is not an error** — it
refreshes the stored metadata and returns 201 with the existing row. Safe
to retry, and safe to call on a repository whose App was reinstalled: the
existing row is relinked to the new installation rather than duplicated.

**Re-connecting does not reset `sync_state`, with one exception.** A
repository that is `failed` comes back `failed`; a re-connect is a
metadata refresh, not a retry, and re-queueing here would restart a run
already in flight. The exception is a repository whose `installation_id`
changed — it has to be fetched again through the new credential, so it
returns to `pending`. **Do not show "queued" on the strength of having
called this**; read the `sync_state` in the response.

**One repository per organization, wherever it already lives.** Connecting
a repository your organization has connected before returns that same row
— even if it sits in a project other than the default, and even if it was
connected before this API existed and carries no GitHub id yet. You will
not get a duplicate, and the `id` you get back may not be new.

| Status | Meaning |
|---|---|
| 400 | Malformed body — `github_repo_id` must be a positive integer, `installation_id` a canonical lowercase UUID. Also returned for a body over 4KB, or for anything after the JSON object. |
| **404** | The installation is not yours, does not exist, **or** the repository is not visible to it. Deliberately one status for all three. |
| 503 | The backend has no GitHub App credentials. Not your fault; retry later. |

**The 404 is deliberately ambiguous and you should not try to
disambiguate it.** Telling "not yours" apart from "does not exist" would
let a caller discover which installation and repository ids exist in other
organizations. Surface it as "we could not find that repository in your
connected accounts".

---

## `GET /api/repositories/{id}`

Returns one repository object, or **404** — which again covers "no such
repository", "belongs to another organization", and "that is not a valid
id", identically. An id in any spelling other than the canonical
lowercase `8-4-4-4-12` form counts as not valid, so echo ids back exactly
as they were given to you.

---

## `DELETE /api/repositories/{id}`

**This deletes ingested data, and it is not reversible.**

The database cascade:

```
repositories ─┬─> ingestion_runs ─> chunks
              └─> chunks ─> retrievals ─> feedback
```

So removing a repository removes everything derived from it — including
**`feedback` the user wrote** on answers that cited its chunks, which is
the one thing here that re-ingesting cannot bring back. `queries`
survive; only the retrievals pointing at this repository's chunks go.

That is intended, not incidental: a repository whose contents stayed
searchable after being disconnected would be the wrong answer for a
product that answers questions from that content — and worse if it was
disconnected *because* it should not have been indexed.

```jsonc
{
  "status": "deleted",
  "repository_id": "…",
  "chunks_deleted": 1284,
  "ingestion_runs_deleted": 3,
  "feedback_deleted": 12,
  "note": "this removed the repository, everything ingested from it, and …"
}
```

**Confirm before calling this**, and use the returned counts to tell the
user what was removed — `feedback_deleted` especially, since it is the
only irreplaceable number in the list. Reconnecting the same repository is
allowed but requires a full re-ingestion.

The counts are read just before the delete in the same transaction. A sync
running concurrently can add rows in between, so treat them as "at least
this many", not as an audited total.

**404** for a repository that is not yours or does not exist. Note this
is what a cross-tenant delete returns — the row is untouched.

---

## Not in this API yet

- **Triggering a sync.** Phase 21 owns the queue; there is no
  "sync now" endpoint.
- **Why a sync failed.** `failed` carries no reason yet.
- **Choosing a project.** Repositories connect to the organization's
  default project. Note that a repository connected before this API
  existed may sit in a different project — do not assume every repository
  shares one.
- **Filtering or sorting.** The list is ordered oldest-first and takes no
  filters.
