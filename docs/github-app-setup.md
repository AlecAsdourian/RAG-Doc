# GitHub App Setup

**This is a manual step. It cannot be scripted** — GitHub requires a
human to create an App through their UI, and the private key is shown
exactly once.

Follow this once per environment (development now; production later, as
a separate App). Budget about 20 minutes.

Phase 20 is planned around registering this **first**, so the contracts
can be verified against the live API before code is written around them.

---

## 1. Create the App

Go to **Settings → Developer settings → GitHub Apps → New GitHub App**.

You can create it under your personal account for development. Production
should live under an organization so it survives any one person leaving.

### Identity

| Field | Value |
|---|---|
| **GitHub App name** | `RAG-Doc (dev)` — must be globally unique across GitHub, so add a suffix if taken |
| **Homepage URL** | `http://localhost:5173` for dev; anything resolvable is fine |
| **Description** | Optional. Shown to anyone installing it. |

### Callback and webhook URLs

The backend runs on `localhost:8080`, which GitHub cannot reach. For
development you need a public tunnel:

```bash
# In its own terminal, left running
ngrok http 8080
# Note the https forwarding URL, e.g. https://a1b2c3d4.ngrok-free.app
```

| Field | Value |
|---|---|
| **Callback URL** | leave **blank** |
| **Request user authorization (OAuth) during installation** | ☐ **unchecked** |
| **Setup URL** (under "Post installation") | `https://<your-ngrok>.ngrok-free.app/api/github/callback` |
| **Redirect on update** | ☑ **checked** |
| **Webhook → Active** | ☑ **checked** |
| **Webhook URL** | `https://<your-ngrok>.ngrok-free.app/webhooks/github` |
| **Webhook secret** | Generate a random string and keep it — see step 3 |

> **Setup URL, not Callback URL** — an earlier draft of this runbook said
> the opposite, and it was wrong.
>
> The two fields do different jobs. **Callback URL** is where GitHub sends
> a user after an *OAuth authorization*, and it only fires if "Request
> user authorization" is checked. **Setup URL** is where GitHub sends a
> user after they *install the App*, with `installation_id`,
> `setup_action`, and the `state` we put on the install link.
>
> Installation is what this flow needs. Users already authenticate through
> Supabase, so adding an OAuth dance would hand us an authorization code
> we have no use for, and a second identity for the same person.
>
> That `state` passes through to the Setup URL is what makes the flow
> safe — it is how the callback knows which organization started the
> install. **20-02 finding E verifies it empirically**; if it turns out
> GitHub does not pass `state` through, 20-04's design needs revisiting
> before it is built, which is exactly why verification comes first.

> Generate the secret with:
> ```bash
> openssl rand -hex 32
> ```

**The ngrok URL changes every time you restart ngrok** on the free tier.
When it does, update both URLs in the App settings. This is the single
most annoying part of the loop; a paid ngrok subdomain or a `cloudflared`
named tunnel makes it stable if it becomes a nuisance.

### User authorization — REQUIRED, and it is a security control

On the App's settings page, under **Identifying and authorizing users**:

- ✅ **Request user authorization (OAuth) during installation**

Then, further down the same page, generate a **client secret** and note
the **Client ID**. Both go in `.env` (step 3).

**Why this is not optional.** GitHub's app-level endpoint
`GET /app/installations/{id}` authenticates as the *App*, so it succeeds
for every installation of this App and says nothing about who is asking.
With this box unchecked there is no `code` on the setup redirect, and the
callback has no way to tell the account's owner from anybody else.

That gap is exploitable, and it was found in review of 20-04 before it
shipped: installing from GitHub's own "Install App" button sends no
`state`, so we refuse it and the installation sits live but unlinked —
and any authenticated user could then claim it by naming its numeric id,
which is visible to the victim at
`github.com/settings/installations/<id>` and in webhook payloads. The
attacker's workspace would own the link, and could ingest the victim's
private source through it.

With the box checked, GitHub sends a `code` alongside `installation_id`.
The backend exchanges it for a user-to-server token and requires the
installation to appear in that user's own `GET /user/installations` before
writing anything. **The callback fails closed**: without
`GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET` it refuses to link
at all, and `GET /api/github/install` returns 503 rather than sending
someone to GitHub for an installation it could not finish.

The cost is one extra GitHub screen during install ("authorize this app").

### Permissions

Under **Repository permissions**, set only these. Everything else stays
**No access** — an App that asks for more than it uses is a harder sell
to whoever approves the installation, and a bigger problem if our
credentials leak.

