"""The default embedding model, named once.

`EmbeddingGenerator`, `OpenAIEmbeddingClient`, `IngestionPipeline` and
`QueryEngine` all default to it, and the quality harness's `--embedding-model`
does too (22.2-07). It stays text-embedding-ada-002 unless the embedding-model
protocol (M2, `scripts/rag_benchmarks/embedding-model-protocol.md`) adopts
text-embedding-3-small; changing it then is 22.2-05's, together with the two
construction sites that still take the generator's default (the answer
generator's cache lookup and the worker handler's `deps_from_env`), QA13.
"""

DEFAULT_EMBEDDING_MODEL = "text-embedding-ada-002"
