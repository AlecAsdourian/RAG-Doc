#!/usr/bin/env python3
"""The chunk census: what the chunker makes of each corpus, offline.

Ported from `.planning/phases/22.2-retrieval-quality/22.2-records/chunk_census.py`
(which stays where it is, as the record the research cites) in 22.2-01, and
held to reproducing those records field by field (`22.2-01-records/
census-reproduction.txt`). No database and no OpenAI: token counts use
tiktoken's local `cl100k_base`, the encoding both embedding models use.

WHAT IS NOT A COPY ANY MORE.
- **File selection is the harness's:** `rag_quality_harness.load_corpus` and
  `collect_files`. A corpus with a spec in `rag_benchmarks/` is always read
  from its spec. Only corpora with no spec are defined here (`CENSUS_ONLY`), and
  even they are collected by the harness's `collect_files`. When 22.2-03
  commits `linkwarden.json`, the spec wins.
- **`self`** is the harness's own `load_corpus("self", ...)`, read from
  `--self-root`. `self-go` and `self-py` are that corpus's files split by
  language, so a file can never be in one and not in `self`. `self-ts`
  (`services/frontend/src`) is not a harness corpus and is census-only.
- **The grammar "as chunked"** is the chunker's own parser
  (`chunker.parser.parse_file`), never a grammar named here.
- **The digest and the chunker version** are `chunk_digest.py`'s, which the
  harness imports too.

WHAT IS STILL A COPY. `embed_text` is the generator's
`_prepare_text_for_embedding` before truncation: the generator has no function
that returns the text before it truncates. A test pins the copy to the
generator (`tests/test_chunk_census.py`), and 22.2-02 replaces it when it makes
one function of that rule.

THE PINS. The census measures with exactly the parsers and tokenizer
`requirements.txt` pins, and refuses to run (exit 2) on any other installed
version, naming it: a grammar release can change node types, and the chunker's
queries name node types, so two censuses made by different parsers do not
compare.

USAGE
    chunk_census.py --corpora <dir> --out <dir> [--self-root <dir>]
                    [--self-commit <sha>] [--only name ...]

Each corpus writes census-<name>.json (the summary) and chunks-<name>.jsonl
(one row per chunk, which `22.2-records/records_analysis.py` joins with the
22-03 records).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional
from uuid import UUID, uuid5

HERE = Path(__file__).resolve().parent              # scripts/rag_benchmarks
SCRIPTS = HERE.parent                               # scripts
WORKERS = SCRIPTS.parent                            # services/workers
REQUIREMENTS = WORKERS / "requirements.txt"
for _p in (str(HERE), str(SCRIPTS), str(WORKERS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import chunk_digest  # noqa: E402

MAX_TOKENS_PER_CHUNK = 8000  # EmbeddingGenerator(max_tokens_per_chunk=8000)
PRICE_PER_M = {"text-embedding-ada-002": 0.10, "text-embedding-3-small": 0.02}
# The packages whose versions the census measures with, pinned in requirements.txt.
PINNED_PACKAGES = chunk_digest.GRAMMAR_PACKAGES + ("tiktoken",)
CENSUS_NAMESPACE = UUID("0ca11117-0000-4000-8000-00000000c3a5")

# Corpora with no spec in rag_benchmarks/: their roots and excludes as a spec
# would carry them. linkwarden's are QD4's candidate until 22.2-03 commits
# `linkwarden.json`, which then wins.
CENSUS_ONLY = {
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
# Read from --self-root like `self`, but not a harness corpus.
SELF_TS_ROOTS = [("services/frontend/src", [".ts", ".tsx"], "typescript")]
SELF_SPLIT = {"self-go": "go", "self-py": "python"}
DEFAULT_CORPORA = ["self", "self-go", "self-py", "self-ts", "miniflux", "mealie",
                   "linkwarden", "ghostfolio-api", "seerr"]

# The chunker's own warnings name why a file fell back to fixed-size windows
# (semantic_chunker.py). The cause is read from them, not re-derived.
FALLBACK_CAUSES = (
    ("raised", "Semantic parsing failed for "),
    ("no_chunk", "Semantic parsing produced no chunks for "),
    ("unsupported", "Unsupported language "),
)
CHUNKER_LOGGER = "workers.chunker.semantic_chunker"

_harness = None


def harness():
    """rag_quality_harness, imported once: its file selection is the census's."""
    global _harness
    if _harness is None:
        import rag_quality_harness
        _harness = rag_quality_harness
    return _harness


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def required_pins(path: Path = REQUIREMENTS) -> Dict[str, str]:
    """The `name==version` pins requirements.txt states for PINNED_PACKAGES."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*==\s*([^\s#;]+)", line)
        if m:
            pins[re.sub(r"\[.*\]", "", m.group(1)).lower()] = m.group(2)
    return {name: pins.get(name) for name in PINNED_PACKAGES}


def pin_mismatches(path: Path = REQUIREMENTS) -> List[str]:
    problems = []
    for name, pinned in required_pins(path).items():
        if pinned is None:
            problems.append(f"{name} is not pinned with == in {path.name}")
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed != pinned:
            problems.append(f"{name} {installed or 'not installed'} is installed; {path.name} pins {pinned}")
    return problems


def versions() -> Dict[str, str]:
    """The installed versions, keyed as the research records key them."""
    out = {}
    for module in ("tree_sitter_go", "tree_sitter_javascript", "tree_sitter_python",
                   "tree_sitter_typescript", "tiktoken"):
        try:
            out[module] = metadata.version(module.replace("_", "-"))
        except metadata.PackageNotFoundError:
            out[module] = "?"
    out["tree-sitter"] = metadata.version("tree-sitter")
    return out


# ---------------------------------------------------------------------------
# Corpora: the harness's selection
# ---------------------------------------------------------------------------


def _census_only_corpus(name: str, root: Path, roots, exclude, commit: str, repository: str):
    h = harness()
    return h.Corpus(name=name, root=root, roots=list(roots), exclude=list(exclude),
                    repository_id=uuid5(CENSUS_NAMESPACE, name), repository_url=repository,
                    commit=commit, exact_paths=True)


def corpus_files(name: str, corpora: Path, self_root: Path, self_commit: Optional[str]):
    """(files, meta) for one census corpus, every file list from the harness."""
    h = harness()
    if name in ("self", *SELF_SPLIT):
        corpus = h.load_corpus("self", corpora, self_root=self_root, self_commit=self_commit)
        if not corpus.commit:
            sys.exit(f"{self_root} is not a git checkout's top level, so its commit is unknown; "
                     "pass --self-commit")
        files = h.collect_files(corpus)
        if name in SELF_SPLIT:
            files = [f for f in files if f[2] == SELF_SPLIT[name]]
        return files, {"commit": corpus.commit, "corpus_dirty": corpus.tree_dirty}
    if name == "self-ts":
        commit = h.checked_commit(self_root, self_commit, "--self-commit")
        if not commit:
            sys.exit(f"{self_root} is not a git checkout's top level, so its commit is unknown; "
                     "pass --self-commit")
        corpus = _census_only_corpus(name, self_root, SELF_TS_ROOTS, [], commit,
                                     "https://github.com/AlecAsdourian/RAG-Doc")
        dirty = h.tree_dirty(self_root, [rel for rel, _, _ in SELF_TS_ROOTS])
        return h.collect_files(corpus), {"commit": commit, "corpus_dirty": dirty}
    if (h.BENCHMARKS_DIR / f"{name}.json").exists():
        corpus = h.load_corpus(name, corpora)
        h.require_fetched(corpus)
        return h.collect_files(corpus), {"commit": corpus.commit}
    if name not in CENSUS_ONLY:
        sys.exit(f"no corpus named {name!r}: neither a spec in {h.BENCHMARKS_DIR} nor a census-only corpus")
    spec = CENSUS_ONLY[name]
    corpus = _census_only_corpus(name, corpora / spec.get("dir", name), spec["roots"], spec["exclude"],
                                 spec["commit"], spec["repository"])
    h.require_fetched(corpus)
    return h.collect_files(corpus), {"commit": spec["commit"], "repository": spec["repository"]}


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


def walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def ancestors(n):
    p = n.parent
    while p is not None:
        yield p
        p = p.parent


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
    """EmbeddingGenerator._prepare_text_for_embedding, before truncation.

    A COPY, pinned to the generator by `test_embed_text_is_the_generators_rule`
    until 22.2-02 makes one function of the rule.
    """
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


class FallbackCapture(logging.Handler):
    """Collects the chunker's warnings, so each fallback's cause is the chunker's own."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def capturing():
    """The chunker's warnings routed to a fresh capture for the block, and the
    logger's level, propagation and handlers restored after it: a census run
    inside a larger process (a test session) leaves its logging as it found it."""
    chunker_logger = logging.getLogger(CHUNKER_LOGGER)
    saved = (chunker_logger.level, chunker_logger.propagate)
    capture = FallbackCapture()
    chunker_logger.setLevel(logging.WARNING)
    chunker_logger.propagate = False
    chunker_logger.addHandler(capture)
    try:
        yield capture
    finally:
        chunker_logger.removeHandler(capture)
        chunker_logger.level, chunker_logger.propagate = saved


