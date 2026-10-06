#!/usr/bin/env python3
"""The mutations of PR #63's review round, each run through mutate.py.

A MEASUREMENT RECORD, not product code. The reviewers found seven mutations
surviving (review A's D, E, F and G; review B's R-M1 to R-M4, R-M3 being G's
twin); after the fixes each is run again here, with the new guards' own
mutations (N1 to N9). Strings are kept here, not on a command line, because
some hold a CRLF or a backslash. Each appends its block to mutations.txt.

USAGE (from the tree's root, on a committed, clean tree)
    <venv>/python .planning/phases/22.2-retrieval-quality/22.2-01-records/mutations_review.py
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAG = "services/workers/scripts/rag_benchmarks/"
HARNESS = "services/workers/scripts/rag_quality_harness.py"
T_TRIP, T_CENSUS = "tests/test_tripwire.py", "tests/test_chunk_census.py"
T_CMP, T_HARNESS = "tests/test_compare_runs.py", "tests/test_rag_quality_harness.py"
T_ISO = "tests/isolation/test_harness_header_reads.py"
CRLF = "\r\n"

MUTATIONS = [
    ("A-D tripwire QUESTION_KEYS without symbol", RAG + "tripwire.py",
     'QUESTION_KEYS = ("question", "set", "path", "symbol")', 'QUESTION_KEYS = ("question", "set", "path")',
     [T_TRIP], ["refusal_gives_2_and_compares_nothing[symbol]"]),
    ("A-E chunker_version without the CRLF->LF replace", RAG + "chunk_digest.py",
     'data = path.read_bytes().replace(b"\\r\\n", b"\\n")', "data = path.read_bytes()  # MUTATION",
     [T_CENSUS], ["test_line_endings_do_not_change_it"]),
    ("A-F tripwire QUESTION_KEYS without set", RAG + "tripwire.py",
     'QUESTION_KEYS = ("question", "set", "path", "symbol")', 'QUESTION_KEYS = ("question", "path", "symbol")',
     [T_TRIP], ["refusal_gives_2_and_compares_nothing[set]"]),
    ("A-G vector_tolerance() keeps the value it set", RAG + "compare_runs.py",
     "        _vector_tolerance = previous", "        _vector_tolerance = _vector_tolerance  # MUTATION",
     [T_CMP], ["test_the_vector_tolerance_changes_the_class"]),
    ("R-M1 tripwire QUESTION_KEYS = (question,)", RAG + "tripwire.py",
     'QUESTION_KEYS = ("question", "set", "path", "symbol")', 'QUESTION_KEYS = ("question",)',
     [T_TRIP], ["[set]", "[path]", "[symbol]"]),
    ("R-M2 capturing() restores the level but leaves propagate False", RAG + "chunk_census.py",
     "chunker_logger.level, chunker_logger.propagate = saved", "chunker_logger.level = saved[0]  # MUTATION",
     [T_CENSUS], ["test_a_census_leaves_logging_as_it_found_it"]),
    ("R-M3 vector_tolerance(): finally: pass", RAG + "compare_runs.py",
     "    finally:" + CRLF + "        _vector_tolerance = previous", "    finally:" + CRLF + "        pass  # MUTATION",
     [T_CMP], ["test_the_vector_tolerance_changes_the_class"]),
    ("R-M4 --exact ignores --vector-tolerance", HARNESS,
     "do_exact(corpus, questions, query_vectors, model, a.exact, a.vector_tolerance)",
     "do_exact(corpus, questions, query_vectors, model, a.exact)  # MUTATION",
     [T_HARNESS], ["test_exact_passes_the_vector_tolerance_through"]),
    ("N1 the exact list's tie tolerance not checked", RAG + "compare_runs.py",
     "elif float(recorded) != tolerance:", "elif float(recorded) != tolerance and False:  # MUTATION",
     [T_CMP], ["evidences_is_refused[exact-other]"]),
    ("N2 a run header's tolerance not checked", RAG + "compare_runs.py",
     'if header.get("vector_tolerance") is not None and float(header["vector_tolerance"]) != tolerance:',
     'if header.get("vector_tolerance") is not None and float(header["vector_tolerance"]) != tolerance and False:',
     [T_CMP], ["evidences_is_refused[header-other]"]),
    ("N3 --no-qdrant accepts two sets of stored vectors", RAG + "compare_runs.py",
     "elif b.get(key) != c.get(key):", "elif b.get(key) != c.get(key) and False:  # MUTATION",
     [T_CMP], ["different_stored_vectors[stored_vectors_digest]"]),
    ("N4 a commit claim the checkout contradicts is accepted", HARNESS,
     "if head and claim and not (", "if False and head and claim and not (",
     [T_HARNESS], ["test_a_claim_the_checkout_contradicts_is_refused"]),
    ("N5 chunker_version recorded unverified", HARNESS,
     '"chunker_version": running_version if verified else None,', '"chunker_version": running_version,  # MUTATION',
     [T_HARNESS], ["test_different_digests_record_no_chunker_version"]),
    ("N6 tripwire: unforeseen input escapes as exit 1", RAG + "tripwire.py",
     "except (KeyError, TypeError, ValueError, AttributeError) as exc:",
     "except (ZeroDivisionError,) as exc:  # MUTATION",
     [T_TRIP], ["test_input_the_checks_did_not_foresee_exits_2_not_1"]),
    ("N7 the stored-vectors digest without the row ids", RAG + "chunk_digest.py",
     "string_agg(id::text || ':' || md5(embedding::text), ',' ORDER BY id)",
     "string_agg(md5(embedding::text), ',' ORDER BY id)",
     [T_ISO], ["test_the_stored_vectors_digest_names_one_ingest"]),
    ("N8 the breadcrumb consistency count neutered", RAG + "chunk_digest.py",
     "AND breadcrumb IS DISTINCT FROM NULLIF(metadata->>'breadcrumb', '')", "AND false  -- MUTATION",
     [T_ISO], ["test_a_breadcrumb_column_that_disagrees_with_metadata_is_counted"]),
    ("N9 a dirty self tree reported clean", HARNESS,
     'return bool(git("status", "--porcelain", "--", *paths, cwd=root))', "return False  # MUTATION",
     [T_HARNESS], ["test_a_dirty_tree_is_flagged_and_a_clean_one_is_not"]),
]


def main() -> int:
    """Runs every mutation, or those whose labels start with an argument (e.g. `N6 N7`)."""
    only = sys.argv[1:]
    chosen = [m for m in MUTATIONS if not only or any(m[0].startswith(o + " ") for o in only)]
    survived = []
    for label, file, find, replace, tests, expect in chosen:
        code = subprocess.run([sys.executable, str(HERE / "mutate.py"), "--label", label, "--file", file,
                               "--find", find, "--replace", replace, "--tests", *tests,
                               "--expect-fail", *expect, "--out", str(HERE / "mutations.txt")]).returncode
        if code:
            survived.append(label)
    print(f"\n{len(chosen)} mutations; survived: {survived or 'none'}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
