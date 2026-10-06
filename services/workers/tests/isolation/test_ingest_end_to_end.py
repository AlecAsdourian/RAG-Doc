"""A repository ingested end to end through the real `Worker` (22-05).

Enqueue with the producer's statement -> the real `Worker` claims, checks
the installation and marks the job started -> the real `full_ingest`
handler fetches, parses, embeds and stores -> `complete()` commits the
chunks with the completion -> a real `QueryEngine` searches them. All of it
against PostgreSQL 16 with pgvector, as `rag_doc_app`.

FAKES ONLY AT THE NETWORK EDGES (`tests/ingest/fakes.py`): the backend's
token route and GitHub are `httpx.MockTransport`s under the REAL
`request_token`, `fetch_repository` and `revoke_token`, and the embedding
API is `text_vector` behind a real `EmbeddingGenerator` (model
`test-fixed`), so batching, the writer's hash lookup and the model column
are the production path.

⚠ THE APP-ROLE PREMISE. Every connection here, the worker's included, is
`app_dsn`'s `rag_doc_app` (NOSUPERUSER NOBYPASSRLS), asserted rather than
trusted: a superuser DSN bypasses row-level security, and the isolation
assertion below would prove nothing. The superuser connection is used for
two premise checks only (the drift query, and what exists regardless of
RLS), never by the code under test.

⚠ EVERY ENDING IS HERE, against the row that survives: completed; a refused
token (nothing written, the job still `running`); a mid-run suspension
(deferred an hour, never `dead`, the documented `syncing` hour); a
misrouted internal API (a loud, retried failure); a chunker failing on
every file (a retried failure, the good index kept); a mid-run uninstall
(abandoned); a cap (`dead` in one attempt); a shutdown during embedding
(deferred, attempt handed back). Logs are read from `caplog` in every test:
no `ghs_`, no `Authorization`.

A WORKER CLAIMS QUEUE-WIDE (21-CONTEXT L5), so each test backdates its own
job and asserts it is the only claimable row, as the runtime suite does, and
takes any job it leaves `running` out of the live set before it ends.
Nothing here deletes a row it did not create.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.ingest.fakes import (
    FIXTURE_FILES,
    GITHUB_API,
    INTERNAL,
    SENTINEL_TOKEN,
    SHA,
    TEST_MODEL,
    FakeGitHub,
    FakeTokenRoute,
    make_archive,
    text_vector,
)
from tests.isolation.test_job_worker_runtime import (
    SETTLE,
    assert_only_claimable,
    backdate,
    build_worker,
    job_row,
    job_when,
    link_installation,
    query,
    repo_row,
    running,
    seed_job,
    sql,
    until,
)
from workers.chunker import SemanticChunker
from workers.db import require_tenant
from workers.embeddings import EmbeddingGenerator
from workers.fetch import Limits
from workers.ingest import IngestDeps, make_full_ingest_handler
from workers.retrieval.query_engine import QueryEngine

IDENTITY_SQL = (
    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
)

# `stalled`, exactly as 21-07's endpoint computes it: the `running`-with-a-
# dead-lease branch the claim and the sweeper share.
STALLED_SQL = (
    "SELECT state = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < NOW()) "
    "AS stalled FROM ingestion_jobs WHERE id = %s"
)


# =====================================================================
# Fixtures and helpers
# =====================================================================


@pytest.fixture
def conn(app_dsn: str):
    """The test's own connection, as the app role, separate from the worker's."""
    connection = psycopg2.connect(app_dsn)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def superuser_conn(test_db_container):
    """For premise checks with RLS bypassed. Never handed to code under test."""
    connection = psycopg2.connect(
        test_db_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    )
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


def assert_app_role(connection: Any) -> None:
    with connection.cursor() as cur:
        cur.execute(IDENTITY_SQL)
        user, is_super, bypasses = cur.fetchone()
    connection.rollback()
    assert user == "rag_doc_app", user
    assert is_super is False, "a superuser bypasses row-level security; this would prove nothing"
    assert bypasses is False


def fake_embedding_generator() -> EmbeddingGenerator:
    """A REAL generator whose one network call is replaced by `text_vector`."""
    generator = EmbeddingGenerator(api_key="test-not-used", model=TEST_MODEL)
    generator.client.generate_embeddings_batch = lambda texts: [text_vector(t) for t in texts]
    return generator


def deps_for(tmp_path, route: FakeTokenRoute, github: FakeGitHub, **overrides: Any) -> IngestDeps:
    values: Dict[str, Any] = dict(
        internal_api_url=INTERNAL,
        chunker=SemanticChunker(),
        embedder=fake_embedding_generator(),
        workdir=str(tmp_path / "work"),
        github_api=GITHUB_API,
        internal_transport=route.transport(),
        github_transport=github.transport(),
    )
    values.update(overrides)
    return IngestDeps(**values)


def ingest_worker(app_dsn: str, deps: IngestDeps, **worker_kwargs: Any):
    handler = make_full_ingest_handler(deps)
    return build_worker(app_dsn, {"full_ingest": handler, "incremental": handler}, **worker_kwargs)


def enqueue(conn: Any, org: Any) -> str:
    job_id = seed_job(conn, org)  # the producer's ENQUEUE_UPSERT_SQL, then `pending`
    backdate(conn, job_id)
    assert_only_claimable(conn, job_id)
    return job_id


def settled(row: dict) -> bool:
    """The worker has written an ending: terminal, or queued with a reason."""
    if row["state"] in ("completed", "dead", "superseded"):
        return True
    return row["state"] == "queued" and row["last_error"] is not None and row["lease_owner"] is None


def chunks_of(conn: Any, org: Any) -> List[dict]:
    with require_tenant(conn, org.id, cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, file_path, start_line, end_line, content, content_hash, "
            "embedding_model, embedding IS NOT NULL AS has_vector, "
            "vector_dims(embedding) AS dims, ingestion_run_id::text AS run_id "
            "FROM chunks WHERE repository_id = %s ORDER BY file_path, start_line, id",
            (org.repo_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def runs_of(conn: Any, org: Any) -> List[dict]:
    with require_tenant(conn, org.id, cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, commit_sha, branch, status, chunks_processed FROM ingestion_runs "
            "WHERE repository_id = %s ORDER BY started_at",
            (org.repo_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def assert_no_secret_logged(caplog) -> None:
    """Read from the captured records, as every "never logged" claim here is."""
    text = caplog.text
    assert SENTINEL_TOKEN not in text, "the installation token reached a log line"
    assert "ghs_" not in text, "a token-shaped string reached a log line"
    assert "Authorization" not in text and "authorization" not in text


def assert_no_drift(superuser_conn: Any, org: Any) -> None:
    """22-02's hand-off: every chunk's tenant is its repository's, and its run
    is its repository's -- read with RLS bypassed, so nothing is hidden."""
    with superuser_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM chunks c "
            "JOIN repositories r ON r.id = c.repository_id "
            "JOIN ingestion_runs ir ON ir.id = c.ingestion_run_id "
            "WHERE c.repository_id = %s "
            "  AND (c.organization_id <> r.organization_id OR ir.repository_id <> c.repository_id)",
            (org.repo_id,),
        )
        assert cur.fetchone()[0] == 0, "a stored chunk drifted from its repository's tenant or run"