| Permission | Access | Why |
|---|---|---|
| **Contents** | Read-only | Clone the code. This is the core one. |
| **Metadata** | Read-only | Mandatory; GitHub selects it automatically. |

Under **Subscribe to events**, check:

- ☑ **Push** — a branch moved, so re-ingestion may be needed
- ☑ **Repository** — renamed, deleted, or visibility changed

(Installation and installation_repositories events are delivered
automatically; they are not in the subscribe list.)

### Installation scope

**Where can this GitHub App be installed?** — "Any account" is fine for
development. Production depends on whether you want public sign-ups
installing it.

Click **Create GitHub App**.

---

## 2. Generate a private key

On the App's settings page, scroll to **Private keys** → **Generate a
private key**. A `.pem` file downloads immediately.

**This is the only time you get it.** GitHub stores only the fingerprint.
If it is lost, generate a new one and delete the old.

Keep it out of the repository. `.pem` is not currently in `.gitignore` —
20-02 adds it, but until then, do not put this file in the project
directory.

---

## 3. Record the values

You need six things. Put them in `services/backend/.env`:

```bash
# From the App settings page, "About" section
GITHUB_APP_ID=123456

# From the App settings page, right-hand side, e.g. "rag-doc-dev"
GITHUB_APP_SLUG=rag-doc-dev

# The webhook secret you generated in step 1
GITHUB_WEBHOOK_SECRET=<the openssl rand output>

# Absolute path to the .pem from step 2 — NOT its contents, and NOT a
# path inside the repository.
#
# QUOTE IT if it contains backslashes. The run command in
# local-development.md sources this file as bash, so an unquoted
# C:\Users\... arrives as C:Users... and the backend panics naming a
# path that is not the one written here.
GITHUB_APP_PRIVATE_KEY_PATH="C:\Users\you\rag-doc-dev.private-key.pem"

# From the App settings page, "Client ID", and a client secret you
# generate there. REQUIRED — see step 1b.
GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxxxxxx
GITHUB_APP_CLIENT_SECRET=<generated client secret>
```

`.env.example` documents these as of **20-04**. This paragraph claimed
20-02 before that, and it was wrong — 20-02 added the code that reads
them but never added them to the template, so anyone following this
runbook found no matching entries there.

**`GITHUB_APP_SLUG` is required whenever `GITHUB_APP_ID` is set**, and the
router refuses to start without it. It appears in exactly one place — the
`github.com/apps/<slug>/installations/new` redirect — so a missing or
wrong value produces a 302 to a GitHub 404, which surfaces days later as
"the install button is broken" with nothing in our logs.

Note the backend does **not** read `.env` itself — only docker-compose
does. See [`local-development.md`](local-development.md).

---

## 4. Install it on a test repository

From the App settings page → **Install App** → pick your account →
choose **Only select repositories** and pick one small test repo.

Do not install it on everything. A large installation makes the
verification step in 20-02 slow and noisy.

After installing, the URL contains the installation id:

```
https://github.com/settings/installations/12345678
                                          ^^^^^^^^
```

Record that number — 20-02's verification uses it.

---

## 5. Confirm webhooks are arriving

With ngrok and the backend running, the App's **Advanced** tab shows
**Recent Deliveries**. Installing the App should have produced an
`installation` delivery.

At this stage the backend has no `/webhooks/github` route yet, so the
delivery will show a **404** — that is expected and still proves the
tunnel and URL are correct. The **Redeliver** button on that tab replays
any past delivery, which is what makes webhook development bearable: you
do not have to keep installing and uninstalling to get a payload.

---

## What to hand back

Once done, confirm:

1. The App exists and its name.
2. The four env values are in `services/backend/.env`.
3. The installation id from step 4.
4. That a delivery appears under Recent Deliveries, even as a 404.

That unblocks 20-02, which verifies what GitHub actually sends before any
schema is designed around it.

---

## Notes for production

Not needed now; recorded so the decision is not rediscovered later.

- **A separate App per environment.** One App cannot have two webhook
  URLs, and pointing production at a developer's tunnel is a bad day.
- **The private key is a credential with the App's full authority.** It
  belongs in a secret manager, not an environment file, once there is
  somewhere to put it (Phase 24).
- **Installation tokens expire after 1 hour** and are minted on demand
  from the App JWT. Nothing should ever persist one.
- **Rate limits are per installation**, not per App, which is the main
  reason a GitHub App was chosen over an OAuth App.
