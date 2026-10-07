"""22.1-05's measurement harness: one real ingest, timed stage by stage.

`22.1-05-PLAN.md`, Task 1, and `22.1-05-operating-numbers.md`, "Rules" --
the measured quantities are defined there; this script produces them.

IT STARTS NOTHING ITSELF. It is given a scratch database (`scratch_db.py
up`: migrated, `rag_doc_app` LOGIN NOSUPERUSER NOBYPASSRLS with the
isolation harness's grants), a repository `full_name`, a pinned SHA, the
repository's default branch and a repository-row label (so copies of one
repository can be separate rows, for N4's confirmation).

WHAT IS REAL. The `Worker` (production lease, beat, heartbeat
`statement_timeout`, `max_job_duration`), the `full_ingest` handler, the
fetcher (download, extraction, caps, filters), `SemanticChunker`, the
`EmbeddingGenerator` and its OpenAI client, `PostgresWriter`, every
transition -- all as `rag_doc_app`.

WHAT IS INJECTED, AT THE EDGES ONLY (`IngestDeps`' two transports):
- the backend's token route answers a MARKED 200 (`X-Rag-Internal:
  repository-token/1`) with a fixed, non-secret dummy token for the target
  `full_name`: the public repositories need no App;
- GitHub's API answers `GET /repos/{full_name}/commits/heads/{branch}` with
  the PINNED SHA (`resolve_head` asks for `heads/{branch}`, so a SHA cannot
  be passed as the branch), answers the tarball request with the redirect
  GitHub's API gives (to `codeload.github.com/{full_name}/legacy.tar.gz/
  {sha}`), synthesized locally so the unauthenticated API's 60-an-hour
  limit cannot fail a run, and answers the token revocation with `204`;
- the download itself goes to the real `codeload.github.com`, WITHOUT
  `Authorization`, and the end of its body is timestamped (informational).

WHAT IS MEASURED (each into one JSON record per run):
- stage times from the runtime's own lines (claim, `stage=` x4, complete);
- the archive's bytes (fetch's line) and the expanded bytes (the sum of the
  fetched files' sizes, recorded by a pass-through chunker);
- every `HEARTBEAT_SQL` execute on the heartbeat connection and every
  statement on the loop connection, timed CLIENT-SIDE by a cursor subclass
  handed to the real `psycopg2.connect` (`cursor_factory=`), including the
  ones that raise, with each exception's `pgcode`;
- beat outcomes two ways (the `57014` count from the cursor; the log records
  holding both "heartbeat failed" and "QueryCanceled"), and every "assuming
  it is lost" line;
- every OpenAI HTTP response through an `httpx` event hook: status,
  latency, `usage.total_tokens`, the `x-ratelimit-limit-*` headers (names
  and numbers only), and EVERY 429, including the ones the SDK retries
  silently;
- peak RSS (`ru_maxrss`) and peak working-directory bytes (sampled 0.5 s).

SPENDING. Embedding runs go through the one ledger (`ledger.py`): the run
is reserved before it starts, every request is guarded by an upper bound
on its tokens, and every response's tokens are recorded. `--no-embed`
REFUSES TO START if `OPENAI_API_KEY` is in its environment, builds the
generator with a fixed non-secret dummy key and replaces its SDK client
with one that raises on any use; it counts the tokens embedding WOULD send
(the generator's own `_prepare_text_for_embedding` and tokenizer, over the
DISTINCT texts, as the handler deduplicates) and stops the job at `embed`.

NOTHING HERE PRINTS A KEY, A TOKEN OR A DSN.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import pathlib
import platform
import random
import re
import resource
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import httpx
import psycopg2
import psycopg2.extensions

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))  # services/workers

from ledger import Ledger, LedgerRefused, tokens_to_usd  # noqa: E402

from workers.db import require_tenant  # noqa: E402
from workers.fetch import MARKER_HEADER, MARKER_VALUE  # noqa: E402
from workers.ingest import IngestDeps, make_full_ingest_handler  # noqa: E402
from workers.jobs import runtime as runtime_module  # noqa: E402
from workers.jobs import transitions as transitions_module  # noqa: E402
from workers.jobs.runtime import (  # noqa: E402
    DEFAULT_MAX_JOB_DURATION,
    HEARTBEAT_APPLICATION_NAME,
    HEARTBEAT_SQL,
    LOOP_APPLICATION_NAME,
    Worker,
)
from workers.jobs.transitions import ENQUEUE_UPSERT_SQL  # noqa: E402
from workers.storage import postgres_writer as writer_module  # noqa: E402

DUMMY_TOKEN = "measure-dummy-not-a-token"
DRY_RUN_KEY = "dry-run-not-a-key"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
CHUNKS_PARTITIONS = 64


# =====================================================================
# Statement timing: a cursor subclass handed to the real connect
# =====================================================================

def _statement_label(query: Any) -> str:
    """Name a statement the worker runs, by identity or by its text."""
    if query is HEARTBEAT_SQL:
        return "heartbeat"
    text = query.decode("utf-8", "replace") if isinstance(query, (bytes, bytearray)) else str(query)
    head = " ".join(text[:400].split())
    fence = getattr(transitions_module, "FENCE_CHECK_SQL", None)
    known = [
        ("heartbeat", HEARTBEAT_SQL),
        ("fence_check", fence),
        ("resolve_run", transitions_module.RESOLVE_RUN_SQL),
        ("attach_run", transitions_module.ATTACH_RUN_SQL),
        ("clear_rerun", transitions_module.CLEAR_RERUN_SQL),
        ("complete", transitions_module.COMPLETE_SQL),
        ("project_synced", transitions_module.PROJECT_SYNCED_SQL),
        ("delete_chunks", writer_module.DELETE_REPOSITORY_CHUNKS_SQL),
        ("complete_run", writer_module.COMPLETE_RUN_SQL),
        ("progress", runtime_module.PROGRESS_SQL),
    ]
    for label, sql in known:
        if sql and head.startswith(" ".join(sql.split())[:60]):
            return label
    if head.startswith("INSERT INTO chunks"):
        return "insert_chunks"
    if head.startswith("SET LOCAL app.current_tenant"):
        return "set_tenant"
    return "other"


class Timings:
    """Every timed execute on the worker's two connections. Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.beats: List[dict] = []
        self.loop: List[dict] = []
        self.t0 = time.time()

    def add(self, app: str, label: str, started: float, seconds: float, pgcode: Optional[str],
            error: Optional[str]) -> None:
        entry = {"label": label, "at": round(started - self.t0, 4), "seconds": round(seconds, 6)}
        if pgcode or error:
            entry["pgcode"] = pgcode
            entry["error"] = error
        with self.lock:
            if app == HEARTBEAT_APPLICATION_NAME and label == "heartbeat":
                self.beats.append(entry)
            elif app.startswith(LOOP_APPLICATION_NAME) and app != HEARTBEAT_APPLICATION_NAME:
                self.loop.append(entry)


