# Boost defaults: decision protocol

Fixed 2026-09-14, before the questions this decision will be judged on were written.

## Question

Should `MetadataBooster`'s default multipliers be replaced by neutral boosts, with every multiplier set to 1.0?

## Evidence so far, and why it does not decide

- **Tuning sets.** Neutral boosts raised symbol-level MRR on both corpora, without lowering file-level top-5:
  - miniflux: 0.133 -> 0.263
  - mealie: 0.080 -> 0.290
- **Holdout sets.** Symbol-level MRR rose on both corpora:
  - miniflux: 0.249 -> 0.478
  - mealie: 0.110 -> 0.244
- **Why the holdout result can't decide it.** Mealie's file-level top-5 fell from 12/15 to 11/15, which failed the rule fixed before that run. The holdout sets have now been consulted for this decision, so they cannot settle it.

## Candidate

```json
{"chunk_type_boosts": {"docs": 1.0, "file_summary": 1.0, "class_summary": 1.0, "function": 1.0, "class": 1.0, "test": 1.0},
 "path_boost": 1.0, "breadcrumb_match_boost": 1.0, "quoted_match_boost": 1.0, "identifier_match_boost": 1.0}
```

`noise_penalty` is unchanged.

## Test set

- **Questions:** 15 new questions per corpus, in set `confirm`, with ids mf-31..mf-45 and ml-31..ml-45.
- **Writers:** written blind by agents with no access to retrieval.
- **Independence:** no question targets a symbol any existing question targets.
- **Timing:** committed before any retrieval result for them is seen.

## Rule

Adopt the candidate as the default only if, on BOTH corpora's `confirm` sets:

1. symbol-level MRR is higher than with the current defaults, and
2. file-level MRR is not lower.

Otherwise, keep the current defaults.

This rule weighs MRR rather than top-5 counts. On 15 questions, one answer crossing the rank-5 line is noise, and the previous rule let a single such question decide.

## Measurement

- **Command:** `rag_quality_harness.py --corpus <name> --measure --set confirm`, run once with no `--boost-config` and once with the candidate.
- **Index:** as it stands, with 2,122 miniflux vectors and 2,736 mealie vectors.
- **Runs:** each configuration is run once, and the results are reported whichever way they fall.

## Result

Recorded 2026-09-14, after the measurement above was run once.

| corpus | configuration | file top-5 | file #1 | file MRR | symbol top-5 | symbol #1 | symbol MRR |
|---|---|---|---|---|---|---|---|
| miniflux | current defaults | 10/15 | 5 | 0.447 | 5/15 | 0 | 0.111 |
| miniflux | candidate | 10/15 | 6 | 0.494 | 8/15 | 3 | 0.319 |
| mealie | current defaults | 12/15 | 5 | 0.473 | 6/15 | 2 | 0.196 |
| mealie | candidate | 14/15 | 6 | 0.596 | 10/15 | 3 | 0.342 |

Against the rule:

- **miniflux:** symbol MRR 0.111 -> 0.319, higher; file MRR 0.447 -> 0.494, not lower.
- **mealie:** symbol MRR 0.196 -> 0.342, higher; file MRR 0.473 -> 0.596, not lower.

No metric fell on either corpus.

**Verdict: adopt neutral boosts as the default.**

Order of record, all times PDT on 2026-09-13:

| commit | time | what |
|---|---|---|
| `64c51b4` | 23:47 | this protocol |
| `0ff72f9` | 23:50 | confirm-set support in the harness |
| `6705cd6` | 23:55 | miniflux confirm questions |
| `d038e59` | 23:57 | mealie confirm questions |

The measurement ran after all four.
