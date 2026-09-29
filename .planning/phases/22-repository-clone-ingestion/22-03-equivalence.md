# 22-03 — The storage-move equivalence rule, and its verdict

**Committed before any measurement**, on 2026-09-29, as `22-03-PLAN.md` Task 1
requires. The rule below is the plan's text, verbatim. The section after it
fixes the precision the rule leaves open — what "top 50", "matches", "equal"
and "fully explained" mean to the comparison script — and it is written here,
now, for the reason `boost-defaults-protocol.md` gives: a definition written
before the numbers exist is a definition; written after, it is tuning. The
measurement sections at the end are empty in this commit and are filled in
only after the runs.

## The rule

*The storage move passes only if, for every question in every set (`tuning`,
`holdout`, `confirm`) of every corpus (`self`, `miniflux`, `mealie`), the final
file rank and the final symbol rank are equal under the Qdrant read path and
the pgvector read path, or the difference is fully explained by one of:*
- *(a) a chunk with no Qdrant point entering or leaving the vector leg's top 50.
  Those are the duplicate-content chunks: 12 in miniflux and 80 in mealie;*
- *(b) the Qdrant vector leg's top 50 differing from exact search while the
  pgvector leg's matches it;*
- *(c) equal fused scores ordered differently.*

*Any unexplained difference fails the gate. Stop, record it, investigate. **Do
not tune.** Aggregate metrics are reported, not judged.*

## Precision, fixed before anything is measured

These definitions are what `scripts/rag_benchmarks/compare_runs.py`
implements. The script is the arbiter: the verdict is its exit code and its
table, not a reading of the records by hand.

### What is compared

- **One scratch database, one set of query vectors.** The baseline (Qdrant
  read path) and the candidate (pgvector read path) are measured against the
  same scratch Postgres, ingested once, so chunk ids and stored vectors are
  identical; and against the same cached query vectors, embedded once with
  `text-embedding-ada-002` (U3) and reused by both runs. Each record carries
  the SHA-256 of the query vector it used, computed over the vector's JSON
  float list (`json.dumps(vector, separators=(",", ":"))`). **The script
  refuses, before comparing anything, if any question's hash differs between
  the two files**, if the two files do not hold the same questions, or if
  their `top_k` or boost configuration differ. Different vectors would make
  every comparison meaningless.
- **The final ranks** are the harness's `file_rank` and `symbol_rank`, over
  the final list cut at the harness's default `top_k = 5`. A question
  *differs* when either rank differs between the runs, a miss (`None`) against
  a number included.
- **The traces.** Each record holds, from inside `QueryEngine.query`: the
  keyword leg and the vector leg (chunk ids with scores, up to 50 each, in
  the order the retriever returned them), the fused list (`rrf_score`), the
  boosted list in its final sorted order (`boosted_score`,
  `boost_multiplier`), and the final `top` list after enrichment.
- **The Qdrant point set** `Q` per corpus: every point id Qdrant holds for
  the corpus's repository, scrolled out before the baseline is measured.
  Class (a) is defined by it. The chunks with no point are the
  duplicate-content chunks; their counts are read from the data and reported.
- **The exact list** `E` per question: the vector ranking computed in
  Postgres with `enable_indexscan = off` and `enable_bitmapscan = off`
  (a sequential scan, so no index is consulted), by cosine distance, with the
  same repository and model filter the retriever uses, from the cached query
  vector, as `rag_doc_app` under the harness organization. It holds the top
  50 **and every chunk tied with the 50th** (see the tolerances), so a tie
  group that straddles the cut is known completely.

### Tolerances

Scores are never compared for exact equality. Two scores are *tied* when:

