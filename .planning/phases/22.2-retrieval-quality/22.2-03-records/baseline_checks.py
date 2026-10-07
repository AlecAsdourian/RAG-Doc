#!/usr/bin/env python3
"""22.2-03 Task 2, step 4 and the aggregates: the baseline's header against the
census, the measuring role, failed queries, the spend, and recall@5, rank-1
and MRR@5 per set at file and symbol level. The aggregates are reported, not
judged.

    baseline_checks.py <22.2-03-records dir>

Reads `baseline-linkwarden.jsonl.gz` (its first line is the run header),
`baseline-linkwarden.summary.json`, `census-linkwarden.json`,
`vecs-linkwarden-ada-002.json.gz`, and the embedding client's usage lines
(`baseline-usage*.txt`). Prints one PASS/FAIL line per check and exits 1 on
any FAIL.
"""
import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "services/workers/scripts/rag_benchmarks"))
from scoring import aggregate  # noqa: E402

PRICE_PER_M = {"text-embedding-ada-002": 0.10}
CAP = 0.05
failures = 0


def check(ok, text):
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def usage_tokens(path: Path):
    tokens = [int(m.group(1)) for m in re.finditer(r"embeddings, (\d+) tokens", path.read_text(encoding="utf-8"))]
    return len(tokens), sum(tokens)


def main(rec: Path) -> int:
    with gzip.open(rec / "baseline-linkwarden.jsonl.gz", "rt", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    h, rows = lines[0], lines[1:]
    census = json.loads((rec / "census-linkwarden.json").read_text(encoding="utf-8"))
    summary = json.loads((rec / "baseline-linkwarden.summary.json").read_text(encoding="utf-8"))
    with gzip.open(rec / "vecs-linkwarden-ada-002.json.gz", "rt", encoding="utf-8") as fh:
        vecs = json.load(fh)

    print("== the header")
    check(h["chunk_set_digest"] == h["offline_chunk_set_digest"] == census["chunk_set_digest"],
          f"chunk_set_digest {h['chunk_set_digest'][:16]} = offline = census-linkwarden.json's")
    check(h["chunk_rows"] == h["offline_chunk_rows"] == census["chunk_rows"] == h["chunks_visible"],
          f"chunk rows {h['chunk_rows']} = offline = census = visible to rag_doc_app")
    check(len(h["chunk_models"]) == 1, f"chunk_models names one model: {h['chunk_models']}")
    check(h["embedding_model"] in h["chunk_models"], f"the query model {h['embedding_model']} is the chunks' model")
    for leg, c in h["connections"].items():
        check(c["current_user"] == "rag_doc_app" and c["rolsuper"] is False and c["rolbypassrls"] is False,
              f"{leg} connection: current_user {c['current_user']}, rolsuper {c['rolsuper']}, "
              f"rolbypassrls {c['rolbypassrls']}")
    check(h["chunker_version"] == census["chunker_version"] and h["chunker_version_verified"] is True,
          f"chunker {h['chunker_version']} = census's, verified")
    check(h["corpus_commit"] == "952ac4540657cae3a67c3ca59433899d2fda8374", f"corpus commit {h['corpus_commit']}")
    check(h["vector_tolerance"] == 2e-6, f"vector tolerance {h['vector_tolerance']} (QD2)")
    check(h["breadcrumb_column_mismatches"] == 0, "breadcrumb column mismatches 0")
    print(f"      harness_commit {h['harness_commit']}, retrieval_code_version {h['retrieval_code_version']}, "
          f"hnsw.ef_search {h['database'].get('hnsw.ef_search')}, server {h['database']['server_version']}")

    print("== the queries")
    check(len(rows) == 30 and summary["summary"]["errors"] == 0 and all(r["error"] is None for r in rows),
          f"{len(rows)} questions recorded, 0 failed")
    check(sorted(vecs) == sorted(r["id"] for r in rows), f"{len(vecs)} cached query vectors, one per question")

    print("== the spend (text-embedding-ada-002 at $0.10 per million tokens)")
    total = 0
    for name in ("baseline-usage-attempt1-429.txt", "baseline-usage.txt"):
        n, t = usage_tokens(rec / name)
        cost = t * PRICE_PER_M["text-embedding-ada-002"] / 1e6
        total += cost
        print(f"      {name}: {n} batches, {t} tokens, ${cost:.6f}")
    check(total <= CAP, f"total ${total:.6f} within the plan's ${CAP:.2f} cap")

    print("== the aggregates per set (reported, not judged)")
    print(f"      {'set':8} {'level':7} {'n':>3} {'recall@5':>9} {'rank-1':>7} {'MRR@5':>7}")
    for s in ("tuning", "holdout", "all"):
        sel = [r for r in rows if s == "all" or r["set"] == s]
        for level in ("file", "symbol"):
            a = aggregate([r[f"{level}_rank"] for r in sel])
            print(f"      {s:8} {level:7} {a['questions']:>3} {a['found']:>3}/{a['questions']:<5} "
                  f"{a['rank1']:>3}/{a['questions']:<3} {a['mrr']:>7.3f}")
    allf = aggregate([r["file_rank"] for r in rows])
    check(abs(allf["mrr"] - summary["summary"]["file"]["mrr"]) < 1e-12
          and allf["found"] == summary["summary"]["file"]["found"],
          "the record's ranks give the summary's file aggregates")
    print(f"{failures} FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
