#!/usr/bin/env python3
"""Does the embedding API return the same vector for the same text? Measured once.

A MEASUREMENT RECORD, not product code (22.2-01 Task 3). QD2's tolerance does
not apply across two separate ingests until this is known. It calls the
product's own client, `EmbeddingGenerator(model=...).client.
generate_embeddings_batch`, and checks OPENAI_API_KEY by its length only.

  1. ACROSS DAYS: the 130 questions of 22-03's `vecs.json.gz` (embedded with
     ada-002 on 2026-09-29) are embedded again with ada-002, in one batch, and
     each vector is compared with the cached one.
  2. WITHIN A SESSION: mealie's first 20 chunks in (file_path, start_line)
     order, as the census's rows give them, their embedded text built by the
     generator's own rule (`_prepare_text_for_embedding`), are embedded twice
     with each model: once as one batch, once as one batch in reverse order.
     The two are compared text by text.

Per comparison it records how many vectors are bit-identical, the maximum
|delta| over components, the minimum cosine similarity, and the dimensions.

BIT-IDENTICAL means equal as float32, the precision the API serves: the
cached vectors were stored as JSON decimals (the shortest form of each
float32), while today's client decodes base64 float32s, so the two are
compared bit for bit after both are read as float32. The float64 values are
also compared, and reported.

It counts the tokens with cl100k_base before sending, and records the tokens
the API reports (the client's own usage log lines), and the spend.

USAGE
    OPENAI_API_KEY=... embedding_repeatability.py --vecs <22-03 vecs.json.gz> --corpora <dir> \
        --out-vectors <file.json.gz>
"""
import argparse
import gzip
import json
import logging
import os
import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[3]
WORKERS = TREE / "services" / "workers"
sys.path.insert(0, str(WORKERS / "scripts" / "rag_benchmarks"))
sys.path.insert(0, str(WORKERS))

PRICE_PER_M = {"text-embedding-ada-002": 0.10, "text-embedding-3-small": 0.02}
MODELS = ("text-embedding-ada-002", "text-embedding-3-small")
COLUMN_DIMENSIONS = 1536  # chunks.embedding is vector(1536) (migration 000017)


