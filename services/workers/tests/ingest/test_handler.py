"""The `full_ingest` handler with fakes at its network edges and no database (22-05).

What this file pins is the HANDLER'S side of the contract: the stages, the
cumulative progress, and which exception each non-happy path raises. What
the RUNTIME then writes for each exception is pinned against a real
database by `tests/isolation/test_ingest_end_to_end.py`, through the real
`Worker`.

The context is a fake with the four things a handler may touch (`job`,
`worker_id`, the two flags and `report_progress`); the token route and
GitHub are `httpx.MockTransport`s under the REAL `request_token`,
`fetch_repository` and `revoke_token`; the chunker is the real one; the
embedder is `tests.ingest.fakes.FakeEmbedder`.

⚠ "THE WORKDIR IS REMOVED ON EVERY PATH" IS ASSERTED AFTER THE CALL, never
inside a `finally`: an assertion in a `finally` replaces the exception
propagating out of the block, so every real failure would read as a
cleanup failure. `_run` returns the outcome and the test asserts on it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from tests.ingest.fakes import (
    FIXTURE_FILES,
    GITHUB_API,
    INTERNAL,
    LINK_SECRET,
    SENTINEL_TOKEN,
    SHA,
    FakeEmbedder,
    FakeGitHub,
    FakeTokenRoute,
    make_archive,
)
from workers.chunker import SemanticChunker
from workers.fetch import FetchRejected, InternalApiMisrouted, Limits
from workers.ingest import (
    STAGES,
    ConfigurationError,
    IngestDeps,
    ParseFailed,
    deps_from_env,
    make_full_ingest_handler,
)
from workers.ingest.handler import MAX_PARSE_ERROR_SHARE
from workers.jobs.handlers import REGISTRY, run_full_ingest
from workers.jobs.runtime import (
    InstallationSuspended,
    InstallationUninstalled,
    Rejected,
    Unfinished,
)
from workers.jobs.transitions import Job, LeaseLost


# ---------------------------------------------------------------------
# The fake context
# ---------------------------------------------------------------------


class FakeContext:
    """What a handler is given, minus the database.

    `on_report(stage)` runs just after a stage is reported, so a test can
    make the lease vanish or a shutdown arrive at a precise point; `land`
    decides whether a report "matched a row".
    """

    def __init__(
        self,
        *,
        on_report: Optional[Callable[["FakeContext", str], None]] = None,
        land: Callable[[str], bool] = lambda stage: True,
    ) -> None:
        self.job = Job(
            id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            repository_id=uuid.uuid4(),
            job_type="full_ingest",
            attempts=1,
            max_attempts=5,
            needs_rerun=False,
            payload=None,
        )
        self.worker_id = str(uuid.uuid4())
        self.reports: List[Tuple[str, Optional[dict]]] = []
        self.aborted = False
        self.stopping = False
        self._on_report = on_report
        self._land = land

    def should_abort(self) -> bool:
        return self.aborted

    def is_shutting_down(self) -> bool:
        return self.stopping

    def report_progress(self, stage: str, progress: Optional[dict] = None) -> bool:
        self.reports.append((stage, progress))
        landed = self._land(stage)
        if not landed:
            self.aborted = True  # what the runtime does on a report that matched nothing
        if self._on_report is not None:
            self._on_report(self, stage)
        return landed


@dataclass
class Outcome:
    ctx: FakeContext
    result: Any
    error: Optional[BaseException]
    route: FakeTokenRoute
    github: FakeGitHub
    embedder: FakeEmbedder
    workdir: str


def _run(
    tmp_path,
    *,
    answer: str = "ok",
    files: Optional[Dict[str, bytes]] = None,
    ctx: Optional[FakeContext] = None,
    limits: Limits = Limits(),
    max_chunks: int = 100_000,
    embed_slice: int = 1_000,
    github: Optional[FakeGitHub] = None,
    embedder: Optional[FakeEmbedder] = None,
    chunker: Any = None,
) -> Outcome:
    """Run the handler once; return what it returned OR raised, never both."""
    route = FakeTokenRoute(answer)
    github = github or FakeGitHub(make_archive(files if files is not None else FIXTURE_FILES))
    embedder = embedder or FakeEmbedder()
    ctx = ctx or FakeContext()
    workdir = str(tmp_path / "work")
    deps = IngestDeps(
        internal_api_url=INTERNAL,
        chunker=chunker or SemanticChunker(),
        embedder=embedder,
        workdir=workdir,
        github_api=GITHUB_API,
        limits=limits,
        max_chunks=max_chunks,
        embed_slice=embed_slice,
        internal_transport=route.transport(),
        github_transport=github.transport(),
    )
    handler = make_full_ingest_handler(deps)
    result: Any = None
    error: Optional[BaseException] = None
    try:
        result = handler(ctx)
    except BaseException as exc:  # noqa: BLE001 - the outcome is what is asserted
        error = exc
    return Outcome(ctx, result, error, route, github, embedder, workdir)


def job_dirs(outcome: Outcome) -> List[str]:
    """Whatever the handler left in its workdir. Must always be empty after a call."""
    if not os.path.isdir(outcome.workdir):
        return []
    return sorted(os.listdir(outcome.workdir))


def stages(outcome: Outcome) -> List[str]:
    return [stage for stage, _ in outcome.ctx.reports]


# ---------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------


def test_the_stages_run_in_order_and_every_report_carries_the_cumulative_progress(tmp_path):
    outcome = _run(tmp_path)

    assert outcome.error is None, repr(outcome.error)
    assert callable(outcome.result), "the handler must return its write_results"
    assert stages(outcome) == list(STAGES) == ["fetch", "parse", "embed", "store"], (
        "the last report must be `store`: COMPLETE_SQL does not touch last_stage, so a "
        "completed job reads whatever was reported last"
    )

    # ⚠ EACH PAYLOAD IS A SUPERSET OF THE ONE BEFORE, value for value.
    # PROGRESS_SQL replaces the whole column, so a report that dropped a key
    # would erase it from the row that survives.
    payloads = [payload for _, payload in outcome.ctx.reports]
    assert all(isinstance(p, dict) for p in payloads), payloads
    for before, after in zip(payloads, payloads[1:]):
        for key, value in before.items():
            assert key in after, f"{key!r} was dropped between reports: {before} -> {after}"
            assert after[key] == value, f"{key!r} changed between reports: {before} -> {after}"

    final = payloads[-1]
    assert final["files_indexable"] == 3
    assert final["skipped"] == {"secret": 1}, "the .env must be counted, and survive to `store`"
    assert final["files_parsed"] == 3
    assert final["parse_errors"] == 0
    assert final["chunks"] > 0
    assert final["chunks_embedded"] == final["chunks"]
    assert final["chunks_stored"] == final["chunks"]
    assert final["chunks_truncated"] == 0, "no fixture chunk is over the token limit"
    # Which report first carries which keys: the stage's own work lands in
    # the NEXT report, because a stage is reported on entry.
    assert payloads[0] == {}
    assert set(payloads[1]) == {"files_indexable", "skipped"}
    # 22.2-02 added `chunks_truncated`, reported with the parse's counts.
    assert set(payloads[2]) == {
        "files_indexable", "skipped", "files_parsed", "parse_errors", "chunks", "chunks_truncated",
    }
    assert job_dirs(outcome) == []


def test_the_worker_presents_its_own_lease_and_the_download_is_the_exact_commit(tmp_path):
    outcome = _run(tmp_path)
    assert outcome.error is None, repr(outcome.error)

    [token_request] = outcome.route.requests
    assert token_request.url.path == f"/internal/jobs/{outcome.ctx.job.id}/repository-token"
    assert json.loads(token_request.content) == {"lease_owner": outcome.ctx.worker_id}, (
        "the credential is the worker's own lease owner"
    )
    paths = [r.url.path for r in outcome.github.requests if r.url.host == "api.github.test"]
    assert f"/repos/acme/widgets/tarball/{SHA}" in paths, "the download is pinned to the SHA"


def test_the_token_is_revoked_once_the_fetch_ends_with_the_token_itself(tmp_path):
    outcome = _run(tmp_path)
    assert outcome.error is None, repr(outcome.error)

    [revocation] = outcome.github.revocations()
    assert revocation.headers["authorization"] == f"Bearer {SENTINEL_TOKEN}", (
        "DELETE /installation/token is authenticated by the token being revoked"
    )
    order = outcome.github.requests
    assert order.index(revocation) > order.index(outcome.github.downloads()[0]), (
        "revoked after the download, not before it"
    )
    # Before parsing: the token lives for the fetch, not for the job.
    assert order[-1] is revocation, "nothing talked to GitHub after the revocation"


def test_duplicate_chunk_texts_are_embedded_once_and_every_chunk_is_covered(tmp_path):
    same = FIXTURE_FILES["app/greeting.py"]
    outcome = _run(tmp_path, files={"a/one.py": same, "b/two.py": same})
    assert outcome.error is None, repr(outcome.error)

    embedded = [text for call in outcome.embedder.calls for text in call]
    assert len(embedded) == len(set(embedded)), "a duplicate chunk text was embedded twice"
    function_text = [text for text in embedded if text.startswith("def greet")]
    assert len(function_text) == 1, "the function both files share is embedded once"
    final = outcome.ctx.reports[-1][1]
    # Each file also gets its own summary chunk, which names the file, so
    # those two differ: four chunks, three distinct texts.
    assert final["chunks"] == len(embedded) + 1, (final, embedded)
    assert final["chunks_embedded"] == final["chunks"], "the shared vector covers both copies"


def test_embedding_happens_in_slices(tmp_path):
    """The slices are what give a shutdown or a lost lease a place to land
    inside the most expensive stage; the next test lands one there."""
    outcome = _run(tmp_path, embed_slice=1)
    assert outcome.error is None, repr(outcome.error)
    assert len(outcome.embedder.calls) > 1, "premise: several slices"
    assert all(len(call) == 1 for call in outcome.embedder.calls)


def test_a_chunker_failure_on_one_file_is_counted_not_fatal(tmp_path, caplog):
    """One file raising is counted and skipped -- and its warning is redacted.

    ⚠ THE WARNING IS READ AS A `LogRecord` (PR #58's review, B-L2). The
    chunker's exception can quote customer code, which U7 says may hold a
    key; this one carries a token-shaped sentinel, and a warning that logged
    `str(exc)` instead of `sanitize_error(exc)` would put it in the log.
    """
    caplog.set_level(logging.WARNING, logger="workers.ingest.handler")
    real = SemanticChunker()

    class FlakyChunker:
        def chunk_file(self, path, content, language):
            if path == "app/billing.py":
                raise RuntimeError(f"grammar exploded near {SENTINEL_TOKEN}")
            return real.chunk_file(path, content, language)

    outcome = _run(tmp_path, chunker=FlakyChunker())
    assert outcome.error is None, repr(outcome.error)
    final = outcome.ctx.reports[-1][1]
    assert final["files_parsed"] == 2
    assert final["parse_errors"] == 1

    [warning] = [
        r.getMessage() for r in caplog.records
        if r.name == "workers.ingest.handler" and "chunking app/billing.py failed" in r.getMessage()
    ]
    assert "[REDACTED]" in warning, "premise: the exception's text reached the line, redacted"
    assert SENTINEL_TOKEN not in warning and "ghs_" not in warning


def test_a_chunker_that_fails_on_every_file_fails_the_job_instead_of_emptying_the_index(tmp_path):
    """PR #58's review (B-M3, A-L5): a parse with NOTHING parsed must not store.

    Without the guard the handler reached `store` with no chunks, and
    `write_results` REPLACES the repository's index: every chunk the last
    good ingest wrote deleted, none inserted, the job `completed` and
    `synced`. The guard makes it an ordinary failure instead -- not a lost
    lease, not a cap -- so the job is retried and the index it would have
    replaced is left alone (asserted on the rows by the end-to-end test).
    """

    class BrokenChunker:
        def __init__(self) -> None:
            self.calls = 0

        def chunk_file(self, path, content, language):
            self.calls += 1
            raise RuntimeError("the grammar is broken for every file")

    chunker = BrokenChunker()
    outcome = _run(tmp_path, chunker=chunker)

    assert isinstance(outcome.error, ParseFailed), repr(outcome.error)
    assert not isinstance(outcome.error, (Rejected, LeaseLost, Unfinished))
    assert outcome.result is None, "no write_results: nothing may replace the index"
    assert "every one of the 3 indexable files failed to parse" in str(outcome.error)
    assert chunker.calls == 3, "every file was tried before giving up"
    assert outcome.embedder.calls == [], "nothing is sent to OpenAI for a failed parse"
    assert stages(outcome) == ["fetch", "parse"]
    assert len(outcome.github.revocations()) == 1
    assert job_dirs(outcome) == []


# ---------------------------------------------------------------------
# Truncation, counted and reported (22.2-02, QA6)
# ---------------------------------------------------------------------

#: Customer code that must never reach a log line.
CONTENT_SENTINEL = "Hug3ContentS3ntinel"

#: One function whose embedding text is far over the generator's 8,000-token
#: limit (about 12,000 tokens), in a file of its own.
OVERSIZED = (
    b"def huge():\n    return [\n"
    + b"".join(b'        "item%d %s",\n' % (i, CONTENT_SENTINEL.encode()) for i in range(1500))
    + b"    ]\n"
)


def test_a_shutdown_during_the_truncation_count_stops_before_counting_on(tmp_path):
    """PR #66's re-check: `_count_truncated` checkpoints every slice. The
    shutdown arrives after the LAST file is parsed, so `_parse`'s per-file
    checkpoint never sees it; the count's must stop the job before it
    tokenizes a single row. Without that checkpoint the count runs over every
    row and only `_enter("embed")` stops the job -- the same ending, which is
    why the test counts the rows the rule was asked about."""
    ctx = FakeContext()
    real = SemanticChunker()
    last = sorted(p for p in FIXTURE_FILES if p.endswith(".py"))[-1]

    class ShutdownAfterLastFile:
        def chunk_file(self, path, content, language):
            produced = real.chunk_file(path, content, language)
            if path == last:
                ctx.stopping = True
            return produced

    class CountingEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.asked = 0

        def tokens_over_limit(self, chunk):
            self.asked += 1
            return super().tokens_over_limit(chunk)

    embedder = CountingEmbedder()
    outcome = _run(tmp_path, ctx=ctx, chunker=ShutdownAfterLastFile(), embedder=embedder, embed_slice=1)

    assert isinstance(outcome.error, Unfinished), repr(outcome.error)
    assert embedder.asked == 0, f"the count tokenized {embedder.asked} rows after the shutdown"
    assert stages(outcome) == ["fetch", "parse"], "no `embed` report"
    assert outcome.result is None and embedder.calls == [], "nothing embedded, nothing to write"
    assert job_dirs(outcome) == []


def test_an_oversized_chunk_is_counted_and_named_in_a_warning_without_its_content(tmp_path, caplog):
    """The count is the generator's own rule (`FakeEmbedder.tokens_over_limit`
    is the real `EmbeddingGenerator`'s), so it cannot disagree with the
    truncation. It counts ROWS, and is in every later report."""
    caplog.set_level(logging.DEBUG)
    files = dict(FIXTURE_FILES, **{"app/huge.py": OVERSIZED})
    outcome = _run(tmp_path, files=files)
    assert outcome.error is None, repr(outcome.error)

    embed_report = dict(outcome.ctx.reports)["embed"]
    final = outcome.ctx.reports[-1][1]
    assert embed_report["chunks_truncated"] == 1
    assert final["chunks_truncated"] == 1, "the store report, which the completed row keeps"
    assert final["skipped"] == {"secret": 1}, "the cumulative dict still holds `skipped`"

    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "workers.ingest.handler" and r.levelno == logging.WARNING
        and "chunks_truncated" in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert "app/huge.py" in warnings[0] and "(huge)" in warnings[0]
    assert all(CONTENT_SENTINEL not in r.getMessage() for r in caplog.records), (
        "a chunk's content reached a log record"
    )


# ---------------------------------------------------------------------
# The tightened guard (22.2-02): the two edges PR #58's let through
# ---------------------------------------------------------------------

#: Four indexable files (and the `.env`), so "more than half" and "exactly
#: half" can both be built.
FOUR_FILES = dict(FIXTURE_FILES, **{"app/extra.py": b"def extra():\n    return 4\n"})


class SelectiveChunker:
    """Raises on the paths in `raising`, returns NO chunks for those in
    `empty`, and chunks everything else with the real chunker."""

    def __init__(self, raising=(), empty=()) -> None:
        self.raising, self.empty = set(raising), set(empty)
        self.real = SemanticChunker()
        self.calls: List[str] = []

    def chunk_file(self, path, content, language):
        self.calls.append(path)
        if path in self.raising:
            raise RuntimeError(f"the grammar broke on {path}")
        if path in self.empty:
            return []
        return self.real.chunk_file(path, content, language)


def test_some_files_raising_and_the_rest_parsing_to_no_chunks_fails_the_job(tmp_path):
    """Clause 2: `parsed > 0`, so PR #58's guard let it through, and the store
    would have replaced the index with nothing."""
    files = {"app/greeting.py": FIXTURE_FILES["app/greeting.py"], "app/billing.py": FIXTURE_FILES["app/billing.py"]}
    chunker = SelectiveChunker(raising={"app/billing.py"}, empty={"app/greeting.py"})
    outcome = _run(tmp_path, files=files, chunker=chunker)

    assert isinstance(outcome.error, ParseFailed), repr(outcome.error)
    assert str(outcome.error) == (
        "1 of the 2 indexable files failed to parse (raised in the chunker) and the other 1 "
        "produced no chunks; refusing to replace the repository's index with nothing"
    )
    assert outcome.result is None, "no write_results: the repository's chunks are left untouched"
    assert outcome.embedder.calls == []
    assert stages(outcome) == ["fetch", "parse"]
    assert job_dirs(outcome) == []


def test_three_of_four_files_raising_fails_the_job_though_one_parsed(tmp_path):
    """Clause 3: one trivial file parsing while the real ones raise would
    shrink the index to that file's chunks."""
    assert 3 > 4 * MAX_PARSE_ERROR_SHARE, "premise: 3 of 4 is over the threshold"
    chunker = SelectiveChunker(raising={"app/greeting.py", "app/billing.py", "app/storage.py"})
    outcome = _run(tmp_path, files=FOUR_FILES, chunker=chunker)

    assert isinstance(outcome.error, ParseFailed), repr(outcome.error)
    assert str(outcome.error) == (
        f"3 of the 4 indexable files failed to parse (raised in the chunker), more than the "
        f"{MAX_PARSE_ERROR_SHARE:.0%} the handler accepts (MAX_PARSE_ERROR_SHARE); refusing to "
        "replace the repository's index with what the other 1 produced"
    )
    assert sorted(chunker.calls) == sorted(p for p in FOUR_FILES if p.endswith(".py"))
    assert outcome.result is None
    assert outcome.embedder.calls == []
    assert stages(outcome) == ["fetch", "parse"]


def test_exactly_half_the_files_raising_completes_with_the_errors_counted(tmp_path):
    """Up to half may raise: one bad file must not dead-letter a repository."""
    assert not 2 > 4 * MAX_PARSE_ERROR_SHARE, "premise: 2 of 4 is within the threshold"
    chunker = SelectiveChunker(raising={"app/greeting.py", "app/billing.py"})
    outcome = _run(tmp_path, files=FOUR_FILES, chunker=chunker)

    assert outcome.error is None, repr(outcome.error)
    final = outcome.ctx.reports[-1][1]
    assert final["files_indexable"] == 4
    assert final["files_parsed"] == 2
    assert final["parse_errors"] == 2
    assert final["chunks_stored"] > 0


def test_files_that_yield_no_chunks_with_nothing_raised_complete(tmp_path):
    """Clause 2 needs a raise (PR #66's review, B I2). Indexable files that
    legitimately produce no chunks -- a tree of empty `__init__.py` files --
    with nothing raised are the truth about the repository, not a failure;
    without `errors` in clause 2 every such repository would be failed, and
    dead-lettered after five attempts."""
    files = {"pkg/__init__.py": b"", "pkg/sub/__init__.py": b""}
    chunker = SelectiveChunker(empty=set(files))
    outcome = _run(tmp_path, files=files, chunker=chunker)

    assert outcome.error is None, repr(outcome.error)
    final = outcome.ctx.reports[-1][1]
    assert final["files_indexable"] == 2
    assert final["files_parsed"] == 2
    assert final["parse_errors"] == 0
    assert final["chunks_stored"] == 0
    assert sorted(chunker.calls) == sorted(files)


def test_a_tree_with_nothing_indexable_is_not_a_parse_failure(tmp_path):
    """The guard is about files that RAISED, not about an empty result.

    A repository whose only files are unsupported has nothing to index, and
    an empty index is then the truth about it; nothing raised, so the job
    completes with zero chunks rather than failing five times.
    """
    outcome = _run(tmp_path, files={"docs/notes.txt": b"no language here\n", ".env": b"X=1\n"})
    assert outcome.error is None, repr(outcome.error)
    final = outcome.ctx.reports[-1][1]
    assert final["files_indexable"] == 0 and final["chunks_stored"] == 0
    assert final["skipped"] == {"secret": 1, "unsupported": 1}


def test_an_embedder_that_returns_no_vector_fails_plainly(tmp_path):
    """`_embed` refuses a result with a vector missing (PR #58's review, B-N2).

    The writer would refuse it too, inside `complete()`'s transaction; this
    stops it before the store, as an ordinary failure naming the count.
    """

    class ForgetfulEmbedder(FakeEmbedder):
        def generate_embeddings_for_chunks(self, chunks, use_cache=True):
            vectors = super().generate_embeddings_for_chunks(chunks, use_cache)
            vectors.pop(next(iter(vectors)))
            return vectors

    outcome = _run(tmp_path, embedder=ForgetfulEmbedder())
    assert isinstance(outcome.error, RuntimeError), repr(outcome.error)
    assert "returned no vector for 1 of" in str(outcome.error)
    assert outcome.result is None
    assert stages(outcome) == ["fetch", "parse", "embed"]


def test_a_file_name_with_a_control_character_never_reaches_a_log_line(tmp_path, caplog):
    """PR #58's review, A-L1: a file name must not be able to forge a log line.

    The chunker logs the path it is chunking. A member named with a newline
    and a forged record used to be extracted, indexed and logged raw; the
    fetcher now refuses it (`unsafe_path`), so it never reaches the chunker,
    a log line or `chunks.file_path`.
    """
    caplog.set_level(logging.DEBUG)
    forged = "app/x\n2026-09-29 20:17:10,248 INFO workers.jobs.transitions job FORGED: complete.py"
    files = dict(FIXTURE_FILES)
    files[forged] = b"def forged():\n    return 1\n"
    seen: List[str] = []
    real = SemanticChunker()

    class RecordingChunker:
        def chunk_file(self, path, content, language):
            seen.append(path)
            return real.chunk_file(path, content, language)

    outcome = _run(tmp_path, files=files, chunker=RecordingChunker())
    assert outcome.error is None, repr(outcome.error)
    assert forged not in seen and not any("\n" in path for path in seen)
    final = outcome.ctx.reports[-1][1]
    assert final["skipped"].get("unsafe_path") == 1, final["skipped"]
    assert "FORGED" not in caplog.text, "a customer's file name forged a log line"


# ---------------------------------------------------------------------
# The endings
# ---------------------------------------------------------------------


def test_a_refused_token_raises_lease_lost_and_touches_github_not_at_all(tmp_path):
    """Fact-check c2: never `return None`, which would complete undone work."""
    outcome = _run(tmp_path, answer="refused")

    assert isinstance(outcome.error, LeaseLost), repr(outcome.error)
    assert outcome.result is None
    assert stages(outcome) == ["fetch"]
    assert outcome.github.requests == [], "no token, so no fetch and nothing to revoke"
    assert job_dirs(outcome) == []


def test_a_misrouted_internal_api_fails_plainly_never_as_a_lost_lease(tmp_path):
    """Fact-check c3: an UNMARKED 404 is a misconfiguration, and must say so."""
    outcome = _run(tmp_path, answer="misrouted")

    assert isinstance(outcome.error, InternalApiMisrouted), repr(outcome.error)
    assert not isinstance(outcome.error, LeaseLost), (
        "a misrouted INTERNAL_API_URL read as a lost lease would expire five times "
        "with last_error NULL"
    )
    assert "INTERNAL_API_URL" in str(outcome.error)
    assert outcome.github.requests == []
    assert job_dirs(outcome) == []


@pytest.mark.parametrize(
    "answer,expected",
    [("suspended", InstallationSuspended), ("uninstalled", InstallationUninstalled)],
)
def test_the_installation_exceptions_propagate_unchanged(tmp_path, answer, expected):
    """The runtime dispatches on the type: defer an hour, or abandon."""
    outcome = _run(tmp_path, answer=answer)

    assert type(outcome.error) is expected, repr(outcome.error)
    assert not isinstance(outcome.error, (Rejected, LeaseLost)), (
        "a suspension must never dead-letter a repository, and neither ending is a lost lease"
    )
    assert outcome.github.requests == []
    assert job_dirs(outcome) == []


@pytest.mark.parametrize("stage", ["parse", "embed", "store"])
def test_a_shutdown_between_stages_raises_unfinished(tmp_path, stage):
    """The job is still ours during a shutdown: back to the queue, attempt kept.

    The shutdown arrives just after the stage BEFORE `stage` was reported,
    so the checkpoint on entry to `stage` is the one that stops it, and
    `last_stage` is the stage the job was actually in.
    """
    previous = STAGES[STAGES.index(stage) - 1]

    def arrive(ctx, reported):
        if reported == previous:
            ctx.stopping = True

    outcome = _run(tmp_path, ctx=FakeContext(on_report=arrive))

    assert isinstance(outcome.error, Unfinished), repr(outcome.error)
    assert outcome.result is None, "an unfinished handler must never return write_results"
    assert stages(outcome)[-1] == previous
    assert len(outcome.github.revocations()) == 1, "the fetch ended, so the token was revoked"
    assert job_dirs(outcome) == []


@pytest.mark.parametrize("stage", ["fetch", "parse", "embed"])
def test_a_lost_lease_between_stages_raises_lease_lost(tmp_path, stage):
    """`should_abort()` is `LeaseLost`, never a return (fact-check c2).

    The lease vanishes just after `stage` is reported; the next checkpoint
    -- the next stage's entry, or the first file or slice inside this one --
    raises.
    """

    def vanish(ctx, reported):
        if reported == stage:
            ctx.aborted = True

    outcome = _run(tmp_path, ctx=FakeContext(on_report=vanish))

    assert isinstance(outcome.error, LeaseLost), repr(outcome.error)
    assert outcome.result is None
    assert stages(outcome)[-1] == stage
    assert job_dirs(outcome) == []


def test_a_lease_lost_after_the_store_report_is_left_to_the_runtime(tmp_path):
    """`store` is the handler's last checkpoint. After it the handler returns,
    and the RUNTIME's own check -- `lease_lost` before `complete` -- is what
    writes nothing: `test_job_worker_runtime.py::
    test_a_supersede_mid_run_aborts_the_handler_and_writes_nothing` pins that
    half (`probe.calls == 0`)."""

    def vanish(ctx, reported):
        if reported == "store":
            ctx.aborted = True

    outcome = _run(tmp_path, ctx=FakeContext(on_report=vanish))
    assert outcome.error is None and callable(outcome.result)
    assert job_dirs(outcome) == []


class _StoreCursor:
    """A cursor for `write_results` with no database: records every statement.

    `fence_row` decides whether `FENCE_CHECK_SQL` finds the job. The store's
    two chunk writes are replaced by the test, so only the statements
    `write_results` runs itself reach `execute`.
    """

    def __init__(self, calls: List[str], fence_row: bool) -> None:
        from workers.jobs.transitions import FENCE_CHECK_SQL

        self._fence_sql = FENCE_CHECK_SQL
        self.calls = calls
        self.fence_row = fence_row
        self._last: Optional[str] = None
        self.rowcount = 1

    def execute(self, sql: str, params: Any = None) -> None:
        from workers.jobs.transitions import ATTACH_RUN_SQL, RESOLVE_RUN_SQL
        from workers.storage.postgres_writer import COMPLETE_RUN_SQL

        names = {self._fence_sql: "fence_check", RESOLVE_RUN_SQL: "resolve_run",
                 ATTACH_RUN_SQL: "attach_run", COMPLETE_RUN_SQL: "complete_run"}
        self.calls.append(names.get(sql, "other"))
        self._last = sql

    def fetchone(self):
        if self._last is self._fence_sql:
            return (1,) if self.fence_row else None
        return (uuid.uuid4(),)


def _spy_on_the_chunk_writes(monkeypatch, calls: List[str]) -> None:
    from workers.storage.postgres_writer import PostgresWriter

    monkeypatch.setattr(PostgresWriter, "delete_repository_chunks_on",
                        staticmethod(lambda cur, repository_id: calls.append("delete") or 0))
    monkeypatch.setattr(PostgresWriter, "insert_chunks_on",
                        classmethod(lambda cls, cur, *a, **k: calls.append("insert") or {}))


def test_a_fence_that_finds_no_row_starts_no_store(tmp_path, monkeypatch):
    """A-L3's fence, the control flow (22.1-05): zero rows -> `LeaseLost`, and
    neither chunk write is called. The fence's PREDICATES are tested on a real
    database (`test_ingest_end_to_end.py::
    test_a_reclaimed_or_superseded_worker_never_starts_the_store`); fakes
    cannot kill a SQL mutation."""
    outcome = _run(tmp_path)
    assert outcome.error is None and callable(outcome.result)
    calls: List[str] = []
    _spy_on_the_chunk_writes(monkeypatch, calls)

    with pytest.raises(LeaseLost, match="fence check"):
        outcome.result(_StoreCursor(calls, fence_row=False))
    assert calls == ["fence_check"], f"the store started for a job that is not ours: {calls}"


def test_the_store_takes_the_job_row_lock_last(tmp_path, monkeypatch):
    """A-L3's order (22.1-05): the fence first, the attach -- the first
    statement that locks the job row -- last. The heartbeat that this order
    frees is measured against a real database by `test_ingest_end_to_end.py::
    test_a_long_store_never_blocks_its_own_heartbeat`."""
    outcome = _run(tmp_path)
    assert outcome.error is None and callable(outcome.result)
    calls: List[str] = []
    _spy_on_the_chunk_writes(monkeypatch, calls)

    outcome.result(_StoreCursor(calls, fence_row=True))
    assert calls == ["fence_check", "resolve_run", "delete", "insert", "complete_run", "attach_run"], calls


def test_a_progress_report_that_matches_no_row_raises_lease_lost(tmp_path):
    outcome = _run(tmp_path, ctx=FakeContext(land=lambda stage: stage != "parse"))

    assert isinstance(outcome.error, LeaseLost), repr(outcome.error)
    assert stages(outcome) == ["fetch", "parse"]
    assert outcome.embedder.calls == [], "nothing is embedded for a job that is not ours"
    assert job_dirs(outcome) == []


def test_a_fetch_report_that_matches_no_row_stops_before_asking_for_a_token(tmp_path):
    """`_enter`'s own guard, made observable (PR #58's review, B-L1).

    The fake, like the runtime, raises the abort flag on a report that lands
    nowhere, so the NEXT checkpoint would stop the job anyway -- but only
    after the stage's first piece of work, which at `fetch` is asking the
    token route to mint a token for a job that is not ours. The guard stops
    it before that request, which is what this pins: no token requested,
    GitHub never touched.
    """
    outcome = _run(tmp_path, ctx=FakeContext(land=lambda stage: stage != "fetch"))

    assert isinstance(outcome.error, LeaseLost), repr(outcome.error)
    assert "progress report matched no row" in str(outcome.error)
    assert outcome.route.requests == [], "a token was requested for a job that is not ours"
    assert outcome.github.requests == []
    assert job_dirs(outcome) == []


def test_a_shutdown_during_parsing_stops_at_the_next_file(tmp_path):
    """The per-file checkpoint in `_parse`, made observable (PR #58's review, B-L1).

    Without it a shutdown that arrives while the first file is being chunked
    would still be caught -- at the `embed` entry, with the same
    `last_stage` -- but only after every other file had been parsed. The
    chunker raises the flag on its first call; exactly one call is made.
    """
    ctx = FakeContext()
    real = SemanticChunker()
    calls: List[str] = []

    class SignallingChunker:
        def chunk_file(self, path, content, language):
            calls.append(path)
            ctx.stopping = True  # SIGTERM arrives while this file is chunked
            return real.chunk_file(path, content, language)

    outcome = _run(tmp_path, ctx=ctx, chunker=SignallingChunker())

    assert isinstance(outcome.error, Unfinished), repr(outcome.error)
    assert len(calls) == 1, f"parsed {len(calls)} files after the shutdown arrived: {calls}"
    assert stages(outcome)[-1] == "parse"
    assert job_dirs(outcome) == []


def test_a_lost_lease_and_a_shutdown_at_once_is_a_lost_lease(tmp_path):
    """When both hold, nothing may be written, so `LeaseLost` wins over `Unfinished`."""

    def both(ctx, reported):
        if reported == "parse":
            ctx.aborted = True
            ctx.stopping = True

    outcome = _run(tmp_path, ctx=FakeContext(on_report=both))
    assert isinstance(outcome.error, LeaseLost), repr(outcome.error)


def test_a_shutdown_during_embedding_stops_at_the_next_slice(tmp_path):
    ctx = FakeContext()

    def signal_mid_embed(calls_so_far: int) -> None:
        if calls_so_far == 0:
            ctx.stopping = True  # SIGTERM arrives while the first slice is embedded

    embedder = FakeEmbedder(before_call=signal_mid_embed)
    outcome = _run(tmp_path, ctx=ctx, embedder=embedder, embed_slice=1)

    assert isinstance(outcome.error, Unfinished), repr(outcome.error)
    assert len(embedder.calls) == 1, "the slices after the signal were not embedded"
    assert stages(outcome)[-1] == "embed"


def test_the_chunk_cap_rejects_before_anything_is_embedded(tmp_path):
    """U6's 100,000 chunks, at a test-sized cap: `Rejected`, dead in one attempt."""
    outcome = _run(tmp_path, max_chunks=2)

    assert isinstance(outcome.error, Rejected), repr(outcome.error)
    assert not isinstance(outcome.error, FetchRejected), "the chunk cap is the handler's, not the fetcher's"
    assert "more than 2 chunks" in outcome.error.reason
    assert "ghs_" not in str(outcome.error) and SENTINEL_TOKEN not in str(outcome.error)
    assert outcome.embedder.calls == [], "a repository over the cap is never sent to OpenAI"
    assert len(outcome.github.revocations()) == 1
    assert job_dirs(outcome) == []


def test_a_fetch_cap_is_a_rejection_too(tmp_path):
    outcome = _run(tmp_path, limits=Limits(max_indexable_files=2))

    assert isinstance(outcome.error, FetchRejected), repr(outcome.error)
    assert isinstance(outcome.error, Rejected), "FetchRejected is a Rejected since 22-05"
    assert stages(outcome) == ["fetch"]
    assert len(outcome.github.revocations()) == 1, "revoked on a failed fetch as on a good one"
    assert job_dirs(outcome) == []


# ---------------------------------------------------------------------
# The token, the logs, and a failed revocation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["status", "transport"])
def test_a_failed_revocation_never_fails_the_job_and_logs_no_token(tmp_path, caplog, failure):
    caplog.set_level(logging.DEBUG)
    archive = make_archive(FIXTURE_FILES)
    github = (
        FakeGitHub(archive, revoke_status=500)
        if failure == "status"
        else FakeGitHub(archive, revoke_error=httpx.ConnectTimeout(f"timed out with {SENTINEL_TOKEN}"))
    )
    outcome = _run(tmp_path, github=github)

    assert outcome.error is None, repr(outcome.error)
    assert callable(outcome.result)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("revok" in message for message in warnings), warnings
    assert SENTINEL_TOKEN not in caplog.text
    assert "ghs_" not in caplog.text


def test_nothing_logs_the_token_the_lease_owner_or_the_link(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)  # the fetcher must hold it back itself
    outcome = _run(tmp_path)
    assert outcome.error is None, repr(outcome.error)

    text = caplog.text
    assert SENTINEL_TOKEN not in text
    assert "ghs_" not in text
    assert outcome.ctx.worker_id not in text, "the lease owner is a credential too"
    assert LINK_SECRET not in text
    assert "Authorization" not in text and "authorization" not in text
    # What IS logged: the revocation's status and the link's parameter NAMES.
    assert "revoked the repository token for acme/widgets" in text
    assert "(HTTP 204)" in text
    assert "query parameter names ['token']" in text


# ---------------------------------------------------------------------
# The registry and the configuration
# ---------------------------------------------------------------------


def test_the_registry_runs_the_full_ingest_for_both_job_types():
    assert set(REGISTRY) == {"full_ingest", "incremental"}, "the two keys 000014's CHECK allows"
    assert REGISTRY["full_ingest"] is run_full_ingest
    assert REGISTRY["incremental"] is run_full_ingest, "until 22.1-02 makes incremental distinct"


def test_the_configuration_refuses_a_missing_internal_api_url():
    with pytest.raises(ConfigurationError, match="INTERNAL_API_URL is not set"):
        deps_from_env({"OPENAI_API_KEY": "sk-test-dummy"})


def test_the_configuration_refuses_a_non_http_internal_api_url_without_echoing_it():
    with pytest.raises(ConfigurationError) as raised:
        deps_from_env(
            {"INTERNAL_API_URL": "ftp://user:hunter2@backend:8081", "OPENAI_API_KEY": "sk-test-dummy"}
        )
    assert "INTERNAL_API_URL" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_the_configuration_refuses_a_missing_openai_key():
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY is not set"):
        deps_from_env({"INTERNAL_API_URL": "http://backend:8081"})


def test_the_configuration_builds_the_real_components(tmp_path):
    deps = deps_from_env(
        {
            "INTERNAL_API_URL": "http://backend:8081",
            "OPENAI_API_KEY": "sk-test-dummy",
            "WORKER_WORKDIR": str(tmp_path),
        }
    )
    assert deps.internal_api_url == "http://backend:8081"
    assert deps.workdir == str(tmp_path)
    assert deps.embedder.model == "text-embedding-ada-002", "ada-002 through the storage move (U3)"
    assert deps.max_chunks == 100_000, "U6"
    assert not hasattr(deps, "private_key"), "the App key never enters the worker (U4)"
