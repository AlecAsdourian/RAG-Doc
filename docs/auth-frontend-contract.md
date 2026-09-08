# Auth & Multi-Org: Frontend Contract

The wire contract Phase 23 implements against. Written so nobody has to
reverse-engineer the backend to build the org switcher.

Related: [`isolation.md`](isolation.md) (why tenant scope is enforced the
way it is), [`local-development.md`](local-development.md) (running the
backend).

## The one thing to understand first

**The active organization lives in the JWT, not on the server.** There is
no session table, no cookie, no server-side "current org". The access
token carries `app_metadata.organization_id`, and every tenant-scoped
request is answered against whatever that claim says.

Two consequences that shape the whole UI:

- Switching organizations **requires a new token**. The switch endpoint
  updates the user's stored metadata; the token in the browser is
  unchanged until you refresh it. Skip the refresh and the app keeps
  operating in the old organization while the UI shows the new one.
- The backend **never issues tokens**. Only Supabase does. No endpoint
  here returns a JWT or a session, and none ever will.

## 1. Sign-in

```ts
await supabase.auth.signInWithOAuth({ provider: 'github' })
```

Standard Supabase. On return, the session's access token carries:

| Claim | Meaning |
|---|---|
| `sub` | Supabase user id. Also what our `users.supabase_user_id` stores. |
| `app_metadata.organization_id` | Active organization (UUID). |
| `app_metadata.organization_role` | Caller's role in **that** organization: `owner`, `admin`, or `member`. |

The organization claims are **nested under `app_metadata`**. There is no
top-level `organization_id` — don't look for one.

### A new user may briefly have no organization claim

Signup provisions the user and their starter organization asynchronously,
via a webhook. Until that completes, the token has `app_metadata` with
Supabase's own keys (`provider`, `providers`) and none of ours.

Handle it: if `organization_id` is absent, the user is not broken. Call
`refreshSession()` once, and if it is still absent, show the org picker
(§3) rather than an error — they may have memberships even with no active
org. Tenant-scoped endpoints will return **403** in this state, and that
403 means "no active organization", not "access denied".

## 2. Reading the active organization

Decode the access token client-side. **No API call.** The claim is
authoritative and already in hand.

```ts
const { data: { session } } = await supabase.auth.getSession()
const claims = jwtDecode(session.access_token)
const activeOrgId = claims.app_metadata?.organization_id ?? null
const activeRole  = claims.app_metadata?.organization_role ?? null
```

Use this for the header display and for hiding admin-only affordances.

> Hiding UI by role is a convenience, not a control. Authorization is
> enforced server-side. Never treat a decoded claim as permission to skip
> a request you'd otherwise make.

## 3. Listing organizations

```http
GET /api/user/organizations
Authorization: Bearer <access_token>
```

```jsonc
{
  "organizations": [
    { "id": "…", "name": "Acme", "slug": "acme", "role": "owner",  "is_active": true  },
    { "id": "…", "name": "Beta", "slug": "beta", "role": "member", "is_active": false }
  ],
  "active_organization_id": "…"   // null if the token carries no claim
}
```

- Always scoped to the caller. There is no parameter naming a user, so
  there is nothing to tamper with.
- `organizations` is always an array — `[]`, never `null`.
- `active_organization_id` is `null` for a user with no claim. That is
  distinct from `organizations: []` (belongs to nothing at all); the two
  want different UI.
- Works **without** an organization claim. This endpoint is how a
  claim-less user gets back to a valid state, so it is deliberately not
  gated on having one.

## 4. Switching organizations

```http
POST /api/user/select-organization
Authorization: Bearer <access_token>
Content-Type: application/json

{ "organization_id": "<uuid>" }
```

**202 Accepted:**

```jsonc
{
  "status": "org_updated",
  "organization_id": "…",
  "organization_role": "member",
  "next_action": "call supabase.auth.refreshSession() …"
}
```

**403 Forbidden** — the caller does not belong to that organization. This
is also the response for an organization that does not exist; the two are
deliberately indistinguishable so the endpoint can't be used to enumerate
other tenants.

**503** — the backend has no Supabase admin credentials configured. The
switch did not happen. Do not refresh; surface a real error.

### The full sequence

```ts
const res = await fetch('/api/user/select-organization', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ organization_id: targetId }),
})

if (res.status === 403) return showError("You don't have access to that organization.")
if (!res.ok)            return showError('Could not switch organizations. Try again.')

// REQUIRED. Until this resolves, the app is still operating as the old org.
await supabase.auth.refreshSession()

// Re-run every query. Cached data belongs to the previous tenant.
window.location.reload()
```

`refreshSession()` genuinely picks up the new claim — verified against the
live project on 2026-09-08 by writing metadata, refreshing, and decoding
the resulting token. A full sign-out/sign-in is **not** required.

**Do not skip the reload** (or an equivalent full cache invalidation).
Every cached response in the app was fetched under the old organization.
Leaving them on screen next to a switched org indicator is the most likely
way this feature ships looking like a data leak, even though the server
never leaked anything.

`organization_role` in the response is the caller's role **in the target
organization**, which may differ from the role they held before. Render
from the response (or the refreshed token), never from remembered state.

## 5. Signing out

```ts
await supabase.auth.signOut()
```

Nothing custom. No server call needed — there is no server-side session to
tear down.

## 6. UX conventions for Phase 23

- Active organization name in the header, always visible. A user who
  can't tell which tenant they're in will eventually act on the wrong one.
- Org switcher renders `GET /api/user/organizations`; check the call's
  result rather than assuming a single-org user has nothing to switch to.
- Hide the switcher entirely when the user belongs to exactly one
  organization.
- After switching, hard-reload.
- Treat 403 on a tenant-scoped endpoint as "pick an organization", not as
  a permission error — see §1.

## Not covered here

Ships in Phase 20+: org invitations, member removal, role changes, org
deletion, and org creation beyond the automatic starter org.

**One caveat those phases inherit.** The organization claim is written
when a user is provisioned and when they switch — nowhere else. It is not
recomputed when a token refreshes. So removing someone from an
organization does **not** revoke their access to it: their claim still
names it, and refreshing preserves it. Whatever ships membership removal
must also rewrite the affected user's claim. Short token lifetimes do not
help here.
