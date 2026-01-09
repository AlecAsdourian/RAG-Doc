# Phase 2: Database Setup - Context

**Gathered:** 2026-01-08
**Status:** Ready for planning

<vision>
## How This Should Work

Rich, accuracy-first schema that supports the platform's core mission: delivering correct, traceable answers. The database implements a layered approach - comprehensive enough to support correctness audits, but lean enough for v1 to ship.

Multi-tenant organization and project tables form the foundation. Ingestion-run lineage tracking captures commit SHAs so we can always trace back to exact source code versions. Chunk and embedding tables include file/line citations - forensic-quality references that let us audit where every answer came from.

Query, retrieval, and feedback logging built in from day one. Every search, every result, every thumbs-up/down gets recorded. This isn't just operational data - it's the accuracy measurement system.

</vision>

<essential>
## What Must Be Nailed

**Audit trail and lineage tracking from day one** - This is non-negotiable.

- Ingestion runs must capture commit SHAs
- Chunks must reference exact file paths and line numbers
- Query logs must capture what was asked, what was retrieved, what was shown
- Feedback must be traceable back to specific retrievals and source chunks

The entire system is built on the premise of accurate answers. Without complete lineage, we can't debug incorrect answers, can't improve retrieval, can't prove accuracy claims. This is the forensics layer that makes everything else possible.

</essential>

<boundaries>
## What's Out of Scope

- **No auth/session tables yet** - Authentication and session management comes in Phase 4 with the auth system implementation
- **No vector storage in Postgres** - Vector embeddings belong in the dedicated vector database (Phase 3). Keep concerns separated.
- **No complex analytics aggregations** - Usage analytics, reporting tables, dashboards come later (Phase 15). Just operational data and raw logs now.
- **No premature optimization** - Focus on correctness and completeness of the schema. Performance tuning comes when we have real workloads.

Phase 2 is purely: operational data model, audit trail infrastructure, multi-tenant foundation.

</boundaries>

<specifics>
## Specific Ideas

**Migrations from day one:**
- Use proper migration tooling (Alembic for Python, golang-migrate for Go)
- Schema changes always versioned, never manual SQL
- Migrations tracked in version control alongside code

**Database access patterns:**
- Keep v1 DB access mostly raw SQL (or tools like sqlc/jOOQ for type safety)
- Stay explicit for ingestion and retrieval workloads - these are critical paths
- ORMs add abstraction overhead where we need clarity about what queries run
- Type-safe query builders acceptable, but avoid heavy ORMs that hide the SQL

**Layered implementation:**
- Core tables first (orgs, projects, chunks)
- Lineage/audit next (ingestion_runs, citations)
- Query/feedback logging (queries, retrievals, feedback)
- Each layer gets us closer to production without creating unnecessary complexity

</specifics>

<notes>
## Additional Context

The database design philosophy is accuracy-first. Every design decision should ask: "Can we audit this? Can we trace answers back to source? Can we measure if we got it right?"

This isn't premature complexity - it's the foundation that makes the core value proposition (accurate answers) possible to deliver and prove.

</notes>

---

*Phase: 02-database-setup*
*Context gathered: 2026-01-08*
