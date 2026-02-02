"""FastAPI route handlers for RAG API."""

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .models import (
    ChatRequest,
    ChatResponse,
    ChunkResult,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    SourceInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="ok", service="rag-api")


@router.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest, req: Request) -> SearchResponse:
    """
    Execute semantic search against codebase.

    Runs hybrid FTS + vector search with RRF fusion and metadata boosting.
    Returns ranked chunks with full content and metadata.
    """
    query_engine = req.app.state.query_engine

    if query_engine is None:
        raise HTTPException(
            status_code=503, detail="Query engine not initialized"
        )

    try:
        # Execute query pipeline
        result = query_engine.query(
            query_text=request.query,
            repository_id=request.repository_id,
            top_k=request.top_k,
        )

        # Convert results to response model
        chunks = []
        for chunk in result.get("results", []):
            chunks.append(
                ChunkResult(
                    chunk_id=chunk.get("chunk_id", ""),
                    content=chunk.get("content", ""),
                    content_preview=chunk.get("content_preview", ""),
                    file_path=chunk.get("file_path", ""),
                    start_line=chunk.get("start_line", 0),
                    end_line=chunk.get("end_line", 0),
                    breadcrumb=chunk.get("breadcrumb"),
                    chunk_type=chunk.get("chunk_type"),
                    score=chunk.get("score", 0.0),
                    rrf_score=chunk.get("rrf_score"),
                    boost_multiplier=chunk.get("boost_multiplier"),
                )
            )

        return SearchResponse(
            results=chunks,
            query_id=result.get("query_id"),
            total_results=len(chunks),
            metadata=result.get("metadata"),
        )

    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, req: Request) -> ChatResponse:
    """
    Generate answer using RAG pipeline.

    Retrieves relevant chunks and generates answer with citations.
    Returns answer with sources and cost information.
    """
    answer_generator = req.app.state.answer_generator

    if answer_generator is None:
        raise HTTPException(
            status_code=503, detail="Answer generator not initialized"
        )

    try:
        # Generate answer
        result = answer_generator.generate(
            query=request.query,
            repository_id=request.repository_id,
            top_k=request.top_k,
        )

        # Convert sources to response model
        sources = []
        for source in result.get("sources", []):
            sources.append(
                SourceInfo(
                    number=source.get("number", 0),
                    file_path=source.get("file_path", ""),
                    start_line=source.get("start_line", 0),
                    end_line=source.get("end_line", 0),
                    breadcrumb=source.get("breadcrumb"),
                    chunk_type=source.get("chunk_type"),
                )
            )

        return ChatResponse(
            answer=result.get("answer", ""),
            sources=sources,
            query_id=result.get("query_id"),
            cost=result.get("total_cost", 0.0),
            tokens_in=result.get("prompt_tokens", 0),
            tokens_out=result.get("completion_tokens", 0),
            cache_hit=result.get("cache_hit", False),
            model=result.get("model"),
            chunks_retrieved=result.get("chunks_retrieved"),
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, req: Request):
    """
    Stream answer generation using RAG pipeline via Server-Sent Events.

    Streams the answer in chunks as it's generated, then sends final
    metadata including sources and cost information.
    """
    answer_generator = req.app.state.answer_generator

    if answer_generator is None:
        async def error_generator():
            yield f"data: {json.dumps({'type': 'error', 'error': 'Answer generator not initialized'})}\n\n"

        return StreamingResponse(
            error_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    async def generate():
        try:
            # Generate full answer (will add true streaming when OpenAI streaming integrated)
            result = answer_generator.generate(
                query=request.query,
                repository_id=request.repository_id,
                top_k=request.top_k,
            )

            # Stream the answer in chunks (simulate streaming for better UX)
            answer = result.get("answer", "")
            chunk_size = 20  # characters per chunk

            for i in range(0, len(answer), chunk_size):
                chunk = answer[i : i + chunk_size]
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
                await asyncio.sleep(0.01)  # Small delay between chunks

            # Build sources list for final event
            sources = []
            for source in result.get("sources", []):
                sources.append({
                    "number": source.get("number", 0),
                    "file_path": source.get("file_path", ""),
                    "start_line": source.get("start_line", 0),
                    "end_line": source.get("end_line", 0),
                    "breadcrumb": source.get("breadcrumb"),
                    "chunk_type": source.get("chunk_type"),
                })

            # Send final metadata
            done_event = {
                "type": "done",
                "sources": sources,
                "query_id": str(result.get("query_id", "")),
                "cost": result.get("total_cost", 0.0),
                "tokens_in": result.get("prompt_tokens", 0),
                "tokens_out": result.get("completion_tokens", 0),
                "cache_hit": result.get("cache_hit", False),
            }
            yield f"data: {json.dumps(done_event)}\n\n"

        except Exception as e:
            logger.error(f"Chat stream error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
