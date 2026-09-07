"""Pydantic models for RAG API request/response validation."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Request model for semantic search endpoint.

    `organization_id` is the tenant the query runs under. Retrieval,
    metadata enrichment, and any DB access it drives are all scoped by
    it via workers.db.require_tenant. Callers (the Go backend today)
    MUST set it — omitting it fails Pydantic validation, which is the
    intended defense.
    """

    query: str = Field(..., min_length=1, max_length=1000)
    organization_id: UUID
    repository_id: UUID
    top_k: int = Field(default=10, ge=1, le=50)


class ChunkResult(BaseModel):
    """Single chunk result with metadata and scores."""

    chunk_id: str
    content: str
    content_preview: str
    file_path: str
    start_line: int
    end_line: int
    breadcrumb: Optional[str] = None
    chunk_type: Optional[str] = None
    score: float
    rrf_score: Optional[float] = None
    boost_multiplier: Optional[float] = None


class SearchResponse(BaseModel):
    """Response model for search endpoint."""

    results: List[ChunkResult]
    query_id: Optional[str] = None
    total_results: int
    metadata: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    """Request model for chat/answer generation endpoint.

    `organization_id` is the tenant the query runs under; see
    SearchRequest for rationale.
    """

    query: str = Field(..., min_length=1, max_length=2000)
    organization_id: UUID
    repository_id: UUID
    top_k: int = Field(default=5, ge=1, le=20)


class SourceInfo(BaseModel):
    """Source citation information."""

    number: int
    file_path: str
    start_line: int
    end_line: int
    breadcrumb: Optional[str] = None
    chunk_type: Optional[str] = None


class ChatResponse(BaseModel):
    """Response model for chat endpoint."""

    answer: str
    sources: List[SourceInfo]
    query_id: Optional[str] = None
    cost: float
    tokens_in: int
    tokens_out: int
    cache_hit: bool
    model: Optional[str] = None
    chunks_retrieved: Optional[int] = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str
