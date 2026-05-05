# Backend Service (Go)

API server and orchestration layer for the Smart Documentation Platform.

## Purpose

- HTTP API endpoints
- Request routing and validation
- Service orchestration
- Business logic coordination

## Tech Stack

- Go 1.21+
- Standard library HTTP server
- (Additional dependencies TBD)

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

# Run tests
make test
```

## Tooling

- **gofmt**: Code formatting
- **golangci-lint**: Comprehensive linting (gofmt, govet, staticcheck, gosimple, etc.)
- **Go built-in testing**: Test framework

## Status

Initial setup - implementation in progress.
