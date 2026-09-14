"""Construction-time validation for FTSRetriever's rank normalisation.

These need no database: the setting is validated in __init__, before any
connection is attempted.
"""

import pytest

from workers.retrieval.fts_retriever import FTSRetriever


def test_default_normalization_is_zero(monkeypatch):
    """Unset env and no argument keeps the original, unnormalised ranking."""
    monkeypatch.delenv("FTS_RANK_NORMALIZATION", raising=False)
    assert FTSRetriever("postgresql://unused").rank_normalization == 0


def test_env_var_is_honoured(monkeypatch):
    monkeypatch.setenv("FTS_RANK_NORMALIZATION", "2")
    assert FTSRetriever("postgresql://unused").rank_normalization == 2


def test_explicit_argument_beats_env(monkeypatch):
    monkeypatch.setenv("FTS_RANK_NORMALIZATION", "2")
    assert FTSRetriever("postgresql://unused", rank_normalization=1).rank_normalization == 1


@pytest.mark.parametrize("bad", [-1, 64, 1000])
def test_out_of_range_bitmask_is_rejected(bad):
    """ts_rank_cd's normalization is a 6-bit mask; anything outside is a bug."""
    with pytest.raises(ValueError, match="0..63"):
        FTSRetriever("postgresql://unused", rank_normalization=bad)


def test_non_integer_env_value_fails_loudly(monkeypatch):
    """A typo in the env var must not silently fall back to 0."""
    monkeypatch.setenv("FTS_RANK_NORMALIZATION", "two")
    with pytest.raises(ValueError):
        FTSRetriever("postgresql://unused")
