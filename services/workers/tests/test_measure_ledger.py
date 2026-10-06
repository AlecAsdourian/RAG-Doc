"""22.1-05's spend ledger: the $3.00 cap the user approved on 2026-10-06.

A refused reservation, a reservation landing exactly on the cap, the
per-request guard, and the arithmetic in whole tokens.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from decimal import Decimal

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "measure" / "ledger.py"
_spec = importlib.util.spec_from_file_location("measure_ledger", _SCRIPT)
ledger_module = importlib.util.module_from_spec(_spec)
sys.modules["measure_ledger"] = ledger_module
_spec.loader.exec_module(ledger_module)

Ledger = ledger_module.Ledger
LedgerRefused = ledger_module.LedgerRefused


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.jsonl"), budget_usd=Decimal("3.00"))


def test_the_cap_is_thirty_million_tokens_at_ten_cents_a_million(ledger):
    assert ledger.budget_tokens == 30_000_000
    assert ledger.total_usd() == Decimal(0)


def test_a_reservation_past_the_cap_is_refused_and_recorded(ledger):
    ledger.record(29_000_000, "earlier runs")  # $2.90 spent
    with pytest.raises(LedgerRefused, match="cap"):
        ledger.reserve(Decimal("0.11"), "the next run")  # $3.01 > $3.00
    summary = ledger.summary()
    assert summary["refusals"] == 1
    assert summary["reservations"] == 0
    assert summary["spent_tokens"] == 29_000_000, "a refusal spends nothing"


def test_a_reservation_landing_exactly_on_the_cap_is_allowed_and_one_token_more_is_not(ledger):
    ledger.record(29_000_000, "earlier runs")
    ledger.reserve(Decimal("0.10"), "exactly to the cap")  # $3.00 == $3.00
    ledger.record(1_000_000, "that run")
    assert ledger.total_tokens() == 30_000_000
    assert ledger.total_usd() == Decimal("3.00")
    with pytest.raises(LedgerRefused):
        ledger.reserve(Decimal("0.0000001"), "one token more")
    with pytest.raises(LedgerRefused):
        ledger.guard(1, "one request of one token")


def test_the_request_guard_refuses_a_request_that_could_cross_the_cap(ledger):
    ledger.record(29_999_000, "almost everything")
    ledger.guard(1_000, "fits exactly")
    with pytest.raises(LedgerRefused):
        ledger.guard(1_001, "one token over")


def test_the_ledger_is_one_file_shared_by_every_instance(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    Ledger(path).record(10_000_000, "process one")
    second = Ledger(path)
    assert second.total_usd() == Decimal("1.00")
    with pytest.raises(LedgerRefused):
        second.reserve(Decimal("2.01"), "too much for what is left")


def test_a_projection_is_rounded_up_never_down():
    assert ledger_module.usd_to_tokens(Decimal("0.0000001")) == 1
    assert ledger_module.usd_to_tokens(Decimal("0.10")) == 1_000_000
