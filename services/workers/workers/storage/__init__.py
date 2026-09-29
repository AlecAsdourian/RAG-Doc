"""Storage package for persisting chunks and their embeddings, in Postgres."""

from .postgres_writer import PostgresWriter

__all__ = ["PostgresWriter"]
