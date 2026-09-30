#!/usr/bin/env python3
"""Two checks behind 22.2-RESEARCH.md's TypeScript findings, run against the repository's own code.

A MEASUREMENT RECORD, not product code.

1. The parser's current queries, compiled against tree-sitter-typescript's
   grammars. The query TEXT is captured from `TreeSitterParser._init_queries`
   as it runs (by wrapping `Query` while the parser is constructed), so this
   checks the queries the code holds, not a copy of them.
2. The inputs of the existing TypeScript parser tests, read out of
   `workers/parser/test_parser.py` with `ast`, parsed with the grammar the
   chunker uses for "typescript" (the JavaScript grammar) and with the real one.

USAGE
    ts_grammar_checks.py --workers <services/workers>
"""
import argparse
import ast
import sys
from pathlib import Path

import tree_sitter_typescript
from tree_sitter import Language, Parser, Query


def count_errors(node):
    stack, errors = [node], 0
    while stack:
        n = stack.pop()
        errors += n.type == "ERROR"
        stack.extend(n.children)
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=Path, required=True)
    a = ap.parse_args()
    sys.path.insert(0, str(a.workers))
    import workers.parser.tree_sitter_parser as tsp

    captured = []
    real_query = tsp.Query

    def recording_query(language, text):
        captured.append((language, text))
        return real_query(language, text)

    tsp.Query = recording_query
    try:
        parser = tsp.TreeSitterParser()
    finally:
        tsp.Query = real_query
    ts_lang = parser.languages["typescript"]
    ts_texts = {name: text for (language, text), name in zip(
        [c for c in captured if c[0] == ts_lang], ("functions", "classes"))}
    grammars = {"typescript": Language(tree_sitter_typescript.language_typescript()),
                "tsx": Language(tree_sitter_typescript.language_tsx())}
    print("1. the parser's current \"typescript\" queries, compiled against the TypeScript grammars")
    for name, text in ts_texts.items():
        for gname, language in grammars.items():
            try:
                Query(language, text)
                verdict = "compiles"
            except Exception as exc:  # noqa: BLE001 - the error IS the finding
                verdict = f"{type(exc).__name__}: {exc}"
            print(f"   {name:<9} under {gname:<10}: {verdict}")

    print()
    print("2. the existing TypeScript parser tests' inputs (workers/parser/test_parser.py)")
    tree = ast.parse((a.workers / "workers" / "parser" / "test_parser.py").read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and "typescript" in n.name):
        code = next((node.value.value for node in ast.walk(fn)
                     if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                     and isinstance(node.value.value, str) and any(
                         isinstance(t, ast.Name) and t.id == "code" for t in node.targets)), None)
        if code is None:
            continue
        chunker_tree = parser.parse_file(code, "typescript")
        real_tree = Parser(grammars["typescript"]).parse(code.encode("utf-8"))
        print(f"   {fn.name}: chunker's grammar has_error={chunker_tree.root_node.has_error} "
              f"(ERROR nodes {count_errors(chunker_tree.root_node)}); "
              f"TypeScript grammar has_error={real_tree.root_node.has_error}")


if __name__ == "__main__":
    main()
