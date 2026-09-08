# Backend Service (Go)

API server and orchestration layer for the Smart Documentation Platform.

## Documentation

The canonical specs live in [`../../docs/`](../../docs/). Read these
before adding an endpoint:

- [**Tenant isolation**](../../docs/isolation.md) — the three walls every
  endpoint inherits, and the CI gate that fails a PR adding a mutation
  endpoint without an isolation test. Not optional reading.
- [**Auth & multi-org frontend contract**](../../docs/auth-frontend-contract.md)
  — how a client authenticates, reads its active organization, and
  switches between organizations.
- [**Local development**](../../docs/local-development.md) — the two
  separate Postgres instances, migrations, and the gotchas that cost the
  most time.

## Purpose

- HTTP API endpoints
- Request routing and validation
- Service orchestration
- Business logic coordination

## Tech Stack

- Go 1.25
- chi router, pgx/v5 + pgxpool, golang-migrate
- Supabase for authentication (JWT verified against JWKS)
- testcontainers-go for integration and isolation tests

## Development

### Prerequisites

Install golangci-lint:
```bash
go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
```

### Running the Server

```bash
# Build
go build

# Run
go run main.go
```

### Code Quality

```bash
# Format code
make fmt

# Run linter
make lint

# Run tests. -p 1 serializes packages that share the testcontainers
# Postgres. Needs `docker compose up -d postgres redis` for the older
# pkg/auth helpers — see docs/local-development.md.
go test -p 1 ./... -count=1
```

`.github/workflows/backend-ci.yml` runs exactly that, plus
`go build ./...` and `go vet ./...`, on every PR — so a green run locally
means a green run in CI.

## Tooling

- **gofmt**: Code formatting
- **golangci-lint**: Comprehensive linting (gofmt, govet, staticcheck, gosimple, etc.)
- **Go built-in testing**: Test framework

## Status

Active. Auth, tenant isolation, and the search/chat proxy endpoints are
implemented; repository integration and ingestion land in Phase 20+.
