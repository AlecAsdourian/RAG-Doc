#!/usr/bin/env python3
"""The plan's census checks, read from committed census directories (22.2-02).

    census_checks.py after-ts     <records-dir>   # Task 1: census-before/ vs census-after-ts/
    census_checks.py before-rerun <records-dir>   # census-before-rerun/ keeps census-before/'s fields
    census_checks.py after        <records-dir>   # Task 2: census-before(-rerun)/ vs census-after/

Reads only the census JSON and the gzipped per-chunk rows in the records; no
corpus, database or network. Prints one line per check with PASS or FAIL and
the numbers it compared, and exits 1 if any check failed. Every number the
SUMMARY cites from a census is printed here.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

BENCHMARK = ["self", "self-go", "self-py", "miniflux", "mealie"]
TS = ["linkwarden", "self-ts"]
PARITY = {"linkwarden": 601, "self-ts": 51}
NO_CHUNKABLE = {"linkwarden": 22, "self-ts": 12}

failures = 0


def check(ok: bool, text: str) -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def census(d: Path, name: str) -> dict:
    return json.loads((d / f"census-{name}.json").read_text(encoding="utf-8"))


def names(d: Path, name: str) -> Counter:
    """The multiset of (file_path, chunk_type, breadcrumb): what symbol scoring reads."""
    with gzip.open(d / f"chunks-{name}.jsonl.gz", "rt", encoding="utf-8") as fh:
        return Counter((r["file_path"], r["chunk_type"], r["breadcrumb"]) for r in map(json.loads, fh))


def named_total(r: dict) -> int:
    return sum(v["function"] + v["class"] for v in r["named_chunks_by_extension"].values())


def raised_everywhere(d: Path, corpora) -> None:
    for c in corpora:
        raised = {lang: v["raised"] for lang, v in census(d, c)["fallback_reasons"].items()}
        check(all(v == 0 for v in raised.values()), f"{c}: fallback_reasons.raised {raised}")


def typescript_whole(before: Path, after: Path) -> None:
    for c in TS:
        b, a = census(before, c), census(after, c)
        errs = {k: v["files_with_errors"] for k, v in a["parse_errors_as_chunked"].items()}
        errs_before = {k: f"{v['files_with_errors']}/{v['files']}" for k, v in b["parse_errors_as_chunked"].items()}
        check(sum(errs.values()) == 0, f"{c}: files with parse errors as chunked {errs} (before {errs_before})")
        by_ext = {k: v for k, v in a["named_chunks_by_extension"].items()}
        total = named_total(a)
        sim = a["typescript"]["sim_chunkable_declarations"]
        check(total == PARITY[c] == sim,
              f"{c}: named chunks {total} (by extension {by_ext}; before {named_total(b)}) "
              f"= parity {PARITY[c]} = census sim {sim}")
        silent = a["files_with_declarations_but_no_named_chunk"]["count"]
        check(silent == 0, f"{c}: files_with_declarations_but_no_named_chunk {silent} "
                           f"(before {b['files_with_declarations_but_no_named_chunk']['count']})")
        fb = a["fallback_files_fixed_size_only"]
        check(fb == NO_CHUNKABLE[c] == a["typescript"]["sim_files_with_no_chunkable_declaration"],
              f"{c}: fallback files {fb} (before {b['fallback_files_fixed_size_only']}) = files with no "
              f"chunkable declaration {NO_CHUNKABLE[c]}")
    lw_b, lw_a = census(before, "linkwarden")["typescript"], census(after, "linkwarden")["typescript"]
    nochunk = lw_a.get("real_top_level_const_function_in_no_chunk_today", 0)
    check(nochunk == 0, f"linkwarden: module-level const functions in no chunk {nochunk} "
                        f"(before {lw_b.get('real_top_level_const_function_in_no_chunk_today')}) "
                        f"of {lw_a['real_top_level_const_function']}")


def digests(before: Path, after: Path, same, changed) -> None:
    for c in same:
        b, a = census(before, c)["chunk_set_digest"], census(after, c)["chunk_set_digest"]
        check(a == b, f"{c}: chunk-set digest unchanged {a[:16]} (before {b[:16]})")
    for c in changed:
        b, a = census(before, c)["chunk_set_digest"], census(after, c)["chunk_set_digest"]
        check(a != b, f"{c}: chunk-set digest changed {b[:16]} -> {a[:16]}")


def after_ts(records: Path) -> None:
    before, after = records / "census-before", records / "census-after-ts"
    typescript_whole(before, after)
    raised_everywhere(after, BENCHMARK + TS)
    digests(before, after, BENCHMARK, [])


def flatten(value, prefix=""):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out or {prefix.rstrip("."): {}}
    return {prefix.rstrip("."): value}


NEW_DECORATOR_FIELDS = ("decorators_in_their_own_chunk", "decorators_only_inside_an_enclosing_class_chunk",
                        "route_decorators_in_their_own_chunk",
                        "route_decorators_only_inside_an_enclosing_class_chunk")


def before_rerun(records: Path) -> None:
    """census-before-rerun/ is the base's chunker measured by this branch's census
    (its two new decorator fields): every field census-before/ has must be equal."""
    before, rerun = records / "census-before", records / "census-before-rerun"
    for c in ("self", "self-py", "mealie"):
        fb, fr = flatten(census(before, c)), flatten(census(rerun, c))
        differ = sorted(k for k in fb if fb[k] != fr.get(k))
        extra = {k: fr[k] for k in fr if k not in fb}
        check(not differ, f"{c}: {len(fb)} fields of census-before equal in the rerun; differing {differ}")
        check(all(k.split(".")[-1] in NEW_DECORATOR_FIELDS for k in extra) and extra,
              f"{c}: fields only in the rerun {extra}")


def after_all(records: Path) -> None:
    before, after = records / "census-before", records / "census-after"
    rerun = records / "census-before-rerun"
    for c in ("mealie", "self-py", "self"):
        b, a = census(rerun, c)["python"], census(after, c)["python"]
        for key in ("decorators_in_no_chunk", "decorators_only_inside_an_enclosing_class_chunk",
                    "route_decorators_in_no_chunk", "route_decorators_only_inside_an_enclosing_class_chunk"):
            if key in b or key in a:
                check(a.get(key, 0) == 0, f"{c}: {key} {b.get(key, 0)} -> {a.get(key, 0)}")
        decorated = a.get("decorated_function_definition", 0) + a.get("decorated_class_definition", 0)
        own = a.get("decorators_in_their_own_chunk", 0) + a.get("route_decorators_in_their_own_chunk", 0)
        check(own == decorated, f"{c}: decorators in their own definition's chunk {own} = decorated "
                                f"definitions {decorated} (before {b.get('decorators_in_their_own_chunk', 0) + b.get('route_decorators_in_their_own_chunk', 0)})")
        # The research's field counts a decorated CLASS held by its own class chunk
        # as "only inside a class chunk": what remains of it is exactly those.
        for key in ("decorators_only_inside_a_class_chunk", "route_decorators_only_inside_a_class_chunk"):
            if key in b or key in a:
                expected = a.get("decorated_class_definition", 0) if key.startswith("decorators") else 0
                check(a.get(key, 0) == expected,
                      f"{c}: {key} {b.get(key, 0)} -> {a.get(key, 0)} = decorated classes in their own "
                      f"class chunk {expected}")
    for c in TS:
        t = census(after, c)["typescript"]
        check(t["chunked_with_docstring"] == t["sim_chunkable_with_jsdoc_directly_above"],
              f"{c}: chunked_with_docstring {t['chunked_with_docstring']} = "
              f"sim_chunkable_with_jsdoc_directly_above {t['sim_chunkable_with_jsdoc_directly_above']} "
              f"(before {census(before, c)['typescript']['chunked_with_docstring']})")
    for c in BENCHMARK:
        nb, na = names(before, c), names(after, c)
        check(nb == na, f"{c}: (file_path, chunk_type, breadcrumb) multiset identical "
                        f"({sum(na.values())} rows; only before {sum((nb - na).values())}, "
                        f"only after {sum((na - nb).values())})")
    typescript_whole(before, after)
    raised_everywhere(after, BENCHMARK + TS)
    digests(before, after, ["self-go", "miniflux"], ["self", "self-py", "mealie"])
    for c in BENCHMARK + TS:
        t = census(after, c)["tokens"]
        print(f"INFO  {c}: chunks over 8000 tokens {t['chunks_over_8000_tokens_truncated']}, "
              f"max {t['max']}, billed tokens {t['billed_distinct_content']}")


if __name__ == "__main__":
    stage, records = sys.argv[1], Path(sys.argv[2])
    {"after-ts": after_ts, "before-rerun": before_rerun, "after": after_all}[stage](records)
    print(f"{failures} failed")
    sys.exit(1 if failures else 0)
