"""Tests that AnswerGenerator does not turn a retrieval failure into an answer (ISS-030).

A failed retrieval must propagate as RetrievalError. The "I don't have enough
information" reply is for a retrieval that succeeded and matched nothing; giving
it after an outage presents the outage as a fact about the code. The failure
must not be cached either.

OpenAI and tiktoken are patched when the generator is built, so nothing here
touches the network.
"""

import logging
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from workers.generation.answer_generator import AnswerGenerator
from workers.retrieval import RetrievalError

SENTINEL = "sk-SENTINEL-must-not-leak"


def _generator(query_engine, semantic_cache=None) -> AnswerGenerator:
    with patch("workers.generation.answer_generator.OpenAI"), patch(
        "workers.generation.answer_generator.tiktoken"
    ):
        return AnswerGenerator(
            query_engine=query_engine,
            openai_api_key="unused",
            semantic_cache=semantic_cache,
        )


def _generate(generator: AnswerGenerator):
    return generator.generate(
        query="how does the parser work",
        organization_id=uuid4(),
        repository_id=uuid4(),
        top_k=5,
    )


def _retrieval_error() -> RetrievalError:
    return RetrievalError({"vector": RuntimeError(f"Error code: 401 - {SENTINEL}")})


class TestAnswerGeneratorRetrievalFailure:
    def test_retrieval_error_propagates_instead_of_becoming_an_answer(self):
        error = _retrieval_error()
        query_engine = MagicMock()
        query_engine.query.side_effect = error
        generator = _generator(query_engine)

        with pytest.raises(RetrievalError) as raised:
            _generate(generator)

        assert raised.value is error
        generator.client.chat.completions.create.assert_not_called()

    def test_retrieval_error_is_not_cached(self):
        query_engine = MagicMock()
        query_engine.query.side_effect = _retrieval_error()
        semantic_cache = MagicMock()
        semantic_cache.get_cached_response.return_value = None
        generator = _generator(query_engine, semantic_cache)

        with patch("workers.embeddings.EmbeddingGenerator") as embedding_generator:
            embedding_generator.return_value.generate_embeddings_for_chunks.return_value = {
                "query": [0.1, 0.2, 0.3]
            }
            with pytest.raises(RetrievalError):
                _generate(generator)

        semantic_cache.get_cached_response.assert_called_once()  # the cache path really ran
        semantic_cache.cache_response.assert_not_called()

    def test_a_retrieval_that_matches_nothing_still_gets_the_dont_know_answer(self):
        query_engine = MagicMock()
        query_engine.query.return_value = {"results": [], "metadata": {}}
        generator = _generator(query_engine)

        result = _generate(generator)

        assert result["answer"].startswith("I don't have enough information")
        assert result["chunks_retrieved"] == 0
        generator.client.chat.completions.create.assert_not_called()


def _chunk() -> dict:
    return {
        "chunk_id": "c1",
        "file_path": "pkg/marmalade.go",
        "start_line": 1,
        "end_line": 3,
        "content": "func Marmalade() {}",
        "breadcrumb": "Marmalade",
        "chunk_type": "function",
    }


def _llm_reply(generator: AnswerGenerator) -> None:
    reply = MagicMock()
    reply.choices[0].message.content = "Marmalade is defined in [1]."
    reply.usage.completion_tokens = 7
    reply.usage.total_tokens = 20
    generator.client.chat.completions.create.return_value = reply
    generator.tokenizer.encode.return_value = [0] * 13


class TestSemanticCacheFailureFallsThrough:
    """The cache is an optimisation: a cache failure must not decide the response.

    Before this, an exception while embedding the query for the cache lookup, or
    from the lookup itself, escaped `generate` as a plain exception. /chat then
    answered 500, even when the cause was the OpenAI outage that retrieval
    reports as a retryable 503.
    """

    @pytest.mark.parametrize("failing", ["embedding", "lookup"])
    def test_a_failed_cache_lookup_still_reaches_retrieval(self, failing, caplog):
        error = _retrieval_error()
        query_engine = MagicMock()
        query_engine.query.side_effect = error
        semantic_cache = MagicMock()
        generator = _generator(query_engine, semantic_cache)

        with patch("workers.embeddings.EmbeddingGenerator") as embedding_generator:
            embed = embedding_generator.return_value.generate_embeddings_for_chunks
            if failing == "embedding":
                embed.side_effect = RuntimeError(f"Error code: 401 - {SENTINEL}")
            else:
                embed.return_value = {"query": [0.1, 0.2, 0.3]}
                semantic_cache.get_cached_response.side_effect = RuntimeError(
                    f"redis down {SENTINEL}"
                )
            with caplog.at_level(logging.WARNING), pytest.raises(RetrievalError) as raised:
                _generate(generator)

        assert raised.value is error, "retrieval must run and report the outage"
        query_engine.query.assert_called_once()
        semantic_cache.cache_response.assert_not_called()
        warnings = [r for r in caplog.records if "Semantic cache lookup failed" in r.getMessage()]
        assert len(warnings) == 1
        assert SENTINEL not in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    def test_a_failed_cache_lookup_still_answers_when_retrieval_works(self):
        query_engine = MagicMock()
        query_engine.query.return_value = {"results": [_chunk()], "metadata": {}}
        semantic_cache = MagicMock()
        semantic_cache.get_cached_response.side_effect = RuntimeError("redis down")
        generator = _generator(query_engine, semantic_cache)
        _llm_reply(generator)

        with patch("workers.embeddings.EmbeddingGenerator") as embedding_generator:
            embedding_generator.return_value.generate_embeddings_for_chunks.return_value = {
                "query": [0.1, 0.2, 0.3]
            }
            result = _generate(generator)

        assert result["answer"] == "Marmalade is defined in [1]."
        assert result["cache_hit"] is False
        # The lookup failed, so the write-back is skipped too.
        semantic_cache.cache_response.assert_not_called()

    def test_a_failed_cache_write_still_returns_the_answer(self, caplog):
        query_engine = MagicMock()
        query_engine.query.return_value = {"results": [_chunk()], "metadata": {}}
        semantic_cache = MagicMock()
        semantic_cache.get_cached_response.return_value = None
        semantic_cache.cache_response.side_effect = RuntimeError(f"redis down {SENTINEL}")
        generator = _generator(query_engine, semantic_cache)
        _llm_reply(generator)

        with patch("workers.embeddings.EmbeddingGenerator") as embedding_generator:
            embedding_generator.return_value.generate_embeddings_for_chunks.return_value = {
                "query": [0.1, 0.2, 0.3]
            }
            with caplog.at_level(logging.WARNING):
                result = _generate(generator)

        assert result["answer"] == "Marmalade is defined in [1]."
        semantic_cache.cache_response.assert_called_once()
        assert SENTINEL not in "".join(r.getMessage() for r in caplog.records)
