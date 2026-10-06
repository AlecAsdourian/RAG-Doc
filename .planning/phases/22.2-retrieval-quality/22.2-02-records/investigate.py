#!/usr/bin/env python3
"""The tripwire fired: every worsened question, its before and after top 5, and the chunk-level cause.

    investigate.py <records-dir>

22.2-02-PLAN.md's tripwire rule, step 2 and 3. For every question whose file
or symbol rank worsened (tripwire.py's list, recomputed here the same way), it
prints both final top-5 lists. Each listed chunk is named by (file_path,
chunk_type, breadcrumb) and marked:

  CHANGED    its census row differs between census-before/ and census-after/
             (start line, end line, characters or embedded tokens): the
             chunker made a different chunk;
  same       the chunker made the identical chunk on both sides.

and carries its vector-leg similarity on each side, so an unchanged chunk's
score moving measures the embedding API's own drift (22.2-01: not
bit-repeatable). A question none of whose before or after top-5 chunks
changed moved by noise alone (the plan's definition). Reads only committed
records; no corpus, database or network.
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "services" / "workers" / "scripts" / "rag_benchmarks"))
import scoring  # noqa: E402

CORPORA = ("self", "miniflux", "mealie")


def load(records: Path, side: str, corpus: str):
    with gzip.open(records / "benchmark" / f"{side}-{corpus}.jsonl.gz", "rt", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    return lines[0], {l["id"]: l for l in lines[1:] if l.get("record") == "question"}


def census_rows(records: Path, stage: str, corpus: str):
    rows = defaultdict(list)
    with gzip.open(records / stage / f"chunks-{corpus}.jsonl.gz", "rt", encoding="utf-8") as fh:
        for r in map(json.loads, fh):
            rows[(r["file_path"], r["chunk_type"], r["breadcrumb"])].append(
                (r["start_line"], r["end_line"], r["chars"], r["tokens"]))
    return {k: sorted(v) for k, v in rows.items()}


def key_of(chunk_by_id, cid):
    c = chunk_by_id[cid]
    return (c["file_path"], c["chunk_type"], c["breadcrumb"])


def main(records: Path) -> int:
    for corpus in CORPORA:
        _, before = load(records, "before", corpus)
        _, after = load(records, "after", corpus)
        rows_b, rows_a = census_rows(records, "census-before", corpus), census_rows(records, "census-after", corpus)
        changed_keys = {k for k in set(rows_b) | set(rows_a) if rows_b.get(k) != rows_a.get(k)}
        print(f"=== {corpus}: {len(changed_keys)} of {len(set(rows_b) | set(rows_a))} chunk keys changed between "
              f"census-before and census-after")
        for qid in sorted(before):
            b, a = before[qid], after[qid]
            worse = []
            for level in ("file", "symbol"):
                rb, ra = b[f"{level}_rank"], a[f"{level}_rank"]
                if level == "symbol" and not b.get("symbol"):
                    continue
                if (ra or 99) > (rb or 99):  # a MISS is worse than any rank
                    worse.append(f"{level} #{rb or 'MISS'} -> #{ra or 'MISS'}")
            if not worse:
                continue
            print(f"\n--- {corpus} {qid} ({b['set']}): {'; '.join(worse)}")
            print(f"    question: {b['question']}")
            print(f"    expected: {b['path']}  symbol {b.get('symbol')}")
            for side, rec in (("before", b), ("after", a)):
                vec = {c["chunk_id"]: c for c in rec["trace"]["vector"]}
                print(f"    {side} top 5:")
                for i, t in enumerate(rec["trace"]["top"], start=1):
                    c = vec.get(t["chunk_id"], {"file_path": t["file_path"], "chunk_type": "?",
                                                 "breadcrumb": t["breadcrumb"]})
                    k = (c["file_path"], c.get("chunk_type", "?"), c["breadcrumb"])
                    mark = "CHANGED" if k in changed_keys else "same   "
                    sim = c.get("score")
                    other = before if side == "after" else after
                    other_vec = {(x["file_path"], x["chunk_type"], x["breadcrumb"]): x["score"]
                                 for x in other[qid]["trace"]["vector"]}
                    osim = other_vec.get(k)
                    hit = ""
                    if t["file_path"] == b["path"]:
                        hit += " <file"
                    if b.get("symbol") and scoring.symbol_matches(b["symbol"], t["breadcrumb"] or ""):
                        hit += " <symbol"
                    sims = (f"sim {sim:.6f}" if sim is not None else "sim (fts only)") + (
                        f", other side {osim:.6f} (delta {sim - osim:+.6f})" if sim is not None and osim is not None
                        else ", not in the other side's vector top 50")
                    print(f"      #{i} {mark} {k[0]} {k[1]} {k[2]}  rrf {t['score']:.6f}; {sims}{hit}")
            top_keys = set()
            for rec in (b, a):
                vec = {c["chunk_id"]: c for c in rec["trace"]["vector"]}
                for t in rec["trace"]["top"]:
                    c = vec.get(t["chunk_id"])
                    top_keys.add((t["file_path"], c["chunk_type"] if c else "?", t["breadcrumb"]))
            changed_in_top = sorted(k for k in top_keys if k in changed_keys)
            print(f"    chunks of either top 5 that the chunker changed: {len(changed_in_top)}"
                  + "".join(f"\n      {k}" for k in changed_in_top))
            print(f"    verdict input: {'NOISE (no top-5 chunk changed)' if not changed_in_top else 'a top-5 chunk changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
