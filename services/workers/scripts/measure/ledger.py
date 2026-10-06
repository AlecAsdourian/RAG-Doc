"""The ONE OpenAI spend ledger for 22.1-05 (`22.1-05-PLAN.md`, "Costs").

The user approved, on 2026-10-06, a HARD CAP of $3.00 on ada-002,
cumulative across every run of the plan. This file is how that cap is kept:

- `reserve(projected_usd)` is called BEFORE any run that can embed. It
  refuses (`LedgerRefused`) when the total already spent plus the run's
  projection would pass the cap. A refusal means ALL embedding work stops
  and the user is asked; nothing proceeds on a partial budget.
- `guard(bound_tokens)` is called before EVERY embeddings HTTP request, with
  a hard upper bound on that request's tokens (the request body's byte
  length: a cl100k token is at least one byte). So even a run whose
  projection was wrong cannot spend past the cap.
- `record(tokens)` adds what the API reported (`usage.total_tokens`).

The arithmetic is in whole tokens, never floats: the cap is
`budget_usd / price_per_million * 1e6` tokens (30,000,000 at $3.00 and
$0.10 per million), and a reservation that lands EXACTLY on the cap is
allowed; one token more is refused.

The ledger is a JSON-lines file in the scratch directory, shared by every
process of a run (the concurrency confirmation runs several), so each
append and each check holds an exclusive file lock.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import threading
import time
from decimal import Decimal
from typing import Iterator, Optional

#: ada-002's price, USD per million tokens (22.1-05-PLAN.md, "Costs").
PRICE_PER_MILLION_USD = Decimal("0.10")

#: The user's hard cap, approved 2026-10-06.
DEFAULT_BUDGET_USD = Decimal("3.00")

_THREAD_LOCK = threading.Lock()


class LedgerRefused(Exception):
    """A reservation or a request would take the total past the cap."""


def usd_to_tokens(usd: Decimal, price_per_million: Decimal = PRICE_PER_MILLION_USD) -> int:
    """Tokens that cost `usd`, rounded UP (a projection never under-reserves)."""
    return int(math.ceil(Decimal(usd) / price_per_million * Decimal(1_000_000)))


def tokens_to_usd(tokens: int, price_per_million: Decimal = PRICE_PER_MILLION_USD) -> Decimal:
    return Decimal(tokens) * price_per_million / Decimal(1_000_000)


@contextlib.contextmanager
def _file_lock(path: str) -> Iterator[None]:
    """An exclusive lock on `path + '.lock'`, across processes and threads."""
    with _THREAD_LOCK:
        handle = open(path + ".lock", "a+")
        try:
            try:
                import fcntl  # Linux: the measurement container

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # Windows: the unit tests; one process
                yield
        finally:
            handle.close()


class Ledger:
    def __init__(
        self,
        path: str,
        budget_usd: Decimal = DEFAULT_BUDGET_USD,
        price_per_million: Decimal = PRICE_PER_MILLION_USD,
    ) -> None:
        self.path = path
        self.budget_usd = Decimal(budget_usd)
        self.price_per_million = Decimal(price_per_million)
        # Floor: the cap in tokens is never rounded up past the dollars.
        self.budget_tokens = int(self.budget_usd / self.price_per_million * Decimal(1_000_000))
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    # -- reading ---------------------------------------------------------

    def _entries(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def total_tokens(self) -> int:
        with _file_lock(self.path):
            return self._total_unlocked()

    def _total_unlocked(self) -> int:
        return sum(int(e["tokens"]) for e in self._entries() if e["kind"] == "record")

    def total_usd(self) -> Decimal:
        return tokens_to_usd(self.total_tokens(), self.price_per_million)

    def remaining_usd(self) -> Decimal:
        return tokens_to_usd(self.budget_tokens - self.total_tokens(), self.price_per_million)

    # -- writing ---------------------------------------------------------

    def _append(self, entry: dict) -> None:
        entry = dict(entry, t=time.time(), pid=os.getpid())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def reserve(self, projected_usd: Decimal, label: str) -> None:
        """Refuse a run whose projection would take the total past the cap."""
        projected_tokens = usd_to_tokens(Decimal(projected_usd), self.price_per_million)
        with _file_lock(self.path):
            spent = self._total_unlocked()
            if spent + projected_tokens > self.budget_tokens:
                self._append(
                    {"kind": "refused", "label": label, "tokens": 0,
                     "projected_tokens": projected_tokens, "spent_tokens": spent}
                )
                raise LedgerRefused(
                    f"{label}: projected ${tokens_to_usd(projected_tokens):.4f} on top of "
                    f"${tokens_to_usd(spent):.4f} spent would pass the "
                    f"${self.budget_usd:.2f} cap; all embedding work stops and the user "
                    "is asked"
                )
            self._append(
                {"kind": "reserve", "label": label, "tokens": 0,
                 "projected_tokens": projected_tokens, "spent_tokens": spent}
            )

    def guard(self, bound_tokens: int, label: str) -> None:
        """Before one HTTP request: refuse if its upper bound could pass the cap."""
        with _file_lock(self.path):
            spent = self._total_unlocked()
            if spent + int(bound_tokens) > self.budget_tokens:
                self._append(
                    {"kind": "refused", "label": label, "tokens": 0,
                     "projected_tokens": int(bound_tokens), "spent_tokens": spent}
                )
                raise LedgerRefused(
                    f"{label}: a request bounded at {bound_tokens} tokens on top of "
                    f"{spent} spent could pass the ${self.budget_usd:.2f} cap"
                )

    def record(self, tokens: int, label: str) -> None:
        with _file_lock(self.path):
            self._append({"kind": "record", "label": label, "tokens": int(tokens)})

    def summary(self) -> dict:
        with _file_lock(self.path):
            entries = self._entries()
        spent = sum(int(e["tokens"]) for e in entries if e["kind"] == "record")
        by_label: dict = {}
        for e in entries:
            if e["kind"] == "record":
                by_label[e["label"]] = by_label.get(e["label"], 0) + int(e["tokens"])
        return {
            "budget_usd": f"{self.budget_usd:.2f}",
            "price_per_million_usd": f"{self.price_per_million:.2f}",
            "spent_tokens": spent,
            "spent_usd": f"{tokens_to_usd(spent, self.price_per_million):.6f}",
            "remaining_usd": f"{tokens_to_usd(self.budget_tokens - spent, self.price_per_million):.6f}",
            "records": sum(1 for e in entries if e["kind"] == "record"),
            "reservations": sum(1 for e in entries if e["kind"] == "reserve"),
            "refusals": sum(1 for e in entries if e["kind"] == "refused"),
            "tokens_by_label": by_label,
        }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="22.1-05's spend ledger")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--budget-usd", default=str(DEFAULT_BUDGET_USD))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    reserve = sub.add_parser("reserve")
    reserve.add_argument("--projected-usd", required=True)
    reserve.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    ledger = Ledger(args.ledger, Decimal(args.budget_usd))
    if args.cmd == "show":
        print(json.dumps(ledger.summary(), indent=2))
        return 0
    try:
        ledger.reserve(Decimal(args.projected_usd), args.label)
    except LedgerRefused as exc:
        print(f"REFUSED: {exc}")
        return 3
    print("reserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
