"""Errors raised by the retrieval pipeline."""

from typing import Dict, Mapping, Tuple

# The names QueryEngine gives its retrievers, and the words used for them in
# messages. These fixed labels are the only part of a RetrievalError that may be
# shown to a client.
RETRIEVER_LABELS: Dict[str, str] = {
    "fts": "keyword search",
    "vector": "vector search",
}


class RetrievalError(Exception):
    """A retriever failed, so the query has no trustworthy result (ISS-030).

    Raised by `QueryEngine.query` when keyword (FTS) or vector search raises.
    The exception each retriever raised is kept in `failures`; the first one is
    also the `__cause__`.

    `str(error)` includes the underlying exception text, which can contain
    secrets: an OpenAI authentication error carries a masked fragment of the API
    key. Log it, but never send it to a client. `failed_description` names the
    failed retrievers without any exception text.

    Attributes:
        failures: Retriever name ("fts" or "vector") mapped to the exception it
            raised, in the order the retrievers were checked.
        retrievers: The failed retriever names, in the same order.
    """

    def __init__(self, failures: Mapping[str, BaseException]):
        if not failures:
            raise ValueError("RetrievalError needs at least one failed retriever")
        self.failures: Dict[str, BaseException] = dict(failures)
        self.retrievers: Tuple[str, ...] = tuple(self.failures)
        details = "; ".join(
            f"{RETRIEVER_LABELS.get(name, name)}: {type(exc).__name__}: {exc}"
            for name, exc in self.failures.items()
        )
        super().__init__(f"Retrieval failed in {details}")

    @property
    def failed_description(self) -> str:
        """The failed retrievers in words, with no exception text.

        For example "vector search", or "keyword search and vector search".
        """
        return " and ".join(RETRIEVER_LABELS.get(name, name) for name in self.retrievers)