TIMINGS = Timings()


class TimingCursor(psycopg2.extensions.cursor):
    """Times every `execute`, the ones that raise included (until the raise).

    psycopg2's cursors are C types, so `execute` cannot be patched on an
    instance; a subclass handed to the real `connect` as `cursor_factory`
    is how the heartbeat (`_unscoped(conn)`) and `complete()`
    (`require_tenant(conn, ...)`), which use the connection's default
    factory, are both timed. The claim's `RealDictCursor` is not.
    """

    def execute(self, query, vars=None):  # noqa: A002 - psycopg2's name
        app = _app_name(self.connection)
        started_wall = time.time()
        started = time.perf_counter()
        pgcode = error = None
        try:
            return super().execute(query, vars)
        except psycopg2.Error as exc:
            pgcode = exc.pgcode
            error = type(exc).__name__
            raise
        finally:
            if app:
                TIMINGS.add(app, _statement_label(query), started_wall,
                            time.perf_counter() - started, pgcode, error)


_APP_NAMES: Dict[int, str] = {}


def _app_name(conn: Any) -> str:
    key = id(conn)
    name = _APP_NAMES.get(key)
    if name is None:
        try:
            name = conn.info.dsn_parameters.get("application_name", "") or ""
        except Exception:  # noqa: BLE001
            name = ""
        _APP_NAMES[key] = name
    return name


_REAL_CONNECT = psycopg2.connect


def timed_connect(*args: Any, **kwargs: Any):
    """`psycopg2.connect`, with the timing cursor as the default factory."""
    kwargs.setdefault("cursor_factory", TimingCursor)
    conn = _REAL_CONNECT(*args, **kwargs)
    _APP_NAMES.pop(id(conn), None)
    return conn


def install_statement_timing() -> None:
    psycopg2.connect = timed_connect
    runtime_module.psycopg2.connect = timed_connect  # the same module object; explicit


# =====================================================================
# Log capture
# =====================================================================

class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: List[logging.LogRecord] = []
        self.lock_ = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self.lock_:
            self.records.append(record)

    def lines(self) -> List[dict]:
        with self.lock_:
            out = []
            for r in self.records:
                try:
                    msg = r.getMessage()
                except Exception:  # noqa: BLE001
                    msg = str(r.msg)
                out.append({"t": r.created, "level": r.levelname, "logger": r.name, "msg": msg})
            return out


# =====================================================================
# The network edges
# =====================================================================

def marked(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body, headers={MARKER_HEADER: MARKER_VALUE})


class TokenRoute:
    """The backend's internal route: a MARKED 200 with a dummy token."""

    def __init__(self, full_names: Dict[str, str], branch: str) -> None:
        # job_id -> full_name (one per job, so copies can share a process)
        self.full_names = full_names
        self.branch = branch
        self.requests = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        m = re.match(r"^/internal/jobs/([0-9a-f-]{36})/repository-token$", request.url.path)
        if request.method != "POST" or not m or m.group(1) not in self.full_names:
            return httpx.Response(404, text="404 page not found\n")
        return marked(200, {
            "token": DUMMY_TOKEN,
            "expires_at": "2099-01-01T00:00:00Z",
            "full_name": self.full_names[m.group(1)],
            "default_branch": self.branch,
        })


class _TimedStream(httpx.SyncByteStream):
    def __init__(self, inner: Any, on_end: Callable[[int], None]) -> None:
        self.inner = inner
        self.on_end = on_end

    def __iter__(self):
        total = 0
        for part in self.inner:
            total += len(part)
            yield part
        self.on_end(total)

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close:
            close()


