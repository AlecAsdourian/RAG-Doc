"""`python -m workers` -- the ingestion worker process.

This is what `services/workers/Dockerfile`'s `CMD ["python", "-m",
"workers"]` has been pointing at since the image was written, and what did
not exist until now: the compose `workers` service built the image, ran
that command and died on `No module named workers.__main__`.

⚠ IT STILL DOES NOT PROCESS JOBS, AND THAT IS THE POINT OF THIS PLAN.
21-03 and 21-04 put real work in the queue -- a connect creates a
`full_ingest` job, a push creates an `incremental` one -- and Phase 22 is
what will know how to run them. A worker started before then would claim
those jobs, fail each of them `max_attempts` times and dead-letter them,
turning a queue that was merely waiting into a queue that has to be
repaired by hand.

So it FAILS CLOSED, and the order of the two checks below is load-bearing:

  1. **Handlers first.** `workers.jobs.handlers.REGISTRY` is empty until
     Phase 22 fills it; empty means log why and exit 2.
  2. **Configuration second.** `DATABASE_URL`.

⚠ THE ORDER IS WHY THE COMPOSE SERVICE SAYS SOMETHING USEFUL. Its
environment is `ENV=development` and nothing else (`docker-compose.yml`,
the `workers` service) -- there is no `DATABASE_URL`. Read configuration
first and the container dies complaining about a missing DSN, which is a
true statement about the wrong problem and would send whoever reads it off
to add one. Check the handlers first and it says the thing that is
actually true: there is no work this build knows how to do.

WHAT PHASE 22 CHANGES, and it is exactly three things:

  1. register `full_ingest` and `incremental` in
     `workers.jobs.handlers.REGISTRY`;
  2. add `DATABASE_URL` to the compose `workers` service;
  3. size the pool -- one job at a time per process, so scale by replicas,
     and 21-RESEARCH leaves the number to be MEASURED once something
     ingests end to end.

After 1 and 2 this entrypoint starts and the queue drains.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Optional, Sequence

logger = logging.getLogger("workers")

#: ⚠ EXIT 2, NOT 1. A container that exits 0 looks healthy to a scheduler
#: with `restart: on-failure`, and 1 is what an unhandled exception already
#: produces -- so it would be indistinguishable from a crash in the logs.
#: 2 is "this build is configured not to run".
REFUSED = 2

#: ASCII on purpose. This line is read out of a container's stderr, whose
#: encoding is whatever the host decided, and a refusal message that raises
#: `UnicodeEncodeError` on its way out is a refusal nobody can read.
NO_HANDLERS_MESSAGE = (
    "no job handlers registered; ingestion handlers arrive in Phase 22 -- "
    "refusing to start so queued jobs are not dead-lettered. Register them "
    "in workers.jobs.handlers.REGISTRY."
)

NO_DSN_MESSAGE = (
    "DATABASE_URL is not set; refusing to start. The compose `workers` "
    "service has no DATABASE_URL yet -- Phase 22 adds it alongside the "
    "handlers."
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Return the process exit code. See the module docstring for the order."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # 1. Handlers, BEFORE any configuration is read.
    from workers.jobs.handlers import REGISTRY

    if not REGISTRY:
        logger.error(NO_HANDLERS_MESSAGE)
        return REFUSED

    # 2. Configuration.
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error(NO_DSN_MESSAGE)
        return REFUSED

    # Imported here rather than at module scope so that the refusal above
    # cannot be turned into an import error by anything the runtime pulls
    # in. The refusal has to survive a broken dependency to be worth
    # having.
    from workers.jobs.runtime import DatabaseUnavailable, Worker
    from workers.jobs.transitions import sanitize_error

    stop = threading.Event()
    _install_signal_handlers(stop)

    worker = Worker(dsn, dict(REGISTRY))
    try:
        worker.run(stop)
    except DatabaseUnavailable as exc:
        # ⚠ EXIT 1, AND THE DIFFERENCE FROM 2 IS THE POINT. 2 means "this
        # build is configured not to run", which no restart can fix. 1
        # means "this process could not do its job" -- the same code an
        # unhandled crash produces, and the one a `restart: on-failure`
        # policy is there for. PR #42's review found the worker staying
        # alive forever on a dead connection, producing no exit code at
        # all, so nothing ever restarted it.
        logger.error("%s", sanitize_error(exc))
        return 1
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    """SIGTERM and SIGINT set `stop`; the loop finishes its job and returns.

    Setting a flag rather than raising is what makes the shutdown graceful:
    a `KeyboardInterrupt` through the middle of `complete` would leave the
    job `running` with its results uncommitted and its lease live, and
    nothing could touch it until the lease expired.

    ⚠ A SECOND SIGNAL IS NOT SPECIAL-CASED. A scheduler that wants a
    faster exit sends SIGKILL, and the lease plus the sweeper are what
    recover from that -- which is the same path a crash takes and is
    therefore the one that is actually tested.
    """

    def _stop(signum, _frame):  # pragma: no cover - signal delivery
        logger.info("signal %s received; finishing the current job", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):  # pragma: no cover
            # Not the main thread, or a platform without this signal.
            logger.warning("could not install a handler for signal %s", sig)


if __name__ == "__main__":
    sys.exit(main())
