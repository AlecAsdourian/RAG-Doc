#!/usr/bin/env python3
"""The spread of per-question reciprocal rank on today's benchmark, and the standard errors it implies.

A MEASUREMENT RECORD, not product code. Reads the committed 22-03 records'
`pgvector-<corpus>.summary.json` (the current read path, top_k = 5, all 45
questions per corpus) and prints, per corpus and level, the mean reciprocal
rank, its population and sample standard deviations, and the standard error of
an MRR taken over 15 questions (one app's share of a decision set) and over 45
(three apps pooled). These are the numbers behind 22.2-RESEARCH.md R4 and the
threshold T in scripts/rag_benchmarks/embedding-model-protocol.md.

USAGE
    rr_spread.py --records <.planning/phases/22-repository-clone-ingestion/22-03-records>
"""
import argparse
import json
import math
import statistics
from pathlib import Path


def rr(rank):
    return 1.0 / rank if rank else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, required=True)
    a = ap.parse_args()
    by_level = {"file": [], "symbol": []}
    print("source: 22-03-records/pgvector-<corpus>.summary.json (pgvector read path, top_k = 5, all sets)")
    print(f"{'corpus':<9} {'level':<7} {'n':>3} {'mean RR':>8} {'SD pop':>7} {'SD samp':>8} "
          f"{'SE15 pop':>9} {'SE15 samp':>10} {'SE45 pop':>9}")
    per_app_se15 = {"file": [], "symbol": []}
    for corpus in ("miniflux", "mealie"):
        rows = json.loads((a.records / f"pgvector-{corpus}.summary.json").read_text(encoding="utf-8"))["rows"]
        for level, key in (("file", "file_rank"), ("symbol", "symbol_rank")):
            values = [rr(r[key]) for r in rows if level == "file" or r.get("symbol")]
            by_level[level].extend(values)
            sd_pop = statistics.pstdev(values)
            sd_samp = statistics.stdev(values)
            se15_pop = sd_pop / math.sqrt(15)
            se15_samp = sd_samp / math.sqrt(15)
            per_app_se15[level] += [se15_pop, se15_samp]
            print(f"{corpus:<9} {level:<7} {len(values):>3} {statistics.fmean(values):>8.3f} {sd_pop:>7.3f} "
                  f"{sd_samp:>8.3f} {se15_pop:>9.4f} {se15_samp:>10.4f} {sd_pop / math.sqrt(45):>9.4f}")
    print()
    for level in ("file", "symbol"):
        values = by_level[level]
        sd_pool = statistics.stdev(values)
        print(f"{level}: both corpora as one sample of {len(values)}: SD (sample) {sd_pool:.4f}; "
              f"SE over 15 = {sd_pool / math.sqrt(15):.4f}; SE over 45 = {sd_pool / math.sqrt(45):.4f}")
        lo, hi = min(per_app_se15[level]), max(per_app_se15[level])
        print(f"{level}: every per-corpus SE over 15 above lies in [{lo:.4f}, {hi:.4f}]; "
              f"rounded to two decimals: {round(lo, 2):.2f} to {round(hi, 2):.2f}")
    print()
    print("one app over 15 questions, file level, is the noise scale for M2's per-app guard (clause 3)")


if __name__ == "__main__":
    main()
