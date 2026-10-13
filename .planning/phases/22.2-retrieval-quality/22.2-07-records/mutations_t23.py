#!/usr/bin/env python3
"""The guards of 22.2-07 Tasks 2 and 3, mutated one at a time under the mutation rule.

A MEASUREMENT RECORD, not product code. Each row runs `mutate.py` on one
committed file (bytes mutated in place, neutered rather than deleted, proven to
land, the tests run, `git checkout --` restores it, proven by bytes), appending
to mutations.txt.

USAGE (with the workers venv's python, on a clean committed tree)
    python mutations_t23.py [--only T2-M1 ...]
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = "services/workers/scripts/rag_quality_harness.py"
HARNESS_TESTS = ["tests/test_rag_quality_harness.py"]
INDEPENDENCE = "test_a_decision_question_on_another_questions_target_is_refused_naming_both"
FLAG = "test_the_flag_reaches_the_pipeline_the_engine_and_exact"

MUTATIONS = [
    ("T2-M1", "the independence check neutered (the plan's)", HARNESS,
     'if (a.get("set") in DECISION_SETS or b.get("set") in DECISION_SETS) and same_target(a, b):',
     'if (a.get("set") in DECISION_SETS or b.get("set") in DECISION_SETS) and same_target(a, b) and False:',
     HARNESS_TESTS, [INDEPENDENCE]),
    ("T2-M2", "only pairs of two decision-set questions checked", HARNESS,
     'or b.get("set") in DECISION_SETS) and same_target(a, b):',
     'and b.get("set") in DECISION_SETS) and same_target(a, b):',
     HARNESS_TESTS, [f"{INDEPENDENCE}[first0"]),
    ("T2-M3", "a symbol-less question no longer targets its file", HARNESS,
     "if not sa or not sb:", "if not sa and not sb:",
     HARNESS_TESTS, [f"{INDEPENDENCE}[first4"]),
    ("T2-M4", "symbols compared by equality, not symbol_matches", HARNESS,
     "return symbol_matches(sa, sb) or symbol_matches(sb, sa)", "return sa == sb",
     HARNESS_TESTS, [f"{INDEPENDENCE}[first1", f"{INDEPENDENCE}[first2"]),
    ("T3-M1", "the pipeline ignores embedding_model (the plan's)",
     "services/workers/workers/pipeline/ingestion_pipeline.py",
     "EmbeddingGenerator(api_key=openai_api_key, model=embedding_model)", "EmbeddingGenerator(api_key=openai_api_key)",
     ["workers/pipeline/test_pipeline.py"], ["test_a_given_model_reaches_the_generator_and_the_stored_rows"]),
    ("T3-M2", "the engine ignores embedding_model (the plan's)",
     "services/workers/workers/retrieval/query_engine.py",
     "EmbeddingGenerator(api_key=openai_api_key, model=embedding_model)", "EmbeddingGenerator(api_key=openai_api_key)",
     ["workers/retrieval/test_query_engine.py"],
     ["test_a_given_model_reaches_the_generator_and_the_vector_legs_filter"]),
    ("T3-M3", "--embedding-model does not reach --ingest's pipeline", HARNESS,
     "openai_api_key=OPENAI, embedding_model=embedding_model)", "openai_api_key=OPENAI)",
     HARNESS_TESTS, [FLAG]),
    ("T3-M4", "--embedding-model does not reach --measure", HARNESS,
     "vector_tolerance=a.vector_tolerance, embedding_model=a.embedding_model)", "vector_tolerance=a.vector_tolerance)",
     HARNESS_TESTS, [FLAG]),
    ("T3-M5", "--embedding-model does not reach --exact", HARNESS,
     "openai_api_key=OPENAI, embedding_model=a.embedding_model)", "openai_api_key=OPENAI)",
     HARNESS_TESTS, [FLAG]),
    ("T3-M6", "the default model changed", "services/workers/workers/embeddings/defaults.py",
     'DEFAULT_EMBEDDING_MODEL = "text-embedding-ada-002"', 'DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"',
     ["workers/pipeline/test_pipeline.py", "workers/retrieval/test_query_engine.py", *HARNESS_TESTS],
     ["TestTheEmbeddingModel::test_the_default_is_ada_002", "test_with_no_flag_the_resolved_model_is_ada_002"]),
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