def take_out_of_the_live_set(conn: Any, job_id: str) -> None:
    """For a job a test leaves `running`: so no later test's worker reclaims it."""
    sql(conn, "UPDATE ingestion_jobs SET state = 'superseded' WHERE id = %s", (job_id,))


class RefusalAt(FakeTokenRoute):
    """THE marked 404, recording WHEN it refused, so the test can show the
    lease was still live in the database at that moment."""

    def __init__(self) -> None:
        super().__init__("refused")
        self.refused_at: List[datetime] = []

    def __call__(self, request):
        self.refused_at.append(datetime.now(timezone.utc))
        return super().__call__(request)


def search_engine(app_dsn: str) -> QueryEngine:
    """A real QueryEngine on the app role, embedding queries with `text_vector`."""
    engine = QueryEngine(postgres_conn=app_dsn, openai_api_key="test-not-used")
    engine.embedding_generator.model = TEST_MODEL  # read at query time, never copied
    engine.embedding_generator.client.generate_embeddings_batch = lambda texts: [
        text_vector(t) for t in texts
    ]
    return engine


def ingest_once(conn, app_dsn, tmp_path, org, caplog, files=None, **deps_overrides):
    """Enqueue, run a worker until the job settles, and return what matters."""
    route = FakeTokenRoute("ok")
    github = FakeGitHub(make_archive(FIXTURE_FILES if files is None else files))
    job_id = enqueue(conn, org)
    with running(ingest_worker(app_dsn, deps_for(tmp_path, route, github, **deps_overrides))):
        row = job_when(conn, job_id, settled, "the ingest to settle")
    return job_id, row, route, github


