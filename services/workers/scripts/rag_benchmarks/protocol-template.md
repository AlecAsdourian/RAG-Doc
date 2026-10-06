# <Decision>: decision protocol

*A template (22.2-07). Copy it to `<decision>-protocol.md` beside this file,
with the rule as `<decision>-rule.json`, and fill each section. Its sections
are those of `boost-defaults-protocol.md` and `embedding-model-protocol.md`.
Delete these italic notes as you go.*

**The rule is the user's.** It is committed on <date>, before any question
this decision will be judged on exists. *Say who wrote what: the user's rule,
the planner's precision, and the commit each landed in.*

## Question

*One sentence: what changes if the candidate is adopted, and from what to
what.*

## Evidence so far, and why it does not decide

*What has been measured or claimed, with sources, and why none of it is the
decision: tuning-set results, vendor figures, cost. Mark inferred figures as
inferred.*

## Candidate

- **The change:** *exactly what the candidate arm runs with.*
- **Everything else identical to the baseline arm:** *the chunker, the model,
  fusion and boosts, `top_k`; say which header fields prove it (the chunk-set
  digest, the model, the chunker version).*
- **Query vectors:** *embedded once per model and cached (`--query-vectors`).*

## Test set

- **The set:** `<set name>`, one of the harness's `DECISION_SETS`
  (`rag_quality_harness.py`): *e.g. `shape-model`, `keyword-leg`.*
- **The questions:** *how many per corpus, on which corpora, and their ids.*
- **The writers and their tools:** *how many writer agents per corpus, their
  agent definition, and what they can read: the pinned checkout only (no
  retrieval results, rankings, records, specs or questions of other sets, or
  decision data; `22.2-CONTEXT.md` QD10).*
- **The targets list they got:** the output of
  `rag_quality_harness.py --corpus <c> --list-targets`, committed with the
  questions. It names each existing question's `(path, symbol)` and no
  question text.
- **Independence:** no question of this set targets an answer any other
  question of the corpus targets, in any set. `validate_spec` enforces it
  (`--check` fails otherwise): one target is one path with symbols that match
  under `scoring.symbol_matches`.
- **Timing:** written after this rule is committed; committed before any
  retrieval result for them is seen; `--check` run offline with 0 failures.

## Arms, and the ones the rule is judged on

- *Each arm: its record prefix (`<arm>-<corpus>.jsonl.gz`), and the value of
  the variable on it (a model, or a chunker version and variant). The JSON's
  `arms` holds the same.*
- *The pair this rule is judged on: `pair` in the JSON, or, when it follows
  another rule, `after` with `arms_by_verdict` (which pair each of that
  rule's verdicts selects), and why.*

## Rule

*The rule's text, the user's words, as numbered clauses: each a level (file
or symbol), a scope (pooled over every question, or each corpus), and a bar
on Δ = candidate − baseline in MRR@k.*

**The JSON beside this file,** `<decision>-rule.json`, encodes the clauses in
`decide.py`'s schema (`rule`, `protocol`, `set`, `corpora`, `top_k`, `metric`,
`variable`, `arms`, `pair` or `after` with `arms_by_verdict`, `clauses`,
`allowance`). A rule that needs a clause type the schema lacks extends the
schema in its own plan, with tests, before its questions exist.

**They agree,** as this command's output shows: *a test that reads this
document's Rule and Precision sections and compares them with the JSON, as
`tests/test_decide.py::TestM2EncodesItsProtocol` does for M2:*

```
pytest tests/test_decide.py -k <the test that ties this protocol to its JSON> -q
```

## Precision

- **MRR@k:** *the mean over the questions of 1/rank of the first result in the
  final top k that satisfies `scoring.py`'s rule, 0 when none does; at file
  level the expected path, at symbol level the expected symbol's breadcrumb.*
- **Pooled:** *over all the questions, each weighted equally.*
- **Differences:** the candidate minus the baseline, on the same questions.
  Comparisons allow 1e-9, for floating-point rounding only: a clause holds
  when Δ ≥ bar − 1e-9.
- **An invalid measurement is not a result:** a failed query (the harness
  exits 2), or anything `decide.py` refuses. Nothing is decided on it; it is
  repeated once the cause is fixed, and the repetition is recorded.
- **Ties:** *ties inside a final list stay in the recorded order; ties at the
  cut, at QD2's 2e-6, are reported by `decide.py`, never judged.*

## Measurement

- **Where:** a scratch pgvector container on a free port, never compose's
  Postgres and never port 5434, removed after the verdict.
- **The commands:** *per arm and corpus, the harness's `--ingest` and
  `--measure --record` invocations, as `rag_doc_app`, with `--set <set name>`,
  `--embedding-model`, `--query-vectors` (one file per model) and
  `--vector-tolerance 2e-6`.*
- **The judge:**

  ```
  scripts/rag_benchmarks/decide.py --rules <rules applied first> <decision>-rule.json \
      --records <records dir> --query-vectors <model>=<file> [<model>=<file>]
  ```

  *Its output and exit code are the verdict (0 every rule adopted, 1 any
  rejected, 2 refused).*
- **Once:** each arm is measured once, and the result is reported whichever
  way it falls.
- **Kept:** the records are committed gzipped, beside the scripts that made
  them.

## Result

*Empty until the measurement. It will hold `decide.py`'s output and exit code,
the aggregates per corpus and pooled, and "adopted" or "not adopted".*

## Order of record

This protocol's commit is `<sha>`, the one that adds this file, on <date>, in
PR #<n>. *Under QD12 (`22.2-CONTEXT.md`) a protocol PR is merged with a merge
commit, so every commit below is an ancestor of `main` and `decide.py` reads
their order from `main`'s history.*

| # | Commit | Time | What |
|---|---|---|---|
| 1 | `<sha>` | <date and time> | this protocol and `<decision>-rule.json` |
| 2 | `<sha>` | | the set's questions, with the targets list the writers got |
| 3 | `<sha>` | | the measurement's records and `decide.py`'s output |
