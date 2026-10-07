#!/usr/bin/env python3
"""Every guard of decide.py, mutated one at a time under the mutation rule (22.2-07 Task 1).

A MEASUREMENT RECORD, not product code. Each row runs `mutate.py` (copied
unchanged from 22.2-01's records): the committed file's bytes are mutated in
place, neutered rather than deleted, proven to differ from HEAD's, the tests
run, and the file is restored with `git checkout --`, proven equal to HEAD's
blob by bytes. The output is appended to mutations.txt.

USAGE (with the workers venv's python, on a clean committed tree)
    python mutations_decide.py [--only T1-M3 ...]
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = "services/workers/scripts/rag_benchmarks/decide.py"
TESTS = ["tests/test_decide.py"]

MUTATIONS = [
    # The six the plan names.
    ("T1-M1", "refusal 5: the chunk-set digest check",
     'if not bh.get("chunk_set_digest") or bh.get("chunk_set_digest") != ch.get("chunk_set_digest"):',
     'if (not bh.get("chunk_set_digest") or bh.get("chunk_set_digest") != ch.get("chunk_set_digest")) and False:',
     ["test_a_model_comparison_with_different_chunk_set_digests_is_refused"]),
    ("T1-M2", "refusal 6: the query-vector model check",
     'elif entry.get("model") != model:', 'elif entry.get("model") != model and False:',
     ["test_a_vector_embedded_with_another_model_is_refused"]),
    ("T1-M3", "refusal 7: the order check (every rule commit taken as an ancestor)",
     "capture_output=True).returncode == 0", "capture_output=True).returncode >= 0",
     ["test_questions_then_rule_is_refused", "test_a_rule_changed_after_its_questions_is_refused"]),
    ("T1-M4", "refusal 2: the rank recompute",
     'if expected != (rec["file_rank"], rec.get("symbol_rank")):',
     'if expected != (rec["file_rank"], rec.get("symbol_rank")) and False:',
     ["test_a_rank_its_final_list_does_not_give_is_refused"]),
    ("T1-M5", "the allowance's sign",
     "return delta >= min_delta - allowance", "return delta >= min_delta + allowance",
     ["test_exactly_at_the_threshold_minus_the_allowance_holds_and_2e9_past_it_fails",
      "test_clause_1_adopts_at_exactly_plus_0_03_and_rejects_one_step_short",
      "test_clause_2_adopts_at_zero_and_rejects_one_step_below",
      "test_clause_3_adopts_at_exactly_minus_0_11_and_rejects_one_step_past"]),
    ("T1-M6", "M2 judged on the wrong chunker's arms",
     'pair, why = rule["arms_by_verdict"][prior],',
     'pair, why = rule["arms_by_verdict"]["REJECT" if prior == "ADOPT" else "ADOPT"],',
     ["test_chunk_shape_adopt_judges_m2_on_the_candidate_arms",
      "test_chunk_shape_reject_judges_m2_on_the_current_arms"]),
    # Every other guard.
    ("T1-M7", "refusal 7: one commit adding both",
     "if r == q:", "if r == q and False:", ["test_one_commit_adding_both_is_refused"]),
    ("T1-M8", "refusal 7: an uncommitted rule or spec",
     "if status:", "if status and False:",
     ["test_an_uncommitted_rule_is_refused", "test_uncommitted_questions_are_refused"]),
    ("T1-M9", "refusal 7: a merge commit counted as writing the rule",
     "if all(_blob(cwd, p, name) != blob for p in _parents(cwd, commit)):",
     "if True or all(_blob(cwd, p, name) != blob for p in _parents(cwd, commit)):",
     ["test_a_protocol_pr_merged_with_a_merge_commit_passes"]),
    ("T1-M10", "refusal 1: the question against the spec",
     "if records[qid].get(key) != spec[qid].get(key):", "if records[qid].get(key) != spec[qid].get(key) and False:",
     ["test_a_question_text_that_differs_is_refused"]),
    ("T1-M11", "refusal 1: the question across the arms",
     "if b[qid].get(key) != c[qid].get(key):", "if b[qid].get(key) != c[qid].get(key) and False:",
     ["test_the_arms_question_sets_are_compared_with_each_other_too"]),
    ("T1-M12", "refusal 1: a symbol clause over a symbol-less question",
     "if nameless:", "if nameless and False:",
     ["test_a_symbol_clause_over_a_question_with_no_symbol_is_refused"]),
    ("T1-M13", "refusal 2: a boosted chunk in neither leg",
     "if leg is None:", "if leg is None and False:", ["test_a_boosted_chunk_in_neither_leg_is_refused"]),
    ("T1-M14", "MRR@20: the keyword leg left out of the join",
     'for leg in ("vector", "fts"):', 'for leg in ("vector",):',
     ["test_a_boosted_chunk_found_by_keyword_search_only_is_joined_to_that_leg"]),
    ("T1-M15", "refusal 3: the header's database connection",
     'if identity["rolsuper"] or identity["rolbypassrls"]:',
     'if (identity["rolsuper"] or identity["rolbypassrls"]) and False:',
     ["test_a_superuser_or_rls_bypassing_connection_is_refused[database"]),
    ("T1-M16", "refusal 3: the legs' connections dropped",
     "for p in connection_problems(header) + database_connection_problems(header):",
     "for p in database_connection_problems(header):",
     ["test_a_superuser_or_rls_bypassing_connection_is_refused[fts",
      "test_an_unrecorded_connection_is_refused[connections"]),
    ("T1-M17", "refusal 4: a failed query",
     'if rec.get("error"):', 'if rec.get("error") and False:', ["test_a_failed_query_is_refused"]),
    ("T1-M18", "refusal 5: the shared header keys",
     "if bh.get(key) != ch.get(key):", "if bh.get(key) != ch.get(key) and False:",
     ["test_arms_that_differ_in_a_shared_key_are_refused", "test_the_unchosen_pair_is_checked_too"]),
    ("T1-M19", "refusal 5: one model in a chunk comparison",
     'if bh.get("embedding_model") != ch.get("embedding_model"):',
     'if bh.get("embedding_model") != ch.get("embedding_model") and False:',
     ["test_a_chunk_comparison_across_models_is_refused"]),
    ("T1-M20", "refusal 5: one query vector in a chunk comparison",
     'if b[qid].get("query_vector_sha256") != c[qid].get("query_vector_sha256"):',
     'if b[qid].get("query_vector_sha256") != c[qid].get("query_vector_sha256") and False:',
     ["test_a_chunk_comparison_with_another_query_vector_is_refused"]),
    ("T1-M21", "refusal 5: the arm's declared model",
     'if header.get("embedding_model") != declared:', 'if header.get("embedding_model") != declared and False:',
     ["test_an_arm_that_is_not_its_declared_model_is_refused"]),
    ("T1-M22", "refusal 5: the arm's declared chunker",
     "if header.get(key) != value:", "if header.get(key) != value and False:",
     ["test_an_arm_that_is_not_its_declared_chunker_is_refused"]),
    ("T1-M23", "refusal 5: the header's set against the rule's",
     'if header.get("set") != rule["set"]:', 'if header.get("set") != rule["set"] and False:',
     ["test_arms_that_agree_with_each_other_but_not_the_rule_are_refused[set"]),
    ("T1-M24", "refusal 6: the cached vector's hash",
     'elif vector_sha256(entry["vector"]) != rec.get("query_vector_sha256"):',
     'elif vector_sha256(entry["vector"]) != rec.get("query_vector_sha256") and False:',
     ["test_a_vector_whose_hash_is_not_the_records_is_refused"]),
    ("T1-M25", "refusal 6: the cached vector's question text",
     'elif entry.get("question") != rec.get("question"):', 'elif entry.get("question") != rec.get("question") and False:',
     ["test_a_missing_vector_or_text_is_refused"]),
    ("T1-M26", "refusal 8: the allowance's range",
     "if not (_is_number(allowance) and 0 <= allowance < MAX_ALLOWANCE):",
     "if not (_is_number(allowance) and 0 <= allowance < MAX_ALLOWANCE) and False:",
     ["allowance must be a number"]),
    ("T1-M27", "refusal 8: after naming a rule not applied first",
     'if "after" in rule and rule["after"] not in seen:', 'if "after" in rule and rule["after"] not in seen and False:',
     ["test_m2_alone_is_refused_because_its_after_rule_was_not_applied",
      "test_m2_before_the_chunk_shape_rule_is_refused"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path, default=HERE / "mutations.txt")
    a = ap.parse_args()
    survived = []
    for label, what, find, replace, expect in MUTATIONS:
        if a.only and label not in a.only:
            continue
        with a.out.open("a", encoding="utf-8") as fh:
            fh.write(f"-- {label}: {what}\n")
        code = subprocess.run([sys.executable, str(HERE / "mutate.py"), "--label", label, "--file", FILE,
                               "--find", find, "--replace", replace, "--tests", *TESTS,
                               "--expect-fail", *expect, "--out", str(a.out)]).returncode
        if code:
            survived.append(label)
    print(f"\nSURVIVED: {survived}" if survived else "\nall killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
