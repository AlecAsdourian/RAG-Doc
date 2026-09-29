"""The token client and the fetcher against fake servers (22-04).

Two fakes:

- `httpx.MockTransport` for the token route and for most of the GitHub
  API, where what matters is which request was made with which headers;
- a REAL socket server (`http.server`, in a thread) for the one thing that
  must be measured rather than assumed: that `Authorization` does not
  survive a redirect to another origin. It is measured twice, once through
  the mock (two host names) and once over real sockets (two ports), because
  httpx's own rule is "same scheme, host and port", and GitHub's redirect
  changes the host.

⚠ EVERY "NEVER LOGGED" CLAIM IS ASSERTED ON `caplog`'s RECORDS, with the
exception logged `exc_info=True` the way the worker logs a failed job, so
a chained traceback carrying a URL would be caught. A test that reads no
log record cannot catch a logging bug.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import tarfile
import threading
import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from workers.fetch import (
    MARKER_HEADER,
    MARKER_VALUE,
    FetchFailed,
    FetchRejected,
    InstallationSuspended,
    InstallationUninstalled,
    InternalApiMisrouted,
    Limits,
    RepositoryToken,
    TokenRefused,
    TokenRequestFailed,
    fetch_repository,
    request_token,
    sweep_stale_workdirs,
)
from workers.jobs.transitions import sanitize_error

# The measured token shape: `ghs_` plus 383 characters (20-02), and a short one.
SENTINEL_TOKEN = "ghs_" + ("S3ntinelT0kenLeak" * 24)[:383]
SHORT_TOKEN = "ghs_x1"
assert len(SENTINEL_TOKEN) == 4 + 383

LEASE_OWNER = "6f1c9a2e-3b4d-4c5e-8f6a-7b8c9d0e1f2a"
JOB_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
INTERNAL = "http://backend:8081"
SHA = "84b2677f76060c4c8a0a5a9b1c2d3e4f5a6b7c8d"
LINK_SECRET = "LINKSECRET-must-never-be-logged"
DOWNLOAD_PATH = f"/acme/widgets/legacy.tar.gz/{SHA}"

MB = 1024 * 1024


def marked(status: int, body: object = None, *, text: Optional[str] = None) -> httpx.Response:
    headers = {MARKER_HEADER: MARKER_VALUE}
    if text is not None:
        return httpx.Response(status, text=text, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


def good_token_body() -> dict:
    return {
        "token": SENTINEL_TOKEN,
        "expires_at": "2026-09-29T15:00:00Z",
        "full_name": "acme/widgets",
        "default_branch": "main",
    }


def transport_answering(response: httpx.Response, seen: Optional[List[httpx.Request]] = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return response

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------
# request_token
# ---------------------------------------------------------------------


def test_a_marked_200_yields_a_token_and_the_request_is_the_contract(caplog) -> None:
    seen: List[httpx.Request] = []
    with caplog.at_level(logging.DEBUG):
        token = request_token(
            INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(200, good_token_body()), seen)
        )
    assert token.token == SENTINEL_TOKEN
    assert token.full_name == "acme/widgets"
    assert token.default_branch == "main"
    assert token.expires_at == datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)

    [request] = seen
    assert request.method == "POST"
    assert request.url.host == "backend" and request.url.port == 8081
    assert request.url.path == f"/internal/jobs/{JOB_ID}/repository-token"
    assert json.loads(request.content) == {"lease_owner": LEASE_OWNER}

    # Renderings and logs: the token travels in the field and nowhere else.
    for rendered in (repr(token), str(token), f"{token}", f"{token!r}"):
        assert SENTINEL_TOKEN not in rendered
        assert "ghs_" not in rendered
        assert "acme/widgets" in rendered
    logging.getLogger("test").info("got %s", token)
    assert SENTINEL_TOKEN not in caplog.text
    assert "ghs_" not in caplog.text
    assert LEASE_OWNER not in caplog.text


def test_a_marked_404_with_the_fixed_body_is_token_refused() -> None:
    with pytest.raises(TokenRefused) as raised:
        request_token(
            INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(404, {"error": "no_live_lease"}))
        )
    assert LEASE_OWNER not in str(raised.value)
    assert not isinstance(raised.value, InternalApiMisrouted)


def test_an_unmarked_404_shaped_like_chis_is_misrouted() -> None:
    chi = httpx.Response(404, text="404 page not found\n", headers={"content-type": "text/plain; charset=utf-8"})
    with pytest.raises(InternalApiMisrouted) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(chi))
    message = str(raised.value)
    assert "backend:8081" in message, "the host, so an operator knows where the request went"
    assert "404" in message
    assert "/internal/jobs" not in message
    assert LEASE_OWNER not in message
    assert not isinstance(raised.value, TokenRefused), "a misroute must never read as a lost lease"


def test_an_unmarked_200_is_misrouted_too() -> None:
    # A plausible token body from something that is not the route.
    body = good_token_body()
    with pytest.raises(InternalApiMisrouted) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(httpx.Response(200, json=body)))
    assert SENTINEL_TOKEN not in str(raised.value)


def test_a_marked_404_with_another_body_fails_loudly_not_as_a_lost_lease() -> None:
    with pytest.raises(TokenRequestFailed):
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(404, {"error": "something"})))


@pytest.mark.parametrize(
    "reason, expected",
    [("installation_suspended", InstallationSuspended), ("installation_uninstalled", InstallationUninstalled)],
)
def test_each_409_raises_its_own_type(reason: str, expected: type) -> None:
    with pytest.raises(expected) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(409, {"reason": reason})))
    # 22-05 dispatches on the TYPE: neither is the other, neither is a
    # refused lease, and neither is a generic failure.
    assert type(raised.value) is expected
    assert not isinstance(raised.value, (TokenRefused, TokenRequestFailed, InternalApiMisrouted))
    assert not issubclass(InstallationSuspended, InstallationUninstalled)
    assert not issubclass(InstallationUninstalled, InstallationSuspended)


def test_a_409_with_an_unknown_reason_is_a_plain_failure() -> None:
    with pytest.raises(TokenRequestFailed):
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(409, {"reason": "eclipse"})))


@pytest.mark.parametrize("status", [400, 500, 502, 503])
def test_other_marked_statuses_are_plain_failures(status: int) -> None:
    with pytest.raises(TokenRequestFailed) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(status, {"error": "x"})))
    assert str(status) in str(raised.value)


def test_a_redirect_from_the_internal_api_is_misrouted_not_followed() -> None:
    seen: List[httpx.Request] = []
    redirect = httpx.Response(302, headers={"location": "http://elsewhere:9/"})
    with pytest.raises(InternalApiMisrouted):
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(redirect, seen))
    assert len(seen) == 1, "the lease owner must not be re-sent to wherever a redirect points"


def test_a_transport_error_names_the_host_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TokenRequestFailed) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=httpx.MockTransport(handler))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__, "no chained httpx exception"
    assert "backend:8081" in str(raised.value)
    assert "ConnectError" in str(raised.value)


def test_a_200_missing_a_field_names_the_field_not_the_value() -> None:
    body = good_token_body()
    del body["default_branch"]
    with pytest.raises(TokenRequestFailed) as raised:
        request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(marked(200, body)))
    assert "default_branch" in str(raised.value)
    assert SENTINEL_TOKEN not in str(raised.value)


def test_the_lease_owner_and_token_never_reach_a_message_or_a_log(caplog) -> None:
    responses = [
        marked(404, {"error": "no_live_lease"}),
        marked(404, {"error": "other"}),
        marked(409, {"reason": "installation_suspended"}),
        marked(409, {"reason": "installation_uninstalled"}),
        marked(500, text=f"boom {LEASE_OWNER} {SENTINEL_TOKEN}"),
        httpx.Response(404, text=f"404 page not found {LEASE_OWNER}\n"),
        httpx.Response(200, json=good_token_body()),
        marked(200, {"token": SENTINEL_TOKEN}),
    ]
    log = logging.getLogger("worker.test")
    with caplog.at_level(logging.DEBUG):
        for response in responses:
            try:
                request_token(INTERNAL, JOB_ID, LEASE_OWNER, transport=transport_answering(response))
            except Exception as exc:  # noqa: BLE001 - every kind is under test
                assert LEASE_OWNER not in str(exc)
                assert SENTINEL_TOKEN not in str(exc)
                assert LEASE_OWNER not in sanitize_error(exc)
                log.error("job failed: %s", sanitize_error(exc), exc_info=True)
            else:
                pytest.fail(f"expected an exception for {response.status_code}")
    assert LEASE_OWNER not in caplog.text
    assert SENTINEL_TOKEN not in caplog.text


def test_the_job_id_must_be_a_uuid() -> None:
    with pytest.raises(ValueError):
        request_token(INTERNAL, "../admin", LEASE_OWNER, transport=transport_answering(marked(200, good_token_body())))


# ---------------------------------------------------------------------
# The fetcher over a fake GitHub
# ---------------------------------------------------------------------

TOP = f"acme-widgets-{SHA[:7]}"


def make_archive(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{TOP}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def token_for(full_name: str = "acme/widgets", branch: str = "main", token: str = SENTINEL_TOKEN) -> RepositoryToken:
    return RepositoryToken(token=token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1), full_name=full_name, default_branch=branch)


class FakeGitHub:
    """A GitHub API host that redirects tarball downloads to a second host."""

    def __init__(self, archive: bytes, *, download_status: int = 200, download_error: Optional[Exception] = None) -> None:
        self.archive = archive
        self.download_status = download_status
        self.download_error = download_error
        self.requests: List[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.github.test" and path == "/repos/acme/widgets/commits/heads/main":
            assert request.headers["accept"] == "application/vnd.github.sha"
            return httpx.Response(200, text=SHA)
        if host == "api.github.test" and path == f"/repos/acme/widgets/tarball/{SHA}":
            return httpx.Response(
                302, headers={"location": f"https://codeload.github.test{DOWNLOAD_PATH}?token={LINK_SECRET}"}
            )
        if host == "codeload.github.test" and path == DOWNLOAD_PATH:
            if self.download_error is not None:
                raise self.download_error
            if self.download_status != 200:
                return httpx.Response(self.download_status, text=f"denied {LINK_SECRET}")
            return httpx.Response(200, content=self.archive, headers={"content-type": "application/x-gzip"})
        return httpx.Response(404, text=f"not here: {path}")

    def by_host(self, host: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


def test_fetch_resolves_the_sha_and_downloads_the_exact_commit(tmp_path) -> None:
    gh = FakeGitHub(make_archive({"src/a.py": b"print(1)\n", "README.md": b"# hi\n", ".env": b"X=1\n"}))
    with fetch_repository(
        token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
    ) as tree:
        assert tree.sha == SHA
        assert tree.branch == "main"
        assert [(f.path, f.content) for f in tree.files] == [("README.md", "# hi\n"), ("src/a.py", "print(1)\n")]
        assert tree.skipped == {"secret": 1}
        assert tree.download is not None and tree.download.bytes == len(gh.archive)
        assert tree.download.redirect_hosts == ["api.github.test"]
        assert tree.download.final_host == "codeload.github.test"
        assert tree.download.query_param_names == ["token"], "names only, never the value"
        assert tree.extract is not None and tree.extract.top_level == TOP
        assert os.path.isdir(os.path.join(str(tmp_path), JOB_ID)), "the job directory exists inside the block"

    api = gh.by_host("api.github.test")
    assert [r.url.path for r in api] == [
        "/repos/acme/widgets/commits/heads/main",
        f"/repos/acme/widgets/tarball/{SHA}",
    ], "the download is pinned to the SHA, never the branch name"
    assert all(r.headers["authorization"] == f"Bearer {SENTINEL_TOKEN}" for r in api)
    assert all(r.headers["x-github-api-version"] == "2022-11-28" for r in api)


def test_no_authorization_header_crosses_the_redirect_through_the_mock(tmp_path) -> None:
    gh = FakeGitHub(make_archive({"src/a.py": b"#\n"}))
    with fetch_repository(
        token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
    ):
        pass
    [download] = gh.by_host("codeload.github.test")
    assert "authorization" not in download.headers, "the token must not reach the download host"
    assert "authorization" in gh.by_host("api.github.test")[0].headers, "premise: the API request carried it"


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records every request's headers; the class attributes are set per server."""

    redirect_to: str = ""
    archive: bytes = b""
    seen: List[Tuple[str, Dict[str, str]]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/repos/") and "/commits/" in self.path:
            body = SHA.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/repos/") and "/tarball/" in self.path:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-gzip")
            self.send_header("Content-Length", str(len(type(self).archive)))
            self.end_headers()
            self.wfile.write(type(self).archive)

    def log_message(self, *args) -> None:  # silence
        pass


