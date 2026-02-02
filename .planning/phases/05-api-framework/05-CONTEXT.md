# Phase 5: API Framework - Context

**Gathered:** 2026-02-01
**Status:** Ready for planning

<vision>
## How This Should Work

A Go REST API using Chi router that extends the existing backend. The API provides both the middleware foundation (auth, logging, error handling, validation) and the search/chat endpoints that Phase 13 needs.

For user-facing search/chat requests, Go calls the Python RAG pipeline and streams LLM responses back to the frontend via Server-Sent Events (SSE). Users see the answer being generated in real-time rather than waiting for the full response.

Background jobs (ingestion, embedding) go through a message queue for async processing. The API provides status/health endpoints to check job progress.

</vision>

<essential>
## What Must Be Nailed

- **Solid middleware foundation** — Auth, logging, error handling, validation that all endpoints use
- **Search/chat endpoints** — The /search and /chat endpoints that Phase 13 needs to call
- **Streaming responses** — SSE streaming for LLM answers so users see results in real-time

All three are equally important for a good developer experience.

</essential>

<boundaries>
## What's Out of Scope

- GraphQL — REST only for now, GraphQL can come later if needed
- Rate limiting — Basic API works first, rate limiting in production hardening phase
- Admin management endpoints — Focus on user-facing functionality

</boundaries>

<specifics>
## Specific Ideas

**Router:**
- Chi router — lightweight, idiomatic Go, good middleware support

**Go ↔ Python Integration:**
- HTTP/REST for real-time search/chat (Go calls Python FastAPI service)
- Message queue for async jobs (ingestion, embedding)
- SSE streaming for LLM responses back to frontend

**Endpoints needed for Phase 13:**
- POST /api/search — Hybrid search (vector + FTS), returns ranked results
- POST /api/chat — Takes query + optional context, streams LLM response
- GET /api/health — Service health check

</specifics>

<notes>
## Additional Context

The Python RAG pipeline (Phases 11-12) is complete:
- QueryEngine for hybrid search
- AnswerGenerator for LLM responses with citations
- SemanticCache for cost reduction

This API layer wraps that pipeline and exposes it to the frontend.

Existing Go backend has:
- /health endpoint
- /webhooks/supabase for auth
- CORS middleware
- PostgreSQL connection

Need to add Chi router and proper API structure.

</notes>

---

*Phase: 05-api-framework*
*Context gathered: 2026-02-01*
