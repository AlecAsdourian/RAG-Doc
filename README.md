# Smart Documentation Platform

A polyglot monorepo for intelligent documentation processing, combining Go backend services, Python ML/RAG workers, and a modern frontend.

## Project Structure

```
smart-docs-platform/
├── services/
│   ├── backend/     # Go API server and orchestration
│   ├── workers/     # Python ML and RAG processing workers
│   └── frontend/    # React/Vue web interface
└── README.md        # This file
```

## Components

### Backend (Go)
API server and orchestration layer. Handles HTTP endpoints, request routing, and coordination between services.

**Location:** `services/backend/`

### Workers (Python)
Machine learning and RAG (Retrieval-Augmented Generation) processing workers. Handles document processing, embeddings, and intelligent search.

**Location:** `services/workers/`

### Frontend
Modern web interface for interacting with the documentation platform.

**Location:** `services/frontend/`

## Prerequisites

- Docker
- Docker Compose

## Getting Started

Start all services with one command:

```bash
./scripts/dev.sh
```

This will build and start all three services:
- **Frontend**: http://localhost:5173
- **Backend API**: http://localhost:8080
- **Workers**: Running in background

To stop all services:

```bash
./scripts/stop.sh
```

## Development

This is a Docker-first monorepo. All services run in containers for consistency across development and deployment.

### Running Services

**Start all services:**
```bash
docker-compose up --build
```

**Start specific service:**
```bash
docker-compose up backend
docker-compose up workers
docker-compose up frontend
```

**Stop all services:**
```bash
docker-compose down
```

### Live Reload

Volume mounts are configured for live code reload:
- Edit code in `services/*/` directories
- Changes are reflected immediately in running containers
- Frontend has HMR (Hot Module Replacement)

### Individual Service Development

See individual service READMEs for service-specific commands and details:
- `services/backend/README.md`
- `services/workers/README.md`
- `services/frontend/README.md`
