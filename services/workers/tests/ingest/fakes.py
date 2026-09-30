"""Fakes for the ingest handler's NETWORK EDGES, shared by its two test files.

Only the edges are faked: the backend's internal token route and GitHub are
`httpx.MockTransport`s that the REAL `request_token`, `fetch_repository` and
`revoke_token` talk to, and the embedding API is a deterministic function
standing in for one HTTP call. The chunker, the fetcher's extraction and
filters, the writer and the runtime are the real ones.

⚠ THE TOKEN IS A SENTINEL with the measured shape (`ghs_` plus 383
characters, 20-02), so every "never logged" assertion searches for a string
that cannot occur by accident.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import tarfile
from typing import Callable, Dict, List, Optional

import httpx

from workers.fetch import MARKER_HEADER, MARKER_VALUE, REFUSED_BODY
from workers.storage.postgres_writer import content_hash

SENTINEL_TOKEN = "ghs_" + ("IngestS3ntinelT0ken" * 24)[:383]
assert len(SENTINEL_TOKEN) == 4 + 383

INTERNAL = "http://backend:8081"
GITHUB_API = "https://api.github.test"
FULL_NAME = "acme/widgets"
BRANCH = "main"
SHA = "3f7c2a9e5b1d4c6f8a0e2b4d6f8a1c3e5b7d9f01"
TOP = f"acme-widgets-{SHA[:7]}"
LINK_SECRET = "LINKSECRET-ingest-must-never-be-logged"
DOWNLOAD_PATH = f"/acme/widgets/legacy.tar.gz/{SHA}"

DIMENSIONS = 1536
TEST_MODEL = "test-fixed"

#: Three Python files and a `.env`. Each Python file has words the others do
#: not, so a bag-of-words vector for a question about one of them lands
#: nearest that file. The `.env` must never be written, sent or stored.
FIXTURE_FILES: Dict[str, bytes] = {
    "app/greeting.py": (
        b'def greet(name):\n'
        b'    """Say hello to a visitor by name, politely."""\n'
        b'    return f"Hello, {name}!"\n'
    ),
    "app/billing.py": (
        b"def compute_invoice_total(items, tax_rate):\n"
        b'    """Sum the invoice line items and apply the sales tax rate."""\n'
        b"    subtotal = sum(item.price * item.quantity for item in items)\n"
        b"    return round(subtotal * (1 + tax_rate), 2)\n"
    ),
    "app/storage.py": (
        b"import os\n"
        b"\n"
        b"\n"
        b"class BlobStore:\n"
        b'    """Keeps uploaded blobs as files in a directory on disk."""\n'
        b"\n"
        b"    def __init__(self, root):\n"
        b"        self.root = root\n"
        b"\n"
        b"    def put(self, key, data):\n"
        b"        path = os.path.join(self.root, key)\n"
        b'        with open(path, "wb") as handle:\n'
        b"            handle.write(data)\n"
        b"        return path\n"
    ),
    ".env": b"SECRET_KEY=must-never-be-indexed-or-embedded\n",
}


def make_archive(files: Dict[str, bytes], top: str = TOP) -> bytes:
    """A gzip tar laid out as GitHub lays one out: everything under `top/`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def text_vector(text: str) -> List[float]:
    """A deterministic unit vector: words hashed into dimensions (bag of words).

    Stands in for the embedding API. Two texts sharing distinctive words are
    near each other, which is all a search assertion here needs.
    """
    vector = [0.0] * DIMENSIONS
    for word in re.findall(r"[a-z]+", text.lower()):
        index = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:8], 16) % DIMENSIONS
        vector[index] += 1.0
    if not any(vector):
        vector[0] = 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class FakeEmbedder:
    """The generator's interface, embedding with `text_vector`. Records calls."""

    model = TEST_MODEL

    def __init__(self, before_call: Optional[Callable[[int], None]] = None) -> None:
        self.calls: List[List[str]] = []
        self._before_call = before_call

    def generate_embeddings_for_chunks(self, chunks, use_cache: bool = True):
        if self._before_call is not None:
            self._before_call(len(self.calls))
        self.calls.append([chunk.content for chunk in chunks])
        return {content_hash(chunk.content): text_vector(chunk.content) for chunk in chunks}


def marked(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body, headers={MARKER_HEADER: MARKER_VALUE})


class FakeTokenRoute:
    """The backend's internal token route, answering one way every time.

    `answer` is one of: `ok` (a marked 200 with the sentinel token),
    `refused` (THE marked 404), `suspended` / `uninstalled` (the marked
    409s) or `misrouted` (chi's UNMARKED `404 page not found`, which is
    what the public router answers when `INTERNAL_API_URL` points at it).
    """

    def __init__(self, answer: str = "ok", token: str = SENTINEL_TOKEN) -> None:
        self.answer = answer
        self.token = token
        self.requests: List[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answer == "ok":
            return marked(
                200,
                {
                    "token": self.token,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "full_name": FULL_NAME,
                    "default_branch": BRANCH,
                },
            )
        if self.answer == "refused":
            return marked(404, REFUSED_BODY)
        if self.answer == "suspended":
            return marked(409, {"reason": "installation_suspended"})
        if self.answer == "uninstalled":
            return marked(409, {"reason": "installation_uninstalled"})
        if self.answer == "misrouted":
            return httpx.Response(404, text="404 page not found\n")
        raise AssertionError(f"unknown answer {self.answer!r}")


class FakeGitHub:
    """GitHub's API host plus the download host it redirects tarballs to.

    Answers the head resolution, the tarball (a 302 to a link carrying a
    `?token=` of its own, as a private repository's may) and the download,
    and records the token revocation (`DELETE /installation/token`) with
    the status `revoke_status`, or raises `revoke_error` for it.
    """

    def __init__(
        self,
        archive: bytes,
        *,
        revoke_status: int = 204,
        revoke_error: Optional[Exception] = None,
    ) -> None:
        self.archive = archive
        self.revoke_status = revoke_status
        self.revoke_error = revoke_error
        self.requests: List[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.github.test" and request.method == "DELETE" and path == "/installation/token":
            if self.revoke_error is not None:
                raise self.revoke_error
            return httpx.Response(self.revoke_status)
        if host == "api.github.test" and path == f"/repos/{FULL_NAME}/commits/heads/{BRANCH}":
            return httpx.Response(200, text=SHA)
        if host == "api.github.test" and path == f"/repos/{FULL_NAME}/tarball/{SHA}":
            return httpx.Response(
                302,
                headers={"location": f"https://codeload.github.test{DOWNLOAD_PATH}?token={LINK_SECRET}"},
            )
        if host == "codeload.github.test" and path == DOWNLOAD_PATH:
            return httpx.Response(200, content=self.archive, headers={"content-type": "application/x-gzip"})
        return httpx.Response(404, text=f"not here: {path}")

    def revocations(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == "DELETE" and r.url.path == "/installation/token"]

    def downloads(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == "codeload.github.test"]
