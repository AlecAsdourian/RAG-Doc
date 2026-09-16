"""The ingestion queue's consumer side.

`workers.jobs.transitions` holds the state machine over `ingestion_jobs`;
`workers.jobs.backoff` holds the retry delay. Read `transitions`' module
docstring before changing either -- the two rules that bite are that every
terminal write is fenced on the lease AND on `state = 'running'`, and that
completing before re-enqueueing is an ordering rule whose violation raises
nothing at all.

21-06 adds `runtime.py` (the loop, the heartbeat, the sweeper's schedule)
and `workers/__main__.py` on top of this.
"""

from workers.jobs.backoff import next_run_after_delay
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
    "Job",
    "LeaseLost",
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
