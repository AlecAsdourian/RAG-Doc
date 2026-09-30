#!/usr/bin/env python3
"""Offline chunk census for the retrieval-quality track: what the chunker makes of each corpus.

A MEASUREMENT RECORD, not product code. It runs the repository's own
`SemanticChunker` (imported from `services/workers`, unmodified) over each
corpus exactly as `rag_quality_harness.py`'s `collect_files` selects files,
and parses the same files again with tree-sitter to count what the chunker
does not see. No database, no OpenAI: token counts use tiktoken's local
`cl100k_base` (the encoding both text-embedding-ada-002 and
text-embedding-3-small use), and the embedded text is rebuilt with the same
rule as `EmbeddingGenerator._prepare_text_for_embedding` (breadcrumb, then
docstring, then content; truncated at 8,000 tokens).

USAGE
    chunk_census.py --workers <services/workers> --corpora <dir> --out <dir>
                    [--self-commit <sha>] [--only name ...]

Corpora: `self`, `self-go` and `self-py` (this repository's Go and Python,
the harness's roots, read from the checkout that --workers is in, whose commit
--self-commit records), `self-ts` (services/frontend/src, not a harness
corpus), `miniflux` and `mealie` (their specs in scripts/rag_benchmarks/), and
TypeScript candidates defined below with the roots and excludes a spec would
carry. Each corpus writes census-<name>.json (the summary 22.2-RESEARCH.md
cites) and chunks-<name>.jsonl (one row per chunk, which records_analysis.py
joins with the 22-03 records). reproduce.py runs this and compares.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import tiktoken
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

MAX_TOKENS_PER_CHUNK = 8000  # EmbeddingGenerator(max_tokens_per_chunk=8000)
PRICE_PER_M = {"text-embedding-ada-002": 0.10, "text-embedding-3-small": 0.02}
SELF_COMMIT = "working tree"
SKIP_PARTS = {"venv", "node_modules", "__pycache__", ".git", "testdata", "vendor"}  # the harness's

TS_CANDIDATES = {
    "linkwarden": {
        "repository": "https://github.com/linkwarden/linkwarden",
        "commit": "952ac4540657cae3a67c3ca59433899d2fda8374",
        "roots": [("apps/web", [".ts", ".tsx"], "typescript"),
                  ("apps/worker", [".ts", ".tsx"], "typescript"),
                  ("packages", [".ts", ".tsx"], "typescript")],
        "exclude": [r"\.d\.ts$", r"\.(test|spec)\.tsx?$", r"(^|/)e2e/"],
    },
    "ghostfolio-api": {
        "dir": "ghostfolio",
        "repository": "https://github.com/ghostfolio/ghostfolio",
        "commit": "c13a0c18fb9c221adc4fed02d3ba32b274c8dc80",
        "roots": [("apps/api", [".ts"], "typescript")],
        "exclude": [r"\.d\.ts$", r"\.(test|spec)\.tsx?$"],
    },
    "seerr": {
        "repository": "https://github.com/seerr-team/seerr",
        "commit": "e9590629b8215676a352ec2413d7c8ee7e6974df",
        "roots": [("server", [".ts"], "typescript"), ("src", [".ts", ".tsx"], "typescript")],
        "exclude": [r"\.d\.ts$", r"\.(test|spec)\.tsx?$", r"(^|/)migration/"],
    },
}


def collect(root: Path, roots, excludes):
    """rag_quality_harness.collect_files, verbatim in behaviour."""
    pats = [re.compile(p) for p in excludes]
    out = []
    for rel, exts, lang in roots:
        base = root / rel
        if not base.exists():
            print(f"  [WARN] missing root {rel}", file=sys.stderr)
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in exts:
                continue
            relative = p.relative_to(root)
            path = relative.as_posix()
            if SKIP_PARTS & set(relative.parts) or any(x.search(path) for x in pats):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not content.strip():
                continue
            out.append((path, content, lang))
    return out


def corpus_files(name: str, workers: Path, corpora: Path):
    repo_root = workers.parents[1]
    if name == "self":
        return collect(repo_root, [("services/backend/pkg", [".go"], "go"),
                                   ("services/workers/workers", [".py"], "python")],
                       [r"(^|/)test_[^/]*$", r"_test\.go$"]), {"commit": SELF_COMMIT}
    if name == "self-go":
        return collect(repo_root, [("services/backend/pkg", [".go"], "go")], [r"_test\.go$"]), {"commit": SELF_COMMIT}
    if name == "self-py":
        return collect(repo_root, [("services/workers/workers", [".py"], "python")], [r"(^|/)test_[^/]*$"]), {
            "commit": SELF_COMMIT}
    if name == "self-ts":
        return collect(repo_root, [("services/frontend/src", [".ts", ".tsx"], "typescript")], []), {
            "commit": SELF_COMMIT}
    if name in ("miniflux", "mealie"):
        spec = json.loads((workers / "scripts" / "rag_benchmarks" / f"{name}.json").read_text(encoding="utf-8"))
        roots = [(r["path"], r["extensions"], r["language"]) for r in spec["roots"]]
        return collect(corpora / name, roots, spec.get("exclude", [])), {"commit": spec["commit"]}
    spec = TS_CANDIDATES[name]
    return collect(corpora / spec.get("dir", name), spec["roots"], spec["exclude"]), {
        "commit": spec["commit"], "repository": spec["repository"]}


def walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def error_counts(tree):
    errors = missing = 0
    for n in walk(tree.root_node):
        if n.type == "ERROR":
            errors += 1
        if n.is_missing:
            missing += 1
    return errors, missing


def pct(values, q):
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def embed_text(chunk):
    """EmbeddingGenerator._prepare_text_for_embedding, before truncation."""
    parts = []
    breadcrumb = chunk.metadata.get("breadcrumb", "")
    if breadcrumb:
        parts += [f"# {breadcrumb}", ""]
    doc = chunk.metadata.get("docstring", "")
    if doc:
        parts += [f'"""{doc}"""', ""]
    parts.append(chunk.content)
    return "\n".join(parts)


def line_set(start, end):
    return set(range(start, end + 1))


def census(name, files, meta, chunker, enc, grammars):
    r = {"corpus": name, **meta, "files": len(files)}
    by_lang = Counter(lang for _, _, lang in files)
    r["files_by_language"] = dict(by_lang)
    r["files_by_extension"] = dict(Counter(Path(p).suffix for p, _, _ in files))
    src_chars = sum(len(c) for _, c, _ in files)
    r["source_chars"] = src_chars
    r["source_lines"] = sum(len(c.splitlines()) for _, c, _ in files)

    chunks_all = []
    per_file = {}
    for path, content, lang in files:
        cs = chunker.chunk_file(path, content, lang)
        per_file[path] = (content, lang, cs)
        chunks_all.extend(cs)
    r["chunks"] = len(chunks_all)
    r["chunks_by_type"] = dict(Counter(c.chunk_type for c in chunks_all))
    chunk_chars = [len(c.content) for c in chunks_all]
    r["chunk_chars_total"] = sum(chunk_chars)
    r["index_text_over_source"] = round(sum(chunk_chars) / src_chars, 3) if src_chars else None
    code_types = ("function", "class", "fixed_size")
    code_chars = [len(c.content) for c in chunks_all if c.chunk_type in code_types]
    r["code_chunk_chars"] = {
        "p50": pct(code_chars, 0.5), "p90": pct(code_chars, 0.9), "p99": pct(code_chars, 0.99),
        "max": max(code_chars) if code_chars else 0,
        "over_2000": sum(1 for x in code_chars if x > 2000),
        "over_5000": sum(1 for x in code_chars if x > 5000),
        "over_8000": sum(1 for x in code_chars if x > 8000),
    }
    largest = sorted(chunks_all, key=lambda c: -len(c.content))[:5]
    r["largest_chunks"] = [{"file": c.file_path, "breadcrumb": c.metadata.get("breadcrumb"),
                            "type": c.chunk_type, "chars": len(c.content),
                            "lines": c.end_line - c.start_line + 1} for c in largest]

    # Tokens: what would be embedded, and billed. The generator embeds each distinct
    # raw-content hash once per run (its in-run cache), so billed = distinct hashes.
    tokens_all = []
    seen = {}
    truncated = []
    for c in chunks_all:
        t = len(enc.encode(embed_text(c)))
        tokens_all.append(t)
        h = hashlib.sha256(c.content.encode("utf-8")).hexdigest()
        if h not in seen:
            seen[h] = min(t, MAX_TOKENS_PER_CHUNK)
        if t > MAX_TOKENS_PER_CHUNK:
            truncated.append((c, t))
    billed = sum(seen.values())
    r["tokens"] = {
        "embedded_total_if_every_chunk": sum(min(t, MAX_TOKENS_PER_CHUNK) for t in tokens_all),
        "billed_distinct_content": billed,
        "cost_full_ingest_usd": {m: round(billed / 1e6 * p, 4) for m, p in PRICE_PER_M.items()},
        "p50": pct(tokens_all, 0.5), "p90": pct(tokens_all, 0.9), "max": max(tokens_all) if tokens_all else 0,
        "chunks_over_8000_tokens_truncated": len(truncated),
        "tokens_cut_by_truncation": sum(t - MAX_TOKENS_PER_CHUNK for _, t in truncated),
        "truncated": [{"file": c.file_path, "breadcrumb": c.metadata.get("breadcrumb"), "type": c.chunk_type,
                       "tokens": t, "chars": len(c.content)} for c, t in truncated],
    }
    hashes = Counter(hashlib.sha256(c.content.encode("utf-8")).hexdigest() for c in chunks_all)
    r["identical_content"] = {
        "distinct_hashes": len(hashes),
        "chunks_sharing_a_hash": sum(n for n in hashes.values() if n > 1),
        "extra_copies": sum(n - 1 for n in hashes.values() if n > 1),
    }

    # Duplication: class chunks covered by their own method chunks (ISS-026), nested
    # functions (a function chunk inside another function chunk), Go structs chunked
    # with their whole grouped `type ( ... )` declaration.
    dup = {"class_chunks": 0, "class_chunks_ge80pct_covered": 0, "class_chars": 0,
           "class_chars_covered_by_methods": 0, "nested_function_chunks": 0,
           "nested_function_chars": 0, "go_struct_chunks_in_groups": 0, "go_group_extra_chars": 0}
    covered_lines_total = 0
    code_lines_total = 0
    fallback_files = 0
    fixed_chunks = 0
    for path, (content, lang, cs) in per_file.items():
        lines = content.splitlines()
        nonblank = {i + 1 for i, ln in enumerate(lines) if ln.strip()}
        code_lines_total += len(nonblank)
        covered = set()
        for c in cs:
            if c.chunk_type in code_types:
                covered |= line_set(c.start_line, c.end_line)
        covered_lines_total += len(covered & nonblank)
        if cs and all(c.chunk_type == "fixed_size" for c in cs):
            fallback_files += 1
        fixed_chunks += sum(1 for c in cs if c.chunk_type == "fixed_size")
        funcs = [c for c in cs if c.chunk_type == "function"]
        for cl in (c for c in cs if c.chunk_type == "class"):
            dup["class_chunks"] += 1
            cl_lines = line_set(cl.start_line, cl.end_line)
            inner = set()
            for f in funcs:
                if cl.start_line <= f.start_line and f.end_line <= cl.end_line:
                    inner |= line_set(f.start_line, f.end_line)
            cl_nonblank = cl_lines & nonblank
            cov = len(inner & cl_nonblank) / len(cl_nonblank) if cl_nonblank else 0
            covered_chars = sum(len(lines[i - 1]) + 1 for i in (inner & cl_lines) if i - 1 < len(lines))
            dup["class_chars"] += len(cl.content)
            dup["class_chars_covered_by_methods"] += covered_chars
            if cov >= 0.8:
                dup["class_chunks_ge80pct_covered"] += 1
        for f in funcs:
            if any(g is not f and g.start_line <= f.start_line and f.end_line <= g.end_line
                   and (g.start_line, g.end_line) != (f.start_line, f.end_line) for g in funcs):
                dup["nested_function_chunks"] += 1
                dup["nested_function_chars"] += len(f.content)
    r["duplication"] = dup
    r["coverage_nonblank_lines_in_code_chunks"] = round(covered_lines_total / code_lines_total, 3) if code_lines_total else None
    r["fallback_files_fixed_size_only"] = fallback_files
    r["fixed_size_chunks"] = fixed_chunks

    # Parse errors, by the grammar the chunker uses and (for TypeScript) by the real one.
    perr = defaultdict(lambda: {"files": 0, "files_with_errors": 0, "error_nodes": 0, "missing_nodes": 0})
    ts_real = defaultdict(lambda: {"files": 0, "files_with_errors": 0, "error_nodes": 0, "missing_nodes": 0})
    decl = Counter()
    go_types = Counter()
    go_type_chunked = Counter()
    py = Counter()
    ts = Counter()
    for path, (content, lang, cs) in per_file.items():
        data = content.encode("utf-8")
        tree = chunker.parser.parse_file(content, lang) if lang in chunker.parser.parsers else None
        if tree is not None:
            e, m = error_counts(tree)
            bucket = perr[f"{lang}{Path(path).suffix if lang == 'typescript' else ''}"]
            bucket["files"] += 1
            bucket["files_with_errors"] += 1 if tree.root_node.has_error else 0
            bucket["error_nodes"] += e
            bucket["missing_nodes"] += m
        chunk_breadcrumbs = {(c.chunk_type, c.metadata.get("breadcrumb")) for c in cs}
        if lang == "go":
            struct_group_sizes = []
            for n in walk(tree.root_node):
                if n.type == "type_declaration":
                    specs = [ch for ch in n.named_children if ch.type in ("type_spec", "type_alias")]
                    nested = any(a.type in ("function_declaration", "method_declaration", "func_literal")
                                 for a in ancestors(n))
                    for sp in specs:
                        kind = "alias" if sp.type == "type_alias" else (sp.child_by_field_name("type").type
                                                                         if sp.child_by_field_name("type") else "?")
                        scope = "local" if nested else "package"
                        go_types[f"{scope}:{kind}"] += 1
                        if kind == "struct_type":
                            go_type_chunked[scope] += 1
                            if len(specs) > 1:
                                dup["go_struct_chunks_in_groups"] += 1
                                own = sp.end_byte - sp.start_byte
                                dup["go_group_extra_chars"] += (n.end_byte - n.start_byte) - own
                if n.type in ("const_declaration", "var_declaration") and n.parent and n.parent.type == "source_file":
                    specs = [ch for ch in n.named_children if ch.type in ("const_spec", "var_spec")]
                    decl[f"go_package_{n.type.split('_')[0]}_specs"] += len(specs)
                if n.type in ("function_declaration", "method_declaration"):
                    decl["go_callables"] += 1
                    prev = n.prev_named_sibling
                    if prev is not None and prev.type == "comment" and prev.end_point[0] == n.start_point[0] - 1:
                        decl["go_callables_with_doc_comment_above_span"] += 1
        elif lang == "python":
            for n in walk(tree.root_node):
                if n.type == "decorated_definition":
                    inner = n.child_by_field_name("definition")
                    decos = [d for d in n.named_children if d.type == "decorator"]
                    text = " ".join(data[d.start_byte:d.end_byte].decode("utf-8", "replace") for d in decos)
                    kind = inner.type if inner is not None else "?"
                    py[f"decorated_{kind}"] += 1
                    is_route = bool(re.search(
                        r"@\w*(router|app|api)\w*\.(get|post|put|delete|patch|head|options|api_route)\b", text))
                    if re.search(r"\boverload\b", text):
                        py["overload_stubs"] += 1
                    if is_route:
                        py["route_decorated"] += 1
                    # Is the decorator text inside ANY chunk's line range today? The
                    # function's own chunk starts at `def`, so only an enclosing
                    # class chunk (ISS-026's duplicate) or a fixed-size chunk holds it.
                    deco_line = n.start_point[0] + 1
                    holders = [c for c in cs if c.chunk_type in ("class", "fixed_size", "function")
                               and c.start_line <= deco_line <= c.end_line]
                    in_class = any(c.chunk_type == "class" for c in holders)
                    in_any = bool(holders)
                    key = "route_decorators" if is_route else "decorators"
                    py[f"{key}_in_no_chunk"] += 0 if in_any else 1
                    py[f"{key}_only_inside_a_class_chunk"] += 1 if (in_class and not any(
                        c.chunk_type != "class" for c in holders)) else 0
                if n.type == "function_definition":
                    if any(a.type == "function_definition" for a in ancestors(n)):
                        py["nested_functions"] += 1
            # (file, breadcrumb, chunk_type) collisions among code chunks
            keys = Counter((c.chunk_type, c.metadata.get("breadcrumb")) for c in cs if c.chunk_type in ("function", "class"))
            py["breadcrumb_collisions_extra_rows"] += sum(v - 1 for v in keys.values() if v > 1)
        elif lang == "typescript":
            suffix = Path(path).suffix
            real = grammars["tsx"] if suffix == ".tsx" else grammars["ts"]
            rtree = real.parse(data)
            e, m = error_counts(rtree)
            b = ts_real[f"typescript{suffix}"]
            b["files"] += 1
            b["files_with_errors"] += 1 if rtree.root_node.has_error else 0
            b["error_nodes"] += e
            b["missing_nodes"] += m
            for n in walk(rtree.root_node):
                t = n.type
                if t in ("interface_declaration", "type_alias_declaration", "enum_declaration",
                         "abstract_class_declaration", "class_declaration", "function_declaration",
                         "generator_function_declaration", "internal_module", "function_signature",
                         "method_definition", "abstract_method_signature"):
                    ts[f"real_{t}"] += 1
                if t == "lexical_declaration" and n.parent is not None and n.parent.type in ("program", "export_statement"):
                    for d in n.named_children:
                        if d.type == "variable_declarator":
                            v = d.child_by_field_name("value")
                            if v is not None and v.type in ("arrow_function", "function_expression", "function"):
                                ts["real_top_level_const_function"] += 1
                                # Where it sits among today's chunks: a 50-line
                                # window (its file fell back), a function or class
                                # chunk, or no chunk at all.
                                line = n.start_point[0] + 1
                                holders = [c for c in cs if c.start_line <= line <= c.end_line
                                           and c.chunk_type in ("function", "class", "fixed_size")]
                                if any(c.chunk_type == "fixed_size" for c in holders):
                                    ts["real_top_level_const_function_in_a_fixed_size_window_today"] += 1
                                elif not holders:
                                    ts["real_top_level_const_function_in_no_chunk_today"] += 1
                                nm = d.child_by_field_name("name")
                                nm_text = data[nm.start_byte:nm.end_byte].decode("utf-8", "replace") if nm else ""
                                if nm_text[:1].isupper():
                                    ts["real_top_level_const_function_capitalised"] += 1
                if t in ("function_declaration", "method_definition", "class_declaration",
                         "abstract_class_declaration") or (
                        t == "lexical_declaration" and n.parent is not None
                        and n.parent.type in ("program", "export_statement")):
                    anchor = n.parent if n.parent is not None and n.parent.type == "export_statement" else n
                    prev = anchor.prev_named_sibling
                    if prev is not None and prev.type == "comment":
                        txt = data[prev.start_byte:prev.end_byte].decode("utf-8", "replace")
                        if txt.startswith("/**"):
                            ts["real_declarations_with_jsdoc_above"] += 1
                if t == "decorator":
                    ts["real_decorators"] += 1
            # What a chunker on the real grammar would have to chunk for parity with
            # Python and Go (named functions, methods, classes), counted, not built.
            chunkable = 0
            for n in walk(rtree.root_node):
                if n.type in ("function_declaration", "generator_function_declaration", "class_declaration",
                              "abstract_class_declaration"):
                    chunkable += 1
                elif n.type == "method_definition" and n.parent is not None and n.parent.type == "class_body":
                    chunkable += 1
                elif n.type == "lexical_declaration" and n.parent is not None and n.parent.type in (
                        "program", "export_statement"):
                    for d in n.named_children:
                        v = d.child_by_field_name("value") if d.type == "variable_declarator" else None
                        if v is not None and v.type in ("arrow_function", "function_expression", "function"):
                            chunkable += 1
            ts["sim_chunkable_declarations"] += chunkable
            ts["sim_files_with_no_chunkable_declaration"] += 0 if chunkable else 1
            # What the chunker actually made of it
            ts["chunked_function_chunks"] += sum(1 for c in cs if c.chunk_type == "function")
            ts["chunked_class_chunks"] += sum(1 for c in cs if c.chunk_type == "class")
            ts["chunked_fixed_size_chunks"] += sum(1 for c in cs if c.chunk_type == "fixed_size")
            ts["chunked_with_docstring"] += sum(1 for c in cs if c.metadata.get("docstring"))
    r["parse_errors_as_chunked"] = {k: dict(v) for k, v in perr.items()}
    if ts_real:
        r["parse_errors_with_typescript_grammar"] = {k: dict(v) for k, v in ts_real.items()}
    if go_types:
        r["go_type_declarations"] = dict(sorted(go_types.items()))
        total = sum(v for k, v in go_types.items() if k.startswith("package:"))
        chunked = go_type_chunked["package"]
        r["go_package_types_total"] = total
        r["go_package_types_without_chunk"] = total - chunked
        r["go_local_types_total"] = sum(v for k, v in go_types.items() if k.startswith("local:"))
        r["go_local_structs_chunked_twice"] = go_type_chunked["local"]
    if py:
        r["python"] = dict(py)
    if ts:
        r["typescript"] = dict(ts)
    if decl:
        r["declarations"] = dict(decl)
    return r, per_file


def ancestors(n):
    p = n.parent
    while p is not None:
        yield p
        p = p.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=Path, required=True)
    ap.add_argument("--corpora", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--self-commit", default="working tree")
    a = ap.parse_args()
    global SELF_COMMIT
    SELF_COMMIT = a.self_commit
    sys.path.insert(0, str(a.workers))
    import logging
    logging.disable(logging.WARNING)  # the chunker logs every fixed-size fallback
    from workers.chunker.semantic_chunker import SemanticChunker

    chunker = SemanticChunker()
    enc = tiktoken.get_encoding("cl100k_base")
    grammars = {"ts": Parser(Language(tree_sitter_typescript.language_typescript())),
                "tsx": Parser(Language(tree_sitter_typescript.language_tsx()))}
    names = a.only or ["self", "self-ts", "miniflux", "mealie", "linkwarden", "ghostfolio-api", "seerr"]
    a.out.mkdir(parents=True, exist_ok=True)
    versions = {}
    for mod in (tree_sitter_go, tree_sitter_javascript, tree_sitter_python, tree_sitter_typescript, tiktoken):
        try:
            from importlib.metadata import version
            versions[mod.__name__] = version(mod.__name__.replace("_", "-"))
        except Exception:  # noqa: BLE001
            versions[mod.__name__] = "?"
    from importlib.metadata import version
    versions["tree-sitter"] = version("tree-sitter")
    for name in names:
        files, meta = corpus_files(name, a.workers, a.corpora)
        r, per_file = census(name, files, meta, chunker, enc, grammars)
        r["versions"] = versions
        (a.out / f"census-{name}.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
        # One row per chunk, for joining with recorded retrieval traces by
        # (file_path, breadcrumb, chunk_type): spans, sizes and ISS-026 coverage.
        with (a.out / f"chunks-{name}.jsonl").open("w", encoding="utf-8") as fh:
            for path, (content, lang, cs) in per_file.items():
                lines = content.splitlines()
                nonblank = {i + 1 for i, ln in enumerate(lines) if ln.strip()}
                funcs = [c for c in cs if c.chunk_type == "function"]
                for c in cs:
                    cov = None
                    if c.chunk_type == "class":
                        inner = set()
                        for f in funcs:
                            if c.start_line <= f.start_line and f.end_line <= c.end_line:
                                inner |= line_set(f.start_line, f.end_line)
                        nb = line_set(c.start_line, c.end_line) & nonblank
                        cov = round(len(inner & nb) / len(nb), 3) if nb else 0
                    fh.write(json.dumps({
                        "file_path": c.file_path, "breadcrumb": c.metadata.get("breadcrumb"),
                        "chunk_type": c.chunk_type, "start_line": c.start_line, "end_line": c.end_line,
                        "chars": len(c.content), "tokens": len(enc.encode(embed_text(c))),
                        "class_method_coverage": cov}) + "\n")
        print(f"{name}: {r['files']} files, {r['chunks']} chunks, index/source {r['index_text_over_source']}, "
              f"truncated {r['tokens']['chunks_over_8000_tokens_truncated']}, billed tokens {r['tokens']['billed_distinct_content']}")


if __name__ == "__main__":
    main()
