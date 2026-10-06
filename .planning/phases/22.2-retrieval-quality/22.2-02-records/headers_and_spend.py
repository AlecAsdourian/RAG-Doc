#!/usr/bin/env python3
"""Step 4 of 22.2-02 Task 3: every run header against its census, the control, and the spend.

    headers_and_spend.py <records-dir>

Reads `benchmark/before-<c>.jsonl.gz` / `benchmark/after-<c>.jsonl.gz` (each record's first line is
its run header), `census-before/` and `census-after/`, and the
`benchmark/usage-<side>-<c>.txt` lines the embedding client logged during each ingest
and measure. Prints one PASS/FAIL line per check and exits 1 on any FAIL. Read
it before reading any number: a pair of digests that differ is stopped on.
"""
import gzip
import json
import re
import sys
from pathlib import Path

CORPORA = ("self", "miniflux", "mealie")
PRICE_PER_M = {"text-embedding-ada-002": 0.10}
failures = 0


def check(ok, text):
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def header(records: Path, side: str, corpus: str) -> dict:
    with gzip.open(records / "benchmark" / f"{side}-{corpus}.jsonl.gz", "rt", encoding="utf-8") as fh:
        return json.loads(fh.readline())


def main(records: Path) -> int:
    for side, census_dir in (("before", "census-before"), ("after", "census-after")):
        for c in CORPORA:
            h = header(records, side, c)
            census = json.loads((records / census_dir / f"census-{c}.json").read_text(encoding="utf-8"))
            check(h["chunk_set_digest"] == h["offline_chunk_set_digest"] == census["chunk_set_digest"],
                  f"{side} {c}: header digest {h['chunk_set_digest'][:16]} = offline "
                  f"{h['offline_chunk_set_digest'][:16]} = {census_dir} {census['chunk_set_digest'][:16]} "
                  f"({h['chunk_rows']} rows)")
            check(h["chunker_version"] == h["running_chunker_version"] == census["chunker_version"]
                  and h["chunker_version_verified"] is True,
                  f"{side} {c}: chunker version {h['chunker_version']} verified "
                  f"= {census_dir}'s {census['chunker_version']}")
            check(h["corpus_tree_digest"] == census["corpus_tree_digest"],
                  f"{side} {c}: corpus tree {h['corpus_tree_digest'][:16]} = {census_dir}'s")
            conns = h["connections"]
            check(all(v["current_user"] == "rag_doc_app" and not v["rolsuper"] and not v["rolbypassrls"]
                      for v in conns.values()),
                  f"{side} {c}: both legs measured as rag_doc_app, NOSUPERUSER NOBYPASSRLS")
            check(h["breadcrumb_column_mismatches"] == 0 and list(h["chunk_models"]) == ["text-embedding-ada-002"],
                  f"{side} {c}: breadcrumb column mismatches {h['breadcrumb_column_mismatches']}, "
                  f"models {h['chunk_models']}")
            print(f"INFO  {side} {c}: commit {h['corpus_commit'][:12]}, harness_commit "
                  f"{(h.get('harness_commit') or 'none')[:12]}, retrieval_code_version "
                  f"{h['retrieval_code_version']}, stored_vectors_digest {h['stored_vectors_digest']}, "
                  f"vector_tolerance {h['vector_tolerance']}")
    b, a = header(records, "before", "miniflux"), header(records, "after", "miniflux")
    check(b["chunk_set_digest"] == a["chunk_set_digest"],
          f"miniflux, the control: before and after chunk sets equal ({a['chunk_set_digest'][:16]})")
    print(f"INFO  miniflux stored vectors: before {b['stored_vectors_digest']}, after "
          f"{a['stored_vectors_digest']} ({'equal' if b['stored_vectors_digest'] == a['stored_vectors_digest'] else 'DIFFERENT: two ingests of the same texts'})")
    for c in ("self", "mealie"):
        check(header(records, "before", c)["chunk_set_digest"] != header(records, "after", c)["chunk_set_digest"],
              f"{c}: before and after chunk sets differ, as the census says (decorators moved)")

    # The spend: the client's own usage lines, per ingest and measure.
    total_tokens = 0
    print("\nSPEND (the embedding client's usage lines; ada-002 at $0.10 per million tokens)")
    for side in ("before", "after"):
        for c in CORPORA:
            text = (records / "benchmark" / f"usage-{side}-{c}.txt").read_text(encoding="utf-8")
            done = [int(m.group(2)) for m in re.finditer(
                r"Batch embeddings generated: (\d+) embeddings, (\d+) tokens", text)]
            embeddings = sum(int(m.group(1)) for m in re.finditer(
                r"Batch embeddings generated: (\d+) embeddings", text))
            tokens = sum(done)
            total_tokens += tokens
            print(f"  {side:6} {c:8}: {len(done)} calls, {embeddings} embeddings, {tokens} tokens, "
                  f"${tokens / 1e6 * PRICE_PER_M['text-embedding-ada-002']:.4f}")
    cost = total_tokens / 1e6 * PRICE_PER_M["text-embedding-ada-002"]
    print(f"  total: {total_tokens} tokens, ${cost:.4f} (plan estimate about $0.19, cap $0.30)")
    check(cost <= 0.30, f"the spend ${cost:.4f} is within the $0.30 cap")
    print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
