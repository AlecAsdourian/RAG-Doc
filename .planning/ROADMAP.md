# Roadmap: Smart Documentation Platform

## Overview

Build a SaaS platform that transforms codebases into queryable knowledge bases. Journey starts with foundational infrastructure (project, databases, auth, API), progresses through repository integration and intelligent code processing (parsing, chunking, embedding), implements dual documentation approaches (AI-generated and manual), delivers the core RAG system for accurate answers, provides interfaces for human and AI consumption, and finishes with analytics and production deployment.

## Domain Expertise

None

## Phases

- [x] **Phase 1: Project Setup** - Initialize project structure, tooling, dev environment
- [x] **Phase 2: Database Setup** - Primary database for metadata and application data
- [x] **Phase 3: Vector Database** - Vector store for embeddings and similarity search
- [ ] **Phase 4: Authentication System** - User auth and org-based access control
- [ ] **Phase 5: API Framework** - REST/GraphQL foundation with middleware
- [ ] **Phase 6: Repository Integration** - GitHub/GitLab connection and code syncing
- [ ] **Phase 7: Code Parsing & Chunking** - Language-agnostic code extraction and chunking
- [ ] **Phase 8: Embedding Pipeline** - Generate and store code embeddings
- [ ] **Phase 9: AI Documentation Agent** - Autonomous documentation generation
- [ ] **Phase 10: Manual Documentation System** - Developer-authored knowledge capture
- [ ] **Phase 11: RAG Query Engine** - Search, retrieval, and context assembly
- [ ] **Phase 12: LLM Answer Generation** - Generate accurate answers from context
- [ ] **Phase 13: Web UI - Search & Chat** - User interface for querying
- [ ] **Phase 14: AI Context Export** - Multiple export formats for AI agents
- [ ] **Phase 15: Feedback & Analytics** - Quality tracking and metrics
- [ ] **Phase 16: Multi-tenant & Deployment** - Organization isolation and production deploy

## Phase Details

### Phase 1: Project Setup
**Goal**: Initialize project with proper structure, tooling, development environment, and workflows
**Depends on**: Nothing (first phase)
**Research**: Unlikely (standard project initialization patterns)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 2: Database Setup
**Goal**: Set up primary database (Postgres) for users, organizations, repositories, and documentation metadata
**Depends on**: Phase 1
**Research**: Unlikely (Postgres setup is well-established)
**Plans**: 3

Plans:
- [x] 2-01: Database Infrastructure & Core Schema - Postgres setup, migrations, multi-tenant foundation
- [x] 2-02: Chunk Storage & Lineage Tracking - Ingestion runs and chunks with file/line citations
- [x] 2-03: Query & Feedback Logging - Query, retrieval, and feedback tracking

### Phase 3: Vector Database
**Goal**: Configure vector database for storing and querying embeddings
**Depends on**: Phase 1
**Research**: Likely (technology choice and integration)
**Research topics**: Compare Pinecone vs Weaviate vs Qdrant (cost, performance, features), API patterns, indexing strategies for code embeddings
**Plans**: 1

Plans:
- [x] 3-01: Vector Database Setup - Qdrant service, Go client, CRUD operations