def attach_capture() -> FallbackCapture:
    """For a process that only runs the census (its CLI, a record script): route
    the chunker's warnings to a capture for good, and keep the rest of the
    workers quiet. Changes process-wide logging; inside a larger process,
    `census()` with no capture uses `capturing()` instead."""
    logging.getLogger("workers").setLevel(logging.ERROR)
    chunker_logger = logging.getLogger(CHUNKER_LOGGER)
    chunker_logger.setLevel(logging.WARNING)
    chunker_logger.propagate = False
    for old in [h for h in chunker_logger.handlers if isinstance(h, FallbackCapture)]:
        chunker_logger.removeHandler(old)
    capture = FallbackCapture()
    chunker_logger.addHandler(capture)
    return capture


def fallback_cause(messages: List[str]) -> Optional[str]:
    for message in messages:
        for cause, prefix in FALLBACK_CAUSES:
            if message.startswith(prefix):
                return cause
    return None


def chunk_files(files, chunker, capture: FallbackCapture):
    """Chunk every file; return {path: (content, lang, chunks)} and {path: fallback cause}."""
    per_file = {}
    causes = {}
    for path, content, lang in files:
        capture.messages.clear()
        cs = chunker.chunk_file(path, content, lang)
        per_file[path] = (content, lang, cs)
        cause = fallback_cause(capture.messages)
        if cause is None and cs and all(c.chunk_type == "fixed_size" for c in cs):
            cause = "unattributed"  # fell back without a warning this census knows
        if cause is not None:
            causes[path] = cause
    return per_file, causes