| Score | Tied when | Why this tolerance |
|---|---|---|
| vector similarity (Qdrant `score`; pgvector `1 - distance`) | \|Δ\| ≤ 1e-5, absolute | both stores hold float32 and accumulate a 1,536-term cosine differently (PR #49's review ruling); an order within 1e-5 is a property of the arithmetic, not of the vectors |
| keyword score (`ts_rank_cd`, float4) | \|Δ\| ≤ 1e-6 × max(1, \|a\|, \|b\|) | float4 output, unchanged computation on both sides |
| fused score (`rrf_score`) and boosted score | \|Δ\| ≤ 1e-9, absolute | computed in Python doubles from integer ranks. The smallest gap between two **distinct** RRF sums over ranks 1–50 in two legs, k = 60, is **2.147e-8** (between (29, 49) and (37, 39); computed before this was written), so 1e-9 can never merge two different rank combinations, and it is far above double rounding |

### Rankings, and what "the same" means

A *ranking* is an ordered list of (chunk id, score). Two rankings **agree**
when they hold the same chunk ids, each chunk's score is tied between the two
(per the table), and every pair of chunks whose scores are *not* tied stands
in the same order in both. In words: identical, up to the order of tied
chunks. This is the only notion of "equal", "matches" and "the same" the
comparison uses.

Where a list is cut (50 per leg), the tie group at the cut may show different
members of one tie group on the two sides. That is accepted only where the
members are shown to be tied by the exact list's recorded tail, or where one
list is shorter and its last group is a subset of the other's corresponding
group. Otherwise the rankings do not agree.

*Prefix agreement* of a shorter ranking with a longer one: the shorter agrees,
as above, with the longer cut to the shorter's length, its last tie group
being allowed to be a subset of the longer's group at that position.

`strip(L)` is the ranking `L` with every chunk not in `Q` removed.

### The classes, precisely

For a question that differs, the script runs these tests in this order and
assigns the first class whose condition holds:

0. **Consistency, both runs.** The fused list recomputed from that run's own
   recorded legs (RRF, k = 60, as `rrf_fusion.py` does it) agrees with the
   recorded fused list; every boosted score is that chunk's fused score times
   its multiplier; every chunk present in both runs has the same multiplier
   in both; and the recorded `top` is the recorded boosted list's first
   `top_k` chunk ids. A failure of any of these is **UNEXPLAINED** — it would
   mean the move changed more than storage, and no class covers that.
1. **The keyword legs must agree.** The keyword leg's SQL changes only its
   filter (latest run → repository, ISS-027); on a corpus with one run the
   rows are the same. If the keyword rankings do not agree: **UNEXPLAINED**.
2. **If the vector rankings agree**, the legs are the same and the only thing
   that can have moved is the order of tied chunks. If the two runs' boosted
   rankings agree, the difference is **(c)**: equal fused scores (the tied
   chunks' scores are the same set, assigned in the other order) ordered
   differently, and the harness's top-5 cut fell between them. If the
   boosted rankings do not agree: **UNEXPLAINED**.
3. **(a).** `strip(pgvector leg)` prefix-agrees with the Qdrant leg: the only
   vector-leg difference is chunks with no Qdrant point entering the top 50
   (and the Qdrant leg's tail being displaced by them). Note that pgvector
   holds a vector for every chunk, so a chunk can only *enter*; "leaving"
   in the rule is the displaced tail, which this test covers.
4. **(b).** The pgvector leg agrees with `E` and the Qdrant leg does not
   prefix-agree with `strip(E)`: pgvector matches exact search and Qdrant did
   not. Whether Qdrant searched approximately is measured here, not assumed.
5. Otherwise **UNEXPLAINED**.

Once a vector-leg difference is (a) or (b), the final-rank difference is
fully explained by it, because fusion and boosts are deterministic in the leg
lists and step 0 has verified that determinism on both runs. A pgvector leg
that misses a chunk exact search finds is *not* explained by anything above:
(b) excuses Qdrant's approximation, never pgvector's.

### Reported, not judged

- Every differing question, its class, and the ranks on both sides, in the
  script's table.
- The counts per class and the UNEXPLAINED count. **The gate passes only with
  zero UNEXPLAINED**; the script's exit code says so.
- Aggregates under both runs (recall@5, rank-1, MRR, file and symbol level,
  per corpus and set), which the rule does not judge.
- For information only: the number of questions whose full boosted rankings
  agree; the largest |Δ similarity| between Qdrant's and pgvector's score for
  a chunk both legs returned; the number of chunks without a Qdrant point per
  corpus, against the plan's 12 and 80.

## The baseline

Recorded 2026-09-29, on the code as 22-02 left it plus the instrumentation
committed in `9bd404b` (after the rule, `e87cbf1`). Nothing in the read path
had changed: the vector leg was Qdrant's. The records are committed, gzipped,
in `22-03-records/`; `compare_runs.py` reads them as they are.

**Scratch stores, on free ports, removed after the verdict.** A fresh
`pgvector/pgvector:pg16@sha256:ccc6e83d…` (`rag2203-pg`, 127.0.0.1:61797,
`--shm-size=1g`) with `migrate … up` to 17 clean (64 partitions, pgvector
0.8.6) and `rag_doc_app` created `NOSUPERUSER NOBYPASSRLS` with the Python
harness's grants; a scratch `qdrant/qdrant:latest` (`rag2203-qdrant`,
127.0.0.1:61799), empty. Port 5434 and compose's stores were never touched;
the guard was shown refusing `--ingest` and `--clear` on them from the CLI
(exit 1, before any connection) before the first ingest.

**Ingest, as the superuser** (`--corpus <c> --corpora-dir <path> --fetch
--check`, then `--ingest`), one embedding call per batch, each vector written
to both stores (P15's transition):

| corpus | files | chunks | embeddings | chunks without a Qdrant point | plan said |
|---|---|---|---|---|---|
| self | 74 | 534 | 534 | 0 | — |
| miniflux (76889f08) | 335 | 2,134 | 2,122 | **12** | 12 |
| mealie (84b2677f) | 390 | 2,816 | 2,736 | **80** | 80 |

The three corpora sit under the harness's one organization, so one
partition, `chunks_p0`, holds all 5,484 rows.

**The Qdrant point set** (`--qdrant-ids`, `qdrant_ids-<c>.json`): 534, 2,122
and 2,736 point ids, scrolled with the repository filter; the counts above
are `postgres_chunks − points`.

**The query vectors** (`vecs.json`): 130 questions (40 `self`, 45 miniflux,
45 mealie), each embedded **once**, with `text-embedding-ada-002`, in three
batch calls during the `--exact` step; every later run read the file and
embedded nothing (`0 embedded now` in each log). 1,536 dimensions each.

**The exact lists** (`--exact`, `exact-<c>.json`), as `rag_doc_app`
(`rolsuper=false, rolbypassrls=false`) under the harness tenant, with
`enable_indexscan` and `enable_bitmapscan` off, filtered by repository and
`embedding_model = 'text-embedding-ada-002'`, from the cached vectors. The
recorded plan on every corpus: `Limit → Sort (embedding <=> '[…]'::vector,
id) → Append, Subplans Removed: 63 → Seq Scan on chunks_p0` — exact search,
pruned to the one partition by the policy. Top 50 per question plus the
chunks tied with the 50th: 0 tail ties in `self`, 2 in miniflux, 36 in
mealie (the duplicate groups), every tail complete.

**The measurement** (`--measure --set all --query-vectors vecs.json --record
baseline-<c>.jsonl`), with `DATABASE_URL` carrying
`options=-c role=rag_doc_app`. Every record header says `vector_backend:
qdrant`, `harness_commit: 9bd404b`, `top_k: 5`, no boost config, and the
measuring connection `current_user = rag_doc_app` (session user `scratch`),
`rolsuper = false`, `rolbypassrls = false`. 130 question records, **0
errors**, 130 distinct query-vector hashes, 50 entries in every vector leg,
5 in every `top`.

**Aggregates under the Qdrant read path**, reported and not judged
(`recall@5`, rank-1, MRR; symbol level where questions name a symbol):

| corpus | set | file recall@5 | file #1 | file MRR | symbol recall@5 | symbol #1 | symbol MRR |
|---|---|---|---|---|---|---|---|
| self | all (40) | 32/40 | 21 | 0.643 | — | — | — |
| miniflux | all (45) | 30/45 | 17 | 0.489 | 25/45 | 11 | 0.353 |
| mealie | all (45) | 33/45 | 15 | 0.475 | 24/45 | 9 | 0.301 |

(`compare_runs.py` prints them per set as well.)

**Two things the baseline shows about the comparison itself.**

- **The keyword leg is empty for 30 of 40 `self` questions and for all 90
  benchmark questions** (ISS-029: `plainto_tsquery` demands every word). So
  on the benchmark corpora the final ranking *is* the vector leg's order
  through fusion and boosts, and the gate is almost entirely a test of the
  vector leg. That is the rule's subject, so nothing changes; it is written
  here so the numbers are read correctly.
- **The keyword leg's two predicate shapes plan identically.** Measured on
  the scratch data as `rag_doc_app` before anything was changed
  (`explain_fts_shapes.py`): 22-02's `ingestion_run_id = <latest run>` and
  22-03's `repository_id = <repo>` both give `Limit → Sort → Append
  (Subplans Removed: 63) → Bitmap Heap Scan on chunks_p0 (Filter: the policy
  and the two tsvector predicates) → Bitmap Index Scan` on the respective
  btree. The sort therefore receives the same rows in the same (heap) order
  either way, which is what decides the order of equal `ts_rank_cd` scores.
  Neither shape consults the GIN indexes on this data, which is why the
  breadcrumb-index proof needs its own shape (Task 2).

`hnsw.ef_search` and `hnsw.iterative_scan` read as unset in the baseline
headers: pgvector registers its parameters when its library loads, which had
not happened on the fresh header connection. The candidate's harness loads it
first. The baseline never set either; the read path was Qdrant's.

## The candidate and the verdict

Recorded 2026-09-29, on the read path of `f946240` (both legs in Postgres)
with the harness of `4fd8f80`, against **the same scratch database** (nothing
written to it since the ingest) and **the same `vecs.json`**: every log line
says `0 embedded now`. The command, per corpus, as `rag_doc_app`:

```
DATABASE_URL="…?options=-c role=rag_doc_app" python scripts/rag_quality_harness.py \
    --corpus <c> --corpora-dir <path> --measure --set all \
    --query-vectors vecs.json --record pgvector-<c>.jsonl
python scripts/rag_benchmarks/compare_runs.py --records <dir>
```

Every candidate header says `vector_backend: pgvector`, and now records
**two** measuring connections, the keyword leg's and the vector leg's, both
`current_user = rag_doc_app`, `rolsuper = false`, `rolbypassrls = false`.
The session-level `hnsw.iterative_scan` reads `off` (the server default: the
retriever sets it `LOCAL`, inside its transaction, which
`test_iterative_scan_is_set_inside_the_retrievers_own_transaction` reads).
`hnsw.ef_search` is 40, the default. 130 records, 0 errors, 130 hashes
identical to the baseline's.

**`compare_runs.py`, exit code 0. Its output, verbatim
(`22-03-records/compare_runs.txt`):**

```
corpus   id         set      file         symbol       class       note
---------------------------------------------------------------------------------------------------------------
(no question differs in file rank or symbol rank)
---------------------------------------------------------------------------------------------------------------
differing questions: 0   (a)=0   (b)=0   (c)=0   UNEXPLAINED=0

Aggregates, reported and not judged (recall@k = found/questions, rank-1, MRR):
corpus    set      side      file recall  file #1  file MRR  sym recall  sym #1  sym MRR 
self      holdout  qdrant    12/15        7        0.633     0/0        0       0.000   
self      tuning   qdrant    20/25        14       0.649     0/0        0       0.000   
self      holdout  pgvector  12/15        7        0.633     0/0        0       0.000   
self      tuning   pgvector  20/25        14       0.649     0/0        0       0.000   
miniflux  confirm  qdrant    10/15        6        0.494     8/15       3       0.319   
miniflux  holdout  qdrant    11/15        5        0.489     11/15       5       0.478   
miniflux  tuning   qdrant    9/15        6        0.483     6/15       3       0.263   
miniflux  confirm  pgvector  10/15        6        0.494     8/15       3       0.319   
miniflux  holdout  pgvector  11/15        5        0.489     11/15       5       0.478   
miniflux  tuning   pgvector  9/15        6        0.483     6/15       3       0.263   
mealie    confirm  qdrant    14/15        7        0.629     10/15       4       0.376   
mealie    holdout  qdrant    11/15        5        0.489     6/15       2       0.239   
mealie    tuning   qdrant    8/15        3        0.307     8/15       3       0.290   
mealie    confirm  pgvector  14/15        7        0.629     10/15       4       0.376   
mealie    holdout  pgvector  11/15        5        0.489     6/15       2       0.239   
mealie    tuning   pgvector  8/15        3        0.307     8/15       3       0.290   

For information only:
  self: 40/40 questions with fully agreeing boosted rankings; max |delta similarity| over 2000 chunk scores both legs returned = 4.66e-07; chunks without a Qdrant point: 0 (534 in Postgres, 534 points)
  miniflux: 39/45 questions with fully agreeing boosted rankings; max |delta similarity| over 2243 chunk scores both legs returned = 4.85e-07; chunks without a Qdrant point: 12 (2134 in Postgres, 2122 points)
  mealie: 25/45 questions with fully agreeing boosted rankings; max |delta similarity| over 2152 chunk scores both legs returned = 6.03e-07; chunks without a Qdrant point: 80 (2816 in Postgres, 2736 points)

VERDICT: PASS (0 UNEXPLAINED)
```

**The verdict: PASS.** Not one of the 130 questions changed its file rank or
its symbol rank, so no class had to explain anything, and every aggregate is
identical under the two read paths. The rule's classes stayed in the
definition, unused. **Stated in the terms of what was measured:** the storage
move, judged by exact search on both sides (Qdrant below its
`full_scan_threshold`, pgvector on plans that never touched the HNSW index,
on repositories of at most 2,816 chunks), changed no ranking on 130
vector-leg questions; the keyword leg was empty for 120 of them (ISS-029).
HNSW-served rankings are not judged here; they are 22.1-05's.

**Re-judged after PR #53's review (2026-09-29)**, with the hardened
`compare_runs.py` that recomputes every record's ranks from its own final
list, refuses a record whose ranks disagree, requires the final lists of a
class-(c) question to differ only by tied chunks, and holds the exact list
to a run's standard (role, model, repository): the same 260 records give
the same line, `differing questions: 0   (a)=0   (b)=0   (c)=0
UNEXPLAINED=0`, `VERDICT: PASS (0 UNEXPLAINED)`, exit 0, with no record
refused — all 260 recorded ranks are what their final lists give. The
records predate the `exact_paths` header field, so the script prints that it
assumed the harness's rule (`self` by substring, benchmark corpora exactly);
a wrong assumption would have been refused, not passed.

**What the information lines add, read and not judged.**
- The Qdrant score and the pgvector score of the same chunk for the same
  query differ by at most **6.03e-07** across 6,395 pairs, none above 1e-6,
  three orders of magnitude inside the 1e-5 tolerance and exactly the
  float32-accumulation difference the review predicted; the ranks they
  induce agreed everywhere. **For the retrieval-quality track, from PR #53's
  review (reviewer A, 2026-09-29):** 1e-5 was about 16× generous, harmless
  here because no adjacent pair within it changed a rank; the track should
  fix its vector-score tolerance at about **2e-6**, from this measurement,
  dated, **before** its next rule is written, so that a tolerance is never
  chosen with a difference in view.
- The **full** boosted rankings (all ~50–100 chunks, scores compared) agree
  on 40/40 `self`, 39/45 miniflux and 25/45 mealie questions. The 6 and 20
  that differ are the questions whose vector top 50 now contains
  duplicate-content chunks that had no Qdrant point (class (a)'s
  mechanism), which shifted ranks below the top-5 cut without reaching it.
  That is the one difference P6 said to expect, seen where it was expected.
- On this data the planner ran the pgvector leg as **exact search**: the
  recorded plans are a Bitmap Heap Scan through the repository btree
  (`self`, miniflux) or a Seq Scan (mealie), each `Subplans Removed: 63`,
  never the HNSW index. So the gate measured the storage move, not HNSW
  recall; HNSW eligibility is `test_the_hnsw_index_can_serve_the_vector_leg`'s
  proof, and the plan at production size is 22.1-05's recall test. Qdrant,
  for its part, searched with `full_scan_threshold: 10000` on collections
  of 534–2,736 points, that is, exactly; the ranks say so.

**Both containers were removed after this was recorded**, as the plan
directs; the database they held is reproducible from the ingest command and
the pinned corpus commits, and the records are committed.
