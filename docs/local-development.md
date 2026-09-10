# Local Development Setup

How to get the backend running against a local database, and the
non-obvious things that will bite you.

## The two data stores

This project uses **two separate stores**, and conflating them is the
most common way to break auth:

| | Holds | Where |
|---|---|---|
| **Application Postgres** | `users`, `organizations`, `organization_memberships`, `projects`, `repositories`, `chunks`, `ingestion_runs`, `queries`, `retrievals`, `feedback` — everything in `services/backend/migrations/` | docker-compose `postgres` service, host port **5434** |
| **Supabase** | `auth.users`, plus the signup bridge table `public.auth_user_events` | Hosted, `SUPABASE_URL` |

`public.auth_user_events` is the one application-shaped table that
legitimately lives in Supabase, and it is load-bearing — a trigger on
`auth.users` writes a row, and a Supabase Database Webhook on that insert
is what calls our backend. Supabase does not allow webhooks directly on
the protected `auth` schema, so the bridge table exists to give the
webhook something in `public` to fire on. It holds email,
`supabase_user_id`, and raw signup metadata, so it is real user data:
`scripts/supabase/001-remove-app-schema-from-supabase.sql` keeps it while
dropping every other duplicated table, and revokes anon/authenticated
access to it.

The two stores are bridged **one-directionally**: Supabase inserts into
`auth.users` → trigger writes `auth_user_events` → webhook POSTs
`/webhooks/supabase` → our backend provisions a mirror user and starter
organization in the application Postgres, then writes the organization
back onto the Supabase user as `raw_app_meta_data`.

**That webhook fires exactly once per user and never retries.** The
trigger is `AFTER INSERT ON auth.users`, so there is one delivery per
account for all time. If the org-context write back to Supabase fails,
that user has no `app_metadata.organization_id` and gets a 403 on every
tenant-scoped route, permanently — refreshing their token does not help.
The repair is `go run ./cmd/backfill-org-claims`; see that command's doc
comment.

**Do not point `DATABASE_URL` at Supabase.** Two reasons: it mixes our
schema into theirs, and Supabase's direct database host
(`db.<ref>.supabase.co`) is **IPv6-only** — on an IPv4-only network it
simply will not resolve to anything reachable. Their IPv4 path is the
connection pooler, a different hostname entirely.

A corollary that cost a plan rewrite in Phase 19-03: a **Supabase Auth
Hook cannot query our tables**, because it executes inside Supabase's
database. Org context reaches the JWT by writing `raw_app_meta_data` on
the Supabase user via the Admin API, which Supabase then surfaces as the
`app_metadata` claim.

## Start the database

```bash
docker compose up -d postgres
```

Host port **5434** → container 5432. Credentials `coderag:coderag`,
database `coderag`.

If the volume predates the current compose file, the role's password may
not match what compose declares (the `POSTGRES_PASSWORD` env only applies
at first initialization). Symptom: `docker exec` works but TCP
connections fail with `password authentication failed`. Fix:

```bash
docker exec testtgsd-postgres-1 psql -U coderag -d coderag \
  -c "ALTER USER coderag WITH PASSWORD 'coderag';"
```

## Apply migrations

Migrations are golang-migrate format in `services/backend/migrations/`.

```bash
cd services/backend
MSYS_NO_PATHCONV=1 docker run --rm \
  --network testtgsd_default \
  -v "$(pwd -W)/migrations:/migrations" \
  migrate/migrate \
  -path=/migrations \
  -database "postgres://coderag:coderag@postgres:5432/coderag?sslmode=disable" \
  up
```

Notes:
- `MSYS_NO_PATHCONV=1` is required on Git Bash for Windows, which
  otherwise rewrites `/migrations` into a Windows path.
- `--network testtgsd_default` reaches the container by service name.
  `host.docker.internal:5434` also works if the password matches.
- `pwd -W` yields the Windows-style path Docker needs for the mount.

**If the schema exists but `schema_migrations` does not** (tables were
created by hand before golang-migrate was adopted), bootstrap the
version before running `up`, or migrate will try to re-create existing
tables:

```bash
# Set the recorded version WITHOUT running any statements.
... migrate/migrate ... force <N>
# Then apply everything after N.
... migrate/migrate ... up
```