class PublicGitHub(httpx.BaseTransport):
    """GitHub for a PUBLIC repository at a PINNED commit, without the App."""

    API_HOST = "api.github.com"
    DOWNLOAD_HOST = "codeload.github.com"

    def __init__(self, full_name: str, branch: str, sha: str,
                 local_archive: Optional[bytes] = None) -> None:
        self.full_name = full_name
        self.branch = branch
        self.sha = sha
        self.local_archive = local_archive
        self.real = httpx.HTTPTransport(retries=2)
        self.events: List[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        if host == self.API_HOST and method == "DELETE" and path == "/installation/token":
            self.events.append({"event": "revoke", "t": time.time()})
            return httpx.Response(204)
        if host == self.API_HOST and path == f"/repos/{self.full_name}/commits/heads/{self.branch}":
            self.events.append({"event": "resolve_head", "t": time.time()})
            return httpx.Response(200, text=self.sha)
        if host == self.API_HOST and path == f"/repos/{self.full_name}/tarball/{self.sha}":
            self.events.append({"event": "tarball_redirect", "t": time.time()})
            return httpx.Response(302, headers={
                "location": f"https://{self.DOWNLOAD_HOST}/{self.full_name}/legacy.tar.gz/{self.sha}"
            })
        if host == self.DOWNLOAD_HOST and path == f"/{self.full_name}/legacy.tar.gz/{self.sha}":
            started = time.time()
            self.events.append({"event": "download_start", "t": started})
            if self.local_archive is not None:
                self.events.append({"event": "download_end", "t": time.time(),
                                    "bytes": len(self.local_archive), "local": True})
                return httpx.Response(200, content=self.local_archive,
                                      headers={"content-type": "application/x-gzip"})
            headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"authorization"]
            forwarded = httpx.Request(method, request.url, headers=headers)
            response = self.real.handle_request(forwarded)

            def ended(total: int) -> None:
                self.events.append({"event": "download_end", "t": time.time(), "bytes": total})

            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=_TimedStream(response.stream, ended),
                                  extensions=response.extensions)
        return httpx.Response(404, text=f"not served by the measurement transport: {host}")


# =====================================================================
# Pass-through chunker and embedders
# =====================================================================

class MeasuringChunker:
    """The real chunker, recording each file's bytes and chunk count."""

    def __init__(self, real: Any, tag_copies: bool = False) -> None:
        self.real = real
        self.tag_copies = tag_copies
        self.files = 0
        self.bytes = 0
        self.chunks = 0
        self.by_language: Dict[str, int] = {}

    def chunk_file(self, path: str, content: str, language: str):
        produced = self.real.chunk_file(path, content, language)
        self.files += 1
        self.bytes += len(content.encode("utf-8"))
        self.chunks += len(produced)
        self.by_language[language] = self.by_language.get(language, 0) + len(produced)
        if self.tag_copies:
            # at_cap.py's synthetic archives only: copies of one repository
            # under `copyNN/`. One comment line naming the copy, appended
            # AFTER the real chunker ran, makes each copy's chunk texts
            # distinct, as a real repository's are (see at_cap.py).
            m = re.match(r"^(copy\d+)/", path)
            if m:
                for chunk in produced:
                    chunk.content = f"{chunk.content}\n# {m.group(1)}"
        return produced


class DryRunStop(Exception):
    """`--no-embed`: the tokens are counted; the job stops at `embed`."""


class _Forbidden:
    """Stands in for the SDK object: every attribute access raises."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"--no-embed: the OpenAI SDK was touched ({name}); nothing may be sent")


class DryRunEmbedder:
    def __init__(self) -> None:
        if os.environ.get(OPENAI_API_KEY_ENV):
            raise SystemExit("--no-embed refuses to start: OPENAI_API_KEY is in its environment")
        from workers.embeddings import EmbeddingGenerator

        self.generator = EmbeddingGenerator(api_key=DRY_RUN_KEY)
        self.generator.client.client = _Forbidden()
        self.model = self.generator.model
        self.distinct = 0
        self.tokens = 0
        self.max_tokens_one = 0

    def tokens_over_limit(self, chunk):
        """The generator's own truncation test (22.2-02), local tokenizer only."""
        return self.generator.tokens_over_limit(chunk)

    def generate_embeddings_for_chunks(self, chunks, use_cache: bool = True):
        client = self.generator.client
        for chunk in chunks:
            text = self.generator._prepare_text_for_embedding(chunk)
            if not text or not text.strip():
                text = "[empty]"  # as generate_embeddings_batch sends it
            n = client.count_tokens(text)
            self.tokens += n
            self.max_tokens_one = max(self.max_tokens_one, n)
        self.distinct += len(chunks)
        raise DryRunStop(f"dry run: {len(chunks)} distinct chunks, {self.tokens} tokens counted")


