"""Tests that AnswerGenerator does not turn a retrieval failure into an answer (ISS-030).

A failed retrieval must propagate as RetrievalError. The "I don't have enough
information" reply is for a retrieval that succeeded and matched nothing; giving
it after an outage presents the outage as a fact about the code. The failure
must not be cached either.

OpenAI and tiktoken are patched when the generator is built, so nothing here
touches the network.
"""

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
