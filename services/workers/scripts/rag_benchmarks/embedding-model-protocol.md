# Embedding model: decision protocol

**The rule is the user's.** It was committed on 2026-09-29, before any question
this decision will be judged on exists. It answers `22.2-CONTEXT.md` QU7
(answered the same day): *switch to `text-embedding-3-small` only if it is
better across all apps combined and no single app is clearly worse.* That is
M2 in `22.2-RESEARCH.md` R4.

**The planner wrote the precision, in the same commit:** the definitions, the
arm the rule is judged on, and the threshold's derivation. They rest on
measurements committed in `.planning/phases/22.2-retrieval-quality/22.2-records/`.

> **One number, T, is still only the planner's candidate. It awaits the user's
> confirmation** (see "The threshold, and what needs the user"). The rule is
> complete, and its deciding questions may be written, only once the user has
> confirmed T or set another value in a commit to this file.

## Question

Should the default embedding model change from `text-embedding-ada-002` to
`text-embedding-3-small`?

## Evidence so far, and why it does not decide

- **Nothing has been measured on our corpora.** OpenAI's own general-benchmark
  (MTEB) figures, 61.0% against 62.3%, are the vendor's claim, and they say
  nothing about code retrieval here (`22-RESEARCH.md` Q3, verified source).
- **Price is not what the rule asks about.** $0.10 against $0.02 per million
  tokens: one full ingest of all four corpora costs $0.119 against $0.024
  (`22.2-RESEARCH.md` R4).

## Candidate

- **The model:** `text-embedding-3-small` at its default 1,536 dimensions,
  for both chunk and query embeddings.
- **Everything else identical to the baseline arm:**
  - the same chunk texts: the two arms' chunk-set digests must be equal;
  - the same fusion and boosts;
  - `top_k = 5`.
- **Query vectors** are embedded once per model and cached, in one
  `--query-vectors` file per model.

## Test set

- **Shared with the chunk-shape decision** (22.2 QU6): one fresh set of 15
  questions per app on miniflux, mealie and linkwarden, 45 in all. Its harness
  set name is fixed in 22.2-01.
- **Written after this rule and the chunk-shape rule are both committed,** by
  blind writers with no retrieval access (`22.2-CONTEXT.md` QD10). It is
  committed before any retrieval result for it is seen.
- **Independence:** no question targets a symbol that an earlier question
  targets.

## Arms, and the ones this rule is judged on

- The shared set is measured on four arms, each once: the current chunker and
  the chunk-shape candidate, each with ada-002 and with 3-small.
- **This rule is judged on the two arms whose chunker the chunk-shape verdict
  adopts.** That is the candidate's if its rule passes, and the current
  chunker's if not.
- That is not a choice. It follows from U10, locked in `22-CONTEXT.md`:
  "each is measured on the chunks and vectors it will ship with".

## Rule

Switch the default to `text-embedding-3-small` only if all three hold.
Otherwise keep `text-embedding-ada-002`.

1. **Better across all apps combined, symbol level:** over all 45 questions,
   symbol MRR@5 with 3-small exceeds ada-002's by at least **0.03**.
2. **Better across all apps combined, file level:** over all 45 questions,
   file MRR@5 with 3-small is not lower than ada-002's.
3. **No single app clearly worse:** on each app's 15 questions, file MRR@5
   with 3-small is not lower than ada-002's by more than **T = 0.11**.
   *T is the planner's candidate, awaiting the user's confirmation.*

## Precision, fixed now

- **MRR@5** is the mean over the questions of 1/rank of the first result in
  the final top five that satisfies `scoring.py`'s rule, and 0 when none does.
  - At file level that means the expected path, exactly.
  - At symbol level it means the expected symbol's breadcrumb.
  - Every benchmark question names a symbol, so symbol MRR is over the same 45.
- **Pooled** means over all 45 questions, each weighted equally. With 15 per
  app, this equals the mean of the three apps' MRRs.