### Phase 4: Authentication System
**Goal**: Implement user authentication, session management, and organization-based access control
**Depends on**: Phase 2
**Research**: Likely (architectural decisions)
**Research topics**: JWT vs session strategy, auth libraries for chosen stack, org-based multi-tenancy patterns, secure token handling
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 5: API Framework
**Goal**: Build REST/GraphQL API foundation with middleware, error handling, and validation
**Depends on**: Phase 2, Phase 4
**Research**: Unlikely (assuming established framework like Express/FastAPI)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 6: Repository Integration
**Goal**: Connect to GitHub/GitLab, clone repositories, sync code changes via webhooks
**Depends on**: Phase 2, Phase 4, Phase 5
**Research**: Likely (external API integration)
**Research topics**: GitHub/GitLab API documentation, webhook patterns, OAuth app setup, repo syncing strategies, handling large repositories
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 7: Code Parsing & Chunking
**Goal**: Build language-agnostic parser that extracts code structure and chunks intelligently
**Depends on**: Phase 6
**Research**: Likely (complex technical challenge)
**Research topics**: Language-agnostic AST parsers (tree-sitter), semantic chunking strategies for code, language detection, handling docstrings and comments
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 8: Embedding Pipeline
**Goal**: Generate embeddings for code chunks and store in vector database
**Depends on**: Phase 3, Phase 7
**Research**: Likely (technology choice and cost optimization)
**Research topics**: Embedding models (OpenAI vs open-source alternatives), batching strategies, cost optimization for embeddings at scale, embedding dimension tradeoffs
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 9: AI Documentation Agent
**Goal**: Create autonomous agent that traverses repositories and generates comprehensive documentation
**Depends on**: Phase 7, Phase 8
**Research**: Likely (LLM integration and agentic patterns)
**Research topics**: LLM API selection for agent tasks, prompting strategies for code documentation, cost-efficient agentic patterns, handling large codebases
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 10: Manual Documentation System
**Goal**: Build markdown editor and workflow for developers to capture knowledge manually
**Depends on**: Phase 2, Phase 5
**Research**: Unlikely (standard markdown editing and storage)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 11: RAG Query Engine
**Goal**: Implement search, retrieval, and context assembly system for answering queries
**Depends on**: Phase 3, Phase 8
**Research**: Likely (core RAG architecture)
**Research topics**: RAG architecture patterns, hybrid search (vector + keyword), reranking strategies, context assembly and window management, relevance scoring
**Plans**: 5

Plans:
- [x] 11-01: FTS Infrastructure & Retrieval - PostgreSQL full-text search with GIN indexes
- [ ] 11-02: Vector Retrieval & Query Parser - Qdrant semantic search and query parsing
- [ ] 11-03: Hybrid Ranking & Fusion - RRF fusion and metadata-based boosting
- [ ] 11-04: Context Assembly - Assemble ranked results into citeable context
- [ ] 11-05: Query API - REST endpoint orchestrating full RAG pipeline

### Phase 12: LLM Answer Generation
**Goal**: Integrate with LLM APIs to generate accurate answers from retrieved context
**Depends on**: Phase 11
**Research**: Likely (critical for cost constraint)
**Research topics**: LLM API comparison (cost/quality tradeoffs), prompt optimization for accuracy, caching strategies to reduce API calls, streaming responses, handling rate limits
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 13: Web UI - Search & Chat
**Goal**: Build web interface for developers to search, query, and browse documentation
**Depends on**: Phase 5, Phase 12
**Research**: Unlikely (standard frontend patterns)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 14: AI Context Export
**Goal**: Generate multiple export formats (markdown, JSON, YAML) for AI agent consumption
**Depends on**: Phase 7, Phase 9, Phase 10
**Research**: Unlikely (file generation is straightforward)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 15: Feedback & Analytics
**Goal**: Implement answer quality tracking, usage metrics, and system improvement mechanisms
**Depends on**: Phase 12, Phase 13
**Research**: Unlikely (standard metrics tracking and analytics)
**Plans**: TBD

Plans:
- TBD during phase planning

### Phase 16: Multi-tenant & Deployment
**Goal**: Implement organization isolation, billing infrastructure, and production deployment
**Depends on**: Phase 4, Phase 13
**Research**: Likely (SaaS architecture and infrastructure)
**Research topics**: Multi-tenancy isolation patterns, deployment options (Vercel/Railway/AWS), billing integration (Stripe), monitoring and observability
**Plans**: TBD

Plans:
- TBD during phase planning

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Project Setup | 4/4 | Complete | 2026-01-08 |
| 2. Database Setup | 3/3 | Complete | 2026-01-08 |
| 3. Vector Database | 1/1 | Complete | 2026-01-08 |
| 4. Authentication System | 0/TBD | Not started | - |
| 5. API Framework | 0/TBD | Not started | - |
| 6. Repository Integration | 0/TBD | Not started | - |
| 7. Code Parsing & Chunking | 0/TBD | Not started | - |
| 8. Embedding Pipeline | 0/TBD | Not started | - |
| 9. AI Documentation Agent | 0/TBD | Not started | - |
| 10. Manual Documentation System | 0/TBD | Not started | - |
| 11. RAG Query Engine | 1/5 | In progress | - |
| 12. LLM Answer Generation | 0/TBD | Not started | - |
| 13. Web UI - Search & Chat | 0/TBD | Not started | - |
| 14. AI Context Export | 0/TBD | Not started | - |
| 15. Feedback & Analytics | 0/TBD | Not started | - |
| 16. Multi-tenant & Deployment | 0/TBD | Not started | - |
