"""Download a repository archive at an exact commit and extract it safely.

⚠ NO CUSTOMER CODE IS EVER EXECUTED. Nothing in this module runs `git`, a
build tool, an install step, a hook, a submodule fetch or an LFS smudge --
the worker image has none of them, and the archive is treated as bytes
that get read. That is the whole security argument for the archive API
over a clone (U5, 22-RESEARCH Q8): the hostile-content surface is
extraction, and extraction is what the guards below and the hostile-archive
tests are about.

WHAT HAPPENS, IN ORDER (`fetch_repository`):

1. **Resolve the head.** `GET /repos/{full_name}/commits/heads/{branch}`
   with `Accept: application/vnd.github.sha` returns the full 40-hex SHA,
   which `ingestion_runs.commit_sha` needs. The branch name is never what
   gets downloaded.
2. **Download** `GET /repos/{full_name}/tarball/{sha}` -- the exact
   commit -- streamed to `workdir/<job_id>/archive.tar.gz`, with
   `Authorization: Bearer <token>` on the API request only. GitHub answers
   with a redirect to a download host; `httpx` strips `Authorization` when
   the host changes, and `test_fetch_client.py` MEASURES that rather than
   assuming it. Downloaded bytes are counted and the download is refused
   past `Limits.max_archive_bytes`.
3. **Extract**, streaming, member by member, into `workdir/<job_id>/tree/`.
   Before a member is written its HEADER is checked: regular files only
   (symlinks, hardlinks, devices and FIFOs are skipped and counted); a
   safe path (no `..`, no absolute path, no drive letter, no backslash, and
   the one top-level directory GitHub's archives have); size within
   `Limits.max_file_bytes` (oversize files are skipped and counted); and
   the name filters (a `.env` is never written to disk). Then
   `tar.extract(..., filter="data")`, which refuses absolute paths, `..`
   and links on its own -- defence in depth, and the mutation table in
   22-04-SUMMARY.md records which guard holds when the other is removed.
   **The bomb guard**: the bytes the gzip stream expands to are counted as
   they are read, and the extraction stops -- within one read of the cap,
   not after reading it all -- past `Limits.max_expanded_bytes`. U6 as
   locked by the user on 2026-09-17 applies the same 500 MB to both the
   download and the expansion.
4. **Walk** the tree with `os.walk(followlinks=False)`, `lstat` on every
   file, judging what is ON DISK: the name filters again, then the content
   checks -- a NUL in the first 8 KB or a failed UTF-8 decode is binary;
   a Go file with the generated-code header is generated.
5. **Cap** indexable files at `Limits.max_indexable_files`, at extraction
   time (the count of files written, which bounds directory entries against
   an inode bomb) and again after the walk.
6. **Return** a `FetchedTree`: the SHA, the branch, the files as
   `(path, content, language)`, and `skipped` as COUNTS ONLY -- the
   secret-looking paths are exactly the ones not to repeat.

CLEANUP. `fetch_repository` is a context manager: the job's directory is
removed on exit, success or failure, and never asserts anything on the way
out. `sweep_stale_workdirs` removes job directories older than a bound that
no live job can reach; 22-05 calls it when the worker starts.

NOTHING HERE LOGS A URL. The download link may carry a credential of its
own ([not verified], 22-RESEARCH Q8), so every message names a HOST, a
status and a count, never a link -- and every `httpx` exception is
re-raised `from None`, because a chained traceback prints the original's
message, and that is where the URL would be.

What `FetchRejected` means for the job: a hard cap, so 22-05 ends it `dead`
in one attempt (U6). `FetchFailed` is an ordinary failure: retried.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import stat
import tarfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

from workers.fetch import filters
from workers.fetch.client import RepositoryToken

logger = logging.getLogger(__name__)


def _silence_library_request_logging() -> None:
    """Keep httpx and httpcore from logging request URLs.

    ⚠ MEASURED, NOT ASSUMED. `httpx` logs every request at INFO as
    `HTTP Request: GET <full url> "HTTP/1.1 200 OK"`, and for the archive
    download that URL is the redirect link, `?token=` included. The
    caplog test in `test_fetch_client.py` caught it on the first run: the
    fetcher's own lines were clean and the library's were not. `httpcore`
    does the same at DEBUG. Both loggers are held at WARNING, here at import
    and again before every download in case something reset them, because
    a worker run at DEBUG must not become a worker that logs credentials.
    """
    for name in ("httpx", "httpcore"):
        library = logging.getLogger(name)
        if library.level == logging.NOTSET or library.level < logging.WARNING:
            library.setLevel(logging.WARNING)


_silence_library_request_logging()

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "rag-doc-worker"

MB = 1024 * 1024

#: The bytes of a file examined for a NUL before it is called text.
NUL_PROBE_BYTES = 8192

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class Limits:
    """U6's caps. The same 500 MB applies to the download AND the expansion."""

    max_archive_bytes: int = 500 * MB
    max_expanded_bytes: int = 500 * MB
    max_file_bytes: int = 1 * MB
    max_indexable_files: int = 20_000


