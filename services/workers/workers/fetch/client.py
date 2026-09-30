"""The worker's side of the backend's repository-token route.

`POST {INTERNAL_API_URL}/internal/jobs/{job_id}/repository-token` with the
body `{"lease_owner": "<the worker's lease owner>"}`. The backend mints a
token scoped to the job's ONE repository, `contents: read`, for an hour --
and only while that job is `running` under that lease. The App's private
key stays in the backend (U4). See `docs/internal-api.md` for the contract.

⚠ THE MARKER IS THE WHOLE OF THE CLIENT'S JUDGEMENT. Every response the
route writes carries `X-Rag-Internal: repository-token/1`. A 404 WITH the
marker and the fixed body means "no live lease for this job under this
owner": the job is not ours any more, and 22-05 turns `TokenRefused` into
`LeaseLost`, so nothing is written. A response WITHOUT the marker -- chi's
`404 page not found` from the public router when `INTERNAL_API_URL` is
misconfigured, a proxy's page, a 200 from something that is not the route
-- is `InternalApiMisrouted`, a plain exception: the job FAILS LOUDLY, an
attempt is consumed and `last_error` names the misconfiguration by host.
Without the marker a misrouted worker would read every chi 404 as a lost
lease and the job would die after five lease expiries with `last_error`
NULL, which is the failure fact-check c3 named.

⚠ TWO DISTINCT 409s, NEVER A GENERIC ONE. `installation_suspended` and
`installation_uninstalled` are different endings in Phase 21's policy (a
suspension DEFERS an hour with the attempt handed back; an uninstall
ABANDONS), and the runtime dispatches on the exception TYPE. Neither may
ever end a job `dead`: a suspension mid-run must not dead-letter a healthy
repository. Since 22-05 both classes are DEFINED in `workers.jobs.runtime`,
which owns every ending, and re-exported here, so there is one class each
and an `except` in either place catches the same thing.

⚠ NOTHING HERE LOGS OR RAISES THE TOKEN, THE LEASE OWNER OR A URL WITH
EITHER IN IT. The lease owner is the credential this route accepts, so it
is as secret as the token it buys. Messages carry the internal API's HOST
and a status code, nothing else from the wire.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

# Defined by the runtime, which owns every handler ending (22-05); re-exported
# from here, where 22-04 first raised them, so there is one class of each.
from workers.jobs.runtime import InstallationSuspended, InstallationUninstalled

logger = logging.getLogger(__name__)

#: The route's marker. Pinned on the Go side as internalapi.MarkerHeader /
#: MarkerValue; the two must change together.
MARKER_HEADER = "X-Rag-Internal"
MARKER_VALUE = "repository-token/1"

#: THE 404 body. Byte-identical for every miss on the Go side; compared
#: here as JSON, so the client is indifferent to whitespace.
REFUSED_BODY = {"error": "no_live_lease"}

REASON_SUSPENDED = "installation_suspended"
REASON_UNINSTALLED = "installation_uninstalled"

USER_AGENT = "rag-doc-worker"

DEFAULT_TIMEOUT_SECONDS = 60.0


class TokenRefused(Exception):
    """A MARKED 404: no live lease for this job under this owner.

    The job is not ours -- reclaimed, superseded, completed, or the lease
    expired. The ingest handler raises `LeaseLost` for it, so nothing is
    written: returning instead would reach `complete`, whose fence does not
    check the lease's expiry, and write `completed` for undone work.
    """


class InternalApiMisrouted(Exception):
    """A response WITHOUT the route's marker, whatever its status.

    A plain exception on purpose: the job fails loudly, consumes an attempt
    and records where the request went (host only). It must never look like
    a lost lease.
    """


class TokenRequestFailed(Exception):
    """The route answered, marked, with something this client does not accept.

    A 500 or 502 from the route, a 400 for a body it did not like, a 404
    with a body that is not THE 404, a 200 missing a field, or a transport
    failure. A plain failure: retried with backoff like any other.
    """


@dataclass(frozen=True, repr=False)
class RepositoryToken:
    """What the route hands back. Its renderings never include the token."""

    token: str
    expires_at: datetime
    full_name: str
    default_branch: str

    def __repr__(self) -> str:
        return (
            f"RepositoryToken(full_name={self.full_name!r}, "
            f"default_branch={self.default_branch!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, token='[REDACTED]')"
        )

    __str__ = __repr__


def _host(url: str) -> str:
    """The host and port of a URL, and nothing else from it."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    return f"{host}:{parts.port}" if parts.port else host