def _parity_ts(rtree, data):
    """What a chunker on the real grammar would chunk for parity with Python and
    Go (named functions, methods, classes), and which of those carry a `/** */`
    block directly above their outermost node. Counted, not built."""
    chunkable = 0
    with_jsdoc = 0
    for n in walk(rtree.root_node):
        hits = 0
        outer = None
        if n.type in ("function_declaration", "generator_function_declaration", "class_declaration",
                      "abstract_class_declaration"):
            hits = 1
            outer = n.parent if n.parent is not None and n.parent.type == "export_statement" else n
        elif n.type == "method_definition" and n.parent is not None and n.parent.type == "class_body":
            hits = 1
            outer = n
            prev = n.prev_named_sibling
            while prev is not None and prev.type == "decorator":
                outer = prev  # a decorated method's outermost node is its first decorator
                prev = prev.prev_named_sibling
        elif n.type == "lexical_declaration" and n.parent is not None and n.parent.type in (
                "program", "export_statement"):
            for d in n.named_children:
                v = d.child_by_field_name("value") if d.type == "variable_declarator" else None
                if v is not None and v.type in ("arrow_function", "function_expression", "function"):
                    hits += 1
            outer = n.parent if n.parent.type == "export_statement" else n
        if not hits:
            continue
        chunkable += hits
        prev = outer.prev_named_sibling
        if (prev is not None and prev.type == "comment"
                and data[prev.start_byte:prev.end_byte].startswith(b"/**")
                and prev.end_point[0] == outer.start_point[0] - 1):
            with_jsdoc += hits
    return chunkable, with_jsdoc


