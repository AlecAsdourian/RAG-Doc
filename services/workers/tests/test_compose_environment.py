"""The compose `workers` service, resolved: no App key, and no accidental start (22-05).

Reads `docker compose ... config --format json`, which is the environment a
container would actually get -- anything an `env_file:` or a variable adds
included -- and NEVER STARTS COMPOSE.

⚠ NAMES ONLY, NEVER VALUES (fact-check c6). The resolved configuration
INTERPOLATES `${OPENAI_API_KEY}` from the environment running the test, so a
failing assertion that printed the environment -- or a helper that returned
it -- would print a real key on a developer's machine. Every helper here
extracts the names (and the two non-secret addresses it checks) inside
itself and discards the rest; no assertion message carries a value, and a
failed `docker compose config` reports its exit code, not its output. The
same caution applies to people: `docs/local-development.md` says so beside
the compose instructions.

Skipped, with the reason, where `docker compose` is not available (the
`python:3.11-slim` image has no Docker CLI).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

#: What the worker must never be given (U4): the App's private key in any
#: form, its id and client secret, and the webhook secret.
FORBIDDEN_NAMES = {"GITHUB_APP_ID", "GITHUB_APP_CLIENT_SECRET", "GITHUB_WEBHOOK_SECRET"}
FORBIDDEN_PREFIXES = ("GITHUB_APP_PRIVATE_KEY",)


def _compose_available() -> Optional[str]:
    """None when `docker compose` works here; otherwise why not."""
    docker = shutil.which("docker")
    if docker is None:
        return "the docker CLI is not installed here"
    try:
        result = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`docker compose version` could not run ({type(exc).__name__})"
    if result.returncode != 0:
        return "`docker compose` is not available (the compose plugin is missing)"
    return None


_UNAVAILABLE = _compose_available()
pytestmark = pytest.mark.skipif(
    _UNAVAILABLE is not None,
    reason=f"docker compose is unavailable, so the resolved compose environment cannot be read: {_UNAVAILABLE}",
)


def _shape(profile: Optional[str]) -> Dict[str, object]:
    """The resolved config's SHAPE: service names, env NAMES, and two addresses.

    ⚠ The parsed configuration never leaves this function. It carries the
    interpolated OpenAI key, so only names -- and the two non-secret
    addresses checked below -- are copied out of it.
    """
    args = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if profile is not None:
        args += ["--profile", profile]
    args += ["config", "--format", "json"]
    result = subprocess.run(args, capture_output=True, timeout=120)
    assert result.returncode == 0, (
        f"`docker compose config` exited {result.returncode}; its output is withheld because "
        "the resolved configuration can carry interpolated secrets"
    )
    services = json.loads(result.stdout)["services"]

    def names(service: dict) -> List[str]:
        environment = service.get("environment") or {}
        if isinstance(environment, dict):
            return sorted(environment)
        return sorted(item.split("=", 1)[0] for item in environment)

    shape: Dict[str, object] = {"services": sorted(services)}
    workers = services.get("workers")
    if workers is not None:
        environment = workers.get("environment") or {}
        shape["workers_env_names"] = names(workers)
        shape["workers_profiles"] = list(workers.get("profiles") or [])
        shape["workers_depends_on"] = sorted((workers.get("depends_on") or {}).keys())
        shape["workers_replicas"] = (workers.get("deploy") or {}).get("replicas")
        shape["workers_ports"] = len(workers.get("ports") or [])
        internal_url = environment.get("INTERNAL_API_URL") if isinstance(environment, dict) else None
        shape["internal_api_url_host_port"] = (
            (urlsplit(internal_url).hostname, urlsplit(internal_url).port) if internal_url else None
        )
    backend = services["backend"]
    backend_env = backend.get("environment") or {}
    shape["backend_expose"] = [str(port) for port in backend.get("expose") or []]
    shape["backend_published"] = sorted(
        str(port.get("target")) for port in backend.get("ports") or [] if port.get("published")
    )
    shape["internal_addr"] = backend_env.get("INTERNAL_ADDR") if isinstance(backend_env, dict) else None
    return shape


def test_workers_is_absent_without_the_ingest_profile():
    """A plain `docker compose up` must never start the worker.

    With the backend not starting under compose today, a worker that did
    would claim the queued jobs 000015 backfilled, fail to reach the token
    route, and walk every one of them to `dead`.
    """
    shape = _shape(None)
    assert "workers" not in shape["services"], (
        "the workers service resolves without the `ingest` profile, so a plain "
        "`docker compose up` would start it"
    )
    assert "backend" in shape["services"], "premise: the file resolved at all"


def test_the_workers_service_never_carries_the_app_key():
    """U4: the App's private key, id and secrets never enter the worker."""
    names = _shape("ingest")["workers_env_names"]
    leaked = sorted(
        name
        for name in names
        if name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES) or name.startswith("GITHUB_APP")
    )
    assert leaked == [], f"the worker's environment carries App credentials by name: {leaked}"


def test_the_workers_service_has_what_it_needs_and_the_provisional_pool():
    shape = _shape("ingest")
    names = shape["workers_env_names"]
    for needed in ("DATABASE_URL", "OPENAI_API_KEY", "INTERNAL_API_URL"):
        assert needed in names, f"{needed} is missing from the worker's environment (names: {names})"
    assert shape["workers_profiles"] == ["ingest"]
    assert shape["workers_depends_on"] == ["postgres"]
    assert shape["workers_replicas"] == 2, "P16: two worker processes, provisional until 22.1-05"
    assert shape["workers_ports"] == 0, "the worker serves nothing"


def test_the_internal_listener_is_exposed_on_the_network_and_never_published():
    """The token route is reachable by the worker and by nothing outside compose.

    Two addresses are read here, both non-secret: the backend's
    `INTERNAL_ADDR` (a service name, never a wildcard, which the backend
    would refuse) and the worker's `INTERNAL_API_URL` host and port, which
    must point at it.
    """
    shape = _shape("ingest")
    assert "8081" in shape["backend_expose"], "the internal listener is not exposed to the network"
    assert "8081" not in shape["backend_published"], (
        "the internal listener is PUBLISHED; anyone who can read a lease owner could mint tokens"
    )
    assert shape["internal_addr"] == "backend:8081", (
        "INTERNAL_ADDR must bind the service name, never a wildcard"
    )
    assert shape["internal_api_url_host_port"] == ("backend", 8081), (
        "the worker's INTERNAL_API_URL must reach the backend's internal listener, not the public API"
    )
