"""Where the answer lands: the scoring rule the harness measures with.

One module, imported by both `rag_quality_harness.py` (which records a
question's ranks) and `compare_runs.py` (which recomputes them from the
recorded final list and refuses a record whose ranks disagree). Two copies
would let the two drift, and a copy in the judge would be blind to a change
in the harness (reviewer A, PR #53).

    file rank    the first result in the file that holds the answer
    symbol rank  the first result that IS the answering symbol, by breadcrumb
                 (only for questions that name a symbol)

The aggregates are one formula too, `aggregate` (22.2-01): the harness's
summary, `compare_runs.py`'s report, `tripwire.py` and (22.2-07)
`decide.py` all call it. Before 22.2-01 the harness and the judge each
carried a copy.

`exact_paths` is a property of the corpus: benchmark corpora match the
expected path exactly; `self` matches any path that contains the expectation
(its tuning set predates exact paths and is kept comparable). The harness
records it in every run header.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence, Tuple


def path_matches(exact_paths: bool, expected: str, actual: str) -> bool:
    return actual == expected if exact_paths else expected in actual


def symbol_matches(symbol: str, breadcrumb: Optional[str]) -> bool:
    return bool(breadcrumb) and (breadcrumb == symbol or breadcrumb.endswith("." + symbol))


def ranks(
    exact_paths: bool,
    expected_path: str,
    symbol: Optional[str],
    results: Iterable[Mapping],
) -> Tuple[Optional[int], Optional[int]]:
    """(file rank, symbol rank) of the answer in `results`, 1-based, None when missed.

    `results` are the final results in order, each with `file_path` and
    `breadcrumb`: the engine's response, or a record's `trace.top`.
    """
    rows = list(results)
    in_file = [path_matches(exact_paths, expected_path, r.get("file_path", "") or "") for r in rows]
    file_rank = next((i for i, hit in enumerate(in_file, 1) if hit), None)
    symbol_rank = None
    if symbol:
        symbol_rank = next(
            (
                i
                for i, (hit, r) in enumerate(zip(in_file, rows), 1)
                if hit and symbol_matches(symbol, r.get("breadcrumb"))
            ),
            None,
        )
    return file_rank, symbol_rank


def aggregate(ranks_: Sequence[Optional[int]]) -> dict:
    """recall (found in the top k), rank-1 and MRR over all the questions.

    `ranks_` holds one rank per question, None for a miss, which counts 0 in
    the MRR. `found / questions` is recall@k, k being the run's top_k.
    """
    found = [r for r in ranks_ if r]
    total = len(ranks_)
    return {
        "questions": total,
        "found": len(found),
        "rank1": sum(1 for r in found if r == 1),
        "mrr": (sum(1.0 / r for r in found) / total) if total else 0.0,
    }