def _serve(handler_cls) -> Tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_no_authorization_header_crosses_the_redirect_over_real_sockets(tmp_path) -> None:
    # Two real servers on two ports of the loopback address: a different
    # port is a different origin, as a different host is, and httpx's rule
    # is measured here on the wire rather than read from its source.
    archive = make_archive({"src/a.py": b"#\n"})

    class Download(_RecordingHandler):
        seen: List[Tuple[str, Dict[str, str]]] = []

    Download.archive = archive
    download_server, download_base = _serve(Download)

    class Api(_RecordingHandler):
        seen: List[Tuple[str, Dict[str, str]]] = []

    Api.redirect_to = f"{download_base}{DOWNLOAD_PATH}?token={LINK_SECRET}"
    api_server, api_base = _serve(Api)
    try:
        with fetch_repository(token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base=api_base) as tree:
            assert [f.path for f in tree.files] == ["src/a.py"]
            assert tree.download is not None
            assert tree.download.final_host == "127.0.0.1"
            assert tree.download.query_param_names == ["token"]
    finally:
        api_server.shutdown()
        download_server.shutdown()

    assert [p for p, _ in Api.seen] == [
        "/repos/acme/widgets/commits/heads/main",
        f"/repos/acme/widgets/tarball/{SHA}",
    ]
    assert all(h.get("authorization") == f"Bearer {SENTINEL_TOKEN}" for _, h in Api.seen), "premise"
    [(download_path, download_headers)] = Download.seen
    assert download_path == f"{DOWNLOAD_PATH}?token={LINK_SECRET}"
    assert "authorization" not in download_headers, (
        f"the token crossed the redirect: {sorted(download_headers)}"
    )


