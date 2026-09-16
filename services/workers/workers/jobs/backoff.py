"""Retry backoff for the ingestion queue: capped, exponential, jittered.

21-CONTEXT left the ceiling as an open question ("pick a cap in 21-02 and
write it down"); the 2026-09-14 plan mapping moved it here, and 21-02's
`failSQL` takes the interval as a parameter, so nothing in the database
constrains the choice. This module is that choice, as a pure function with
its own unit test.

THE FORMULA. After attempt `n` fails, the delay before the next attempt is

    min(60s * 4 ** (n - 1), 60 minutes)  *  U[0.5, 1.0)

⚠ THE JITTER IS A MULTIPLIER, NOT AN ADDEND, which is why `tenacity` is not
used here despite being a dependency and despite `21-RESEARCH.md`'s
"don't hand-roll ... use the existing pattern from tenacity". `wait_random`
and `wait_exponential + wait_random` ADD a uniform term to the exponential
one; no tenacity strategy multiplies. Adding jitter to a capped exponential
pushes the tail ABOVE the cap, so the cap stops being a cap. Multiplying
keeps every delay inside [cap/2, cap) and still spreads a thundering herd,
which is the whole point of jittering.

tenacity is also a retry-LOOP driver, and nothing here loops: a failed
attempt writes `run_after` and RELEASES the worker. The delay is a column
value, not a sleep.

WHAT IT COSTS, with `max_attempts = 5` (migration 000014's default). Four
delays separate five attempts, so the worst case is

    60 + 240 + 960 + 3600 = 4860s  =  81 minutes

before the fifth attempt writes `dead`, and about three quarters of that in
expectation. That is the number 21-CONTEXT's open question asked for.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Callable

#: The first delay, and the base of the exponential: attempt 1 waits ~60s.
BASE_DELAY = timedelta(seconds=60)

#: The multiplier per attempt. 4, not 2: with only five attempts, doubling
#: would put the last retry about 16 minutes out in total, which is shorter
#: than a single large ingest and so retries a transient outage while it is
#: still happening.
MULTIPLIER = 4

#: ⚠ THE CAP. Without it, the tail of a five-attempt sequence lands hours
#: out and a job's failure stops being visible on the day it happened.
#: Pinned by test_backoff.py; mutation E removes it.
MAX_DELAY = timedelta(minutes=60)

#: The jitter multiplier is drawn from [JITTER_FLOOR, 1.0). The floor is a
#: half, not zero: a factor near zero retries immediately, which is the one
#: thing a backoff exists to prevent.
JITTER_FLOOR = 0.5

# 4 ** 16 * 60s is already longer than the age of the universe, so anything
# past this is the cap by construction. Clamping the exponent keeps a
# corrupt `attempts` from building a multi-thousand-digit integer before
# `min` throws it away.
_MAX_EXPONENT = 16


def next_run_after_delay(
    attempts: int,
    rng: Callable[[], float] = random.random,
) -> timedelta:
    """Return the delay before the next attempt, after `attempts` have failed.

    Args:
        attempts: the job's `attempts` column AFTER the claim incremented
            it -- i.e. the number of the attempt that has just failed. The
            claim query always increments, so this is >= 1 in practice; a
            smaller value is clamped rather than raised on, because a
            failure write must never be lost to an argument check.
        rng: a zero-argument callable returning a float in [0, 1). Injected
            so the unit test can pin both ends of the jitter interval
            without sampling.

    Returns:
        A `timedelta` in [capped/2, capped), where `capped` is
        `min(BASE_DELAY * MULTIPLIER ** (attempts - 1), MAX_DELAY)`.
    """
    exponent = min(max(attempts, 1) - 1, _MAX_EXPONENT)
    uncapped = BASE_DELAY.total_seconds() * (MULTIPLIER**exponent)
    capped = min(uncapped, MAX_DELAY.total_seconds())

    # rng() is in [0, 1), so the factor is in [0.5, 1.0) -- never above the
    # cap, and never low enough to be a retry storm.
    factor = JITTER_FLOOR + (1.0 - JITTER_FLOOR) * rng()
    return timedelta(seconds=capped * factor)
