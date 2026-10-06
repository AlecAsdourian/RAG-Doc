#!/usr/bin/env python3
"""QD1's tripwire: did any benchmark number fall between two recorded runs?

A correctness fix is adopted on offline proof, with the benchmark reported
and this tripwire read (22.2-CONTEXT.md QD1; 22.2-02-PLAN.md says what is
done when it fires). This script only compares; it decides nothing.

INPUT. For each corpus, one `--record` file of `rag_quality_harness.py` per
side: `<before-prefix>-<c>.jsonl` in --before and `<after-prefix>-<c>.jsonl`
in --after (`.gz` accepted; the two directories may be the same).

IT REFUSES (exit 2, nothing compared) when:
  - the question ids, texts, sets, expected paths or symbols differ;
  - a record's ranks are not what its own final list gives under
    `scoring.ranks` (a rank edited without its list);
  - a measuring connection is a superuser, bypasses RLS, or is unrecorded;
  - any query failed;
  - a question's query-vector hash differs between the sides: the same cached
    vectors are required, or the comparison would measure the query
    embedding too;
  - the embedding models, `top_k`, `boost_config` or `exact_paths` differ;
  - a header has no `chunk_set_digest`: a record from before 22.2-01 cannot
    say what it measured.

IT COMPUTES, before and after, from the recomputed ranks, with
`scoring.aggregate` (the harness's own formula, one module): every corpus x
every set x level (file; symbol where the set's questions name one) x metric
(recall@k, rank-1, MRR@k).

A NUMBER FALLS when after < before - 1e-9 (the allowance M2 uses for float
rounding). For recall and rank-1, which are counts, that is one question
fewer. An equal or higher number has not fallen.

IT PRINTS the table; every question whose file or symbol rank worsened or
improved; and per corpus both chunk-set digests, marked "identical chunk set"
when equal. Such a corpus is a control: any change on it is not the chunker's.

EXIT 0 when nothing fell, 1 when anything fell, 2 when the inputs are refused.

USAGE
    tripwire.py --before DIR --after DIR --corpora self miniflux mealie
                [--before-prefix before] [--after-prefix after]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from compare_runs import _find, connection_problems, load_run  # noqa: E402  (one reader of a record)
from scoring import aggregate, ranks  # noqa: E402  (the harness's own rules, one module)

FALL_EPSILON = 1e-9
SET_ORDER = ("tuning", "holdout", "confirm")
HEADER_KEYS = ("embedding_model", "top_k", "boost_config", "exact_paths")
QUESTION_KEYS = ("question", "set", "path", "symbol")


def refusals(name: str, before: Tuple[dict, Dict[str, dict]], after: Tuple[dict, Dict[str, dict]]) -> List[str]:
    """Everything that makes the comparison meaningless, for one corpus."""
    problems: List[str] = []
    (bh, b), (ah, a) = before, after
    if set(b) != set(a):
        problems.append(f"{name}: the question ids differ (before only: {sorted(set(b) - set(a))[:5]}; "
                        f"after only: {sorted(set(a) - set(b))[:5]})")
    for qid in sorted(set(b) & set(a)):
        for key in QUESTION_KEYS:
            if b[qid].get(key) != a[qid].get(key):
                problems.append(f"{name}: {qid}: the question's {key} differs between the sides")
        hb, ha = b[qid].get("query_vector_sha256"), a[qid].get("query_vector_sha256")
        if not hb or not ha or hb != ha:
            problems.append(f"{name}: {qid}: the query-vector hash differs ({hb} vs {ha}); "
                            "the same cached vectors are required")
    for key in HEADER_KEYS:
        if bh.get(key) != ah.get(key):
            problems.append(f"{name}: the runs differ in {key}: {bh.get(key)!r} vs {ah.get(key)!r}")
    for side, header, records in (("before", bh, b), ("after", ah, a)):
        if not header.get("chunk_set_digest"):
            problems.append(f"{name}: the {side} header has no chunk_set_digest, so it cannot say what it measured")
        problems.extend(f"{name}: the {side} run {p}" for p in connection_problems(header))
        exact_paths = header.get("exact_paths")
        if not isinstance(exact_paths, bool):
            problems.append(f"{name}: the {side} header records no exact_paths, so its ranks cannot be recomputed")
            continue
        for qid, rec in sorted(records.items()):
            if rec.get("error"):
                problems.append(f"{name}: {qid}: the {side} query failed ({str(rec['error'])[:60]}); "
                                "a failed query is not a measurement")
                continue
            expected = ranks(exact_paths, rec["path"], rec.get("symbol"), rec["trace"]["top"])
            if expected != (rec["file_rank"], rec.get("symbol_rank")):
                problems.append(f"{name}: {qid}: the {side} record's ranks {(rec['file_rank'], rec.get('symbol_rank'))} "
                                f"are not what its final list gives {expected}")
    return problems


def _ordered_sets(records: Dict[str, dict]) -> List[str]:
    present = {r["set"] for r in records.values()}
    return [s for s in SET_ORDER if s in present] + sorted(present - set(SET_ORDER))


def numbers(records: Dict[str, dict], top_k: int) -> List[Tuple[str, str, str, float, str]]:
    """(set, level, metric, value, shown) for every set x level x metric, from the records' ranks."""
    out = []
    for set_name in _ordered_sets(records):
        rows = [r for r in records.values() if r["set"] == set_name]
        levels = [("file", [r["file_rank"] for r in rows])]
        if any(r.get("symbol") for r in rows):
            levels.append(("symbol", [r.get("symbol_rank") for r in rows if r.get("symbol")]))
        for level, level_ranks in levels:
            agg = aggregate(level_ranks)
            n = agg["questions"]
            out.append((set_name, level, f"recall@{top_k}", agg["found"] / n if n else 0.0, f"{agg['found']}/{n}"))
            out.append((set_name, level, "rank-1", agg["rank1"] / n if n else 0.0, f"{agg['rank1']}/{n}"))
            out.append((set_name, level, f"MRR@{top_k}", agg["mrr"], f"{agg['mrr']:.4f}"))
    return out