def test_the_download_cap_stops_within_one_chunk(tmp_path) -> None:
    chunk = b"\x1f\x8b" + b"z" * (16 * 1024 - 2)
    pulled: List[int] = []

    def chunks():
        for i in range(64):  # 1 MB on offer
            pulled.append(i)
            yield chunk

    # A generator-backed stream, so the transport hands the body over in
    # chunks and the test can count how many were pulled.
    class GenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks()

    class Streamed(FakeGitHub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == "codeload.github.test":
                self.requests.append(request)
                return httpx.Response(200, stream=GenStream())
            return super().__call__(request)

    gh = Streamed(b"")
    limits = Limits(max_archive_bytes=64 * 1024)
    with pytest.raises(FetchRejected) as raised:
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test",
            transport=gh.transport(), limits=limits,
        ):
            pytest.fail("the block must not run")
    assert "archive exceeds" in raised.value.reason
    assert len(pulled) <= 64 * 1024 // len(chunk) + 1, f"pulled {len(pulled)} chunks past the cap"
    assert not os.path.exists(os.path.join(str(tmp_path), JOB_ID)), "cleaned up after the rejection"


def test_a_declared_content_length_over_the_cap_is_rejected_before_reading(tmp_path) -> None:
    class Declared(FakeGitHub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == "codeload.github.test":
                self.requests.append(request)
                return httpx.Response(200, content=b"x" * 10, headers={"content-length": str(600 * MB)})
            return super().__call__(request)

    with pytest.raises(FetchRejected):
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test",
            transport=Declared(b"").transport(),
        ):
            pytest.fail("the block must not run")


