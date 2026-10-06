"""The handler registry: which job types this process knows how to run.

⚠ BOTH KEYS ARE THE FULL INGEST, UNTIL 22.1-02. 22-05 registered
`full_ingest` and `incremental` -- the two job types producers create (a
connect creates `full_ingest`, a push `incremental`, and `complete`'s rerun
follow-up is always `incremental`) -- and both run
`workers.ingest.handler`'s fetch -> parse -> embed -> store, which replaces
the repository's chunks. That is CORRECT for a push, only slower than it
needs to be: 22.1-02 makes `incremental` re-parse only what changed (P11).

⚠ THE TWO KEYS ARE FIXED BY THE SCHEMA, not by convention: migration
000014 declares `CHECK (job_type IN ('full_ingest','incremental'))`, so a
third key here could never be claimed, and a missing one is a job the
worker fails with `UnknownJobType` on its way to `dead`.

⚠ AN EMPTY MAP STILL REFUSES TO START. `workers/__main__` checks this map
before it reads any configuration and exits 2 when it is empty, so a build
that has lost its handlers says so instead of claiming jobs it cannot run
and dead-lettering them. That refusal is tested with the map cleared
(`test_the_entrypoint_refuses_to_start_without_handlers`), since the
shipped map is never empty.

⚠ THE INGEST MODULE IS IMPORTED WHEN A JOB RUNS, NOT WHEN THIS MODULE IS,
and that is an import-cycle fix, not laziness. `workers.fetch` defines
`FetchRejected` as a subclass of `workers.jobs.runtime.Rejected`, so
importing `workers.fetch` imports the `workers.jobs` package, whose
`__init__` imports this module; if this module imported
`workers.ingest.handler` (which imports `workers.fetch`) at the top, a
process that imported `workers.fetch` first would meet a half-initialised
`workers.fetch` and fail. `workers/__main__` imports the ingest module and
builds its configuration at startup, after the registry check, so a broken
dependency or a missing setting stops the process before it claims
anything rather than failing jobs.
"""

from __future__ import annotations

from typing import Dict, Optional

from workers.jobs.runtime import Handler, JobContext, WriteResults


def run_full_ingest(ctx: JobContext) -> Optional[WriteResults]:
    """The registered handler for both job types: the full ingest.

    Delegates to `workers.ingest.handler.full_ingest`, which builds its
    dependencies from the environment on its first call unless
    `workers/__main__` has configured them already. See the module
    docstring for why the import is here.
    """
    from workers.ingest.handler import full_ingest

    return full_ingest(ctx)


#: job_type -> handler. See the module docstring.
REGISTRY: Dict[str, Handler] = {
    "full_ingest": run_full_ingest,
    "incremental": run_full_ingest,
}

__all__ = ["REGISTRY", "run_full_ingest"]
