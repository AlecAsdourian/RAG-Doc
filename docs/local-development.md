# Local Development Setup

How to get the backend running against a local database, and the
non-obvious things that will bite you.

## The two data stores

This project uses **two separate stores**, and conflating them is the
most common way to break auth:

| | Holds | Where |
|---|---|---|
| **Application Postgres** | `users`, `organizations`, `organization_memberships`, `projects`, `repositories`, `chunks`, `ingestion_runs`, `queries`, `retrievals`, `feedback` — everything in `services/backend/migrations/` | docker-compose `postgres` service, host port **5434** |
| **Supabase** | `auth.users` only | Hosted, `SUPABASE_URL` |

They are bridged **one-directionally** by the webhook: Supabase fires
`user.created` → `POST /webhooks/supabase` → our backend provisions a
mirror user and starter organization in the application Postgres.

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

(The Python workers service *does* auto-load `.env` via `load_dotenv()`.
The inconsistency is known.)

## Verify

```bash
curl -s http://localhost:8080/health
# {"service":"backend-api","status":"ok"}

# OAuth wiring — 307 to GitHub with a state token (requires Redis).
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" \
  http://localhost:8080/auth/github/login

# Webhook rejects unsigned payloads.
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" -d '{}' \
  http://localhost:8080/webhooks/supabase   # expect 401

# Protected routes reject missing JWT.
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" -d '{}' \
  http://localhost:8080/api/search          # expect 401
```

If port 8080 is occupied by something else, set `PORT` to a free port —
note that `BASE_URL` (used to build the OAuth `redirect_uri`) is
independent and must match whatever GitHub has registered.

## Redis

Required for the OAuth state store (CSRF protection). If it's
unreachable, the OAuth routes are **skipped at router construction**
with a logged warning rather than failing startup — so a missing
`/auth/github/login` route means Redis, not routing.

```bash
docker compose up -d redis
```

## Running tests

The isolation harness (`pkg/testing/isolation`) manages its own
throwaway Postgres via testcontainers and needs no configuration.

The older `pkg/auth` helpers do not — they read `DATABASE_TEST_URL`
and fall back to a host that may not exist. To run those against the
isolation harness's container:

```bash
DATABASE_TEST_URL="postgres://isolation:isolation@localhost:<port>/isolation?sslmode=disable" \
  go test ./pkg/auth/...
```

Find `<port>` with `docker port rag-doc-isolation-tests 5432`.
Migrating these helpers onto the testcontainers harness is tracked as a
follow-up in `19-02-SUMMARY.md`.
