# GitHub App install flow

How a user connects their GitHub account, and what the frontend has to do
about each way it can end.

Related: [`api-repositories.md`](api-repositories.md) (what to do once an
installation exists), [`github-app-setup.md`](github-app-setup.md) (how
the App itself is registered).

---

## The shape of the flow

```
  your UI                    our backend                      GitHub
     │                            │                              │
     │  "Connect GitHub" ────────►│                              │
     │                            │  mint state, bind org        │
     │◄───── 302 ─────────────────│                              │
     │  follow it ───────────────────────────────────────────────►│
     │                            │        user picks an account │
     │                            │◄──── 302 /api/github/callback │
     │                            │  validate, verify, persist   │
     │◄───── 302 back to you, with ?github_result=… ─────────────│
```

**Start it with a full-page navigation, not `fetch()`.** The user has to
end up on github.com, and the callback comes back as a browser redirect
carrying no `Authorization` header. An XHR cannot follow that.

---

## `GET /api/github/install`

Authenticated and organization-scoped. Responds **302** to
`https://github.com/apps/<slug>/installations/new?state=<token>`.

The state token is single-use, expires in 5 minutes, and **carries the
organization server-side**. That is why there is no `organization_id`
parameter to pass: whichever organization the caller is in when they hit
this endpoint is the one the installation will be linked to, and nothing
later in the flow can change it. A user who switches organizations in
another tab mid-flow still links the installation to the one they
started from.

| Status | Meaning |
|---|---|
| 302 | Follow it. |
| 401 | Not signed in. |
| 403 | Signed in with no organization selected. Send them through `POST /api/user/select-organization` first. |
| 503 | The state store or the App credentials are unavailable. The flow refuses to start rather than sending the user to GitHub for an installation it could not finish. |

---

## `GET /api/github/callback`

**GitHub calls this, not you.** It is deliberately unauthenticated — a
browser following GitHub's redirect sends no bearer token — so the state
token is the only credential on the request.

It always answers **302** back to `FRONTEND_URL` with:

- `github_result` — one of the values below
- `github_message` — a sentence safe to show verbatim (absent on success)

| `github_result` | What happened | What the UI should do |
|---|---|---|
| `connected` | Linked. | Refresh the installation list; offer repository selection. |
| `missing_state` | The user installed from GitHub's own "Install App" button, so there is no state token and therefore no way to know which workspace they meant. | Explain that they need to start from the in-app button, and show it. **Do not guess an organization.** |
| `invalid_state` | Expired (5 min), already used, or forged. | Offer to start again. Not an error worth alarming anyone about — a slow user hits this. |
| `invalid_installation` | GitHub did not send a usable id, or we could not reach that installation. | Offer to start again; suggest checking the App is still installed. |
| `suspended` | The installation exists but is suspended on GitHub. | Tell them to un-suspend it in GitHub's settings. |
| `already_connected` | That GitHub account is already linked to a **different** workspace. | Show the message as-is. |
| `unavailable` | Our GitHub integration or state store is down. | Retry later. |
| `error` | We failed to save it. | Retry; if it persists it is ours to fix. |

**On `already_connected`, do not try to find out who has it.** The
response deliberately does not say. Naming the other workspace would
confirm that workspace exists and uses this product; the collision is
logged with both organizations for operators, and neither reaches the
user.

This is the case where a GitHub account is genuinely shared — someone
installs the App on an organization another customer already connected.
It is a comprehensible situation with a human resolution (uninstall on
GitHub, or connect a different account), which is why it is a normal
result rather than a server error.

---

## `GET /api/github/installations`

Authenticated and organization-scoped. Every installation this
organization has linked, oldest first.

```jsonc
{
  "installations": [
    {
      "id": "…",                          // ours, a UUID — address installations by this
      "account_login": "acme-inc",
      "account_type": "Organization",
      "repository_selection": "selected", // or "all"
      "created_at": "2026-09-09T…-07:00"
    }
  ]
}
```

**This is how you get an `id` for the endpoint below.** The success
redirect also hands one back directly (`?installation_id=…`), which
covers the just-installed case without a round trip; use this endpoint on
any later visit.

GitHub's own numeric installation id is deliberately not returned. A UI
addresses installations by our uuid, and the number is the value someone
would need in order to talk to GitHub about an installation that is not
theirs.

---

## `GET /api/github/installations/{id}/repositories`

Authenticated and organization-scoped. One page of what an installation
can see on GitHub, for a picker to offer before calling
`POST /api/repositories`.

| Query | Default | Notes |
|---|---|---|
| `page` | 1 | 1-based. |
| `per_page` | 30 | 1–100. |

```jsonc
{
  "repositories": [ /* GitHub's repository shape */ ],
  "page": 1,
  "has_next": true
}
```

**Page through it; do not ask for everything.** This proxies GitHub's own
pagination rather than accumulating server-side, so an installation with
5,000 repositories stays 5,000 repositories on GitHub's side instead of
becoming one enormous response built from 50 sequential round trips.

`has_next` is derived from the page being full, so the last page of an
exactly-divisible set costs one extra empty request. That is the cheap
failure; misreading GitHub's `Link` header would be the expensive one.

**404** for an installation that is not yours or does not exist —
identical answers, as everywhere else in this API. The check happens
before GitHub is contacted, so a probe for other tenants' installation
ids cannot be distinguished by timing either.

---

## Not in this API yet

- **Uninstalling.** Removing the App on GitHub sets `installation_id` to
  null on the affected repositories (Phase 20-05 handles the webhook);
  there is no endpoint to disconnect from our side.
- **Pagination on the installation list.** `GET /api/github/installations`
  returns all of them. An organization with enough installations for that
  to matter does not exist yet.
- **Re-requesting permissions.** If the App needs new scopes, the user
  goes through GitHub's UI.
