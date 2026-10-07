#!/usr/bin/env python3
"""22.2-03 Task 2, step 4 and the aggregates: the baseline's header against the
census, the measuring role, failed queries, the spend, and recall@5, rank-1
and MRR@5 per set at file and symbol level. The aggregates are reported, not
judged.

    baseline_checks.py <22.2-03-records dir>

Reads `baseline-linkwarden.jsonl.gz` (its first line is the run header),
`baseline-linkwarden.summary.json`, `census-linkwarden.json`,
`vecs-linkwarden-ada-002.json.gz`, the spec `rag_benchmarks/linkwarden.json`,
and the embedding client's usage lines (`baseline-usage*.txt`). Prints one
PASS/FAIL line per check and exits 1 on any FAIL.

Hardened after PR #70's review (M1), so that it can fail in each of these
cases, where it once passed vacuously:
- a connection missing from the header;
- empty or short usage logs (the batch and text counts are asserted, and the
  ingest's tokens must equal the census's `embedded_total_if_every_chunk`);
- ranks that disagree with `trace.top` (both ranks are recomputed with
  `scoring.ranks`, and the table is built from the recomputed ranks);
- rows whose id, set, question, path or symbol differ from the spec;
- a run that is not `top_k` 5, `--set all` and no boost config.
"""
import gzip
import json
import re
import sys
from pathlib import Path

TREE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(TREE / "services/workers/scripts/rag_benchmarks"))
from scoring import aggregate, ranks  # noqa: E402

SPEC = TREE / "services/workers/scripts/rag_benchmarks/linkwarden.json"
PRICE_PER_M = {"text-embedding-ada-002": 0.10}
CAP = 0.05
# The usage logs' expected shape (baseline.txt): the first attempt sent 7
# batches, of which 6 succeeded before the 429; the run that succeeded sent 10
# ingest batches and one batch of the 30 questions.
ATTEMPT1_SENT, ATTEMPT1_BILLED, RUN_BATCHES = 7, 6, 11
failures = 0


def check(ok, text):
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def usage(path: Path):
    text = path.read_text(encoding="utf-8")
    sent = [int(m.group(1)) for m in re.finditer(r"Generating batch embeddings for (\d+) texts", text)]
    billed = [(int(m.group(1)), int(m.group(2)))
              for m in re.finditer(r"Batch embeddings generated: (\d+) embeddings, (\d+) tokens", text)]
    return sent, billed