class OpenAIRecorder:
    """The `httpx` event hooks on the OpenAI client. Every response, every 429."""

    def __init__(self, ledger: Ledger, label: str) -> None:
        self.ledger = ledger
        self.label = label
        self.lock = threading.Lock()
        self.calls: List[dict] = []
        self.started: Dict[int, float] = {}
        self.limits: Dict[str, str] = {}

    def on_request(self, request: httpx.Request) -> None:
        body = request.content or b""
        # A cl100k token is at least one byte, so the body's length bounds the
        # tokens this request can be billed for.
        self.ledger.guard(len(body), self.label)
        with self.lock:
            self.started[id(request)] = time.perf_counter()

    def on_response(self, response: httpx.Response) -> None:
        response.read()
        ended = time.perf_counter()
        with self.lock:
            started = self.started.pop(id(response.request), ended)
        tokens = None
        if response.status_code == 200:
            try:
                tokens = int(response.json().get("usage", {}).get("total_tokens"))
            except Exception:  # noqa: BLE001
                tokens = None
        headers = {}
        for name in ("x-ratelimit-limit-tokens", "x-ratelimit-limit-requests",
                     "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-requests"):
            value = response.headers.get(name)
            if value is not None and re.fullmatch(r"\d+", value.strip()):
                headers[name] = int(value.strip())
        entry = {"t": time.time(), "status": response.status_code,
                 "seconds": round(ended - started, 4), "tokens": tokens, **headers}
        with self.lock:
            self.calls.append(entry)
            for name in ("x-ratelimit-limit-tokens", "x-ratelimit-limit-requests"):
                if name in headers:
                    self.limits[name] = str(headers[name])
        if tokens:
            self.ledger.record(tokens, self.label)

    def client(self, api_key: str):
        from openai import OpenAI

        http = httpx.Client(
            event_hooks={"request": [self.on_request], "response": [self.on_response]},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        return OpenAI(api_key=api_key, http_client=http)

    def summary(self) -> dict:
        with self.lock:
            calls = list(self.calls)
        return {
            "calls": len(calls),
            "status_counts": _count(c["status"] for c in calls),
            "http_429": [c["t"] for c in calls if c["status"] == 429],
            "tokens": sum(c["tokens"] or 0 for c in calls),
            "limits": dict(self.limits),
            "latency_seconds": _stats([c["seconds"] for c in calls if c["status"] == 200]),
        }


def real_embedder(recorder: OpenAIRecorder):
    from workers.embeddings import EmbeddingGenerator

    key = os.environ.get(OPENAI_API_KEY_ENV, "").strip()
    if not key:
        raise SystemExit("an embedding run needs OPENAI_API_KEY in its environment")
    generator = EmbeddingGenerator(api_key=key)
    generator.client.client = recorder.client(key)
    return generator


class ReplayEmbedder:
    """The at-cap runs: exported REAL vectors replayed, cycled. No OpenAI.

    On every pass through the export after the first, each vector is
    perturbed deterministically (seeded by its position; noise 0.01 per
    dimension, then re-normalised), so no two rows share a vector: pgvector's
    HNSW stores identical vectors as ONE element with several heap TIDs,
    which would make the store cheaper than a real repository's.
    """

    def __init__(self, vectors: Any, model: str = "replay-of-text-embedding-ada-002") -> None:
        self.vectors = vectors
        self.model = model
        self.next = 0
        self.distinct = 0
        self.perturbed = 0
        from workers.embeddings import EmbeddingGenerator

        # Only for `tokens_over_limit` (22.2-02's truncation count); its SDK
        # client is replaced, so nothing can reach OpenAI.
        self._generator = EmbeddingGenerator(api_key=DRY_RUN_KEY)
        self._generator.client.client = _Forbidden()

    def tokens_over_limit(self, chunk):
        return self._generator.tokens_over_limit(chunk)

    def generate_embeddings_for_chunks(self, chunks, use_cache: bool = True):
        import numpy as np

        from workers.storage.postgres_writer import content_hash

        out = {}
        n = len(self.vectors)
        for chunk in chunks:
            base = np.asarray(self.vectors[self.next % n], dtype=np.float64)
            if self.next >= n:
                noise = np.random.default_rng(self.next).standard_normal(base.shape[0]) * 0.01
                base = base + noise
                base = base / np.linalg.norm(base)
                self.perturbed += 1
            out[content_hash(chunk.content)] = base.tolist()
            self.next += 1
        self.distinct += len(chunks)
        return out


# =====================================================================
# Samplers
# =====================================================================

class DiskSampler(threading.Thread):
    """Peak bytes under the working directory, every 0.5 s."""

    def __init__(self, path: str, interval: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.interval = interval
        self.peak = 0
        self.samples = 0
        self.halt = threading.Event()

    def _size(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
        return total

    def run(self) -> None:
        while not self.halt.is_set():
            try:
                self.peak = max(self.peak, self._size())
                self.samples += 1
            except Exception:  # noqa: BLE001
                pass
            self.halt.wait(self.interval)


def peak_rss_bytes() -> int:
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(kb) * 1024 if sys.platform != "darwin" else int(kb)


# =====================================================================
# Seeding
# =====================================================================

def partition_of(conn: Any, org_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r FROM generate_series(0, %s) r "
            "WHERE satisfies_hash_partition('public.chunks'::regclass, %s, r, %s::uuid)",
            (CHUNKS_PARTITIONS - 1, CHUNKS_PARTITIONS, org_id),
        )
        return int(cur.fetchone()[0])


def occupied_partitions(super_conn: Any) -> Dict[int, int]:
    with super_conn.cursor() as cur:
        cur.execute("SELECT tableoid::regclass::text, count(*) FROM chunks GROUP BY 1")
        rows = cur.fetchall()
    return {int(name.rsplit("_p", 1)[1]): n for name, n in rows}


def organization_in_an_empty_partition(app: Any, super_conn: Any, avoid: set) -> tuple:
    """An organization id whose chunks partition holds nothing yet."""
    busy = set(occupied_partitions(super_conn)) | set(avoid)
    for _ in range(10_000):
        candidate = str(uuid.uuid4())
        r = partition_of(app, candidate)
        if r not in busy:
            return candidate, r
    raise RuntimeError("no empty chunks partition left")


@dataclass
class Seeded:
    org: str
    project: str
    installation: str
    repo: str
    job: str
    partition: int
    label: str
    full_name: str


def seed(app: Any, super_conn: Any, full_name: str, branch: str, label: str,
         avoid: set) -> Seeded:
    """Organization, project, installation, repository and job, as rag_doc_app."""
    org, partition = organization_in_an_empty_partition(app, super_conn, avoid)
    slug = f"measure-{label}-{uuid.uuid4().hex[:6]}".lower()
    with app.cursor() as cur:
        cur.execute("INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
                    (org, f"Measure {label}", slug))
        cur.execute(
            "INSERT INTO projects (organization_id, name, slug, is_default) "
            "VALUES (%s, 'Default', %s, true) RETURNING id::text", (org, f"{slug}-default"))
        project = cur.fetchone()[0]
    app.commit()
    with require_tenant(app, org) as cur:
        cur.execute(
            "INSERT INTO github_installations (organization_id, github_installation_id, "
            "account_login, account_type, repository_selection) "
            "VALUES (%s, %s, %s, 'Organization', 'selected') RETURNING id::text",
            (org, random.randint(10**8, 10**9), full_name.split("/")[0]))
        installation = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO repositories (project_id, name, git_url, default_branch, installation_id, "
            "github_repo_id, visibility) VALUES (%s, %s, %s, %s, %s, %s, 'public') RETURNING id::text",
            (project, label, f"https://github.com/{full_name}", branch, installation,
             random.randint(10**9, 2 * 10**9)))
        repo = cur.fetchone()[0]
    return Seeded(org, project, installation, repo, "", partition, label, full_name)


def enqueue(app: Any, s: Seeded, job_type: str) -> str:
    with require_tenant(app, s.org) as cur:
        cur.execute(ENQUEUE_UPSERT_SQL, (s.org, s.repo, job_type))
        job_id, _existing = cur.fetchone()
        cur.execute("UPDATE repositories SET sync_state = 'pending', updated_at = NOW() WHERE id = %s",
                    (s.repo,))
    return job_id


def job_row(app: Any, job_id: str) -> dict:
    with app.cursor() as cur:
        cur.execute(
            "SELECT state, attempts, last_stage, last_error, lease_owner, progress "
            "FROM ingestion_jobs WHERE id = %s", (job_id,))
        state, attempts, stage, error, owner, progress = cur.fetchone()
    app.rollback()
    return {"state": state, "attempts": attempts, "last_stage": stage, "last_error": error,
            "lease_owner": owner, "progress": progress}


def settled(row: dict) -> bool:
    if row["state"] in ("completed", "dead", "superseded"):
        return True
    return row["state"] == "queued" and row["last_error"] is not None and row["lease_owner"] is None


# =====================================================================
# Stage times from the runtime's own lines
# =====================================================================

def stage_times(lines: List[dict], job_id: str) -> dict:
    prefix = f"job {job_id}: "
    marks: Dict[str, float] = {}
    for line in lines:
        msg = line["msg"]
        if not msg.startswith(prefix):
            continue
        rest = msg[len(prefix):]
        if rest.startswith("claim queued->running") and "claim" not in marks:
            marks["claim"] = line["t"]
        m = re.match(r"stage=(\w+) ", rest)
        if m and m.group(1) not in marks:
            marks[m.group(1)] = line["t"]
        if rest.startswith("complete running->completed"):
            marks["completed"] = line["t"]
    order = ["claim", "fetch", "parse", "embed", "store", "completed"]
    out: Dict[str, Any] = {"marks": {k: marks[k] for k in order if k in marks}}
    seconds = {}
    for a, b, name in (("claim", "fetch", "claim_to_fetch"), ("fetch", "parse", "fetch"),
                       ("parse", "embed", "parse"), ("embed", "store", "embed"),
                       ("store", "completed", "store"), ("claim", "completed", "claim_to_completion")):
        if a in marks and b in marks:
            seconds[name] = round(marks[b] - marks[a], 4)
    out["seconds"] = seconds
    return out


def _count(values) -> dict:
    out: Dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _stats(values: List[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "min": ordered[0], "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 6),
            "p50": ordered[len(ordered) // 2], "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]}


def statement_summary(entries: List[dict]) -> dict:
    by: Dict[str, List[dict]] = {}
    for e in entries:
        by.setdefault(e["label"], []).append(e)
    return {
        label: {"count": len(items), "total_seconds": round(sum(i["seconds"] for i in items), 4),
                "max_seconds": max(i["seconds"] for i in items),
                "first_at": items[0]["at"], "last_end_at": round(max(i["at"] + i["seconds"] for i in items), 4),
                "errors": _count(i.get("pgcode") for i in items if i.get("pgcode") or i.get("error"))}
        for label, items in by.items()
    }


def beat_outcomes(lines: List[dict], job_ids: List[str]) -> dict:
    with TIMINGS.lock:
        beats = list(TIMINGS.beats)
    log_cancelled = sum(1 for l in lines if "heartbeat failed" in l["msg"] and "QueryCanceled" in l["msg"])
    false_loss = sum(1 for l in lines if "assuming it is lost" in l["msg"])
    return {
        "beats": beats,
        "beats_total": len(beats),
        "beats_57014": sum(1 for b in beats if b.get("pgcode") == "57014"),
        "beats_raised": sum(1 for b in beats if b.get("error")),
        "log_heartbeat_failed_querycanceled": log_cancelled,
        "log_assuming_it_is_lost": false_loss,
        "max_beat_seconds": max((b["seconds"] for b in beats), default=None),
    }


# =====================================================================
# One run
# =====================================================================

def host_facts(app: Any) -> dict:
    with app.cursor() as cur:
        cur.execute("SHOW server_version")
        server = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        vector = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_conn = int(cur.fetchone()[0])
    app.rollback()
    cpus = os.cpu_count()
    mem = None
    try:
        with open("/proc/meminfo") as fh:
            mem = int(fh.readline().split()[1]) * 1024
    except OSError:
        pass
    return {"hostname": socket.gethostname(), "platform": platform.platform(),
            "python": platform.python_version(), "cpus": cpus, "mem_bytes": mem,
            "postgres": server, "pgvector": vector, "max_connections": max_conn,
            "docker_host_note": os.environ.get("MEASURE_HOST_NOTE", "")}


def run_workers(app_dsn: str, handler: Any, count: int, deadline_seconds: float,
                app: Any, job_ids: List[str]) -> tuple:
    """Start `count` real Workers (production settings), wait until every job settles."""
    stops, threads, workers = [], [], []
    for _ in range(count):
        w = Worker(app_dsn, {"full_ingest": handler, "incremental": handler},
                   max_job_duration=DEFAULT_MAX_JOB_DURATION)
        stop = threading.Event()
        t = threading.Thread(target=w.run, args=(stop,), name=f"worker-{w.worker_id[:8]}", daemon=True)
        workers.append(w)
        stops.append(stop)
        threads.append(t)
    started = time.time()
    for t in threads:
        t.start()
    rows: Dict[str, dict] = {}
    while time.time() - started < deadline_seconds:
        rows = {j: job_row(app, j) for j in job_ids}
        if all(settled(r) for r in rows.values()):
            break
        time.sleep(1.0)
    for s in stops:
        s.set()
    for t in threads:
        t.join(timeout=120)
    return rows, workers, time.time() - started


#: The SHAPES of the three secrets the plan names. A bare `sk-` substring
#: is not one: mealie ships `frontend/app/lang/locales/sk-SK.ts`, whose
#: chunker log line matched it (measured on the first mealie dry run).
_SECRET_SHAPES = {
    "sk-": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    "ghs_": re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    "Authorization:": re.compile(r"Authorization:", re.IGNORECASE),
}


def _write_results_order() -> List[str]:
    """The statements `_write_results` makes, in order, read from its source.

    So every record says which code it measured: the attach first (before
    22.1-05's A-L3 fix) or last (after).
    """
    import inspect

    from workers.ingest import handler as handler_module

    source = inspect.getsource(handler_module._write_results)
    names = ["check_fence", "resolve_ingestion_run", "attach_ingestion_run",
             "delete_repository_chunks_on", "insert_chunks_on", "complete_ingestion_run_on"]
    found = [(source.find(f"{n}(cur"), n) for n in names]
    return [n for pos, n in sorted(found) if pos >= 0]


def scrub_check(text: str) -> List[str]:
    return [name for name, shape in _SECRET_SHAPES.items() if shape.search(text)]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--state", required=True, help="scratch_db.py's state.json (DSNs; never printed)")
    p.add_argument("--inside", action="store_true", help="use the in-container DSNs")
    p.add_argument("--full-name", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--copies", type=int, default=1, help="N4: this many repository rows, one job each")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--job-type", choices=["full_ingest", "incremental"], default="full_ingest")
    p.add_argument("--no-embed", action="store_true")
    p.add_argument("--replay-vectors", help="a float32 .npy of exported vectors (at-cap runs)")
    p.add_argument("--local-archive", help="serve this tar.gz instead of downloading (at-cap runs)")
    p.add_argument("--ledger")
    p.add_argument("--budget-usd", default="3.00")
    p.add_argument("--projected-usd", help="this run's projected cost, reserved before it starts")
    p.add_argument("--workdir", default="/work")
    p.add_argument("--records", required=True)
    p.add_argument("--record-name", required=True)
    p.add_argument("--deadline-seconds", type=float, default=4 * 3600)
    p.add_argument("--max-chunks", type=int, default=None)
    p.add_argument("--keep-log", action="store_true")
    p.add_argument("--tag-copies", action="store_true",
                   help="at_cap.py archives: make each copyNN/ chunk text distinct")
    p.add_argument("--code-label", default="",
                   help="which code ran (for example the commit of a git-archive snapshot)")
    args = p.parse_args(argv)

    if args.no_embed and os.environ.get(OPENAI_API_KEY_ENV):
        # Before anything else: a dry run can never be the run that spends.
        print("REFUSED: --no-embed refuses to start because OPENAI_API_KEY is in its environment")
        return 4

    state = json.loads(pathlib.Path(args.state).read_text(encoding="utf-8"))
    app_dsn = state["app_dsn_inside" if args.inside else "app_dsn"]
    super_dsn = state["super_dsn_inside" if args.inside else "super_dsn"]

    capture = Capture()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(capture)
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    install_statement_timing()

    app = _REAL_CONNECT(app_dsn)
    super_conn = _REAL_CONNECT(super_dsn)
    super_conn.autocommit = True
    with app.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        probe = cur.fetchone()
    app.rollback()
    assert probe == ("rag_doc_app", False, False), f"not the app role: {probe}"

    # ---- the embedder ---------------------------------------------------
    ledger = Ledger(args.ledger, Decimal(args.budget_usd)) if args.ledger else None
    recorder = None
    if args.no_embed:
        embedder = DryRunEmbedder()
        mode = "no-embed"
    elif args.replay_vectors:
        import numpy as np

        if os.environ.get(OPENAI_API_KEY_ENV):
            print("REFUSED: a replay run needs no key; refusing with OPENAI_API_KEY set")
            return 4
        embedder = ReplayEmbedder(np.load(args.replay_vectors, mmap_mode="r"))
        mode = "replay"
    else:
        if ledger is None or args.projected_usd is None:
            print("REFUSED: an embedding run needs --ledger and --projected-usd")
            return 4
        try:
            ledger.reserve(Decimal(args.projected_usd), args.record_name)
        except LedgerRefused as exc:
            print(f"LEDGER REFUSED: {exc}")
            return 3
        recorder = OpenAIRecorder(ledger, args.record_name)
        embedder = real_embedder(recorder)
        mode = "embed"

    # ---- seed ----------------------------------------------------------
    seeded: List[Seeded] = []
    used: set = set()
    for i in range(args.copies):
        label = args.label if args.copies == 1 else f"{args.label}-copy{i + 1:02d}"
        s = seed(app, super_conn, args.full_name, args.branch, label, used)
        used.add(s.partition)
        seeded.append(s)
    # ⚠ RACE TESTS RACE: every job is enqueued before any worker starts.
    for s in seeded:
        s.job = enqueue(app, s, args.job_type)

    from workers.chunker import SemanticChunker

    chunker = MeasuringChunker(SemanticChunker(), tag_copies=args.tag_copies)
    local_archive = pathlib.Path(args.local_archive).read_bytes() if args.local_archive else None
    github = PublicGitHub(args.full_name, args.branch, args.sha, local_archive)
    route = TokenRoute({s.job: args.full_name for s in seeded}, args.branch)
    os.makedirs(args.workdir, exist_ok=True)
    deps_kwargs: Dict[str, Any] = dict(
        internal_api_url="http://backend.measure:8081",
        chunker=chunker,
        embedder=embedder,
        workdir=args.workdir,
        internal_transport=route.transport(),
        github_transport=github,
    )
    if args.no_embed:
        deps_kwargs["embed_slice"] = 10**9  # one call sees every distinct chunk
    if args.max_chunks:
        deps_kwargs["max_chunks"] = args.max_chunks
    handler = make_full_ingest_handler(IngestDeps(**deps_kwargs))

    sampler = DiskSampler(args.workdir)
    sampler.start()
    TIMINGS.t0 = time.time()
    rows, workers, wall = run_workers(app_dsn, handler, args.workers, args.deadline_seconds,
                                      app, [s.job for s in seeded])
    sampler.halt.set()
    sampler.join(timeout=10)
    rss = peak_rss_bytes()

    lines = capture.lines()
    jobs = []
    for s in seeded:
        row = rows.get(s.job) or job_row(app, s.job)
        with require_tenant(app, s.org) as cur:
            cur.execute("SELECT count(*), count(DISTINCT content_hash) FROM chunks WHERE repository_id = %s",
                        (s.repo,))
            stored, distinct_stored = cur.fetchone()
        fetched = [l["msg"] for l in lines if l["msg"].startswith(f"job {s.job}: fetched ")]
        archive_bytes = None
        if fetched:
            m = re.search(r"archive (-?\d+) bytes", fetched[0])
            archive_bytes = int(m.group(1)) if m else None
        jobs.append({
            "label": s.label, "job_id": s.job, "repository_id": s.repo, "organization_id": s.org,
            "partition": s.partition, "final": {k: row[k] for k in ("state", "attempts", "last_stage")},
            "last_error": (row["last_error"] or "")[:300] or None,
            "progress": row["progress"], "stages": stage_times(lines, s.job),
            "archive_bytes": archive_bytes, "chunks_stored": stored, "distinct_stored": distinct_stored,
        })
    expand_lines = [l["msg"] for l in lines if l["logger"].endswith("archive") and " bytes expanded " in l["msg"]]

    record: Dict[str, Any] = {
        "record": args.record_name,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "code": args.code_label,
        "write_results_order": _write_results_order(),
        "job_type": args.job_type,
        "job_type_note": ("incremental runs the full ingest until 22.1-02 (handlers.py registers one "
                          "handler for both)") if args.job_type == "incremental" else None,
        "repository": {"full_name": args.full_name, "sha": args.sha, "branch": args.branch},
        "copies": args.copies, "workers": args.workers,
        "worker_settings": {
            "lease_s": workers[0].lease.total_seconds(), "heartbeat_s": workers[0].heartbeat.total_seconds(),
            "heartbeat_statement_timeout_ms": workers[0]._heartbeat_timeout_ms(),
            "max_job_duration_s": workers[0].max_job_duration.total_seconds(),
        },
        "host": host_facts(app),
        "wall_seconds": round(wall, 3),
        "jobs": jobs,
        "fetch_expanded_lines": expand_lines,
        "parse": {"files": chunker.files, "bytes_expanded_files": chunker.bytes, "chunks": chunker.chunks,
                  "chunks_by_language": chunker.by_language},
        "github_events": github.events,
        "statements": {"loop": statement_summary(TIMINGS.loop), "loop_entries": len(TIMINGS.loop)},
        "loop_statements_detail": [e for e in TIMINGS.loop if e["label"] != "insert_chunks"],
        "heartbeat": beat_outcomes(lines, [s.job for s in seeded]),
        "peak_rss_bytes": rss,
        "peak_workdir_bytes": sampler.peak, "workdir_samples": sampler.samples,
    }
    if isinstance(embedder, DryRunEmbedder):
        record["dry_run"] = {"distinct_chunks": embedder.distinct, "tokens": embedder.tokens,
                             "max_tokens_one_chunk": embedder.max_tokens_one,
                             "projected_usd_one_pass": f"{tokens_to_usd(embedder.tokens):.6f}"}
    if isinstance(embedder, ReplayEmbedder):
        record["replay"] = {"distinct_chunks": embedder.distinct, "model": embedder.model,
                            "vectors_available": int(len(embedder.vectors)),
                            "perturbed": embedder.perturbed,
                            "note": "vectors memory-mapped from the export; not counted as heap"}
    if recorder is not None:
        record["openai"] = recorder.summary()
        record["openai"]["calls_detail"] = recorder.calls
        record["ledger_after"] = ledger.summary()

    out_dir = pathlib.Path(args.records)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=1, default=str)
    log_text = "\n".join(f"{datetime.fromtimestamp(l['t'], timezone.utc).isoformat()} {l['level']} "
                         f"{l['logger']}: {l['msg']}" for l in lines
                         if not l["logger"].startswith("workers.embeddings"))
    leaks = scrub_check(text) + scrub_check(log_text)
    if leaks:
        print(f"WARNING: a scrubbed pattern appears in the record or log: {sorted(set(leaks))}")
        record["scrub_findings"] = sorted(set(leaks))
        text = json.dumps(record, indent=1, default=str)
    (out_dir / f"{args.record_name}.json").write_text(text, encoding="utf-8")
    if args.keep_log:
        with gzip.open(out_dir / f"{args.record_name}.log.gz", "wt", encoding="utf-8") as fh:
            fh.write(log_text)

    # A short, secret-free summary on stdout.
    for j in jobs:
        print(json.dumps({"label": j["label"], "final": j["final"], "seconds": j["stages"]["seconds"],
                          "chunks_stored": j["chunks_stored"], "archive_bytes": j["archive_bytes"]}))
    hb = record["heartbeat"]
    print(json.dumps({"beats": hb["beats_total"], "57014": hb["beats_57014"],
                      "log_cancelled": hb["log_heartbeat_failed_querycanceled"],
                      "false_loss": hb["log_assuming_it_is_lost"], "max_beat_s": hb["max_beat_seconds"],
                      "peak_rss_mb": round(rss / 2**20, 1), "peak_workdir_mb": round(sampler.peak / 2**20, 1),
                      "parse": record["parse"]["chunks"], "files": record["parse"]["files"]}))
    if "dry_run" in record:
        print(json.dumps(record["dry_run"]))
    if recorder is not None:
        o = record["openai"]
        print(json.dumps({"calls": o["calls"], "status": o["status_counts"], "429s": len(o["http_429"]),
                          "tokens": o["tokens"], "limits": o["limits"],
                          "ledger_spent_usd": record["ledger_after"]["spent_usd"]}))
    # A WORKER CLAIMS QUEUE-WIDE (21-CONTEXT L5): a job this run leaves
    # queued (a dry run's `DryRunStop` is an ordinary failure, re-queued with
    # a backoff) would be claimed by the NEXT run's worker once its backoff
    # passed -- measured on the first dry runs: each was re-claimed and failed
    # at the token route, which knows only its own run's jobs. So every job
    # this run seeded leaves the live set, as the isolation tests do.
    with app.cursor() as cur:
        cur.execute(
            "UPDATE ingestion_jobs SET state = 'superseded', updated_at = NOW() "
            "WHERE id = ANY(%s::uuid[]) AND state IN ('queued', 'running')",
            ([s.job for s in seeded],),
        )
    app.commit()
    app.close()
    super_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
