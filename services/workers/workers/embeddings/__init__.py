"""Embeddings package for generating vector representations of code chunks."""

from .openai_client import OpenAIEmbeddingClient
from .embedding_generator import EmbeddingGenerator

__all__ = ["OpenAIEmbeddingClient", "EmbeddingGenerator"]