def _parity_python(tree):
    return sum(1 for n in walk(tree.root_node) if n.type in ("function_definition", "class_definition"))


def _parity_go(tree):
    count = 0
    for n in walk(tree.root_node):
        if n.type in ("function_declaration", "method_declaration"):
            count += 1
        elif n.type == "type_spec":
            t = n.child_by_field_name("type")
            if t is not None and t.type == "struct_type":
                count += 1
    return count


def census(name, files, meta, chunker, enc, grammars, capture: Optional[FallbackCapture] = None):
    """The census of one corpus: (summary, {path: (content, lang, chunks)}).

    `capture` is an attached FallbackCapture (`attach_capture()`); with none,
    one is attached for this call only and detached after it.
    """
    if capture is None:
        with capturing() as scoped:
            return census(name, files, meta, chunker, enc, grammars, scoped)
    r = {"corpus": name, **meta, "files": len(files)}
    by_lang = Counter(lang for _, _, lang in files)
    r["files_by_language"] = dict(by_lang)
    r["files_by_extension"] = dict(Counter(Path(p).suffix for p, _, _ in files))
    src_chars = sum(len(c) for _, c, _ in files)
    r["source_chars"] = src_chars
    r["source_lines"] = sum(len(c.splitlines()) for _, c, _ in files)

    per_file, causes = chunk_files(files, chunker, capture)
    chunks_all = [c for _, _, cs in per_file.values() for c in cs]
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

    # Why each of those files fell back, from the chunker's own warnings, per language.
    reasons: Dict[str, Dict[str, int]] = {}
    for path, (_, lang, _) in per_file.items():
        bucket = reasons.setdefault(lang, {"raised": 0, "no_chunk": 0, "unsupported": 0})
        cause = causes.get(path)
        if cause is not None:
            bucket[cause] = bucket.get(cause, 0) + 1
    r["fallback_reasons"] = reasons

    # Named chunks per extension, and files the parity simulation finds a chunkable
    # declaration in while the chunker names none. A query that compiles but matches
    # nothing shows up only here: as a fallback it is just "no chunk".
    named = defaultdict(lambda: {"function": 0, "class": 0})
    for path, (_, _, cs) in per_file.items():
        ext = Path(path).suffix
        named[ext]  # every extension gets a row, a zero row included
        for c in cs:
            if c.chunk_type in ("function", "class"):
                named[ext][c.chunk_type] += 1
    r["named_chunks_by_extension"] = {k: dict(v) for k, v in sorted(named.items())}
    silent: Dict[str, List[str]] = {}

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
        tree = chunker.parser.parse_file(content, lang, path=path) if lang in chunker.parser.parsers else None
        if tree is not None:
            e, m = error_counts(tree)
            bucket = perr[f"{lang}{Path(path).suffix if lang == 'typescript' else ''}"]
            bucket["files"] += 1
            bucket["files_with_errors"] += 1 if tree.root_node.has_error else 0
            bucket["error_nodes"] += e
            bucket["missing_nodes"] += m
        named_here = any(c.chunk_type in ("function", "class") for c in cs)
        if lang == "go":
            if _parity_go(tree) and not named_here:
                silent.setdefault(lang, []).append(path)
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
            if _parity_python(tree) and not named_here:
                silent.setdefault(lang, []).append(path)
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
                # The upper bound the research cites: any kind of declaration, constants
                # included, with any `/**` comment as its previous sibling, adjacent or not.
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
            chunkable, with_jsdoc = _parity_ts(rtree, data)
            ts["sim_chunkable_declarations"] += chunkable
            ts["sim_files_with_no_chunkable_declaration"] += 0 if chunkable else 1
            # The definition 22.2-02's chunker uses, and QA4 compares with
            # chunked_with_docstring: the `/** */` block ends on the line before the
            # declaration's outermost node (its export, or a method's first decorator).
            ts["sim_chunkable_with_jsdoc_directly_above"] += with_jsdoc
            if chunkable and not named_here:
                silent.setdefault(lang, []).append(path)
            # What the chunker actually made of it
            ts["chunked_function_chunks"] += sum(1 for c in cs if c.chunk_type == "function")
            ts["chunked_class_chunks"] += sum(1 for c in cs if c.chunk_type == "class")
            ts["chunked_fixed_size_chunks"] += sum(1 for c in cs if c.chunk_type == "fixed_size")
            ts["chunked_with_docstring"] += sum(1 for c in cs if c.metadata.get("docstring"))
    r["files_with_declarations_but_no_named_chunk"] = {
        "count": sum(len(v) for v in silent.values()),
        "by_language": {k: len(v) for k, v in sorted(silent.items())},
        "files": sorted(p for v in silent.values() for p in v),
    }
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

    # What a record measured: the digest of these rows, and the code that chunked them.
    r["chunk_set_digest"], r["chunk_rows"] = chunk_digest.digest_of_chunks(chunks_all)
    r["corpus_tree_digest"] = chunk_digest.tree_digest(files)
    r["chunker_version"] = chunk_digest.chunker_version()
    return r, per_file


