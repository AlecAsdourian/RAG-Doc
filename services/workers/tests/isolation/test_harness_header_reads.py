"""The quality harness's record-header reads, on a real database, as the app role (22.2-01).

`rag_quality_harness.database_chunk_set` names what a record measured: the
chunk-set digest of the rows with the run's model, the rows per model, the
stored-vector digest, and how many rows' `breadcrumb` column disagrees with
their metadata. These are contract tests on the production statements, written
through the production writer, read under the tenant as `rag_doc_app`. They
replace a test that matched the function's source text (review B, finding 5).

No OpenAI call: the chunks come from the repository's own chunker and the
vectors are fixed lists.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace
from typing import Dict, List

import pytest

from workers.chunker.semantic_chunker import SemanticChunker
from workers.db import require_tenant
from workers.storage.postgres_writer import PostgresWriter, content_hash

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "rag_quality_harness.py"
_spec = importlib.util.spec_from_file_location("rag_quality_harness_isolation", _SCRIPT)
harness = importlib.util.module_from_spec(_spec)
sys.modules["rag_quality_harness_isolation"] = harness
_spec.loader.exec_module(harness)

MODEL = "test-fixed"
OTHER_MODEL = "test-other"
SOURCE = '''class Store:
    """Keeps things."""

    def save(self, item):
        """Saves one item."""
        return item


def helper():
    return 1
'''


def _dsn(container) -> str:
    return container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def _chunks():
    chunks = SemanticChunker().chunk_file("pkg/store.py", SOURCE, "python")
    assert any(c.metadata.get("docstring") for c in chunks) and any(c.metadata.get("breadcrumb") for c in chunks)
    return chunks


def _vectors(chunks, fill: float) -> Dict[str, List[float]]:
    return {content_hash(c.content): [fill + i * 1e-3] * 1536 for i, c in enumerate(chunks)}


def _ingest(container, org, chunks, model: str, fill: float, sha: str) -> None:
    writer = PostgresWriter(_dsn(container))
    writer.connect()
    with writer.conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    writer.conn.commit()
    try:
        run_id = writer.create_ingestion_run(organization_id=org.id, repository_id=org.repo_id,
                                             commit_sha=sha * 40, branch="main")
        writer.insert_chunks(organization_id=org.id, chunks=list(chunks), ingestion_run_id=run_id,
                             repository_id=org.repo_id, embeddings=_vectors(chunks, fill), embedding_model=model)
        writer.complete_ingestion_run(organization_id=org.id, ingestion_run_id=run_id, chunks_count=len(chunks))
    finally:
        writer.close()


def _read(app_dsn, org, model=MODEL) -> dict:
    return harness.database_chunk_set(SimpleNamespace(repository_id=org.repo_id), model, dsn=app_dsn, org=org.id)


def test_the_database_digest_equals_the_offline_digest_of_the_same_chunks(test_db_container, app_dsn, with_two_orgs):
    org_a, org_b = with_two_orgs
    chunks = _chunks()
    _ingest(test_db_container, org_a, chunks, MODEL, 0.01, "a")
    _ingest(test_db_container, org_a, chunks[:1], OTHER_MODEL, 0.02, "c")

    read = _read(app_dsn, org_a)
    assert (read["chunk_set_digest"], read["chunk_rows"]) == harness.chunk_digest.digest_of_chunks(chunks)
    # Only the run's model's rows are in the digest; every model is counted.
    assert read["chunk_models"] == {MODEL: len(chunks), OTHER_MODEL: 1}
    assert read["breadcrumb_column_mismatches"] == 0
    # Another tenant reads none of it under its own scope.
    other = harness.database_chunk_set(SimpleNamespace(repository_id=org_a.repo_id), MODEL, dsn=app_dsn,
                                       org=org_b.id)
    assert other["chunk_rows"] == 0 and other["chunk_models"] == {}


def test_the_stored_vectors_digest_names_one_ingest(test_db_container, app_dsn, with_two_orgs):
    org_a, _ = with_two_orgs
    chunks = _chunks()
    _ingest(test_db_container, org_a, chunks, MODEL, 0.01, "a")
    first, again = _read(app_dsn, org_a), _read(app_dsn, org_a)
    assert first["stored_vectors_digest"] == again["stored_vectors_digest"]

    # A re-ingest of the same chunks with the same vectors still makes new rows:
    # a different ingest, so a different digest, while the chunk set is equal.
    with require_tenant(_conn(test_db_container), org_a.id) as cur:
        cur.execute("DELETE FROM ingestion_runs WHERE repository_id = %s", (org_a.repo_id,))
    _ingest(test_db_container, org_a, chunks, MODEL, 0.01, "d")
    after = _read(app_dsn, org_a)
    assert after["chunk_set_digest"] == first["chunk_set_digest"]
    assert after["stored_vectors_digest"] != first["stored_vectors_digest"]


def test_a_breadcrumb_column_that_disagrees_with_metadata_is_counted(test_db_container, app_dsn, with_two_orgs):
    org_a, _ = with_two_orgs
    _ingest(test_db_container, org_a, _chunks(), MODEL, 0.01, "a")
    with require_tenant(_conn(test_db_container), org_a.id) as cur:
        cur.execute("UPDATE chunks SET breadcrumb = 'tampered' WHERE repository_id = %s "
                    "AND breadcrumb IS NOT NULL AND ctid IN (SELECT ctid FROM chunks WHERE repository_id = %s "
                    "AND breadcrumb IS NOT NULL LIMIT 1)", (org_a.repo_id, org_a.repo_id))
    assert _read(app_dsn, org_a)["breadcrumb_column_mismatches"] == 1


_conns: list = []


def _conn(container):
    """An app-role connection for setup writes, closed at module teardown."""
    import psycopg2

    conn = psycopg2.connect(_dsn(container))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET ROLE rag_doc_app")
    conn.autocommit = False
    _conns.append(conn)
    return conn


@pytest.fixture(autouse=True, scope="module")
def _close_conns():
    yield
    for conn in _conns:
        conn.close()
