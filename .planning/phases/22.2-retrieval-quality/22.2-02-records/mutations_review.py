#!/usr/bin/env python3
"""Re-run all of 22.2-02's mutations on the reviewed code, plus PR #66's new ones (by mutate.py).

    mutations_review.py <out-file>

The 14 of `mutations.txt`, unchanged in what they neuter, then four new ones:
R-X1 is review B's probe on clause 2's "needs a raise"; R-I1, R-M1 and R-M2
neuter the fixes for review A's I-1, M-1 and M-2; R-N5, added at review A's
re-check, removes the truncation count's checkpoint (A N-5). Exits 1 if any survives.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARSER = "services/workers/workers/parser/tree_sitter_parser.py"
CHUNKER = "services/workers/workers/chunker/semantic_chunker.py"
META = "services/workers/workers/chunker/metadata_builder.py"
HANDLER = "services/workers/workers/ingest/handler.py"
GENERATOR = "services/workers/workers/embeddings/embedding_generator.py"
PC = ["workers/parser", "workers/chunker"]

MUTATIONS = [
    ("T1-M1", PARSER, 'return "tsx" if path.lower().endswith(".tsx") else "typescript"',
     'return "typescript" if path.lower().endswith(".tsx") else "typescript"', 1, PC,
     ["test_each_fixture_parses_clean_in_the_grammar_the_parser_chooses[tsx]"]),
    ("T1-M2", PARSER, "        return self.queries[grammar]",
     '        return self.queries["typescript" if grammar == "tsx" else grammar]', 1, PC,
     ["test_a_tsx_file_yields_its_named_chunks"]),
    ("T1-M3", PARSER, '"functions": Query(self.languages[grammar], _ECMASCRIPT_FUNCTIONS),',
     '"functions": Query(self.languages["typescript" if grammar == "javascript" else grammar], _ECMASCRIPT_FUNCTIONS),',
     1, PC, ["test_the_javascript_queries_match_javascript", "test_a_js_file_yields_its_named_chunks"]),
    ("T1-M4", PARSER, "value: [(arrow_function) (function_expression)]", "value: [(regex) (regex)]", 2, PC,
     ["test_a_const_bound_function_is_named_by_its_variable_and_spans_the_declaration"]),
    ("T1-M5", PARSER, "if tree.language != self.languages[grammar]:",
     "if False and tree.language != self.languages[grammar]:", 1, PC,
     ["test_a_tree_is_never_queried_with_another_grammars_set"]),
    ("T2-M1", CHUNKER, 'if node.parent is not None and node.parent.type == "decorated_definition":',
     'if False and node.parent is not None and node.parent.type == "decorated_definition":', 1, PC,
     ["test_a_decorated_top_level_function_starts_at_its_decorator",
      "test_a_decorated_method_starts_at_its_decorator_and_keeps_its_breadcrumb"]),
    ("T2-M2", META, "        prev = outer.prev_named_sibling", "        prev = node.prev_sibling", 1, PC,
     ["test_jsdoc_above_a_function_an_exported_function_and_a_const_is_the_docstring"]),
    ("T2-M3", PARSER, 'if language == "go" and spec_nodes and self._is_grouped(class_node):',
     'if False and language == "go" and spec_nodes and self._is_grouped(class_node):', 1, PC,
     ["test_each_struct_in_a_group_is_chunked_as_its_own_spec"]),
    ("T2-M4", CHUNKER, "            if decorators:", "            if False and decorators:", 1, PC,
     ["test_a_decorated_method_starts_at_its_first_decorator_and_keeps_its_breadcrumb",
      "test_an_exported_class_starts_at_the_decorator_before_export"]),
    ("T3-M1", HANDLER, "    if files and errors and not chunks:", "    if False and files and errors and not chunks:", 1,
     ["tests/ingest"], ["test_some_files_raising_and_the_rest_parsing_to_no_chunks_fails_the_job"]),
    ("T3-M2", HANDLER, "    if errors > len(files) * MAX_PARSE_ERROR_SHARE:",
     "    if False and errors > len(files) * MAX_PARSE_ERROR_SHARE:", 1, ["tests/ingest"],
     ["test_three_of_four_files_raising_fails_the_job_though_one_parsed"]),
    ("T3-M3", HANDLER, "    if files and parsed == 0:", "    if False and files and parsed == 0:", 1, ["tests/ingest"],
     ["test_a_chunker_that_fails_on_every_file_fails_the_job_instead_of_emptying_the_index"]),
    ("T3-M4", HANDLER, '        progress["chunks_truncated"] = _count_truncated(ctx, deps, chunks)',
     '        progress["chunks_truncated"] = 0 and _count_truncated(ctx, deps, chunks)', 1, ["tests/ingest"],
     ["test_an_oversized_chunk_is_counted_and_named_in_a_warning_without_its_content"]),
    ("T3-M5", GENERATOR, "            logger.warning(", "            (lambda *a: None)(", 1, ["workers/embeddings"],
     ["test_an_over_limit_chunk_is_truncated_and_logged_without_its_content"]),
    ("R-X1", HANDLER, "    if files and errors and not chunks:", "    if files and (errors or True) and not chunks:", 1,
     ["tests/ingest"], ["test_files_that_yield_no_chunks_with_nothing_raised_complete"]),
    ("R-I1", META, 'while prev is not None and prev.type in ("decorator", "comment"):',
     'while prev is not None and prev.type in ("decorator",):', 1, PC,
     ["test_a_jsdoc_between_a_decorator_and_its_method_stays_inside_the_span",
      "test_a_comment_between_two_decorators_does_not_drop_the_first"]),
    ("R-M1", PARSER, 'return any(child.type == "(" for child in type_declaration.children)',
     'return len([c for c in type_declaration.named_children if c.type in ("type_spec", "type_alias")]) > 1',
     1, PC, ["test_a_parenthesised_group_of_one_is_chunked_as_its_spec"]),
    ("R-M2", META, 'return node.parent is not None and node.parent.type == "class_body"',
     'return True or (node.parent is not None and node.parent.type == "class_body")', 1, PC,
     ["test_an_object_literals_method_names_nothing_but_a_class_method_does"]),
    ("R-N5", HANDLER, "            _checkpoint(ctx)", "            pass", 1, ["tests/ingest"],
     ["test_a_shutdown_during_the_truncation_count_stops_before_counting_on"]),
]


def main(out: Path) -> int:
    out.write_text("", encoding="utf-8")
    survived = []
    for label, path, find, replace, occurrences, tests, expect in MUTATIONS:
        r = subprocess.run([sys.executable, str(HERE / "mutate.py"), "--label", label, "--file", path,
                            "--find", find, "--replace", replace, "--occurrences", str(occurrences),
                            "--tests", *tests, "--expect-fail", *expect, "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8")
        verdict = [ln for ln in r.stdout.splitlines() if "VERDICT" in ln]
        print(verdict[0].strip() if verdict else f"{label}: no verdict, exit {r.returncode}: {r.stderr[-300:]}")
        if r.returncode:
            survived.append(label)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} killed" + (f"; NOT: {survived}" if survived else ""))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
