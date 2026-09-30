"""The `full_ingest` handler: fetch -> parse -> embed -> store (22-05).

Registered for BOTH job types (`workers.jobs.handlers`) until 22.1-02 makes
`incremental` re-parse only what changed. One job, four reported stages,
one transactional write:

  1. **fetch** -- ask the backend's internal route for a one-repository,
     read-only token (U4), fetch the archive at the default branch's exact
     SHA under U6's caps and U7's filters (`workers.fetch`), then REVOKE the
     token: the lease gates its issuance, not its hour of validity.
  2. **parse** -- chunk every indexable file (`SemanticChunker`). More than
     `MAX_CHUNKS` (U6: 100,000) is `Rejected`, which ends the job `dead` in
     one attempt.
  3. **embed** -- one vector per DISTINCT chunk text, in slices, checking
     between slices whether the job is still ours and the worker still up.
  4. **store** -- reported, then `write_results` is RETURNED. The runtime
     runs it inside `complete()`'s transaction: resolve and attach the
     `ingestion_runs` row, delete the repository's chunks, insert the new
     set with their vectors and model, mark the run completed. The chunks,
     the run and the job's completion commit together or not at all (L1).

⚠ WHAT IT RAISES, AND WHEN. Which ending each exception takes is
`docs/api-ingestion-jobs.md`, "How a job ends" -- the authority, not
restated here. What this handler owns is choosing the exception:

  - `should_abort()` at a checkpoint, or a refused token (`TokenRefused`,
    the route's MARKED 404) -> **`LeaseLost`**. Never a `return`:
    `COMPLETE_SQL` does not check the lease's expiry, so an expired but
    unreclaimed lease would reach `complete()` and write `completed` for
    work that was never done (fact-check c2).
  - `is_shutting_down()` at a checkpoint -> **`Unfinished`**.
  - a U6 cap -> **`Rejected`**: its own 100,000-chunk cap here, the
    fetcher's caps as `FetchRejected`.
  - **every** indexable file raising in the chunker -> **`ParseFailed`**, an
    ordinary failure: the job is retried and never replaces a good index
    with nothing (PR #58's review, B-M3). One file raising is counted
    (`parse_errors`) and the job goes on.
  - `InstallationSuspended` / `InstallationUninstalled` from the token route
    -> propagated unchanged.
  - `InternalApiMisrouted` (an UNMARKED answer: `INTERNAL_API_URL` reaches
    something that is not the route) and everything else -> propagated
    unchanged, never turned into a lost lease (fact-check c3).

⚠ `progress` IS CUMULATIVE. `PROGRESS_SQL` replaces the whole column, so
every report carries the ONE running dict, each stage adding its keys and
removing none; which keys each report first carries is
`docs/api-ingestion-jobs.md`, "Stages and progress" (the authority, not
restated here). A completed row's `progress` therefore still holds
`skipped` -- the count of secret-looking files never sent to OpenAI.

⚠ THE STAGE IS REPORTED ON ENTRY, AFTER THE CHECKPOINT. So `last_stage` is
the stage the job was in when it stopped: a shutdown during embedding leaves
`last_stage = 'embed'`. `store` is the last report: `report_progress` cannot
run inside `write_results` (same connection; psycopg2's re-entrancy guard
refuses it), and `COMPLETE_SQL` does not touch `last_stage`, so a completed
job reads `last_stage = 'store'`.

NOTHING HERE LOGS A TOKEN, a lease owner or a download link. The redirect
link's query-parameter NAMES are logged after each fetch (never values),
which is how 22-05's live proof settles whether a private repository's link
carries a credential of its own.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from workers.chunker.models import Chunk
from workers.fetch import (
    DEFAULT_LIMITS,
    FetchedFile,
    Limits,
    TokenRefused,
    fetch_repository,
    request_token,
    revoke_token,
)
from workers.fetch.archive import GITHUB_API
from workers.jobs.runtime import Handler, JobContext, Rejected, Unfinished, WriteResults
from workers.jobs.transitions import (
    Job,
    LeaseLost,
    attach_ingestion_run,
    resolve_ingestion_run,
    sanitize_error,
)
from workers.storage.postgres_writer import PostgresWriter, content_hash

logger = logging.getLogger(__name__)

#: U6's chunk cap, enforced here, after parsing and BEFORE embedding -- the
#: step that costs money. Above it the job is `Rejected`: `dead` in one
#: attempt with a plain reason. (The fetcher enforces U6's other caps.)
MAX_CHUNKS = 100_000

#: Distinct chunks embedded between two checkpoints. The generator batches
#: 100 per API call, so this is ten calls -- seconds, not minutes -- between
#: the moments a shutdown or a lost lease can stop the most expensive stage.
EMBED_SLICE = 1_000

#: The stage vocabulary, in order. `docs/api-ingestion-jobs.md` is its
#: authority; `last_stage` carries no `CHECK`, so this tuple is the only
#: thing in code that says what the values are.
STAGES = ("fetch", "parse", "embed", "store")

INTERNAL_API_URL_ENV = "INTERNAL_API_URL"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
WORKDIR_ENV = "WORKER_WORKDIR"

#: Where job directories are made. ⚠ SIZE ITS DISK FOR ~1 GB PER WORKER
#: PROCESS: the archive (up to 500 MB) is kept until extraction finishes,
#: and the extracted tree can reach U6's 500 MB plus one file (1 MB) before
#: the expansion counters stop it -- a dense bomb is stopped AT the cap, not
#: before it (22-04's re-check).
DEFAULT_WORKDIR = os.path.join(tempfile.gettempdir(), "rag-doc-worker")


class ConfigurationError(RuntimeError):
    """The ingest handler cannot be configured from the environment.

    `workers/__main__` turns this into exit 2 at startup, before the worker
    claims anything: a worker that cannot reach the token route or OpenAI
    would fail every job it claimed five times and dead-letter it.
    """


class ParseFailed(Exception):
    """Every indexable file raised in the chunker. An ORDINARY failure: retried.

    ⚠ WHY IT EXISTS (PR #58's review, B-M3 and A-L5). One file raising is
    counted and skipped (`parse_errors`), which is right for one bad file.
    But when EVERY file raises, carrying on would reach `store` with no
    chunks, and `write_results` REPLACES the repository's index: it would
    delete every chunk the last good ingest wrote, insert none, and complete
    the job `synced` -- a `completed` over work that did not happen, and on a
    re-index the destruction of a good index. `SemanticChunker` catches its
    own grammar errors and falls back, so this takes a systematic failure --
    exactly the kind a chunker change can introduce, and exactly the kind
    that would wipe every repository it touched.

    Not `Rejected`: nothing about the repository is over a cap, and a fixed
    chunker will parse it. The job fails with this message in `last_error`,
    takes a backoff and is retried; the index it would have replaced is left
    exactly as it was.
    """


@dataclass
class IngestDeps:
    """Everything the handler talks to. Injectable, so tests fake only the edges.

    `internal_transport` and `github_transport` are `httpx` transports for
    the token route and for GitHub; production leaves both None. The
    chunker needs `chunk_file(path, content, language)`; the embedder needs
    `.model` and `generate_embeddings_for_chunks(chunks, use_cache=False)`.
    """

    internal_api_url: str
    chunker: Any
    embedder: Any
    workdir: str
    github_api: str = GITHUB_API
    limits: Limits = DEFAULT_LIMITS
    max_chunks: int = MAX_CHUNKS
    embed_slice: int = EMBED_SLICE
    internal_transport: Optional[httpx.BaseTransport] = None
    github_transport: Optional[httpx.BaseTransport] = None


def deps_from_env(environ: Optional[Mapping[str, str]] = None) -> IngestDeps:
    """Build the production dependencies from the environment.

    Reads `INTERNAL_API_URL` (the backend's INTERNAL listener, e.g.
    `http://backend:8081`; never the public API), `OPENAI_API_KEY` and
    `WORKER_WORKDIR` (default `DEFAULT_WORKDIR`). The App's private key is
    not among them, and never will be (U4).

    Raises:
        ConfigurationError: a required setting is missing or unusable. The
            message names the variable and never echoes a value.
    """
    env = os.environ if environ is None else environ

    internal = (env.get(INTERNAL_API_URL_ENV) or "").strip()
    if not internal:
        raise ConfigurationError(
            f"{INTERNAL_API_URL_ENV} is not set; refusing to start. It must name the "
            "backend's INTERNAL listener (for example http://backend:8081), never the "
            "public API. Without it every job would fail to get a repository token."
        )
    parts = urlsplit(internal)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigurationError(
            f"{INTERNAL_API_URL_ENV} must be an http(s) URL naming a host; refusing to start"
        )

    api_key = (env.get(OPENAI_API_KEY_ENV) or "").strip()
    if not api_key:
        raise ConfigurationError(
            f"{OPENAI_API_KEY_ENV} is not set; refusing to start. Every chunk is embedded "
            "before it is stored."
        )

    workdir = (env.get(WORKDIR_ENV) or "").strip() or DEFAULT_WORKDIR

    # Imported here so that importing this module never loads tiktoken or
    # opens an OpenAI client; `workers/__main__` calls this after its
    # registry and DSN checks.
    from workers.chunker import SemanticChunker
    from workers.embeddings import EmbeddingGenerator

    return IngestDeps(
        internal_api_url=internal,
        chunker=SemanticChunker(),
        embedder=EmbeddingGenerator(api_key=api_key),
        workdir=workdir,
    )


# ---------------------------------------------------------------------
# The stages
# ---------------------------------------------------------------------


def _checkpoint(ctx: JobContext) -> None:
    """Stop if the job is not ours any more, or the worker is going away.

    ⚠ ONE FLAG, ONE MEANING (21-06). A lost lease is `LeaseLost` -- write
    nothing, the job is someone else's; a shutdown is `Unfinished` -- the job
    is still ours and goes back to the queue with its attempt. The lost
    lease is tested first: when both hold, nothing may be written at all.
    """
    if ctx.should_abort():
        raise LeaseLost(
            f"job {ctx.job.id}: the lease is not ours any more (superseded, "
            "reclaimed, or cut loose by max_job_duration); stopping and writing nothing"
        )
    if ctx.is_shutting_down():
        raise Unfinished(
            "the worker is shutting down; the job goes back to the queue with its "
            "attempt handed back"
        )


def _enter(ctx: JobContext, stage: str, progress: Dict[str, Any]) -> None:
    """Checkpoint, then report `stage` with the WHOLE cumulative `progress`.

    A report that matches no row means the fenced `UPDATE` found the job
    under someone else's lease; the runtime has raised the abort flag, and
    this raises `LeaseLost` straight away rather than working on. Without it
    the NEXT checkpoint would still stop the job -- the flag is set -- but
    only after the stage's first piece of work: at `fetch`, asking the token
    route to mint a token for a job that is not ours
    (`test_a_fetch_report_that_matches_no_row_stops_before_asking_for_a_token`).
    """
    _checkpoint(ctx)
    if not ctx.report_progress(stage, copy.deepcopy(progress)):
        raise LeaseLost(
            f"job {ctx.job.id}: the {stage!r} progress report matched no row; "
            "the job is not ours any more"
        )


def _parse(ctx: JobContext, deps: IngestDeps, files: List[FetchedFile]) -> tuple:
    """Chunk every file. Returns (chunks, files_parsed, parse_errors).

    A file whose chunking RAISES is skipped and counted (`parse_errors`),
    as `IngestionPipeline` always did: the chunker already falls back to
    fixed-size chunks when a grammar fails, so a raise is a bug on one
    file, and dead-lettering the whole repository for it helps nobody. The
    cap is checked as chunks accumulate, so a repository over it stops
    being parsed the moment it crosses.

    ⚠ BUT NOT WHEN EVERY FILE RAISES: that is `ParseFailed`, because the
    store that follows would replace the repository's index with nothing.
    A tree with no indexable files at all, or whose files parse into no
    chunks, is not that case -- nothing raised, and an empty index is then
    the truth about the repository.

    The checkpoint runs before EVERY file, not only between stages, so a
    shutdown or a lost lease lands within one file's parse.
    """
    chunks: List[Chunk] = []
    parsed = errors = 0
    for number, fetched in enumerate(files, start=1):
        _checkpoint(ctx)
        try:
            produced = deps.chunker.chunk_file(fetched.path, fetched.content, fetched.language)
        except Exception as exc:  # noqa: BLE001 - one file, counted
            errors += 1
            logger.warning(
                "job %s: chunking %s failed; the file is skipped and counted: %s",
                ctx.job.id,
                fetched.path,
                sanitize_error(exc),
            )
            continue
        parsed += 1
        chunks.extend(produced)
        if len(chunks) > deps.max_chunks:
            raise Rejected(
                f"the repository produces more than {deps.max_chunks} chunks (U6's cap); "
                f"stopped after {number} of {len(files)} files"
            )
    if files and parsed == 0:
        raise ParseFailed(
            f"every one of the {len(files)} indexable files failed to parse "
            f"({errors} raised in the chunker); refusing to replace the repository's "
            "index with nothing"
        )
    return chunks, parsed, errors


def _embed(ctx: JobContext, deps: IngestDeps, chunks: List[Chunk]) -> Dict[str, Any]:
    """One vector per DISTINCT chunk text, keyed by `content_hash`.

    Duplicates are embedded once: every chunk with the same text shares the
    vector (the writer gives each its own row). `use_cache=False`, because
    the generator's cache lives as long as the process and would otherwise
    hold every vector of every repository this worker ever ingested.
    """
    distinct: Dict[str, Chunk] = {}
    for chunk in chunks:
        distinct.setdefault(content_hash(chunk.content), chunk)
    ordered = list(distinct.values())

    vectors: Dict[str, Any] = {}
    for start in range(0, len(ordered), max(1, deps.embed_slice)):
        _checkpoint(ctx)
        part = ordered[start : start + max(1, deps.embed_slice)]
        vectors.update(deps.embedder.generate_embeddings_for_chunks(part, use_cache=False))

    missing = [key for key in distinct if key not in vectors]
    if missing:
        raise RuntimeError(
            f"the embedding generator returned no vector for {len(missing)} of "
            f"{len(distinct)} distinct chunks"
        )
    return vectors


def _write_results(
    job: Job,
    worker_id: str,
    full_name: str,
    sha: str,
    branch: str,
    chunks: List[Chunk],
    embeddings: Mapping[str, Any],
    model: str,
) -> WriteResults:
    """The store stage's write, run by the runtime inside `complete()`.

    In that one tenant-scoped transaction, in this order:

      1. `resolve_ingestion_run` then `attach_ingestion_run`: a retry of the
         same commit reuses its run (`UNIQUE (repository_id, commit_sha)`),
         and the attach is fenced, so a worker that lost its lease raises
         `LeaseLost` here and the whole transaction rolls back;
      2. delete EVERY chunk of the repository -- which is what makes a
         retry, a rerun or a second push idempotent (ISS-027's full-ingest
         half) -- leaving any `retrievals` that cited them dangling, by
         design (see `DELETE_REPOSITORY_CHUNKS_SQL`, P17);
      3. insert the new set, each chunk with its vector and `model`;
      4. mark the run `completed` with its count.

    It must not commit, roll back or open a transaction: `complete()`
    commits it with the job's completion, or rolls it all back.
    """

    def write_results(cur: Any) -> None:
        run_id = resolve_ingestion_run(cur, job.repository_id, sha, branch)
        attach_ingestion_run(cur, job, worker_id, run_id)
        replaced = PostgresWriter.delete_repository_chunks_on(cur, job.repository_id)
        PostgresWriter.insert_chunks_on(
            cur,
            job.organization_id,
            chunks,
            run_id,
            job.repository_id,
            embeddings=embeddings,
            embedding_model=model,
        )
        PostgresWriter.complete_ingestion_run_on(cur, run_id, len(chunks))
        logger.info(
            "job %s: stored %d chunks of %s@%s under run %s (model %s), replacing %d",
            job.id,
            len(chunks),
            full_name,
            sha[:12],
            run_id,
            model,
            replaced,
        )

    return write_results


def make_full_ingest_handler(deps: IngestDeps) -> Handler:
    """Return a `Handler` running the four stages against `deps`."""

    def full_ingest_with(ctx: JobContext) -> WriteResults:
        job = ctx.job
        progress: Dict[str, Any] = {}

        # 1. fetch --------------------------------------------------------
        _enter(ctx, "fetch", progress)
        try:
            token = request_token(
                deps.internal_api_url,
                str(job.id),
                ctx.worker_id,
                transport=deps.internal_transport,
            )
        except TokenRefused as exc:
            # ⚠ LeaseLost, NEVER A RETURN (fact-check c2). The route refuses
            # a lease that has EXPIRED, while `COMPLETE_SQL` does not check
            # expiry and the heartbeat sets the abort flag only on its next
            # tick -- so returning would complete a job whose work never ran.
            raise LeaseLost(f"the repository-token route refused this job's lease: {exc}") from None

        try:
            with fetch_repository(
                token,
                job_id=str(job.id),
                workdir=deps.workdir,
                api_base=deps.github_api,
                limits=deps.limits,
                transport=deps.github_transport,
            ) as tree:
                sha, branch = tree.sha, tree.branch
                files = list(tree.files)
                skipped = dict(sorted(tree.skipped.items()))
                download = tree.download
        finally:
            # ⚠ ON EVERY PATH, the moment the fetch is over: the lease gates
            # the token's issuance, not its hour (PR #52's review). Never
            # raises; a failed revocation is logged, not fatal.
            revoke_token(token, api_base=deps.github_api, transport=deps.github_transport)

        logger.info(
            "job %s: fetched %s@%s (branch %s): %d indexable files, skipped %s; archive "
            "%d bytes via %s to %s; download link query parameter names %s",
            job.id,
            token.full_name,
            sha[:12],
            branch,
            len(files),
            skipped,
            download.bytes if download else -1,
            ",".join(download.redirect_hosts) if download and download.redirect_hosts else "-",
            download.final_host if download else "-",
            download.query_param_names if download else [],
        )
        progress["files_indexable"] = len(files)
        progress["skipped"] = skipped

        # 2. parse --------------------------------------------------------
        _enter(ctx, "parse", progress)
        chunks, parsed, errors = _parse(ctx, deps, files)
        progress["files_parsed"] = parsed
        progress["parse_errors"] = errors
        progress["chunks"] = len(chunks)

        # 3. embed --------------------------------------------------------
        _enter(ctx, "embed", progress)
        embeddings = _embed(ctx, deps, chunks)
        progress["chunks_embedded"] = len(chunks)

        # 4. store: reported LAST, then the write is handed to `complete()`.
        progress["chunks_stored"] = len(chunks)
        _enter(ctx, "store", progress)
        return _write_results(
            job,
            ctx.worker_id,
            token.full_name,
            sha,
            branch,
            chunks,
            embeddings,
            deps.embedder.model,
        )

    return full_ingest_with


# ---------------------------------------------------------------------
# The registered handler
# ---------------------------------------------------------------------

_lock = threading.Lock()
_default: Optional[Handler] = None


def configure(deps: IngestDeps) -> None:
    """Install the dependencies the registered handler uses.

    `workers/__main__` calls this at startup with `deps_from_env()`, so a
    missing setting stops the process before it claims a job.
    """
    global _default
    with _lock:
        _default = make_full_ingest_handler(deps)


def full_ingest(ctx: JobContext) -> WriteResults:
    """The registered `full_ingest` (and, until 22.1-02, `incremental`).

    Uses the dependencies `configure` installed; a process that never called
    it builds them from the environment on this first call, so importing the
    registry never reads configuration (`workers/__main__` checks the
    registry first).
    """
    global _default
    with _lock:
        if _default is None:
            _default = make_full_ingest_handler(deps_from_env())
        handler = _default
    return handler(ctx)
