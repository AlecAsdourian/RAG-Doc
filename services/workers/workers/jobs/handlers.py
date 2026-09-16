"""The handler registry: which job types this process knows how to run.

⚠ IT IS EMPTY IN PHASE 21, AND THAT IS THE POINT. 21-03 and 21-04 put real
work in the queue -- a connect creates a `full_ingest` job, a push creates
an `incremental` one -- and a worker with no real handler would claim those
jobs, fail each of them five times and dead-letter them. `workers/__main__`
therefore refuses to start while this map is empty, BEFORE it reads any
configuration, so the refusal is what an operator sees rather than a
crash on the missing `DATABASE_URL`.

Until then, jobs stay `queued`. They lose nothing by waiting: `run_after`
is in the past, `attempts` is 0, and the partial unique index keeps each
repository to one of them however many pushes arrive.

PHASE 22 REGISTERS THEM, like this:

    from workers.jobs.handlers import REGISTRY
    REGISTRY["full_ingest"] = run_full_ingest
    REGISTRY["incremental"] = run_incremental

and adds `DATABASE_URL` to the compose `workers` service
(`docker-compose.yml`, which today gives it only `ENV=development`). After
those two changes the entrypoint starts and the queue drains.

⚠ THE TWO KEYS ARE FIXED BY THE SCHEMA, not by convention: migration
000014 declares `CHECK (job_type IN ('full_ingest','incremental'))`, so a
third key here could never be claimed, and a missing one is a job the
worker fails with `UnknownJobType` on its way to `dead`.
"""

from __future__ import annotations

from typing import Dict

from workers.jobs.runtime import Handler

#: job_type -> handler. See the module docstring; Phase 22 fills it.
REGISTRY: Dict[str, Handler] = {}

__all__ = ["REGISTRY"]
