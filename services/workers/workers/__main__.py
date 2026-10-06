"""`python -m workers` -- the ingestion worker process.

This is what `services/workers/Dockerfile`'s `CMD ["python", "-m",
"workers"]` has been pointing at since the image was written. Since 22-05 it
processes jobs: `workers.jobs.handlers.REGISTRY` carries `full_ingest` and
`incremental`, both the ingest handler in `workers.ingest`.

It still FAILS CLOSED, and the order of the checks below is load-bearing:

  1. **Handlers first.** An EMPTY `REGISTRY` means log why and exit 2,
     before any configuration is read. The shipped registry is never
     empty; this is what a build that lost its handlers says, rather than
     claiming jobs it cannot run and dead-lettering them (21-06). Tested
     with the registry cleared.
  2. **`DATABASE_URL`.** Missing -> exit 2. This refusal became reachable
     only once the registry was filled (22-05), and has its own test.
  3. **The ingest configuration** -- `INTERNAL_API_URL` and
     `OPENAI_API_KEY` (`workers.ingest.handler.deps_from_env`). Missing ->
     exit 2. Without them every job would fail five times and dead-letter,
     which is the accident step 1 exists to prevent, one step later.
  4. **The operating numbers** (P16, provisional until 22.1-05):
     `max_job_duration` two hours, the heartbeat's `statement_timeout`
     fifteen seconds, each with an environment override. A malformed
     override -> exit 2.
  5. **Sweep stale job directories** once, then run.

Exit codes: 2 is "this build or its configuration says not to run", which
no restart can fix; 1 is "this process could not do its job" (the database
stayed unreachable), which a `restart: on-failure` policy is for.

**What turned the worker on is recorded in `docs/api-ingestion-jobs.md`,
under "The Phase 22 hand-off", which is the authority.** This docstring
keeps no copy of that list: PR #43's review found three files carrying
three different versions of it, which is the failure mode `21-CONTEXT.md`
opens by naming.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import timedelta
from typing import Mapping, Optional, Sequence, Tuple

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
    "no job handlers registered; Phase 22 registers full_ingest and incremental, "
    "so an empty map means this build was altered -- refusing to start so queued "
    "jobs are not dead-lettered. Register them in workers.jobs.handlers.REGISTRY."
)

NO_DSN_MESSAGE = (
    "DATABASE_URL is not set; refusing to start. The worker claims jobs from "
    "the application Postgres and writes each repository's chunks there."
)


def operating_numbers(environ: Mapping[str, str]) -> Tuple[timedelta, timedelta]:
    """P16's `(max_job_duration, heartbeat_statement_timeout)`, with overrides.

    PROVISIONAL UNTIL 22.1-05: the defaults are `workers.jobs.runtime`'s
    `DEFAULT_MAX_JOB_DURATION` (2 h) and
    `DEFAULT_HEARTBEAT_STATEMENT_TIMEOUT` (15 s);
    `WORKER_MAX_JOB_DURATION_SECONDS` and
    `WORKER_HEARTBEAT_STATEMENT_TIMEOUT_MS` override them. The third P16
    number, the pool size, is the compose service's replica count.

    Raises:
        ValueError: an override is not a positive whole number. It names the
            variable; the value is a number, not a secret.
    """
    from workers.jobs.runtime import (
        DEFAULT_HEARTBEAT_STATEMENT_TIMEOUT,
        DEFAULT_MAX_JOB_DURATION,
        HEARTBEAT_STATEMENT_TIMEOUT_ENV,
        MAX_JOB_DURATION_ENV,
    )

    def positive(name: str) -> Optional[int]:
        raw = (environ.get(name) or "").strip()
        if not raw:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError(f"{name} must be a positive whole number, got {raw!r}")
        return int(raw)

    seconds = positive(MAX_JOB_DURATION_ENV)
    millis = positive(HEARTBEAT_STATEMENT_TIMEOUT_ENV)
    return (
        DEFAULT_MAX_JOB_DURATION if seconds is None else timedelta(seconds=seconds),
        DEFAULT_HEARTBEAT_STATEMENT_TIMEOUT if millis is None else timedelta(milliseconds=millis),
    )


def build_worker(dsn: str, handlers, environ: Mapping[str, str]):
    """The `Worker` this entrypoint runs: P16's numbers, never an unset bound.

    Separate from `main` so a test can assert what a deployed worker is
    built with -- above all that `max_job_duration` is SET, so the
    unset-bound WARNING a bare `Worker` gives cannot appear in production.
    """
    from workers.jobs.runtime import Worker

    max_job_duration, heartbeat_statement_timeout = operating_numbers(environ)
    return Worker(
        dsn,
        dict(handlers),
        max_job_duration=max_job_duration,
        heartbeat_statement_timeout=heartbeat_statement_timeout,
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

    # 2. The database.
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error(NO_DSN_MESSAGE)
        return REFUSED

    # Imported here rather than at module scope so that the refusals above
    # cannot be turned into an import error by anything the runtime pulls
    # in. From here on an import error is a crash at startup (exit 1), which
    # is the right ending for a broken build: it happens before a claim.
    from workers.fetch import sweep_stale_workdirs
    from workers.ingest.handler import ConfigurationError, configure, deps_from_env
    from workers.jobs.runtime import DatabaseUnavailable
    from workers.jobs.transitions import sanitize_error

    # 3. The ingest configuration: refuse now rather than fail every job.
    try:
        deps = deps_from_env(os.environ)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return REFUSED

    # 4. The operating numbers (P16, provisional until 22.1-05).
    try:
        worker = build_worker(dsn, REGISTRY, os.environ)
    except ValueError as exc:
        logger.error("%s; refusing to start", exc)
        return REFUSED
    configure(deps)

    # 5. Job directories a crashed run left behind. The bound is one
    # `max_job_duration` plus a lease: past it, no job this host could still
    # be running has been alive that long, so nothing live is swept.
    sweep_stale_workdirs(deps.workdir, worker.max_job_duration + worker.lease)

    stop = threading.Event()
    _install_signal_handlers(stop)

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
    nothing could touch it until the lease expired. The ingest handler
    checks the flag between stages (and between embedding slices) and
    raises `Unfinished`, so the job goes back to the queue with its attempt.

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
