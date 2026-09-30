"""Fetching a repository safely (22-04, decision P10).

Three modules, one rule each:

- `workers.fetch.client` asks the backend's INTERNAL listener for a
  one-hour, one-repository, read-only token, presenting the job id and the
  lease the worker holds. The App's private key never enters this process
  (U4). The client refuses to interpret any response that does not carry
  the route's marker, so a misconfigured `INTERNAL_API_URL` fails loudly
  instead of looking like a lost lease.
- `workers.fetch.archive` downloads the repository as a tar archive at an
  EXACT commit (U5), never `git clone` -- the worker image has no `git` --
  extracts it into a per-job directory under the v1 caps (U6), and cleans
  up after itself. NO CUSTOMER CODE IS EVER EXECUTED: no `git`, no build
  tools, no hooks, no install steps. The archive is bytes that get read.
- `workers.fetch.filters` is the U7 deny-list of secret-looking files,
  plus the vendored, generated and lockfile rules, applied BEFORE a file
  is written to disk, so a committed `.env` never lands anywhere.

22-05 wires these into the `full_ingest` handler (`workers.ingest`) and
maps the exceptions onto Phase 21's endings: `TokenRefused` -> `LeaseLost`
(write nothing); `InstallationSuspended` -> defer; `InstallationUninstalled`
-> abandon; `FetchRejected`, a `workers.jobs.runtime.Rejected` -> `dead` in
one attempt; anything else -> `fail`. The two installation classes are
defined by the runtime and re-exported here. `revoke_token` ends the
token the moment the fetch is over, because the lease gates a token's
issuance, not its hour of validity.
"""

from workers.fetch.archive import (
    DEFAULT_LIMITS,
    DownloadStats,
    ExtractStats,
    FetchedFile,
    FetchedTree,
    FetchFailed,
    FetchRejected,
    Limits,
    collect_tree,
    download_archive,
    expected_top_levels_for,
    extract_archive,
    fetch_repository,
    job_directory,
    resolve_head,
    revoke_token,
    sweep_stale_workdirs,
)
from workers.fetch.client import (
    MARKER_HEADER,
    MARKER_VALUE,
    REFUSED_BODY,
    InstallationSuspended,
    InstallationUninstalled,
    InternalApiMisrouted,
    RepositoryToken,
    TokenRefused,
    TokenRequestFailed,
    request_token,
)
from workers.fetch.filters import LANGUAGES, Verdict, classify_path, is_generated_go, is_secret_name

__all__ = [
    "DEFAULT_LIMITS",
    "DownloadStats",
    "ExtractStats",
    "FetchFailed",
    "FetchRejected",
    "FetchedFile",
    "FetchedTree",
    "InstallationSuspended",
    "InstallationUninstalled",
    "InternalApiMisrouted",
    "LANGUAGES",
    "Limits",
    "MARKER_HEADER",
    "MARKER_VALUE",
    "REFUSED_BODY",
    "RepositoryToken",
    "TokenRefused",
    "TokenRequestFailed",
    "Verdict",
    "classify_path",
    "collect_tree",
    "download_archive",
    "expected_top_levels_for",
    "extract_archive",
    "fetch_repository",
    "is_generated_go",
    "is_secret_name",
    "job_directory",
    "request_token",
    "resolve_head",
    "revoke_token",
    "sweep_stale_workdirs",
]
