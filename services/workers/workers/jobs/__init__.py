"""The ingestion queue's consumer side.

`workers.jobs.transitions` holds the state machine over `ingestion_jobs`;
`workers.jobs.backoff` holds the retry delay. Read `transitions`' module
docstring before changing either -- the two rules that bite are that every
terminal write is fenced on the lease AND on `state = 'running'`, and that
completing before re-enqueueing is an ordering rule whose violation raises
nothing at all.

`workers.jobs.runtime` is the process: the loop, the heartbeat, the
sweeper's schedule and the claim-time installation check. It DRIVES the
transitions and reimplements none of them. `workers.jobs.handlers` is the
registry it runs, empty until Phase 22, and `workers/__main__.py` refuses
to start while it is -- so nothing claims a real job before there is
something that can do it.
"""

from workers.jobs.backoff import next_run_after_delay
from workers.jobs.handlers import REGISTRY
from workers.jobs.runtime import (
    DatabaseUnavailable,
    Handler,
    JobContext,
    Unfinished,
    UnknownJobType,
    Worker,
    WriteResults,
)
from workers.jobs.transitions import (
    Job,
    LeaseLost,
    abandon,
    attach_ingestion_run,
    claim,
    complete,
    defer,
    fail,
    mark_started,
    new_worker_id,
    resolve_ingestion_run,
    sanitize_error,
    sweep,
)

__all__ = [
    "DatabaseUnavailable",
    "Handler",
    "Job",
    "JobContext",
    "LeaseLost",
    "REGISTRY",
    "Unfinished",
    "UnknownJobType",
    "Worker",
    "WriteResults",
    "abandon",
    "attach_ingestion_run",
    "claim",
    "complete",
    "defer",
    "fail",
    "mark_started",
    "new_worker_id",
    "next_run_after_delay",
    "resolve_ingestion_run",
    "sanitize_error",
    "sweep",
]