@pytest.mark.parametrize("failure", ["status", "transport"])
def test_a_download_failure_never_leaks_the_link_into_a_message_or_a_log(tmp_path, caplog, failure: str) -> None:
    if failure == "status":
        gh = FakeGitHub(b"", download_status=500)
    else:
        gh = FakeGitHub(b"")
        gh.download_error = httpx.ReadTimeout(f"timed out reading https://codeload.github.test{DOWNLOAD_PATH}?token={LINK_SECRET}")

    log = logging.getLogger("worker.test")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FetchFailed) as raised:
            with fetch_repository(
                token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
            ):
                pytest.fail("the block must not run")
        exc = raised.value
        message = str(exc)
        assert LINK_SECRET not in message
        assert DOWNLOAD_PATH not in message
        assert "codeload.github.test" in message or "api.github.test" in message, "the host is fine"
        assert LINK_SECRET not in sanitize_error(exc)
        # No chained httpx exception with the URL in it: either nothing was
        # chained (the status case raises directly) or the chain was
        # suppressed with `from None` (the transport case).
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__
        # The way the worker logs a failed job.
        log.error("job failed: %s", sanitize_error(exc), exc_info=True)
    assert LINK_SECRET not in caplog.text
    assert DOWNLOAD_PATH not in caplog.text
    assert SENTINEL_TOKEN not in caplog.text
    assert "ghs_" not in caplog.text
    assert not os.path.exists(os.path.join(str(tmp_path), JOB_ID)), "cleaned up after the failure"


