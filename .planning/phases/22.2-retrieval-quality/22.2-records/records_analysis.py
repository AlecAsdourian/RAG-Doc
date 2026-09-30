#!/usr/bin/env python3
"""Read the committed 22-03 records (the pgvector read path) and describe the current ranking.

A MEASUREMENT RECORD, not product code. No database, no OpenAI: it reads
`22-03-records/pgvector-<corpus>.jsonl.gz` (each question's keyword leg, vector
leg with file_path/breadcrumb/chunk_type, fused, boosted and top lists) and the
census's per-chunk table (`chunks-<corpus>.jsonl`, spans and ISS-026 coverage),
joined by (file_path, breadcrumb, chunk_type). Scoring is the harness's own
(`scripts/rag_benchmarks/scoring.py`).

All 130 recorded questions have been consulted by earlier decisions (the boost
protocol read the confirm sets; 22-03 read all of them), so nothing here is
blind and nothing here decides anything: it describes the baseline.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    lines = gzip.open(path, "rt", encoding="utf-8").read().splitlines()
    header = json.loads(lines[0])
    return header, [json.loads(x) for x in lines[1:]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--chunks", type=Path, required=True)
    ap.add_argument("--scoring", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    sys.path.insert(0, str(a.scoring))
    from scoring import path_matches, symbol_matches

    report = {}
    for corpus in ("self", "miniflux", "mealie"):
        header, qs = load(a.records / f"pgvector-{corpus}.jsonl.gz")
        exact = bool(header.get("exact_paths", corpus != "self"))
        table = defaultdict(list)
        chunk_file = a.chunks / f"chunks-{corpus}.jsonl"
        if chunk_file.exists():
            for line in chunk_file.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                table[(row["file_path"], row["breadcrumb"], row["chunk_type"])].append(row)
        r = {"questions": len(qs), "chunks_visible": header.get("chunks_visible"),
             "keyword_leg_nonempty": 0, "keyword_leg_sizes": Counter(),
             "top5_by_type": Counter(), "top5_slots": 0,
             "top5_class_ge80": 0, "questions_with_dup_class_in_top5_holding_answer": 0,
             "symbol_questions": 0, "answer_symbol_rank_in_vector_leg": Counter(),
             "answer_file_rank_in_vector_leg": Counter(), "final_symbol_rank": Counter(),
             "unmatched_join": 0, "by_set": {}}
        for q in qs:
            t = q["trace"]
            if t["fts"]:
                r["keyword_leg_nonempty"] += 1
            r["keyword_leg_sizes"][len(t["fts"])] += 1
            meta = {e["chunk_id"]: e for e in t["vector"]}
            for e in t["fts"]:
                meta.setdefault(e["chunk_id"], e)
            top = t["top"]
            # the answering symbol's chunk, to test class containment
            answer_rows = []
            if q.get("symbol"):
                for key, rows in table.items():
                    if path_matches(exact, q["path"], key[0]) and symbol_matches(q["symbol"], key[1]) and key[2] == "function":
                        answer_rows.extend(rows)
            dup_hold = False
            for e in top:
                m = meta.get(e["chunk_id"], {})
                ctype = m.get("chunk_type", "?")
                r["top5_by_type"][ctype] += 1
                r["top5_slots"] += 1
                rows = table.get((m.get("file_path"), m.get("breadcrumb"), ctype), [])
                if not rows and ctype != "?":
                    r["unmatched_join"] += 1
                if ctype == "class" and rows and max(x["class_method_coverage"] or 0 for x in rows) >= 0.8:
                    r["top5_class_ge80"] += 1
                    for cl in rows:
                        for ans in answer_rows:
                            if (cl["file_path"] == ans["file_path"] and cl["start_line"] <= ans["start_line"]
                                    and ans["end_line"] <= cl["end_line"]):
                                dup_hold = True
            if dup_hold:
                r["questions_with_dup_class_in_top5_holding_answer"] += 1
            if q.get("symbol"):
                r["symbol_questions"] += 1
                rank = next((i for i, e in enumerate(t["vector"], 1)
                             if path_matches(exact, q["path"], e.get("file_path", ""))
                             and symbol_matches(q["symbol"], e.get("breadcrumb"))), None)
                r["answer_symbol_rank_in_vector_leg"][bucket(rank)] += 1
                r["final_symbol_rank"][bucket(q.get("symbol_rank"), "not in top 5")] += 1
            frank = next((i for i, e in enumerate(t["vector"], 1)
                          if path_matches(exact, q["path"], e.get("file_path", ""))), None)
            r["answer_file_rank_in_vector_leg"][bucket(frank)] += 1
            s = r["by_set"].setdefault(q["set"], {"n": 0, "kw_nonempty": 0})
            s["n"] += 1
            s["kw_nonempty"] += 1 if t["fts"] else 0
        for k in ("keyword_leg_sizes", "top5_by_type", "answer_symbol_rank_in_vector_leg",
                  "answer_file_rank_in_vector_leg", "final_symbol_rank"):
            r[k] = dict(sorted(r[k].items(), key=lambda kv: str(kv[0])))
        if corpus == "self":
            r["top5_class_ge80"] = None
            r["questions_with_dup_class_in_top5_holding_answer"] = None
            r["note"] = ("join fields not computed for self: the 22-03 records were measured at harness commit "
                         "9bd404b/4fd8f80, and the census table is of 0f2e4df, where self has 595 chunks, not 534; "
                         "unmatched_join counts every slot")
        report[corpus] = r
    a.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


def bucket(rank, miss="not in 50"):
    if rank is None:
        return miss
    if rank == 1:
        return "1"
    if rank <= 5:
        return "2-5"
    if rank <= 10:
        return "6-10"
    if rank <= 20:
        return "11-20"
    return "21-50"


if __name__ == "__main__":
    main()