def make_tools():
    """(chunker, tokenizer, real TypeScript grammars), as the census uses them."""
    import tiktoken
    import tree_sitter_typescript
    from tree_sitter import Language, Parser
    from workers.chunker.semantic_chunker import SemanticChunker

    grammars = {"ts": Parser(Language(tree_sitter_typescript.language_typescript())),
                "tsx": Parser(Language(tree_sitter_typescript.language_tsx()))}
    return SemanticChunker(), tiktoken.get_encoding("cl100k_base"), grammars


def write_chunk_rows(out: Path, name: str, per_file, enc) -> None:
    """One row per chunk, for joining with recorded retrieval traces by
    (file_path, breadcrumb, chunk_type): spans, sizes and ISS-026 coverage."""
    with (out / f"chunks-{name}.jsonl").open("w", encoding="utf-8") as fh:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpora", type=Path, required=True,
                    help="where the benchmark and census-only corpora are fetched, each at its pin")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--self-root", type=Path, default=None,
                    help="the tree `self`, self-go, self-py and self-ts are read from (default: this checkout)")
    ap.add_argument("--self-commit", default=None,
                    help="the commit --self-root holds, for an export with no .git")
    a = ap.parse_args(argv)

    problems = pin_mismatches()
    if problems:
        print("REFUSED: the census measures only with the versions requirements.txt pins:")
        for p in problems:
            print(f"  - {p}")
        return 2

    self_root = a.self_root or harness().REPO_ROOT
    capture = attach_capture()
    chunker, enc, grammars = make_tools()
    a.out.mkdir(parents=True, exist_ok=True)
    found_versions = versions()
    for name in a.only or DEFAULT_CORPORA:
        files, meta = corpus_files(name, a.corpora, self_root, a.self_commit)
        r, per_file = census(name, files, meta, chunker, enc, grammars, capture)
        r["versions"] = found_versions
        (a.out / f"census-{name}.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
        write_chunk_rows(a.out, name, per_file, enc)
        print(f"{name}: {r['files']} files, {r['chunks']} chunks, digest {r['chunk_set_digest'][:16]}, "
              f"chunker {r['chunker_version']}, index/source {r['index_text_over_source']}, "
              f"truncated {r['tokens']['chunks_over_8000_tokens_truncated']}, "
              f"billed tokens {r['tokens']['billed_distinct_content']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
