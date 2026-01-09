# Phase 1: Project Setup - Context

**Gathered:** 2026-01-08
**Status:** Ready for planning

<vision>
## How This Should Work

A polyglot monorepo with Docker-first development. Three main components living together:
- **Go backend**: Handles API and ingestion orchestration (performance-critical paths)
- **Python workers**: ML/RAG heavy lifting (embeddings, parsing, analysis) - leveraging Python's ecosystem
- **Frontend**: Modern React or Vue application

Everything runs in containers from day one for consistency across development and deployment. The project structure should make it obvious where each component lives and how they fit together.

Communication patterns between Go and Python will be determined during planning/research (could be message queues for async work, HTTP for sync queries, or hybrid).

</vision>

<essential>
## What Must Be Nailed

All three foundational pieces are equally critical:

- **Clean project structure that scales** - Folder layout, module organization, and architecture done right from day one
- **Dev environment setup** - Docker compose, local dev workflow that makes it easy to run and iterate on all services
- **Tooling and DX** - Linting, formatting, testing frameworks for Go, Python, and frontend - good developer experience from the start

This is the foundation everything else builds on. Get this right now, avoid refactoring later.

</essential>

<boundaries>
## What's Out of Scope

This is purely foundational setup - no features, no production, no polish:

- **No actual features** - No auth, no database connections, no business logic. Just project scaffolding.
- **No production concerns** - No CI/CD pipelines, no deployment configs, no production infrastructure yet
- **No frontend polish** - Basic React/Vue app setup is fine. Design systems and styling come later.

Phase 1 = skeleton that works. Features come in subsequent phases.

</boundaries>

<specifics>
## Specific Ideas

- **Monorepo structure** - Keep Go backend, Python workers, and frontend in one repository for easier development and coordination
- **Docker-first development** - Everything runs in containers from day one. Docker Compose for local dev environment.
- Communication architecture between Go and Python to be determined during planning (message queue vs HTTP vs hybrid)

</specifics>

<notes>
## Additional Context

The polyglot approach is intentional:
- Go for performance where it matters (API, orchestration)
- Python where the ecosystem excels (ML, embeddings, RAG tooling)
- Modern JS framework for UI

This is a SaaS product, so the foundation needs to support multi-service architecture from the start.

</notes>

---

*Phase: 01-project-setup*
*Context gathered: 2026-01-08*