Determine `<N>` by inspecting what's actually present — e.g. RLS
policies mean 000008 landed; `SELECT count(*) FROM pg_trigger WHERE
tgname='trg_assert_tenant'` returning 6 means 000009 landed.

## Configure the backend

Copy `services/backend/.env.example` to `services/backend/.env` and fill
in the Supabase values. Two fields matter more than they look:

- **`DATABASE_URL`** — the application Postgres, not Supabase.
- **`SUPABASE_WEBHOOK_SECRET`** — must be **non-empty**. Since Phase
  19-01 the webhook handler has no signature-bypass fallback, and
  `NewRouterWithValidator` panics at construction if it's empty. This is
  deliberate: the previous behavior silently accepted unsigned webhook
  payloads whenever the variable was unset. Any non-empty value works
  locally.

## Run the backend

**The Go backend does not load `.env` itself** — `main.go` reads
`os.Getenv` directly. Only docker-compose consumes the file (via
`env_file`). Running the binary directly requires exporting it yourself:

```bash
cd services/backend
set -a && . ./.env && set +a
go run .
```

**Two secrets are required or the backend refuses to start**, both by
design (an unsigned webhook receiver is worse than none):

- `SUPABASE_WEBHOOK_SECRET` — since 19-01.
- `GITHUB_WEBHOOK_SECRET` — since 20-05. Note the confusing pair of lines
  you get without it: a WARN saying GitHub webhooks are *unavailable*,
  immediately followed by a panic *because* of them.

**Two traps in that one line, both hit for real on 2026-09-09.** `.` is
shell *sourcing*, so the file is interpreted as bash rather than parsed as
a `KEY=VALUE` list:

- **A UTF-8 BOM breaks it.** An editor that saves `.env` with a byte-order
  mark makes line 1 fail with `command not found`, and sourcing stops
  there — so *nothing* is exported, and the failure looks like missing
  configuration rather than a broken file. Check with
  `head -c 3 .env | od -An -tx1`; `ef bb bf` means strip it.

- **Unquoted Windows paths lose their backslashes.**
  `GITHUB_APP_PRIVATE_KEY_PATH=C:\Users\Alec\key.pem` sources as
  `C:UsersAleckey.pem`, because `\U` and `\A` are escapes. The backend
  then panics with a file-not-found naming a path that is *not* the one in
  the file, which is a genuinely confusing thing to debug.

  **Quote any value containing backslashes** — `KEY="C:\Users\..."` — or
  write the path with forward slashes, which Go accepts on Windows.

(The Python workers service *does* auto-load `.env` via `load_dotenv()`,
which has neither problem. The inconsistency is known.)

**docker-compose's `backend` service does not pass these through.** It
sets only `ENV`, `DATABASE_URL` and `RAG_SERVICE_URL` — no `SUPABASE_*`,
no `REDIS_URL`, no `GITHUB_APP_*` — and the router panics without
`SUPABASE_WEBHOOK_SECRET`, so `docker compose up backend` does not
currently start. Run the backend directly, as above; compose is for
Postgres, Redis and Qdrant.

## Verify

```bash
curl -s http://localhost:8080/health
# {"service":"backend-api","status":"ok"}

# Webhook rejects unsigned payloads.
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" -d '{}' \
  http://localhost:8080/webhooks/supabase   # expect 401

# Protected routes reject missing JWT.
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" -d '{}' \
  http://localhost:8080/api/search          # expect 401
```

If port 8080 is occupied by something else, set `PORT` to a free port.

**There are no `/auth/github/*` or `/auth/gitlab/*` routes.** Sign-in goes
through Supabase directly from the frontend
(`supabase.auth.signInWithOAuth`), which redirects to Supabase's hosted
endpoint and back to the app — the Go backend is never in that path. The
one thing the backend does at signup is receive Supabase's webhook. See
[`auth-frontend-contract.md`](auth-frontend-contract.md).

The Go handlers for direct OAuth still exist as Phase-4 reference code but
are deliberately not mounted (ISS-011): they returned 500 on every
completed callback, and repairing that alone would have produced users
with no organization claim.

## Redis

**Required in the request path as of 20-04.** The GitHub App install flow
stores its organization-bound state tokens there, so without Redis
`GET /api/github/install` returns 503 and the callback refuses to link —
deliberately, since a flow that cannot be completed should not be
started. (This paragraph previously said nothing in the request path used
Redis. That stopped being true when the install flow shipped.)

