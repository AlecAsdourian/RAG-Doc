"""Pydantic models for RAG API request/response validation."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Request model for semantic search endpoint."""

    query: str = Field(..., min_length=1, max_length=1000)
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
    """Request model for chat/answer generation endpoint."""

    query: str = Field(..., min_length=1, max_length=2000)
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
