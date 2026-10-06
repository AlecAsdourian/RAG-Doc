#!/usr/bin/env python3
"""The noise the tripwire is read against (22.2-02, the tripwire's step 3, and Task 3's 22-03 comparison).

    noise.py <records-dir> <22-03-records-dir>

1. Between this plan's before and after runs, per corpus: for every chunk in
   both sides' vector-leg top 50 of the same question, the change in its
   similarity, split by whether the chunker changed that chunk (census rows,
   as in investigate.py). miniflux's chunk set is identical on both sides, so
   every one of its differences is the embedding API's (22.2-01: not
   bit-repeatable).
2. 22-03's committed `pgvector-<c>` runs against this plan's `before-<c>`, for
   miniflux and mealie: the same chunker and retrieval code, the same cached
   query vectors, two ingests a week apart. Questions whose rank differs, and
   the same similarity statistics.

Reads only committed records.
"""
import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    return lines[0], {l["id"]: l for l in lines[1:] if l.get("record") == "question"}


def changed_keys(records: Path, corpus: str):
    def rows(stage):
        out = defaultdict(list)
        with gzip.open(records / stage / f"chunks-{corpus}.jsonl.gz", "rt", encoding="utf-8") as fh:
            for r in map(json.loads, fh):
                out[(r["file_path"], r["chunk_type"], r["breadcrumb"])].append(
                    (r["start_line"], r["end_line"], r["chars"], r["tokens"]))
        return {k: sorted(v) for k, v in out.items()}
    b, a = rows("census-before"), rows("census-after")
    return {k for k in set(b) | set(a) if b.get(k) != a.get(k)}


def deltas(qa, qb, changed=frozenset()):
    same, moved = [], []
    for qid in qa:
        if qid not in qb:
            continue
        va = {}
        for c in qa[qid]["trace"]["vector"]:
            va.setdefault((c["file_path"], c["chunk_type"], c["breadcrumb"]), c["score"])
        for c in qb[qid]["trace"]["vector"]:
            k = (c["file_path"], c["chunk_type"], c["breadcrumb"])
            if k in va:
                (moved if k in changed else same).append(abs(c["score"] - va[k]))
    return same, moved


def stats(xs):
    if not xs:
        return "n=0"
    s = sorted(xs)
    return (f"n={len(s)}, median {statistics.median(s):.2e}, p95 {s[int(0.95 * (len(s) - 1))]:.2e}, "
            f"max {s[-1]:.2e}, exactly 0: {sum(1 for x in s if x == 0)}")


def rank_moves(qa, qb):
    moves = []
    for qid in sorted(qa):
        for level in ("file", "symbol"):
            ra, rb = qa[qid][f"{level}_rank"], qb[qid][f"{level}_rank"]
            if level == "symbol" and not qa[qid].get("symbol"):
                continue
            if ra != rb:
                moves.append(f"{qid} {qa[qid]['set']} {level} #{ra or 'MISS'} -> #{rb or 'MISS'}")
    return moves


def main(records: Path, r2203: Path) -> int:
    print("1. This plan's before -> after, |delta similarity| of the same chunk in both vector top-50s")
    for corpus in ("self", "miniflux", "mealie"):
        _, qb = load(records / "benchmark" / f"before-{corpus}.jsonl.gz")
        _, qa = load(records / "benchmark" / f"after-{corpus}.jsonl.gz")
        ch = changed_keys(records, corpus)
        same, moved = deltas(qb, qa, ch)
        print(f"   {corpus}: chunks the chunker did not change: {stats(same)}")
        print(f"   {corpus}: chunks the chunker changed:        {stats(moved)}")
    print("\n2. 22-03's pgvector runs (2026-09-29) -> this plan's before runs (2026-10-06): one chunker, "
          "one retrieval code, the same cached query vectors, two ingests")
    for corpus in ("miniflux", "mealie"):
        h03, q03 = load(r2203 / f"pgvector-{corpus}.jsonl.gz")
        hb, qb = load(records / "benchmark" / f"before-{corpus}.jsonl.gz")
        same_vecs = all(q03[q]["query_vector_sha256"] == qb[q]["query_vector_sha256"] for q in qb if q in q03)
        print(f"   {corpus}: 22-03 chunks_visible {h03.get('chunks_visible')}, before chunk_rows "
              f"{hb['chunk_rows']}; query-vector hashes equal on all {len(qb)} questions: {same_vecs}")
        have_trace = all("trace" in q for q in q03.values())
        if have_trace:
            same, _ = deltas(q03, qb)
            print(f"   {corpus}: |delta similarity|, same chunk key: {stats(same)}")
        moves = rank_moves(q03, qb)
        print(f"   {corpus}: {len(moves)} rank(s) differ" + "".join(f"\n      {m}" for m in moves))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