def _rank(r: Optional[int]) -> str:
    return f"#{r}" if r else "MISS"


def _worse(before: Optional[int], after: Optional[int]) -> bool:
    if before is None:
        return False
    return after is None or after > before


def moves(b: Dict[str, dict], a: Dict[str, dict]) -> Tuple[List[str], List[str]]:
    worsened, improved = [], []
    for qid in sorted(b):
        for level, key in (("file", "file_rank"), ("symbol", "symbol_rank")):
            if level == "symbol" and not b[qid].get("symbol"):
                continue
            rb, ra = b[qid].get(key), a[qid].get(key)
            line = f"{qid:<10} {b[qid]['set']:<8} {level:<6} {_rank(rb)}->{_rank(ra)}"
            if _worse(rb, ra):
                worsened.append(line)
            elif _worse(ra, rb):
                improved.append(line)
    return worsened, improved


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", type=Path, required=True)
    ap.add_argument("--after", type=Path, required=True)
    ap.add_argument("--corpora", nargs="+", required=True)
    ap.add_argument("--before-prefix", default="before")
    ap.add_argument("--after-prefix", default="after")
    args = ap.parse_args(argv)

    loaded = {}
    problems: List[str] = []
    for name in args.corpora:
        try:
            before = load_run(_find(args.before, f"{args.before_prefix}-{name}.jsonl"))
            after = load_run(_find(args.after, f"{args.after_prefix}-{name}.jsonl"))
        except (FileNotFoundError, ValueError) as exc:
            problems.append(f"{name}: {exc}")
            continue
        problems.extend(refusals(name, before, after))
        loaded[name] = (before, after)
    if problems:
        print("REFUSED: nothing was compared.")
        for p in problems:
            print(f"  - {p}")
        return 2

    fell = []
    print(f"{'corpus':<10} {'set':<8} {'level':<7} {'metric':<9} {'before':>8} {'after':>8} {'delta':>9}  ")
    print("-" * 72)
    for name, ((bh, b), (ah, a)) in loaded.items():
        top_k = int(bh.get("top_k", 5))
        for (s, level, metric, vb, shown_b), (_, _, _, va, shown_a) in zip(numbers(b, top_k), numbers(a, top_k)):
            falls = va < vb - FALL_EPSILON
            mark = "FELL" if falls else ("rose" if va > vb + FALL_EPSILON else "")
            if falls:
                fell.append((name, s, level, metric))
            print(f"{name:<10} {s:<8} {level:<7} {metric:<9} {shown_b:>8} {shown_a:>8} {va - vb:>+9.4f}  {mark}")
    print("-" * 72)

    for name, ((bh, b), (ah, a)) in loaded.items():
        worsened, improved = moves(b, a)
        print(f"\n{name}: {len(worsened)} rank(s) worsened, {len(improved)} improved")
        for line in worsened:
            print(f"  worsened  {line}")
        for line in improved:
            print(f"  improved  {line}")

    print("\nChunk sets (the rows each run measured, with its model):")
    for name, ((bh, _), (ah, _)) in loaded.items():
        same = bh["chunk_set_digest"] == ah["chunk_set_digest"]
        print(f"  {name}: before {bh['chunk_set_digest'][:16]} ({bh.get('chunk_rows')} rows, chunker "
              f"{bh.get('chunker_version')}); after {ah['chunk_set_digest'][:16]} ({ah.get('chunk_rows')} rows, "
              f"chunker {ah.get('chunker_version')})"
              + ("  identical chunk set: a control, so any change here is not the chunker's" if same else ""))

    print(f"\nTRIPWIRE: {'FIRED' if fell else 'quiet'} ({len(fell)} number(s) fell)")
    for name, s, level, metric in fell:
        print(f"  fell: {name} {s} {level} {metric}")
    return 1 if fell else 0


if __name__ == "__main__":
    sys.exit(main())
