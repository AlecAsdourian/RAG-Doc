#!/usr/bin/env python3
"""Print one recorded question's vector leg, its boost multipliers and its final top five.

A MEASUREMENT RECORD, not product code. Reads a committed 22-03 record
(`pgvector-<corpus>.jsonl.gz`) and shows how one question's answer moved from
the vector leg to the final list. It produced the ml-05 trace behind ISS-038
and 22.2-RESEARCH.md R5.

USAGE
    trace_question.py --records <22-03-records> --corpus mealie --id ml-05 [--top 8]
"""
import argparse
import gzip
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()
    lines = gzip.open(a.records / f"pgvector-{a.corpus}.jsonl.gz", "rt", encoding="utf-8").read().splitlines()
    for line in lines[1:]:
        q = json.loads(line)
        if q["id"] != a.id:
            continue
        t = q["trace"]
        print(f"{q['id']} ({q['set']}): {q['question']}")
        print(f"expected: {q['path']} :: {q['symbol']}   recorded file_rank={q['file_rank']} "
              f"symbol_rank={q['symbol_rank']}   keyword leg: {len(t['fts'])} results")
        multiplier = {b["chunk_id"]: b["boost_multiplier"] for b in t["boosted"]}
        print(f"vector leg, top {a.top}:")
        for i, e in enumerate(t["vector"][: a.top], 1):
            print(f"  #{i:<2} {e['score']:.4f} x{multiplier[e['chunk_id']]:<4} {e['chunk_type']:<13} "
                  f"{e['file_path']} :: {e['breadcrumb']}")
        first = t["vector"][0]["chunk_id"]
        position = next(i for i, b in enumerate(t["boosted"], 1) if b["chunk_id"] == first)
        print(f"the vector leg's #1 is #{position} in the boosted list")
        print("final top five:")
        for i, e in enumerate(t["top"], 1):
            print(f"  #{i} {e['file_path']} :: {e['breadcrumb']}")
        return
    raise SystemExit(f"no question {a.id} in {a.corpus}")


if __name__ == "__main__":
    main()
