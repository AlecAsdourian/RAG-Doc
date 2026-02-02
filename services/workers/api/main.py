"""FastAPI application for RAG API service."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routes import router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Initializes QueryEngine and AnswerGenerator on startup,
    cleans up resources on shutdown.
    """
    logger.info("RAG API starting up...")

    # Load configuration from environment
    postgres_conn = os.getenv("DATABASE_URL")
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    redis_url = os.getenv("REDIS_URL")

    # Initialize components (optional - graceful degradation if not configured)
    app.state.query_engine = None
    app.state.answer_generator = None

    if postgres_conn and qdrant_url and openai_api_key:
        try:
            from workers.retrieval.query_engine import QueryEngine
            from workers.generation.answer_generator import AnswerGenerator

            # Initialize QueryEngine
            query_engine = QueryEngine(
                postgres_conn=postgres_conn,
                qdrant_url=qdrant_url,
                openai_api_key=openai_api_key,
            )
            app.state.query_engine = query_engine
            logger.info("QueryEngine initialized")

            # Initialize AnswerGenerator
            # Optionally initialize semantic cache if Redis is available
            semantic_cache = None
            if redis_url:
                try:
                    from workers.generation.semantic_cache import SemanticCache

                    semantic_cache = SemanticCache(
                        redis_url=redis_url,
                        qdrant_url=qdrant_url,
                        openai_api_key=openai_api_key,
                    )
                    logger.info("SemanticCache initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize SemanticCache: {e}")

            answer_generator = AnswerGenerator(
                query_engine=query_engine,
                openai_api_key=openai_api_key,
                semantic_cache=semantic_cache,
            )
            app.state.answer_generator = answer_generator
            logger.info("AnswerGenerator initialized")

        except Exception as e:
            logger.error(f"Failed to initialize RAG components: {e}")
            logger.warning("RAG API running in degraded mode - endpoints will return 503")
    else:
        missing = []
        if not postgres_conn:
            missing.append("DATABASE_URL")
        if not openai_api_key:
            missing.append("OPENAI_API_KEY")
        logger.warning(
            f"Missing required environment variables: {', '.join(missing)}. "
            "RAG API running in degraded mode."
        )

    logger.info("RAG API startup complete")
    yield

    # Cleanup on shutdown
    logger.info("RAG API shutting down...")
    app.state.query_engine = None
    app.state.answer_generator = None
    logger.info("RAG API shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="RAG API",
    description="RAG-powered code search and Q&A API for Smart Documentation Platform",
    version="0.1.0",
    lifespan=lifespan,
)

# Include routes
app.include_router(router)
