#!/usr/bin/env python3
"""The guards PR #67's reviews added, mutated one at a time under the mutation rule.

A MEASUREMENT RECORD, not product code. Each row runs `mutate.py` on one
committed file (bytes mutated in place, neutered rather than deleted, proven to
land, the tests run, `git checkout --` restores it, proven by bytes), appending
to mutations.txt. The ids name the review finding each guard answers.

USAGE (with the workers venv's python, on a clean committed tree)
    python mutations_review.py [--only R-I1 ...]
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECIDE = "services/workers/scripts/rag_benchmarks/decide.py"
DECIDE_TESTS = ["tests/test_decide.py"]

MUTATIONS = [
    ("R-I1", "a spec arriving with its questions (a rename) accepted", DECIDE,
     "if not any(_blob(spec.parent, p, spec.name) for p in _parents(spec.parent, q)):",
     "if not any(_blob(spec.parent, p, spec.name) for p in _parents(spec.parent, q)) and False:",
     DECIDE_TESTS, ["test_specs_moved_after_the_rule_are_refused"]),
    ("R-I2a", "main catching only foreseen exceptions", DECIDE,
     "except Exception as exc:  # noqa: BLE001", "except ArithmeticError as exc:  # noqa: BLE001",
     DECIDE_TESTS, ["test_any_unforeseen_exception_exits_2", "test_a_malformed_record_is_refused_not_judged"]),
    ("R-I2b", "a truncated .gz not read as unreadable", DECIDE,
     "UNREADABLE = (OSError, ValueError, EOFError)", "UNREADABLE = (OSError, ValueError)",
     DECIDE_TESTS, ["test_a_truncated_record_is_refused", "test_a_truncated_query_vectors_file_is_refused"]),
    ("R-I3", "retrieval_code_version and corpus_tree_digest not compared", DECIDE,
     '"retrieval_code_version", "corpus_tree_digest", "vector_backend")', '"vector_backend")',
     DECIDE_TESTS, ["test_arms_that_differ_in_a_shared_key_are_refused[retrieval_code_version",
                    "test_arms_that_differ_in_a_shared_key_are_refused[corpus_tree_digest"]),
    ("R-M1", "a run off pgvector accepted", DECIDE,
     'if header.get("vector_backend") != "pgvector":', 'if header.get("vector_backend") != "pgvector" and False:',
     DECIDE_TESTS, ["test_a_run_not_on_pgvector_is_refused"]),
    ("R-M2", "a final list past the cut accepted", DECIDE,
     'if len(top) > rule["top_k"]:', 'if len(top) > rule["top_k"] and False:',
     DECIDE_TESTS, ["test_a_final_list_longer_than_the_cut_is_refused"]),
    ("R-M3", "chunker_variant accepted again", DECIDE,
     'CHUNKER_KEYS = ("chunker_version",)', 'CHUNKER_KEYS = ("chunker_version", "chunker_variant")',
     DECIDE_TESTS, ["test_a_chunker_variant_no_header_records_is_refused"]),
    ("R-N1", "pooled MRR@20 scored with the first corpus's exact_paths", DECIDE,
     'recs = [(r, runs[(arm, c)][0]["exact_paths"]) for c in corpora',
     'recs = [(r, runs[(arm, corpora[0])][0]["exact_paths"]) for c in corpora',
     DECIDE_TESTS, ["test_pooled_mrr_at_20_scores_each_corpus_with_its_own_exact_paths"]),
    ("R-N2", "an after-rule on other questions accepted", DECIDE,
     "if prior[key] != rule[key]:", "if prior[key] != rule[key] and False:",
     DECIDE_TESTS, ["test_an_after_rule_on_other_questions_is_refused"]),
    ("R-N3", "the verified order not printed", DECIDE,
     '*order_record, ""]', '""]',
     DECIDE_TESTS, ["test_the_default_arms_adopt_the_chunk_shape_and_reject_m2"]),
    ("R-m5c", "git missing reported as 'not in a git checkout'", DECIDE,
     "except FileNotFoundError:", "except NotADirectoryError:",
     DECIDE_TESTS, ["test_git_missing_at_run_time_is_named_as_such"]),
    ("R-m1", "the aggregate back on the built-in sum (ISS-041)",
     "services/workers/scripts/rag_benchmarks/scoring.py",
     '"mrr": (math.fsum(1.0 / r for r in found) / total)', '"mrr": (sum(1.0 / r for r in found) / total)',
     ["tests/test_tripwire.py"], ["test_the_aggregate_adds_with_fsum"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path, default=HERE / "mutations.txt")
    a = ap.parse_args()
    survived = []
    for label, what, file, find, replace, tests, expect in MUTATIONS:
        if a.only and label not in a.only:
            continue
        with a.out.open("a", encoding="utf-8") as fh:
            fh.write(f"-- {label}: {what}\n")
        code = subprocess.run([sys.executable, str(HERE / "mutate.py"), "--label", label, "--file", file,
                               "--find", find, "--replace", replace, "--tests", *tests,
                               "--expect-fail", *expect, "--out", str(a.out)]).returncode
        if code:
            survived.append(label)
    print(f"\nSURVIVED: {survived}" if survived else "\nall killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