DEFAULT_LIMITS = Limits()


class FetchRejected(Exception):
    """A hard cap tripped. 22-05 ends the job `dead` in one attempt (U6).

    `reason` is plain and token-free. `members_seen` says how far the
    extractor got, so a test can prove it stopped at the cap rather than
    after reading everything.
    """

    def __init__(self, reason: str, *, members_seen: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.members_seen = members_seen


class FetchFailed(Exception):
    """A download or API call failed. An ordinary, retryable failure."""


class FetchedFile(NamedTuple):
    path: str
    content: str
    language: str


@dataclass
class DownloadStats:
    """What the download measured. Hosts and parameter NAMES only, never a URL."""

    bytes: int = 0
    elapsed_seconds: float = 0.0
    status: int = 0
    redirect_hosts: List[str] = field(default_factory=list)
    final_host: str = ""
    query_param_names: List[str] = field(default_factory=list)


@dataclass
class ExtractStats:
    """What the extractor measured."""

    members_seen: int = 0
    #: Bytes the gzip stream expanded to (the tar stream, headers included).
    expanded_bytes: int = 0
    #: Bytes of file content actually written to disk.
    payload_bytes: int = 0
    files_written: int = 0
    top_level: Optional[str] = None
    skipped: Dict[str, int] = field(default_factory=dict)


@dataclass
class FetchedTree:
    sha: str
    branch: str
    files: List[FetchedFile]
    #: Skip reason -> count. Counts only, never paths.
    skipped: Dict[str, int]
    download: Optional[DownloadStats] = None
    extract: Optional[ExtractStats] = None


# ---------------------------------------------------------------------
# The API calls
# ---------------------------------------------------------------------


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


def _host(url: Any) -> str:
    parts = urlsplit(str(url))
    return parts.hostname or "?"


def resolve_head(
    client: httpx.Client,
    full_name: str,
    branch: str,
    token: str,
    *,
    api_base: str = GITHUB_API,
) -> str:
    """The full 40-hex SHA the branch points at right now."""
    ref = quote("heads/" + branch, safe="/")
    url = f"{api_base.rstrip('/')}/repos/{full_name}/commits/{ref}"
    headers = _headers(token)
    headers["Accept"] = "application/vnd.github.sha"
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise FetchFailed(
            f"resolving {full_name}@{branch} at {_host(api_base)} failed: {type(exc).__name__}"
        ) from None
    if response.status_code != 200:
        raise FetchFailed(
            f"resolving {full_name}@{branch}: {_host(api_base)} answered {response.status_code}"
        )
    sha = response.text.strip()
    if not _SHA.match(sha):
        raise FetchFailed(
            f"resolving {full_name}@{branch}: {_host(api_base)} did not return a 40-hex commit SHA"
        )
    return sha


def download_archive(
    client: httpx.Client,
    full_name: str,
    sha: str,
    token: Optional[str],
    dest_path: str,
    *,
    api_base: str = GITHUB_API,
    limits: Limits = DEFAULT_LIMITS,
) -> DownloadStats:
    """Stream the tarball of the EXACT commit to `dest_path`, counting bytes.

    `token` may be None for a public repository (the mealie measurement);
    the request then carries no Authorization header at all.
    """
    if not _SHA.match(sha):
        raise FetchFailed("refusing to download anything but a full commit SHA")
    _silence_library_request_logging()
    url = f"{api_base.rstrip('/')}/repos/{full_name}/tarball/{sha}"
    headers = _headers(token) if token else {
        k: v for k, v in _headers("").items() if k != "Authorization"
    }
    stats = DownloadStats()
    started = time.monotonic()
    cap_mb = limits.max_archive_bytes // MB
    try:
        with client.stream("GET", url, headers=headers) as response:
            stats.status = response.status_code
            stats.redirect_hosts = [_host(r.url) for r in response.history]
            stats.final_host = _host(response.url)
            stats.query_param_names = sorted(response.url.params.keys())
            if response.status_code != 200:
                raise FetchFailed(
                    f"archive download for {full_name}@{sha[:12]}: "
                    f"{stats.final_host} answered {response.status_code}"
                )
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limits.max_archive_bytes:
                raise FetchRejected(f"archive exceeds {cap_mb} MB")
            with open(dest_path, "wb") as out:
                for chunk in response.iter_bytes():
                    stats.bytes += len(chunk)
                    if stats.bytes > limits.max_archive_bytes:
                        raise FetchRejected(f"archive exceeds {cap_mb} MB")
                    out.write(chunk)
    except httpx.HTTPError as exc:
        # `from None`: an httpx exception can carry the download URL, which
        # may carry a credential, and a chained traceback would print it.
        raise FetchFailed(
            f"archive download for {full_name}@{sha[:12]} from {_host(api_base)} failed: "
            f"{type(exc).__name__}"
        ) from None
    stats.elapsed_seconds = time.monotonic() - started
    logger.info(
        "downloaded archive for %s@%s: %d bytes in %.1fs from %s",
        full_name,
        sha[:12],
        stats.bytes,
        stats.elapsed_seconds,
        stats.final_host,
    )
    return stats


# ---------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------


class _CountingReader:
    """Feeds tarfile from the gzip stream, counting the bytes it expands to.

    THIS IS THE BOMB GUARD. The count is the tar stream -- headers, padding
    and payload alike -- so a bomb made of headers is caught as surely as
    one made of zeros, and the check runs on every read, so it trips within
    one buffer of the cap rather than after the whole stream.
    """

    def __init__(self, raw: Any, cap: int, stats: ExtractStats) -> None:
        self._raw = raw
        self._cap = cap
        self._stats = stats

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._stats.expanded_bytes += len(data)
        if self._stats.expanded_bytes > self._cap:
            raise FetchRejected(
                f"archive expands past {self._cap // MB} MB",
                members_seen=self._stats.members_seen,
            )
        return data


def _unsafe(name: str) -> bool:
    """True for any member name that must not be joined to a directory."""
    if not name or "\x00" in name or "\\" in name:
        return True
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return True
    if name.startswith("/") or _DRIVE.match(name):
        return True
    return any(part in ("", ".", "..") for part in name.split("/"))


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk() or member.isfifo():
        return "special"
    return "other"


def extract_archive(archive_path: str, dest: str, limits: Limits = DEFAULT_LIMITS) -> ExtractStats:
    """Extract the regular files that pass every check into `dest`, streaming.

    See the module docstring, step 3. Raises `FetchRejected` at a cap.
    """
    stats = ExtractStats()
    skipped: Counter = Counter()
    os.makedirs(dest, exist_ok=True)
    real_dest = os.path.realpath(dest)

    with gzip.open(archive_path, "rb") as raw:
        reader = _CountingReader(raw, limits.max_expanded_bytes, stats)
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                stats.members_seen += 1

                if member.isdir():
                    # Structure, not content; parents are created on demand.
                    continue
                if not member.isreg():
                    skipped[_member_kind(member)] += 1
                    continue
                if _unsafe(member.name):
                    skipped["unsafe_path"] += 1
                    continue

                # GitHub's archives put everything under one directory,
                # `owner-repo-sha/`. Strip it, and refuse a second one.
                top, _, rel = member.name.partition("/")
                if not rel:
                    skipped["unexpected_top_level"] += 1
                    continue
                if stats.top_level is None:
                    stats.top_level = top
                elif top != stats.top_level:
                    skipped["unexpected_top_level"] += 1
                    continue

                if member.size > limits.max_file_bytes:
                    skipped["oversize_file"] += 1
                    continue

                verdict = filters.classify_path(rel)
                if not verdict.indexable:
                    # Never written. A committed `.env` does not touch the disk.
                    skipped[verdict.reason] += 1
                    continue

                if stats.files_written >= limits.max_indexable_files:
                    stats.skipped = dict(skipped)
                    raise FetchRejected(
                        f"more than {limits.max_indexable_files} indexable files",
                        members_seen=stats.members_seen,
                    )

                try:
                    tar.extract(
                        member.replace(name=rel), path=real_dest, set_attrs=False, filter="data"
                    )
                except tarfile.FilterError:
                    # The header check above should have caught it; this is
                    # the second guard doing its job.
                    skipped["refused_by_filter"] += 1
                    continue
                except (tarfile.TarError, OSError):
                    skipped["unwritable"] += 1
                    continue
                stats.files_written += 1
                stats.payload_bytes += member.size

    stats.skipped = dict(skipped)
    return stats


# ---------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------


def collect_tree(
    dest: str, limits: Limits = DEFAULT_LIMITS
) -> Tuple[List[FetchedFile], Dict[str, int]]:
    """Read the indexable files off disk. Never follows a link."""
    files: List[FetchedFile] = []
    skipped: Counter = Counter()
    for root, dirs, names in os.walk(dest, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                skipped["link"] += 1
                continue
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            verdict = filters.classify_path(rel)
            if not verdict.indexable:
                skipped[verdict.reason] += 1
                continue
            if info.st_size > limits.max_file_bytes:
                skipped["oversize_file"] += 1
                continue
            with open(full, "rb") as fh:
                raw = fh.read(limits.max_file_bytes + 1)
            if b"\x00" in raw[:NUL_PROBE_BYTES]:
                skipped["binary"] += 1
                continue
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                skipped["non_utf8"] += 1
                continue
            if verdict.language == "go" and filters.is_generated_go(text):
                skipped["generated"] += 1
                continue
            files.append(FetchedFile(rel, text, verdict.language or ""))
            if len(files) > limits.max_indexable_files:
                raise FetchRejected(f"more than {limits.max_indexable_files} indexable files")
    return files, dict(skipped)


# ---------------------------------------------------------------------
# The per-job directory
# ---------------------------------------------------------------------


def job_directory(workdir: str, job_id: str) -> str:
    """`workdir/<job_id>/`, fresh. The id must be a UUID, so it cannot traverse."""
    canonical = str(uuid.UUID(str(job_id)))
    path = os.path.join(workdir, canonical)
    _rmtree_quiet(path)
    os.makedirs(path)
    return path


def _rmtree_quiet(path: str) -> None:
    """Remove a tree without ever raising. Cleanup must not mask the real error."""

    def _make_writable(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    if os.path.lexists(path):
        try:
            shutil.rmtree(path, onerror=_make_writable)
        except OSError:
            pass


def sweep_stale_workdirs(workdir: str, older_than: timedelta) -> int:
    """Remove job directories older than `older_than`. Returns how many.

    Only directories named by a UUID are ours to remove; anything else in
    `workdir` is left alone, as is a symlink of any name.
    """
    if not os.path.isdir(workdir):
        return 0
    cutoff = time.time() - older_than.total_seconds()
    removed = 0
    for entry in os.scandir(workdir):
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            uuid.UUID(entry.name)
        except ValueError:
            continue
        if entry.stat(follow_symlinks=False).st_mtime > cutoff:
            continue
        _rmtree_quiet(entry.path)
        removed += 1
    if removed:
        logger.info("swept %d stale job director%s from %s", removed, "y" if removed == 1 else "ies", workdir)
    return removed


# ---------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------


@contextmanager
def fetch_repository(
    token: RepositoryToken,
    *,
    job_id: str,
    workdir: str,
    api_base: str = GITHUB_API,
    limits: Limits = DEFAULT_LIMITS,
    transport: Optional[httpx.BaseTransport] = None,
    timeout: Optional[httpx.Timeout] = None,
) -> Iterator[FetchedTree]:
    """Fetch the repository the token is for, at its default branch's head.

    A context manager: the per-job directory exists for the block and is
    removed afterwards, success or failure. The yielded tree holds the
    files' CONTENT, so nothing on disk is needed after the block.
    """
    jobdir = job_directory(workdir, job_id)
    try:
        if timeout is None:
            timeout = httpx.Timeout(30.0, read=120.0)
        with httpx.Client(follow_redirects=True, transport=transport, timeout=timeout) as client:
            sha = resolve_head(
                client, token.full_name, token.default_branch, token.token, api_base=api_base
            )
            archive = os.path.join(jobdir, "archive.tar.gz")
            download = download_archive(
                client, token.full_name, sha, token.token, archive,
                api_base=api_base, limits=limits,
            )

        tree_dir = os.path.join(jobdir, "tree")
        extract = extract_archive(archive, tree_dir, limits)
        os.remove(archive)
        files, walk_skipped = collect_tree(tree_dir, limits)

        skipped: Counter = Counter(extract.skipped)
        skipped.update(walk_skipped)
        logger.info(
            "fetched %s@%s: %d indexable files, %d members, %d bytes expanded, skipped=%s",
            token.full_name,
            sha[:12],
            len(files),
            extract.members_seen,
            extract.expanded_bytes,
            dict(sorted(skipped.items())),
        )
        yield FetchedTree(
            sha=sha,
            branch=token.default_branch,
            files=files,
            skipped=dict(skipped),
            download=download,
            extract=extract,
        )
    finally:
        _rmtree_quiet(jobdir)
