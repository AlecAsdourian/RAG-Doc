"""Embeddings package for generating vector representations of code chunks."""

from .defaults import DEFAULT_EMBEDDING_MODEL
from .openai_client import OpenAIEmbeddingClient
from .embedding_generator import EmbeddingGenerator

__all__ = ["DEFAULT_EMBEDDING_MODEL", "OpenAIEmbeddingClient", "EmbeddingGenerator"]