def _json_or_none(response: httpx.Response) -> Optional[Any]:
    try:
        return response.json()
    except ValueError:
        return None


def request_token(
    internal_api_url: str,
    job_id: str,
    lease_owner: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RepositoryToken:
    """Ask the backend for this job's repository token.

    Args:
        internal_api_url: the backend's INTERNAL listener, e.g.
            `http://backend:8081`. Never the public API.
        job_id: the job the worker is running.
        lease_owner: the worker's lease owner for that job -- the credential.
        transport: an `httpx` transport, for tests. Production leaves it None.
        timeout: seconds; the backend makes two GitHub calls on our behalf.

    Raises:
        TokenRefused, InternalApiMisrouted, InstallationSuspended,
        InstallationUninstalled, TokenRequestFailed -- see each class. The
        two installation classes are `workers.jobs.runtime`'s.
    """
    canonical_job = str(uuid.UUID(str(job_id)))
    base = internal_api_url.rstrip("/")
    host = _host(base)
    url = f"{base}/internal/jobs/{canonical_job}/repository-token"

    # ⚠ trust_env=False, AND IT IS LOAD-BEARING (PR #52's review, L2).
    # httpx defaults to trust_env=True, which honours HTTP_PROXY /
    # HTTPS_PROXY / ALL_PROXY from the environment. This request is plain
    # HTTP on the compose network and carries the lease owner in its body
    # and the token in its reply; through a forward proxy both would
    # transit the proxy in the clear -- and because a proxy relays headers,
    # the marker would survive and nothing would fail loudly. So this one
    # client reads no proxy from the environment. The fetcher's client
    # (workers.fetch.archive) keeps the default on purpose: an egress proxy
    # for GitHub is a legitimate deployment, and its traffic is HTTPS
    # through a CONNECT tunnel the proxy cannot read.
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(
                url,
                json={"lease_owner": lease_owner},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
    except httpx.HTTPError as exc:
        # `from None`: an httpx exception can carry the request's URL, and
        # a chained traceback would print it.
        raise TokenRequestFailed(
            f"the internal API at {host} could not be reached: {type(exc).__name__}"
        ) from None

    if response.headers.get(MARKER_HEADER) != MARKER_VALUE:
        raise InternalApiMisrouted(
            f"INTERNAL_API_URL does not reach the repository-token route: {host} "
            f"answered {response.status_code} without {MARKER_HEADER}"
        )

    status = response.status_code
    if status == 200:
        return _parse_token(response, host)
    if status == 404:
        if _json_or_none(response) == REFUSED_BODY:
            raise TokenRefused(
                f"job {canonical_job}: no live lease for it under this worker; "
                "the job is no longer ours"
            )
        raise TokenRequestFailed(
            f"the repository-token route at {host} answered 404 with an unexpected body"
        )
    if status == 409:
        body = _json_or_none(response)
        reason = body.get("reason") if isinstance(body, dict) else None
        if reason == REASON_SUSPENDED:
            raise InstallationSuspended(
                f"job {canonical_job}: the GitHub App installation is suspended"
            )
        if reason == REASON_UNINSTALLED:
            raise InstallationUninstalled(
                f"job {canonical_job}: the GitHub App installation is uninstalled or missing"
            )
        raise TokenRequestFailed(
            f"the repository-token route at {host} answered 409 with reason {reason!r}"
        )
    raise TokenRequestFailed(f"the repository-token route at {host} answered {status}")


def _parse_token(response: httpx.Response, host: str) -> RepositoryToken:
    body = _json_or_none(response)
    if not isinstance(body, dict):
        raise TokenRequestFailed(f"the repository-token route at {host} answered 200 without JSON")
    for key in ("token", "expires_at", "full_name", "default_branch"):
        value = body.get(key)
        if not isinstance(value, str) or not value:
            # The key name only, never its value.
            raise TokenRequestFailed(
                f"the repository-token route at {host} answered 200 without a usable {key!r}"
            )
    try:
        expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    except ValueError:
        raise TokenRequestFailed(
            f"the repository-token route at {host} answered with an unparseable expires_at"
        ) from None
    logger.info(
        "repository token received for %s (default branch %s), expires %s",
        body["full_name"],
        body["default_branch"],
        expires_at.isoformat(),
    )
    return RepositoryToken(
        token=body["token"],
        expires_at=expires_at,
        full_name=body["full_name"],
        default_branch=body["default_branch"],
    )