def test_the_token_never_reaches_a_log_line_during_a_successful_fetch(tmp_path, caplog) -> None:
    # ⚠ THIS TEST FOUND A REAL LEAK on its first run: the fetcher's own lines
    # were clean, and `httpx` logged the redirect link, `?token=` and all,
    # at INFO. The fetcher now holds httpx's logger at WARNING; this is the
    # test that keeps it so. Something resetting the level to DEBUG first,
    # as an operator's logging config might, must not reopen it.
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    gh = FakeGitHub(make_archive({"src/a.py": b"#\n"}))
    with caplog.at_level(logging.DEBUG):
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
        ) as tree:
            logging.getLogger("worker.test").info("tree %s", tree.skipped)
    assert "fetched acme/widgets@" in caplog.text, "premise: the fetch logged"
    assert SENTINEL_TOKEN not in caplog.text
    assert "ghs_" not in caplog.text
    assert LINK_SECRET not in caplog.text
    assert DOWNLOAD_PATH not in caplog.text


def test_the_job_directory_is_removed_after_the_block(tmp_path) -> None:
    gh = FakeGitHub(make_archive({"src/a.py": b"#\n"}))
    jobdir = os.path.join(str(tmp_path), JOB_ID)
    with fetch_repository(
        token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
    ) as tree:
        assert os.path.isdir(jobdir)
        assert tree.files
    # Asserted AFTER the block, never inside a finally.
    assert not os.path.lexists(jobdir)
    assert os.listdir(str(tmp_path)) == []


def test_the_job_directory_is_removed_when_the_block_raises(tmp_path) -> None:
    gh = FakeGitHub(make_archive({"src/a.py": b"#\n"}))
    jobdir = os.path.join(str(tmp_path), JOB_ID)

    class HandlerBlewUp(RuntimeError):
        pass

    with pytest.raises(HandlerBlewUp):
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test", transport=gh.transport()
        ):
            assert os.path.isdir(jobdir)
            raise HandlerBlewUp("parser died")
    assert not os.path.lexists(jobdir)


def test_a_failed_head_resolution_is_a_plain_failure(tmp_path) -> None:
    class NoBranch(FakeGitHub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "/commits/" in request.url.path:
                return httpx.Response(404, json={"message": "Not Found"})
            return super().__call__(request)

    with pytest.raises(FetchFailed) as raised:
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test",
            transport=NoBranch(b"").transport(),
        ):
            pytest.fail("the block must not run")
    assert "acme/widgets@main" in str(raised.value)
    assert "404" in str(raised.value)


def test_a_short_sha_from_the_api_is_refused(tmp_path) -> None:
    class ShortSha(FakeGitHub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "/commits/" in request.url.path:
                return httpx.Response(200, text=SHA[:12])
            return super().__call__(request)

    with pytest.raises(FetchFailed):
        with fetch_repository(
            token_for(), job_id=JOB_ID, workdir=str(tmp_path), api_base="https://api.github.test",
            transport=ShortSha(b"").transport(),
        ):
            pytest.fail("the block must not run")


def test_sweep_stale_workdirs_removes_only_old_job_directories(tmp_path) -> None:
    old_job = os.path.join(str(tmp_path), str(uuid.uuid4()))
    fresh_job = os.path.join(str(tmp_path), str(uuid.uuid4()))
    not_ours = os.path.join(str(tmp_path), "not-a-job")
    for path in (old_job, fresh_job, not_ours):
        os.makedirs(path)
        with open(os.path.join(path, "x"), "w", encoding="utf-8") as fh:
            fh.write("x")
    two_days_ago = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(old_job, (two_days_ago, two_days_ago))
    os.utime(not_ours, (two_days_ago, two_days_ago))

    removed = sweep_stale_workdirs(str(tmp_path), older_than=timedelta(hours=1))
    assert removed == 1
    assert not os.path.exists(old_job)
    assert os.path.exists(fresh_job)
    assert os.path.exists(not_ours), "only UUID-named directories are ours to remove"
    assert sweep_stale_workdirs(os.path.join(str(tmp_path), "absent"), timedelta(hours=1)) == 0