def main(rec: Path) -> int:
    with gzip.open(rec / "baseline-linkwarden.jsonl.gz", "rt", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    h, rows = lines[0], lines[1:]
    census = json.loads((rec / "census-linkwarden.json").read_text(encoding="utf-8"))
    summary = json.loads((rec / "baseline-linkwarden.summary.json").read_text(encoding="utf-8"))
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    with gzip.open(rec / "vecs-linkwarden-ada-002.json.gz", "rt", encoding="utf-8") as fh:
        vecs = json.load(fh)

    print("== the header")
    check(h["chunk_set_digest"] == h["offline_chunk_set_digest"] == census["chunk_set_digest"],
          f"chunk_set_digest {h['chunk_set_digest'][:16]} = offline = census-linkwarden.json's")
    check(h["chunk_rows"] == h["offline_chunk_rows"] == census["chunk_rows"] == h["chunks_visible"],
          f"chunk rows {h['chunk_rows']} = offline = census = visible to rag_doc_app")
    check(len(h["chunk_models"]) == 1, f"chunk_models names one model: {h['chunk_models']}")
    check(h["embedding_model"] in h["chunk_models"], f"the query model {h['embedding_model']} is the chunks' model")
    check(set(h["connections"]) == {"fts", "vector"},
          f"the header records exactly the fts and vector connections: {sorted(h['connections'])}")
    for leg, c in h["connections"].items():
        check(c["current_user"] == "rag_doc_app" and c["rolsuper"] is False and c["rolbypassrls"] is False,
              f"{leg} connection: current_user {c['current_user']}, rolsuper {c['rolsuper']}, "
              f"rolbypassrls {c['rolbypassrls']}")
    check(h["chunker_version"] == census["chunker_version"] and h["chunker_version_verified"] is True,
          f"chunker {h['chunker_version']} = census's, verified")
    check(h["corpus_commit"] == spec["commit"], f"corpus commit {h['corpus_commit']} = the spec's pin")
    check(h["vector_tolerance"] == 2e-6, f"vector tolerance {h['vector_tolerance']} (QD2)")
    check(h["breadcrumb_column_mismatches"] == 0, "breadcrumb column mismatches 0")
    for where, d in (("header", h), ("summary", summary)):
        check(d["top_k"] == 5 and d["set"] == "all" and d["boost_config"] is None,
              f"{where}: top_k {d['top_k']}, set {d['set']!r}, boost_config {d['boost_config']}")
    print(f"      harness_commit {h['harness_commit']}, retrieval_code_version {h['retrieval_code_version']}, "
          f"hnsw.ef_search {h['database'].get('hnsw.ef_search')}, server {h['database']['server_version']}")

    print("== the questions against the spec")
    key = ("id", "set", "question", "path", "symbol")
    want = [tuple(q[k] for k in key) for q in spec["questions"]]
    got = [tuple(r[k] for k in key) for r in rows]
    check(got == want, f"{len(got)} rows' (id, set, question, path, symbol) equal the spec's {len(want)}, in order")
    check(all(r["set"] == ("tuning" if int(r["id"][3:]) % 2 else "holdout") for r in rows),
          "odd ids are tuning and even ids holdout")

    print("== the queries")
    check(len(rows) == 30 and summary["summary"]["errors"] == 0 and all(r["error"] is None for r in rows),
          f"{len(rows)} questions recorded, 0 failed")
    check(sorted(vecs) == sorted(r["id"] for r in rows), f"{len(vecs)} cached query vectors, one per question")

    print("== the ranks, recomputed from each row's trace.top with scoring.ranks")
    recomputed = {r["id"]: ranks(h["exact_paths"], r["path"], r.get("symbol"), r["trace"]["top"]) for r in rows}
    check(all(len(r["trace"]["top"]) <= h["top_k"] for r in rows), f"every trace.top has at most {h['top_k']} results")
    bad = [r["id"] for r in rows if recomputed[r["id"]] != (r["file_rank"], r["symbol_rank"])]
    check(not bad, f"recorded file and symbol ranks equal the recomputed ones (mismatches: {bad or 'none'})")
    by_id = {r["id"]: r for r in summary["rows"]}
    bad = [i for i, (f, s) in recomputed.items() if (by_id[i]["file_rank"], by_id[i]["symbol_rank"]) != (f, s)]
    check(not bad, f"summary.json's ranks equal the recomputed ones (mismatches: {bad or 'none'})")

    print("== the spend (text-embedding-ada-002 at $0.10 per million tokens)")
    sent1, billed1 = usage(rec / "baseline-usage-attempt1-429.txt")
    sent2, billed2 = usage(rec / "baseline-usage.txt")
    check(len(sent1) == ATTEMPT1_SENT and len(billed1) == ATTEMPT1_BILLED,
          f"attempt 1: {len(sent1)} batches sent, {len(billed1)} billed (expected {ATTEMPT1_SENT} and "
          f"{ATTEMPT1_BILLED}; the last was the 429)")
    check(len(sent2) == len(billed2) == RUN_BATCHES,
          f"the run: {len(sent2)} batches sent, {len(billed2)} billed (expected {RUN_BATCHES})")
    ingest_texts = sum(n for n, _ in billed2[:-1])
    ingest_tokens = sum(t for _, t in billed2[:-1])
    check(ingest_texts == census["chunk_rows"] and sent2[:-1] == [n for n, _ in billed2[:-1]],
          f"the ingest embedded {ingest_texts} texts = the census's {census['chunk_rows']} chunk rows")
    check(ingest_tokens == census["tokens"]["embedded_total_if_every_chunk"],
          f"the ingest's {ingest_tokens} tokens = the census's embedded_total_if_every_chunk "
          f"{census['tokens']['embedded_total_if_every_chunk']}")
    last = billed2[-1] if billed2 else (0, 0)
    check(last[0] == len(rows), f"the last batch is the {len(rows)} questions ({last[0]} texts)")
    tokens = sum(t for _, t in billed1) + sum(t for _, t in billed2)
    total = tokens * PRICE_PER_M["text-embedding-ada-002"] / 1e6
    print(f"      attempt 1: {sum(t for _, t in billed1)} tokens; the run: {ingest_tokens} ingest + "
          f"{last[1]} questions; total {tokens} tokens, ${total:.6f}")
    check(0 < total <= CAP, f"total ${total:.6f} within the plan's ${CAP:.2f} cap")

    print("== the aggregates per set, from the recomputed ranks (reported, not judged)")
    print(f"      {'set':8} {'level':7} {'n':>3} {'recall@5':>9} {'rank-1':>7} {'MRR@5':>7}")
    for s in ("tuning", "holdout", "all"):
        sel = [r["id"] for r in rows if s == "all" or r["set"] == s]
        for li, level in enumerate(("file", "symbol")):
            a = aggregate([recomputed[i][li] for i in sel])
            print(f"      {s:8} {level:7} {a['questions']:>3} {a['found']:>3}/{a['questions']:<5} "
                  f"{a['rank1']:>3}/{a['questions']:<3} {a['mrr']:>7.3f}")
            if s == "all":
                want_a = summary["summary"][level]
                check(a["found"] == want_a["found"] and a["rank1"] == want_a["rank1"]
                      and abs(a["mrr"] - want_a["mrr"]) < 1e-12,
                      f"all / {level}: the recomputed aggregates equal summary.json's")
    print(f"{failures} FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