class Usage(logging.Handler):
    """The client's own per-batch usage lines: what the API says it billed."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.tokens = []

    def emit(self, record):
        m = re.search(r"Batch embeddings generated: (\d+) embeddings, (\d+) tokens", record.getMessage())
        if m:
            self.tokens.append((int(m.group(1)), int(m.group(2))))


def compare(label, first, second, out):
    a32 = [np.asarray(v, dtype=np.float32) for v in first]
    b32 = [np.asarray(v, dtype=np.float32) for v in second]
    dims = sorted({len(v) for v in a32} | {len(v) for v in b32})
    identical32 = sum(1 for x, y in zip(a32, b32) if x.shape == y.shape and np.array_equal(x.view(np.uint32),
                                                                                           y.view(np.uint32)))
    identical64 = sum(1 for x, y in zip(first, second) if list(map(float, x)) == list(map(float, y)))
    a64 = [np.asarray(v, dtype=np.float64) for v in first]
    b64 = [np.asarray(v, dtype=np.float64) for v in second]
    max_delta = max(float(np.max(np.abs(x - y))) for x, y in zip(a64, b64))
    min_cos = min(float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y))) for x, y in zip(a64, b64))
    line = (f"{label}: {len(first)} vectors; bit-identical (float32) {identical32}/{len(first)}; "
            f"identical as float64 {identical64}/{len(first)}; max |delta| {max_delta:.3e}; "
            f"min cosine {min_cos:.9f}; dimensions {dims}")
    print(line)
    out.append({"comparison": label, "vectors": len(first), "bit_identical_float32": identical32,
                "identical_float64": identical64, "max_abs_delta": max_delta, "min_cosine": min_cos,
                "dimensions": dims})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vecs", type=Path, required=True)
    ap.add_argument("--corpora", type=Path, required=True)
    ap.add_argument("--out-vectors", type=Path, required=True)
    a = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY", "")
    print(f"OPENAI_API_KEY: {'present' if key else 'absent'}, {len(key)} characters")
    if not key:
        raise SystemExit("OPENAI_API_KEY not set")

    import tiktoken
    import chunk_census
    from workers.embeddings.embedding_generator import EmbeddingGenerator

    chunk_census.attach_capture()  # the chunker's fallback warnings go to the census's capture, not the record
    usage = Usage()
    client_logger = logging.getLogger("workers.embeddings.openai_client")
    client_logger.setLevel(logging.INFO)
    client_logger.addHandler(usage)
    enc = tiktoken.get_encoding("cl100k_base")

    with gzip.open(a.vecs, "rt", encoding="utf-8") as fh:
        cached = json.load(fh)
    ids = sorted(cached)
    questions = [cached[i]["question"] for i in ids]
    assert {cached[i]["model"] for i in ids} == {"text-embedding-ada-002"}, "22-03's vectors are ada-002's"

    files, meta = chunk_census.corpus_files("mealie", a.corpora, WORKERS.parents[1], None)
    chunker, _, _ = chunk_census.make_tools()
    rows = []
    for path, content, lang in files:
        rows.extend(chunker.chunk_file(path, content, lang))
    rows = sorted(rows, key=lambda c: (c.file_path, c.start_line))[:20]
    print(f"mealie at {meta['commit']}: {len(files)} files; the first 20 chunks in (file_path, start_line) order run "
          f"from {rows[0].file_path}:{rows[0].start_line} to {rows[-1].file_path}:{rows[-1].start_line}")

    q_tokens = sum(len(enc.encode(q)) for q in questions)
    results, spend, sent = [], {}, {}
    vectors_out = {"questions_ada_002_today": {}, "chunks": {}}

    print("\n1. Across days: 22-03's 130 questions (ada-002, 2026-09-29) embedded again today with ada-002")
    gen = EmbeddingGenerator(model="text-embedding-ada-002")
    before = len(usage.tokens)
    today = gen.client.generate_embeddings_batch(questions)
    sent.setdefault("text-embedding-ada-002", []).extend(usage.tokens[before:])
    print(f"   cl100k_base count of the 130 questions: {q_tokens} tokens; the API reported "
          f"{sum(t for _, t in usage.tokens[before:])}")
    compare("ada-002, 2026-09-29 cache vs today", [cached[i]["vector"] for i in ids], today, results)
    vectors_out["questions_ada_002_today"] = dict(zip(ids, today))

    print("\n2. Within a session: mealie's first 20 chunk texts, one batch in order and one in reverse")
    for model in MODELS:
        gen = EmbeddingGenerator(model=model)
        texts = [gen._prepare_text_for_embedding(c) for c in rows]
        t_tokens = sum(len(enc.encode(t)) for t in texts)
        before = len(usage.tokens)
        forward = gen.client.generate_embeddings_batch(texts)
        backward = list(reversed(gen.client.generate_embeddings_batch(list(reversed(texts)))))
        sent.setdefault(model, []).extend(usage.tokens[before:])
        print(f"   {model}: cl100k_base count of the 20 texts: {t_tokens} tokens per batch; the API reported "
              f"{[t for _, t in usage.tokens[before:]]}")
        compare(f"{model}, in order vs reversed", forward, backward, results)
        if model == "text-embedding-3-small":
            dims = {len(v) for v in forward + backward}
            print(f"   3-small's dimensions {sorted(dims)}; the column's {COLUMN_DIMENSIONS}: "
                  f"{'match' if dims == {COLUMN_DIMENSIONS} else 'MISMATCH'}")
        vectors_out["chunks"][model] = {"rows": [[c.file_path, c.start_line, c.end_line, c.chunk_type] for c in rows],
                                        "in_order": forward, "reversed_batch": backward}

    print("\nTokens the API reported, and the spend:")
    total = 0.0
    for model in MODELS:
        tokens = sum(t for _, t in sent.get(model, []))
        cost = tokens / 1e6 * PRICE_PER_M[model]
        spend[model] = {"calls": len(sent.get(model, [])), "tokens": tokens, "usd": cost}
        total += cost
        print(f"   {model}: {len(sent.get(model, []))} calls, {tokens} tokens, ${cost:.6f}")
    print(f"   total: ${total:.6f} (cap $0.05)")
    with gzip.GzipFile(filename=a.out_vectors.name[:-3], mode="wb", fileobj=a.out_vectors.open("wb"), mtime=0) as gz:
        gz.write(json.dumps({"results": results, "spend": spend, "vectors": vectors_out}).encode("utf-8"))
    print(f"\nwrote {a.out_vectors.name}: the vectors compared, so the comparison can be recomputed offline")


if __name__ == "__main__":
    main()
