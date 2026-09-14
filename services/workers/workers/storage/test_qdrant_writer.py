"""QdrantWriter.upsert_embeddings, with the Qdrant client mocked.

Sending every point in one request failed for any ingestion of more than
somewhere between 1,000 and 1,500 chunks, because Qdrant rejects a request body
over its 32 MB limit. These pin the batching, and the clean-up that keeps a
failed upsert from leaving vectors searchable.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from workers.storage.qdrant_writer import UPSERT_BATCH_SIZE, QdrantWriter


def _writer():
    with patch("workers.storage.qdrant_writer.QdrantClient") as client_cls:
        writer = QdrantWriter(url="http://qdrant.invalid")
    client = client_cls.return_value
    client.get_collections.return_value.collections = []
    return writer, client


def _embeddings(n):
    ids = [uuid4() for _ in range(n)]
    embeddings = {chunk_id: [0.0] * 4 for chunk_id in ids}
    metadata = {chunk_id: {"repository_id": "repo", "file_path": "main.go"} for chunk_id in ids}
    return embeddings, metadata


def _sent_batches(client):
    return [[point.id for point in call.kwargs["points"]] for call in client.upsert.call_args_list]


def test_upsert_splits_points_into_bounded_requests():
    writer, client = _writer()
    embeddings, metadata = _embeddings(600)

    assert writer.upsert_embeddings(embeddings, metadata, batch_size=256) == 600

    batches = _sent_batches(client)
    assert [len(batch) for batch in batches] == [256, 256, 88]
    assert sorted(point_id for batch in batches for point_id in batch) == sorted(str(i) for i in embeddings)
    client.delete.assert_not_called()


def test_default_batch_stays_well_under_qdrant_request_limit():
    """About 34 KB of JSON per 1536-dimension point, against a 32 MB default cap."""
    assert UPSERT_BATCH_SIZE * 34_258 < 16 * 2**20


def test_failed_batch_deletes_every_point_this_call_sent():
    """Including the batch that raised, which Qdrant may have applied anyway."""
    writer, client = _writer()
    embeddings, metadata = _embeddings(400)
    client.upsert.side_effect = [None, RuntimeError("connection aborted")]

    with pytest.raises(RuntimeError, match="connection aborted"):
        writer.upsert_embeddings(embeddings, metadata, batch_size=256)

    first, second = _sent_batches(client)
    assert (len(first), len(second)) == (256, 144)
    client.delete.assert_called_once()
    assert client.delete.call_args.kwargs["points_selector"].points == first + second


def test_a_batch_that_times_out_is_still_deleted():
    """A client-side timeout can arrive after Qdrant has stored the batch.

    Found in review of PR #30: with a delayed reply, Qdrant kept the batch while
    the writer raised "timed out", and the first version of this clean-up only
    deleted batches that had returned successfully.
    """
    writer, client = _writer()
    embeddings, metadata = _embeddings(10)
    client.upsert.side_effect = TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        writer.upsert_embeddings(embeddings, metadata)

    assert client.delete.call_args.kwargs["points_selector"].points == _sent_batches(client)[0]


def test_a_failed_clean_up_does_not_hide_the_original_error():
    writer, client = _writer()
    embeddings, metadata = _embeddings(10)
    client.upsert.side_effect = RuntimeError("connection aborted")
    client.delete.side_effect = RuntimeError("delete failed too")

    with pytest.raises(RuntimeError, match="connection aborted"):
        writer.upsert_embeddings(embeddings, metadata)


def test_no_embeddings_make_no_requests():
    writer, client = _writer()

    assert writer.upsert_embeddings({}, {}) == 0
    client.upsert.assert_not_called()
    client.delete.assert_not_called()


def test_batch_size_must_be_positive():
    writer, _ = _writer()
    embeddings, metadata = _embeddings(1)

    with pytest.raises(ValueError, match="batch_size"):
        writer.upsert_embeddings(embeddings, metadata, batch_size=0)