`pkg/auth`'s state-store tests need it too, so it is required to run the
full suite either way.

If Redis is down when the router is CONSTRUCTED, the install flow stays
disabled for the life of the process — there is no reconnect. Restart the
backend after bringing Redis back.

```bash
docker compose up -d redis
```

## Repairing missing organization claims

A user whose org-context push failed holds a token with no
`app_metadata.organization_id` and gets 403 on every tenant-scoped route.
Nothing repairs this automatically — see "The two data stores" above for
why. The fix:

```bash
# Audit first — no Supabase credentials needed, changes nothing.
DATABASE_URL="postgres://coderag:coderag@localhost:5434/coderag?sslmode=disable" \
  go run ./cmd/backfill-org-claims -dry-run

# Repair everyone.
DATABASE_URL=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  go run ./cmd/backfill-org-claims

# Or one user, by Supabase user id.
... go run ./cmd/backfill-org-claims -user 550e8400-e29b-41d4-a716-446655440000
```

The push is a merge, so this is idempotent and safe to run at any time,
including against a healthy system or while the webhook is live. Run it
after any incident touching Supabase or the webhook path — and consider
scheduling it, since a periodic run is what turns "one shot per user"
into something eventually consistent.

## Running tests

The isolation harness (`pkg/testing/isolation`) manages its own
throwaway Postgres via testcontainers and needs no configuration.

The `migrate` CLI is needed. Install it once:

```bash
go install -tags postgres github.com/golang-migrate/migrate/v4/cmd/migrate@v4.19.1
```

Then, **from the repository root**:

```bash
docker compose up -d postgres redis

# pkg/auth's helpers use this database directly and do NOT apply
# migrations themselves. On a fresh volume, skipping this makes every
# pkg/auth test fail on a missing table.
export DATABASE_TEST_URL="postgres://coderag:coderag@localhost:5434/coderag?sslmode=disable"
migrate -path services/backend/migrations -database "$DATABASE_TEST_URL" up

# -C because the Go module lives under services/backend; running this
# from the repo root without it fails with "directory prefix . does not
# contain main module".
go test -C services/backend ./... -count=1 -p 1 -timeout 15m
```

**This is most of what CI runs, not all of it.** CI additionally runs
`go build ./...`, `go vet ./...`, `go mod verify`, `go mod tidy -diff`, a
`-race` pass, and a parallel-harness regression guard. A green run here
is a good signal, not a guarantee — check the PR.

**Why `-p 1`.** It serializes packages, not tests within a package, which
keeps the run deterministic: one process, one container, nothing
cross-package to explain away.

Note the trade-off, because it is not obvious: **`-p 1` makes the run
unable to observe the ISS-010 race through package contention.** With one
process calling `setupContainer`, that window never opens. Measured on
the pre-fix code with a cold container, default parallelism failed 4 of 8
runs and `-p 1` failed 0 of 5.

What actually guards it is `TestEnsureAppRoleIsConcurrencySafe` in
`pkg/testing/isolation`: it releases 16 concurrent callers through a
barrier rather than waiting for the scheduler to produce contention, and
detected the missing advisory lock 8 times out of 8. That test runs under
`-p 1` like any other, so this command does catch a regression.

CI additionally runs the harness packages at default parallelism, but
that step is defense in depth — it detected the same breakage only about
one time in eight, so a green result there proves very little on its own.

**Why compose is needed at all.** The 17-01 harness
(`pkg/testing/isolation`) provisions its own throwaway Postgres and needs
nothing. The older `pkg/auth` helpers predate it: they connect to a fixed
DSN (`DATABASE_TEST_URL`, defaulting to the compose Postgres on 5434,
which must have migrations applied) and the OAuth state-store tests need
Redis. Migrating those helpers onto the testcontainers harness is a
tracked follow-up in `19-02-SUMMARY.md`; doing it would remove the
compose dependency and let CI drop its service containers.

To point `pkg/auth` at the harness's container instead of compose:

```bash
DATABASE_TEST_URL="postgres://isolation:isolation@localhost:<port>/isolation?sslmode=disable" \
  go test ./pkg/auth/...
```

Find `<port>` with `docker port rag-doc-isolation-tests 5432`.