- **Differences** are the candidate minus the baseline, on the same questions.
  Comparisons allow 1e-9, for floating-point rounding only:
  - "at least 0.03" is Δ ≥ 0.03 − 1e-9;
  - "not lower" is Δ ≥ −1e-9;
  - "not lower by more than T" is Δ ≥ −T − 1e-9.

  The smallest non-zero change is 0.05 / 45 ≈ 0.0011 in a pooled MRR@5 (one
  rank moving between 4 and 5) and 0.05 / 15 ≈ 0.0033 in one app's. So 1e-9
  never merges two different outcomes.
- **An invalid measurement is not a result.** That covers a failed query (the
  harness exits 2) and a record the judge refuses. Nothing is decided on it. It
  is repeated once the cause is fixed, and the repetition is recorded.
- **Ties inside a final list** stay in the recorded order. On one database with
  one set of query vectors, the harness is deterministic (22-03).

## The threshold, and what needs the user

**What the data gives, mechanically** (`22.2-records/rr-spread.txt`, from
22-03's pgvector records, all 45 questions per app):

| | File RR standard deviation (sample) | Standard error of one app's file MRR@5 over 15 questions |
|---|---|---|
| miniflux | 0.435 | 0.112 |
| mealie | 0.411 | 0.106 |
| both, as one sample of 90 | 0.421 | 0.109 |

Every file-level variant lies in [0.105, 0.112], and each rounds to **0.11**.
That holds for the population or the sample deviation, per app or pooled.

**The candidate:** T = 0.11. That is one standard error of one app's file
MRR@5 over 15 questions: a fall larger than the measurement's own noise.

**Readings the user should confirm.** Without them the number does not follow
mechanically:
1. **"Clearly worse" means worse by more than one standard error.** Two
   standard errors (about 0.22) would read as "worse at about 95% confidence".
   It would also let one app lose a fifth of its MRR unguarded.
2. **The per-app guard is on file MRR,** as M2 was presented (`22.2-RESEARCH.md`
   R4, QU7). If "no single app clearly worse" should also cover symbol MRR, the
   same derivation gives T_symbol = 0.10 (standard errors 0.098–0.105).
3. **It replaces the 0.067 M2 was presented with.** That value was "one answer
   going from #1 to a miss on 15 questions": a resolution threshold, not a noise
   threshold. The user accepted M2 with 0.067 in its text, and the coordinator
   asked for the value derived from the standard error. Only the user can pick
   between them.
4. **linkwarden's spread is not measured yet,** because no questions exist.
   miniflux and mealie stand in for it.
5. **The standard error is one arm's.** The noise in a paired difference depends
   on how correlated the two arms are, which only the measurement shows. It is
   equal to one arm's at a correlation of 0.5, and smaller above that.

**A note on clause 1's 0.03, as accepted.** It is about half the pooled standard
error over 45 questions (0.059 at symbol level, `rr-spread.txt`). So clause 1
alone cannot tell a small real gain from noise; clauses 2 and 3 are what stop a
worse model being adopted. This is recorded so the rule is read correctly, not
changed.

## Measurement

- **The run** (22.2-01 builds the flags):
  - ingest each corpus with each chunker variant and each model into a scratch
    Postgres. Never compose's, and never port 5434;
  - one `--query-vectors` file per model;
  - `--record` as `rag_doc_app`;
  - `decide.py` applies the chunk-shape rule, then this rule on the adopted
    chunk arm.
- **Once:** each arm is run once, and the results are reported whichever way
  they fall.
- **Kept:** the records are committed gzipped.

## Result

*Empty until the measurement. It will hold `decide.py`'s output, the verdict
and the order of record.*

## Order of record

This protocol's commit is the one that adds this file
(`git log --diff-filter=A -- <this file>`). To follow, in this order, each with
its commit and time:
1. T confirmed, or set, by the user;
2. the chunk-shape rule;
3. the shared questions;
4. the measurement.