# =====================================================================
# The happy path, the search, and isolation
# =====================================================================


def test_a_repository_is_ingested_end_to_end_as_the_app_role(
    conn, superuser_conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    org_a, org_b = with_two_orgs
    assert_app_role(conn)
    link_installation(conn, org_a)
    link_installation(conn, org_b)

    job_id, row, route, github = ingest_once(conn, app_dsn, tmp_path, org_a, caplog)

    # The job row: completed in ONE attempt, `store` the last stage.
    assert row["state"] == "completed", row
    assert row["attempts"] == 1
    assert row["last_stage"] == "store"
    assert row["last_error"] is None
    assert row["lease_owner"] is None
    # ⚠ THE CUMULATIVE-PROGRESS RULE, on the row that survives: `skipped`
    # (the .env) was reported at `parse` and is still here after `store`.
    progress = row["progress"]
    assert progress["skipped"] == {"secret": 1}, progress
    assert progress["files_indexable"] == 3
    assert progress["files_parsed"] == 3
    assert progress["chunks_stored"] == progress["chunks"] == progress["chunks_embedded"] > 0
    assert progress["chunks_truncated"] == 0, "22.2-02: no fixture chunk is over the token limit"

    # The projection.
    repo = repo_row(conn, org_a.id, org_a.repo_id)
    assert repo["sync_state"] == "synced"
    assert repo["last_synced_at"] is not None

    # The run: the fixture's SHA and count, attached to the job.
    [run] = runs_of(conn, org_a)
    assert run["commit_sha"] == SHA
    assert run["branch"] == "main"
    assert run["status"] == "completed"
    chunks = chunks_of(conn, org_a)
    assert run["chunks_processed"] == len(chunks) == progress["chunks_stored"]
    assert str(row["ingestion_run_id"]) == run["id"], "the job points at its run"

    # Every chunk: its vector, the model that made it, and this run.
    assert chunks, "nothing was stored"
    assert all(c["has_vector"] and c["dims"] == 1536 for c in chunks)
    assert {c["embedding_model"] for c in chunks} == {TEST_MODEL}
    assert {c["run_id"] for c in chunks} == {run["id"]}
    assert {c["file_path"] for c in chunks} == {"app/greeting.py", "app/billing.py", "app/storage.py"}
    # The .env: never stored, and its content nowhere.
    assert not any(".env" in c["file_path"] for c in chunks)
    assert not any("must-never-be-indexed" in c["content"] for c in chunks)

    # The token: presented with this job's own lease, then revoked by itself.
    [token_request] = route.requests
    assert token_request.url.path == f"/internal/jobs/{job_id}/repository-token"
    [revocation] = github.revocations()
    assert revocation.headers["authorization"] == f"Bearer {SENTINEL_TOKEN}"

    # Drift (22-02's hand-off), read with RLS bypassed.
    assert_no_drift(superuser_conn, org_a)

    # The search, as the app role: A finds the fixture, with its provenance.
    engine = search_engine(app_dsn)
    try:
        found = engine.query(
            query_text="compute the invoice total and apply the sales tax rate",
            organization_id=org_a.id,
            repository_id=org_a.repo_id,
            top_k=5,
        )["results"]
        assert_app_role(engine.vector_retriever.conn)
        assert found, "A's own repository returned nothing"
        assert found[0]["file_path"] == "app/billing.py", [r["file_path"] for r in found]
        assert found[0]["provenance"]["commit_sha"] == SHA

        # As B, with A's repository id: nothing, from either leg.
        leaked = engine.query(
            query_text="compute the invoice total and apply the sales tax rate",
            organization_id=org_b.id,
            repository_id=org_a.repo_id,
            top_k=5,
        )["results"]
        assert leaked == [], "tenant B read tenant A's repository"
    finally:
        engine.vector_retriever.close()
        engine.fts_retriever.close()

    assert_no_secret_logged(caplog)


def test_an_oversized_chunk_is_counted_on_the_completed_row(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """22.2-02 (QA6): a chunk over the generator's token limit is embedded
    truncated, and the COMPLETED row's progress says how many rows were --
    beside `skipped`, which the cumulative dict still carries. The generator
    is the real one (only its API call is replaced), so the count is its rule."""
    caplog.set_level(logging.DEBUG)
    org_a, _ = with_two_orgs
    link_installation(conn, org_a)
    sentinel = "E2eHug3ContentS3ntinel"
    oversized = (
        b"def huge():\n    return [\n"
        + b"".join(b'        "item%d %s",\n' % (i, sentinel.encode()) for i in range(1500))
        + b"    ]\n"
    )
    files = dict(FIXTURE_FILES, **{"app/huge.py": oversized})

    _, row, _, _ = ingest_once(conn, app_dsn, tmp_path, org_a, caplog, files=files)

    assert row["state"] == "completed", row
    progress = row["progress"]
    assert progress["chunks_truncated"] == 1, progress
    assert progress["skipped"] == {"secret": 1}, progress
    assert progress["chunks_stored"] == len(chunks_of(conn, org_a))
    assert all(sentinel not in r.getMessage() for r in caplog.records), "a chunk's content was logged"
    assert_no_secret_logged(caplog)


def test_a_second_ingest_replaces_the_chunks_idempotently(
    conn, superuser_conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """Enqueue again: the same count, no duplicates, the same run reused.

    The replacement is `DELETE` by repository then `INSERT`, in the
    completion's transaction, so a retry, a rerun or a second push never
    doubles a repository (ISS-027's full-ingest half). The drift check runs
    on the replacement too (PR #58's review, B-N3): a re-ingest is the write
    that could attach new chunks to the wrong run.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    _, first_row, _, _ = ingest_once(conn, app_dsn, tmp_path, org, caplog)
    assert first_row["state"] == "completed", first_row
    first = chunks_of(conn, org)

    _, second_row, _, _ = ingest_once(conn, app_dsn, tmp_path, org, caplog)
    assert second_row["state"] == "completed", second_row
    second = chunks_of(conn, org)

    assert len(second) == len(first), "a re-ingest changed the chunk count"
    keys = [(c["file_path"], c["start_line"], c["end_line"], c["content_hash"]) for c in second]
    assert len(keys) == len(set(keys)), "a re-ingest left duplicate chunks"
    assert {c["id"] for c in first}.isdisjoint({c["id"] for c in second}), (
        "the second ingest must REPLACE the chunks, not keep the old rows"
    )
    [run] = runs_of(conn, org)
    assert run["commit_sha"] == SHA, "the same commit reuses its run (UNIQUE repository, sha)"
    assert run["chunks_processed"] == len(second)
    assert {c["run_id"] for c in second} == {run["id"]}
    assert_no_drift(superuser_conn, org)
    assert_no_secret_logged(caplog)


def test_a_chunker_that_fails_on_every_file_fails_the_job_and_keeps_the_index(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """PR #58's review (B-M3, A-L5), against the rows: the good index survives.

    A first ingest stores the fixture. A second, whose chunker raises on
    every file, must end as an ORDINARY failure -- the attempt consumed,
    `last_error` naming the count, a backoff -- and leave every chunk, its
    id and the run exactly as the first ingest wrote them. Before the guard
    it completed `synced` over an emptied index: `write_results` deleted
    every chunk of the repository and inserted none.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    _, first_row, _, _ = ingest_once(conn, app_dsn, tmp_path, org, caplog)
    assert first_row["state"] == "completed", first_row
    before, runs_before = chunks_of(conn, org), runs_of(conn, org)
    assert before, "premise: the first ingest stored an index for the second to protect"

    class BrokenChunker:
        def chunk_file(self, path, content, language):
            raise RuntimeError("the grammar is broken for every file")

    _, row, _, github = ingest_once(conn, app_dsn, tmp_path, org, caplog, chunker=BrokenChunker())

    assert row["state"] == "queued", f"an ordinary failure re-queues below max_attempts, not {row['state']!r}"
    assert row["attempts"] == 1, "the attempt is consumed: it is a failure, not a lost lease"
    assert row["lease_owner"] is None
    assert "ParseFailed" in row["last_error"], row["last_error"]
    assert "every one of the 3 indexable files failed to parse" in row["last_error"]
    assert row["run_after"] > row["updated_at"], "a failure takes a backoff"
    assert row["last_stage"] == "parse"
    assert chunks_of(conn, org) == before, "the repository's good index was replaced"
    assert runs_of(conn, org) == runs_before
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"
    assert len(github.revocations()) == 1, "the fetch ended, so the token was revoked"
    assert_no_secret_logged(caplog)


def test_a_reingest_leaves_a_retrieval_of_a_replaced_chunk_dangling(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """ISS-027's note, handled consciously: LEFT DANGLING, BY DESIGN (P17, U9).

    Replacing a repository's chunks is the event that leaves `retrievals`
    citing chunks that no longer exist. The user chose (U9, option A) to
    drop the key and let "a logged result keep a chunk id that may later
    point at nothing"; deleting the retrieval would destroy its feedback on
    every re-index, and repointing it is the link-shape decision deferred
    to when feedback ships. So the re-ingest must leave the retrieval, its
    chunk id and its feedback exactly as they were. This test pins that, so
    a change to it is a decision rather than an accident.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    _, row, _, _ = ingest_once(conn, app_dsn, tmp_path, org, caplog)
    assert row["state"] == "completed", row
    shown = chunks_of(conn, org)[0]["id"]

    with require_tenant(conn, org.id) as cur:
        cur.execute(
            "INSERT INTO queries (project_id, query_text) VALUES (%s, %s) RETURNING id::text",
            (org.project_id, "where is the greeting"),
        )
        query_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO retrievals (query_id, chunk_id, rank, shown_to_user) "
            "VALUES (%s, %s, 1, true) RETURNING id::text",
            (query_id, shown),
        )
        retrieval_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO feedback (retrieval_id, feedback_type) VALUES (%s, 'positive')",
            (retrieval_id,),
        )

    _, again, _, _ = ingest_once(conn, app_dsn, tmp_path, org, caplog)
    assert again["state"] == "completed", again

    with require_tenant(conn, org.id) as cur:
        cur.execute("SELECT chunk_id::text FROM retrievals WHERE id = %s", (retrieval_id,))
        survivor = cur.fetchone()
        cur.execute("SELECT count(*) FROM feedback WHERE retrieval_id = %s", (retrieval_id,))
        feedback = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM chunks WHERE id = %s", (shown,))
        chunk_left = cur.fetchone()[0]
    assert survivor is not None, "the re-ingest deleted a retrieval; P17 says leave it"
    assert survivor[0] == shown, "the retrieval must keep the chunk id it was shown"
    assert chunk_left == 0, "premise: the chunk it cites was replaced"
    assert feedback == 1, "the user's feedback must survive a re-index"


# =====================================================================
# The endings
# =====================================================================


def test_a_refused_token_writes_nothing_and_the_job_stays_running(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """Fact-check c2: `TokenRefused` is `LeaseLost`, never a completion.

    ⚠ THE PREMISE IS THE POINT: the lease is still LIVE in the database when
    the token is refused, and the heartbeat has not ticked (its interval is
    longer than the test). That is exactly the state in which a `return
    None` reaches `complete()`, whose fence does not check expiry, and
    writes `completed` for work that never ran -- which the mutation that
    maps `TokenRefused` back to `return None` reproduces here.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    route = RefusalAt()
    github = FakeGitHub(make_archive(FIXTURE_FILES))

    job_id = enqueue(conn, org)
    worker = ingest_worker(
        app_dsn,
        deps_for(tmp_path, route, github),
        lease=timedelta(seconds=60),
        heartbeat=timedelta(seconds=120),
    )
    with running(worker):
        until(lambda: route.refused_at, "the token route to refuse")
        time.sleep(0.5)  # a completion, if there were one, lands well within this
        row = job_row(conn, job_id)
    refused_at = route.refused_at

    assert row["state"] == "running", (
        f"a refused token wrote {row['state']!r}; it must write nothing (fact-check c2)"
    )
    assert row["lease_owner"] == worker.worker_id, "the job is left under the lease it was claimed with"
    assert row["lease_expires_at"] > refused_at[0], (
        "premise: the lease was LIVE in the database when the token was refused"
    )
    assert row["attempts"] == 1
    assert row["last_error"] is None
    assert row["last_stage"] == "fetch"
    assert chunks_of(conn, org) == [], "no chunk may be written for a refused token"
    assert runs_of(conn, org) == [], "no run may be written for a refused token"
    assert github.requests == [], "nothing was fetched, so there was nothing to revoke"
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "syncing", (
        "mark_started ran; nothing after it wrote a projection"
    )
    take_out_of_the_live_set(conn, job_id)
    assert_no_secret_logged(caplog)


def test_a_mid_run_suspension_defers_an_hour_and_never_dead_letters(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """Fact-check c1: the claim-time ending, mid-run. Deferred, never `dead`.

    ⚠ AND THE DOCUMENTED GAP IS PINNED: `sync_state` stays `syncing` --
    `mark_started` projected it, and `DEFER_SQL` (frozen since 21-05)
    projects nothing -- for up to the hour. The JOB ROW is the truth a UI
    must read: `queued`, `run_after` an hour out, a reason, no lease, and
    `stalled` false.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)
    github = FakeGitHub(make_archive(FIXTURE_FILES))

    job_id = enqueue(conn, org)
    worker = ingest_worker(
        app_dsn,
        deps_for(tmp_path, FakeTokenRoute("suspended"), github),
        suspended_defer=timedelta(minutes=60),
    )
    with running(worker):
        row = job_when(conn, job_id, settled, "the deferral")

    assert row["state"] == "queued", f"a mid-run suspension must defer, not {row['state']!r}"
    assert row["state"] != "dead"
    assert row["attempts"] == 0, "the attempt must be handed back"
    assert row["lease_expires_at"] is None
    assert "suspended" in row["last_error"]
    delay = query(
        conn,
        "SELECT EXTRACT(EPOCH FROM (run_after - updated_at)) AS s FROM ingestion_jobs WHERE id = %s",
        (job_id,),
    )[0]["s"]
    assert abs(float(delay) - 3600.0) < 5.0, f"run_after is {float(delay):.0f}s out, not an hour"
    assert query(conn, STALLED_SQL, (job_id,))[0]["stalled"] is False
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "syncing", (
        "the documented, accepted gap: the repository reads `syncing` for up to the hour"
    )
    assert chunks_of(conn, org) == [] and runs_of(conn, org) == []
    assert_no_secret_logged(caplog)


def test_a_misrouted_internal_api_fails_loudly_never_as_a_lost_lease(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """Fact-check c3: an UNMARKED 404 fails the job, with the reason recorded.

    Read as a lost lease, a misconfigured `INTERNAL_API_URL` would let the
    job expire five times with `last_error` NULL and nothing to say why.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    job_id = enqueue(conn, org)
    worker = ingest_worker(
        app_dsn, deps_for(tmp_path, FakeTokenRoute("misrouted"), FakeGitHub(make_archive(FIXTURE_FILES)))
    )
    with running(worker):
        row = job_when(conn, job_id, settled, "the failure")

    assert row["state"] == "queued", "an ordinary failure below max_attempts re-queues"
    assert row["attempts"] == 1, "the attempt is consumed"
    assert "InternalApiMisrouted" in row["last_error"]
    assert "INTERNAL_API_URL" in row["last_error"], "last_error names the misconfiguration"
    assert row["run_after"] > row["updated_at"], "a failure takes a backoff"
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"
    assert_no_secret_logged(caplog)


def test_a_mid_run_uninstall_abandons(conn, app_dsn, with_two_orgs, tmp_path, caplog):
    """The claim-time ending, mid-run: `superseded` and `never_synced`."""
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    job_id = enqueue(conn, org)
    worker = ingest_worker(
        app_dsn, deps_for(tmp_path, FakeTokenRoute("uninstalled"), FakeGitHub(make_archive(FIXTURE_FILES)))
    )
    with running(worker):
        row = job_when(conn, job_id, settled, "the abandonment")

    assert row["state"] == "superseded", row
    assert row["lease_owner"] is None
    assert "uninstalled" in row["last_error"]
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "never_synced", (
        "never `failed`: nothing failed, and the queue must not retry it"
    )
    assert chunks_of(conn, org) == [] and runs_of(conn, org) == []
    assert_no_secret_logged(caplog)


@pytest.mark.parametrize("cap", ["chunks", "files"])
def test_a_cap_ends_the_job_dead_in_one_attempt(conn, app_dsn, with_two_orgs, tmp_path, caplog, cap):
    """U6: a hard cap is `dead` at `attempts = 1`, with a plain, token-free reason.

    `chunks` is 22-05's own cap (checked after parsing, before embedding);
    `files` is the fetcher's (`FetchRejected`, a `Rejected` since 22-05).
    As a plain failure either would retry the whole download five times.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)
    github = FakeGitHub(make_archive(FIXTURE_FILES))
    overrides = {"max_chunks": 2} if cap == "chunks" else {"limits": Limits(max_indexable_files=2)}

    job_id = enqueue(conn, org)
    with running(ingest_worker(app_dsn, deps_for(tmp_path, FakeTokenRoute("ok"), github, **overrides))):
        row = job_when(conn, job_id, settled, "the rejection")

    assert row["state"] == "dead", f"a cap must end the job dead, not {row['state']!r}"
    assert row["attempts"] == 1, "in ONE attempt"
    assert row["lease_owner"] is None
    expected = "more than 2 chunks" if cap == "chunks" else "more than 2 indexable files"
    assert expected in row["last_error"], row["last_error"]
    assert "ghs_" not in row["last_error"]
    # The stage it stopped in: the chunk cap during parsing, the file cap
    # inside the fetch.
    assert row["last_stage"] == ("parse" if cap == "chunks" else "fetch"), row["last_stage"]
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "failed"
    assert chunks_of(conn, org) == [] and runs_of(conn, org) == []
    assert len(github.revocations()) == 1, "the fetch ended, so the token was revoked"
    assert_no_secret_logged(caplog)


def test_a_shutdown_during_embedding_defers_with_the_attempt_handed_back(
    conn, app_dsn, with_two_orgs, tmp_path, caplog
):
    """SIGTERM mid-embed: the job goes back to the queue, `last_stage = 'embed'`.

    The first embedding call blocks until the test has set the worker's
    stop flag -- a shutdown arriving mid-stage -- and the handler's
    checkpoint before the next slice raises `Unfinished`.
    """
    caplog.set_level(logging.DEBUG)
    org, _ = with_two_orgs
    link_installation(conn, org)

    embedding = threading.Event()
    may_continue = threading.Event()
    calls: List[int] = []
    generator = fake_embedding_generator()

    def blocking_batch(texts):
        calls.append(len(texts))
        embedding.set()
        may_continue.wait(timeout=SETTLE)
        return [text_vector(t) for t in texts]

    generator.client.generate_embeddings_batch = blocking_batch
    deps = deps_for(
        tmp_path,
        FakeTokenRoute("ok"),
        FakeGitHub(make_archive(FIXTURE_FILES)),
        embedder=generator,
        embed_slice=1,
    )

    job_id = enqueue(conn, org)
    with running(ingest_worker(app_dsn, deps)) as stop:
        assert embedding.wait(timeout=SETTLE), "the embed stage never started"
        stop.set()
        may_continue.set()
        row = job_when(conn, job_id, settled, "the deferral")

    assert row["state"] == "queued", row
    assert row["attempts"] == 0, "a shutdown hands the attempt back"
    assert row["last_stage"] == "embed", "the stage the job was in when it stopped"
    assert "shutting down" in row["last_error"]
    assert len(calls) == 1, "no slice was embedded after the shutdown"
    assert chunks_of(conn, org) == [] and runs_of(conn, org) == []
    assert repo_row(conn, org.id, org.repo_id)["sync_state"] == "syncing", (
        "DEFER_SQL projects nothing, so mark_started's `syncing` stands until the job runs again"
    )
    assert_no_secret_logged(caplog)
