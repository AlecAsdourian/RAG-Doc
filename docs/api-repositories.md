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

**`archived` repositories still appear.** GitHub archived them; we do not
filter. Worth a visual marker.

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
      "created_at": "2026-09-08T…Z"
    }
  ],
  "next_cursor": "MjAyNi0wOS0wOF…"     // null on the last page
}
```

**Paginate by following `next_cursor` until it is null.** Do not construct
one — it encodes a position in an ordering, not an identifier, and a
client that parses it will break when the ordering changes.

Cursor rather than offset because **this list changes while you page
through it**: the GitHub webhook (Phase 20-05) inserts repositories
without the user doing anything, and offset pagination would skip and
duplicate rows when that happens.

A stale or malformed cursor is a **400**. Restart from no cursor.

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

**201** returns the repository object above, `sync_state: "pending"`.

**Connecting an already-connected repository is not an error** — it
refreshes the stored metadata and returns 201 with the existing row. Safe
to retry.

| Status | Meaning |
|---|---|
| 400 | Malformed body — `github_repo_id` must be a positive integer, `installation_id` a UUID. Also returned for a body over 4KB. |
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
id", identically.

---

## `DELETE /api/repositories/{id}`

**This deletes ingested data, and it is not reversible.**

The database cascade is `repositories → ingestion_runs → chunks →
retrievals`, so removing a repository removes everything derived from it.
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
  "note": "this removed the repository and everything ingested from it; …"
}
```

**Confirm before calling this**, and use the returned counts to tell the
user what was removed. Reconnecting the same repository is allowed but
requires a full re-ingestion.

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
