---
phase: 01-project-setup
plan: 03
type: execute
---

<objective>
Create Docker development environment for all three services.

Purpose: Docker-first development from day one - consistency across development and deployment. Everything runs in containers locally.
Output: Dockerfiles for each service, docker-compose.yml orchestrating all services, working local dev environment.
</objective>

<execution_context>
@./.claude/get-shit-done/workflows/execute-phase.md
@./.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/phases/01-project-setup/1-CONTEXT.md
@.planning/phases/01-project-setup/1-01-SUMMARY.md
@.planning/phases/01-project-setup/1-02-SUMMARY.md

**Vision:** Docker-first development. Everything runs in containers from day one. Docker Compose for local dev environment.

**Essential:** Dev environment setup - Docker compose, local dev workflow that makes it easy to run and iterate on all services.
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create Dockerfiles for each service</name>
  <files>services/backend/Dockerfile, services/workers/Dockerfile, services/frontend/Dockerfile</files>
  <action>Create optimized Dockerfiles for local development:

**Backend Dockerfile (Go):**
- FROM golang:1.21-alpine
- WORKDIR /app
- Copy go.mod, go.sum (when it exists)
- RUN go mod download
- Copy source code
- No build step yet - use `go run` for dev (hot reload later)
- EXPOSE 8080
- CMD ["go", "run", "main.go"]

**Workers Dockerfile (Python):**
- FROM python:3.11-slim
- WORKDIR /app
- Copy requirements.txt
- RUN pip install --no-cache-dir -r requirements.txt (empty for now)
- Copy source code
- CMD ["python", "-m", "workers"] (placeholder - adjust when workers have entry point)

**Frontend Dockerfile (Node):**
- FROM node:20-alpine
- WORKDIR /app
- Copy package.json, package-lock.json
- RUN npm ci
- Copy source code
- EXPOSE 5173
- CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

Use multi-stage builds later for production. These are dev-optimized.</action>
  <verify>Each Dockerfile builds successfully: `docker build services/backend`, `docker build services/workers`, `docker build services/frontend`</verify>
  <done>All three Dockerfiles build without errors, follow best practices for layer caching</done>
</task>

<task type="auto">
  <name>Task 2: Create docker-compose.yml for local development</name>
  <files>docker-compose.yml, .env.example</files>
  <action>Create docker-compose.yml in repository root:

```yaml
version: '3.8'

services:
  backend:
    build: ./services/backend
    ports:
      - "8080:8080"
    volumes:
      - ./services/backend:/app
    environment:
      - ENV=development

  workers:
    build: ./services/workers
    volumes:
      - ./services/workers:/app
    environment:
      - ENV=development

  frontend:
    build: ./services/frontend
    ports:
      - "5173:5173"
    volumes:
      - ./services/frontend:/app
      - /app/node_modules  # Don't mount node_modules
    environment:
      - ENV=development
```

Volume mounts enable live code reload. Port mappings for backend (8080) and frontend (5173).

Create .env.example (empty for now - populated in later phases).

No databases yet - those come in Phase 2 and 3.</action>
  <verify>docker-compose config validates successfully, docker-compose up builds all services without errors</verify>
  <done>docker-compose.yml orchestrates all three services, services can be started together, volume mounts configured for live reload</done>
</task>

<task type="auto">
  <name>Task 3: Add development scripts and update documentation</name>
  <files>scripts/dev.sh, scripts/stop.sh, README.md</files>
  <action>Create convenience scripts in scripts/ directory:

**scripts/dev.sh:**
```bash
#!/bin/bash
docker-compose up --build
```

**scripts/stop.sh:**
```bash
#!/bin/bash
docker-compose down
```

Make scripts executable: `chmod +x scripts/*.sh`

Update root README.md with:
- Prerequisites (Docker, Docker Compose)
- Quick start: `./scripts/dev.sh`
- Access points: Frontend (http://localhost:5173), Backend (http://localhost:8080)
- Stopping: `./scripts/stop.sh`
- Individual service commands if needed

Keep it practical - developer should be able to clone repo and run `./scripts/dev.sh` immediately.</action>
  <verify>./scripts/dev.sh runs successfully, all three services start, README documents complete dev workflow</verify>
  <done>Scripts work, documentation complete, new developer can start working in under 2 minutes</done>
</task>

</tasks>

<verification>
Before declaring plan complete:
- [ ] All three Dockerfiles build: `docker-compose build`
- [ ] Services start together: `docker-compose up` (Ctrl+C to stop)
- [ ] Frontend accessible at http://localhost:5173
- [ ] Backend container runs (even if it just exits - no server yet)
- [ ] README documents Docker-based workflow
- [ ] Scripts are executable and work
</verification>

<success_criteria>
- All tasks completed
- All verification checks pass
- Docker Compose successfully orchestrates all services
- Volume mounts enable live reload during development
- Documentation enables quick onboarding
- Developer experience is smooth
</success_criteria>

<output>
After completion, create `.planning/phases/01-project-setup/1-03-SUMMARY.md`:

# Phase 1 Plan 3: Docker Development Environment Summary

**Docker-first development environment with all services containerized.**

## Accomplishments

- Dockerfiles for Go backend, Python workers, and React frontend
- docker-compose.yml orchestrating all three services
- Volume mounts for live code reload
- Development scripts for easy startup/shutdown
- Complete documentation of Docker workflow

## Files Created/Modified

- `services/backend/Dockerfile` - Go service container
- `services/workers/Dockerfile` - Python service container
- `services/frontend/Dockerfile` - Frontend container
- `docker-compose.yml` - Service orchestration
- `scripts/dev.sh` - Start development environment
- `scripts/stop.sh` - Stop all services
- `README.md` - Updated with Docker workflow

## Decisions Made

- Docker Compose for local development (not Kubernetes - too heavy for local)
- Volume mounts for live reload (developer experience over production parity)
- Separate Dockerfiles per service (optimized per language)

## Issues Encountered

None expected - standard Docker setup.

## Next Step

Ready for 1-04-PLAN.md (Tooling & DX)
</output>
