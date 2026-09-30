"""Ingesting a repository: the job handler the worker runs (22-05).

`workers.ingest.handler` is the `full_ingest` handler -- fetch, parse, embed,
store -- registered in `workers.jobs.handlers` for both job types until
22.1-02 distinguishes `incremental`. Read its module docstring for the
endings it raises and the cumulative-progress rule.
"""

from workers.ingest.handler import (
    EMBED_SLICE,
    MAX_CHUNKS,
    STAGES,
    ConfigurationError,
    IngestDeps,
    configure,
    deps_from_env,
    full_ingest,
    make_full_ingest_handler,
)

__all__ = [
    "EMBED_SLICE",
    "MAX_CHUNKS",
    "STAGES",
    "ConfigurationError",
    "IngestDeps",
    "configure",
    "deps_from_env",
    "full_ingest",
    "make_full_ingest_handler",
]
