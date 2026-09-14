#!/usr/bin/env python3
"""Does retrieval actually work? Ingest a corpus, then measure.

WHY THIS EXISTS. Three of nine phases are done and the core experience had
never been measured end to end. `test_ingestion.py` ingests three hardcoded
files and prints that it worked; that is a smoke test, not evidence. This
ingests a real corpus and scores retrieval against questions whose answers were
established by reading the code.

CORPORA.

    self     this repository's Go backend and Python workers, with the 40
             questions below. Small (62 files), heavily commented, and
             circular: the questions were written by someone who had just read
             the code, and many ask about the search system itself. Good for
             catching bugs; poor for judging quality.
    <name>   an open-source application nobody on this project wrote, pinned
             to a commit and described by `rag_benchmarks/<name>.json`. Its
             questions were written and verified against the code before any
             retrieval result for that corpus was seen.

WHAT IT MEASURES. For each question, where the answer lands in the results, at
two levels:

    file     the first result in the file that holds the answer
    symbol   the first result that IS the answering function, method or type
             (only for questions that name a symbol)

File level flatters -- the right file can be the wrong function -- so read the
two together. Each level is reported three ways:

    recall@k  the answer appears anywhere in the top k
    rank-1    the answer is the very first result
    MRR       mean reciprocal rank -- 1/rank averaged, 0 when missed

MRR is the number to tune against: moving an answer from rank 5 to rank 2
counts, which rank-1 and recall@k both ignore.

QUESTION SETS, AND WHY. Tuning weights against the same questions you score
with fits the weights to the questions, not to retrieval. So every corpus splits
its questions:

    tuning   look at these while changing ranking
    holdout  check these only after a change is made, never while making it
    confirm  written after a candidate change was chosen, to decide it on
             questions nobody had looked at: read once, under a rule fixed
             before they were written (rag_benchmarks/*-protocol.md)

A change that lifts `tuning` and not `holdout` has been overfitted. `self`'s
holdout set has been consulted for many configurations and is no longer blind
(ISS-029); decide on a benchmark corpus's holdout set instead.

READ SMALL DIFFERENCES WITH CARE. When no query fails, the measurement is
deterministic -- re-running a configuration reproduces every rank exactly, one
run at a time or several at once -- so a difference between two configurations
is a real ranking change. But on 15 questions one answer moving from #1 to #2
shifts MRR by 0.033, so a one- or two-question gap is weak evidence that a
change generalises.

A FAILED QUERY IS AN ERROR, NOT A MISS. QueryEngine does not raise when keyword
or vector search fails: it records the error in the response metadata and ranks
whatever the other retriever returned, often nothing. The harness reports any
such query as an error, prints MEASUREMENT INVALID and exits 2, so a broken run
cannot pass as a score (ISS-030).

KNOWN SOFTNESS in `self`'s tuning set, left in deliberately. An expectation
matches any file whose path contains it, and as of 2026-09-13 five tuning
questions accept more than one file: `pkg/auth/` (11 files), `workers/chunker/`
(6), `workers/embeddings/` (3), and both `handlers/github_webhook` questions,
which also match `github_webhook_events.go`. That flatters recall. They are
unchanged so the recorded baseline stays comparable. Every `self` holdout
expectation matches exactly one file, and benchmark corpora match paths exactly.

USAGE
    cd services/workers

    # this repository
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --clear --ingest
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --measure --set tuning

    # a benchmark corpus: fetch it at its pinned commit, check the questions
    # offline, index it, then measure
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --corpus miniflux --fetch --check
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --corpus miniflux --ingest
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --corpus miniflux --measure --set holdout \\
        --json-out miniflux-holdout.json

Ranking knobs usable here: the BOOST_* environment variables QueryEngine reads,
and --boost-config, which reaches every MetadataBooster weight including the
ones no environment variable exposes.

RE-INDEXING NEEDS --clear (ISS-027). Earlier runs' vectors stay searchable, so
--ingest refuses a corpus that is already indexed unless --clear comes with it.
--clear deletes that corpus's Qdrant points and ingestion runs, and nothing else.

Ingestion costs OpenAI credits; measurement is cheap and re-runnable.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import UUID, uuid5

import psycopg2
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.chunker.semantic_chunker import SemanticChunker  # noqa: E402
from workers.db import require_tenant  # noqa: E402
from workers.pipeline.ingestion_pipeline import IngestionPipeline  # noqa: E402
from workers.retrieval.query_engine import QueryEngine  # noqa: E402

PG = os.getenv("DATABASE_URL", "postgresql://coderag:coderag@127.0.0.1:5434/coderag")
QDRANT = os.getenv("QDRANT_URL", "http://localhost:6333")
OPENAI = os.getenv("OPENAI_API_KEY")
QDRANT_COLLECTION = "code_embeddings"  # QdrantWriter's default

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS_DIR = Path(__file__).parent / "rag_benchmarks"
DEFAULT_CORPORA_DIR = REPO_ROOT.parent / "rag-bench-corpora"

# Stable ids so --ingest and --measure agree across runs.
ORG = UUID("0ca11117-0000-4000-8000-00000000f001")
PROJ = UUID("0ca11117-0000-4000-8000-00000000f002")
REPO = UUID("0ca11117-0000-4000-8000-00000000f003")  # the `self` corpus
# Benchmark corpora derive their repository id from their name, so no registry
# is needed for --ingest and --measure to agree.
BENCHMARK_NAMESPACE = UUID("0ca11117-0000-4000-8000-00000000f0b0")

# `self`: this repository's own Go backend and Python workers. Real code,
# written by this project, with answers checkable by reading it.
CORPUS_ROOTS = [
    ("services/backend/pkg", [".go"], "go"),
    ("services/workers/workers", [".py"], "python"),
]
SELF_EXCLUDE = [r"(^|/)test_[^/]*$", r"_test\.go$"]
SKIP_PARTS = {"venv", "node_modules", "__pycache__", ".git", "testdata", "vendor"}
QUESTION_SETS = ("tuning", "holdout", "confirm")

# ---------------------------------------------------------------------------
# TUNING SET. Each answer established by reading the code; the expected path is
# where the answer lives. Phrased the way someone would ask, not by echoing an
# identifier, because keyword-echo questions flatter FTS and prove nothing.
# ---------------------------------------------------------------------------
TUNING = [
    ("how do we stop one tenant reading another tenant's rows in a request",
     "services/backend/pkg/db/"),
    ("where is the GitHub App JWT signed",
     "services/backend/pkg/github/"),
    ("how does the webhook confirm a delivery really came from GitHub",
     "services/backend/pkg/api/handlers/github_webhook"),
    ("what stops the same webhook delivery being processed twice",
     "services/backend/pkg/api/handlers/github_webhook"),
    ("how do we work out which customer an incoming webhook belongs to",
     "services/backend/pkg/api/handlers/github_webhook_events"),
    ("where do we check a user actually controls the installation they claim",
     "services/backend/pkg/github/"),
    ("how is a repository connected to an organisation",
     "services/backend/pkg/api/handlers/repositories"),
    ("what happens when someone connects a repository that already exists",
     "services/backend/pkg/api/handlers/repositories"),
    ("how are routes and middleware wired together",
     "services/backend/pkg/api/router"),
    ("where are secrets stripped before logging",
     "services/backend/pkg/github/client"),
    ("how is a one-time token for the install flow generated and consumed",
     "services/backend/pkg/auth/"),
    ("how do we split source code into pieces for indexing",
     "services/workers/workers/chunker/"),
    ("where do we turn text into vectors",
     "services/workers/workers/embeddings/"),
    ("how are keyword results and vector results combined into one ranking",
     "services/workers/workers/retrieval/rrf_fusion"),
    ("how does search boost results that look more relevant",
     "services/workers/workers/retrieval/metadata_booster"),
    ("where does full text search run against the database",
     "services/workers/workers/retrieval/fts_retriever"),
    ("how do we avoid paying for the same question twice",
     "services/workers/workers/generation/semantic_cache"),
    ("where is the prompt for answering a question assembled",
     "services/workers/workers/generation/answer_generator"),
    ("how is a parsed file turned into functions and classes",
     "services/workers/workers/parser/tree_sitter_parser"),
    ("where is the breadcrumb for a symbol built",
     "services/workers/workers/chunker/metadata_builder"),
    ("how are chunks written to the database",
     "services/workers/workers/storage/postgres_writer"),
    ("how are vectors stored for similarity search",
     "services/workers/workers/storage/qdrant_writer"),
    ("what orchestrates the whole ingestion flow",
     "services/workers/workers/pipeline/ingestion_pipeline"),
    ("how does a worker scope its database writes to one tenant",
     "services/workers/workers/db/tenant"),
    ("where is a natural language query broken into terms",
     "services/workers/workers/retrieval/query_parser"),
]

# ---------------------------------------------------------------------------
# HOLDOUT SET. Written 2026-09-13 before any retrieval result for these was
# seen, each answer verified against the file's own code and comments. All
# target files the tuning set never targets. File-level expectations only.
#
# The last question is a deliberate disambiguation trap: "webhook" in the
# tuning set always meant GitHub's; this one means Supabase's.
# ---------------------------------------------------------------------------
HOLDOUT = [
    ("what blocks throwaway email addresses from signing up",
     "services/backend/pkg/auth/abuse"),
    ("how is a user record created the first time someone logs in with oauth",
     "services/backend/pkg/auth/provisioning"),
    ("how do we check that a login token is genuine",
     "services/backend/pkg/auth/jwt"),
    ("how does organization information get into a user's login token",
     "services/backend/pkg/auth/supabase_admin"),
    ("how does the backend talk to the python search service",
     "services/backend/pkg/client/rag_client"),
    ("how does a user who belongs to several organizations switch between them",
     "services/backend/pkg/api/handlers/user_orgs"),
    ("how are streaming answers sent back to the browser",
     "services/backend/pkg/api/handlers/chat"),
    ("how are http error responses formatted",
     "services/backend/pkg/api/handlers/errors"),
    ("how do tests get a throwaway database to run against",
     "services/backend/pkg/testing/isolation/container"),
    ("what happens when a file cannot be parsed into functions",
     "services/workers/workers/chunker/fixed_size_chunker"),
    ("how do we describe what a whole file is for",
     "services/workers/workers/chunker/summary_generator"),
    ("how do we keep text under the embedding model's token limit",
     "services/workers/workers/embeddings/openai_client"),
    ("how do we avoid embedding the same text twice",
     "services/workers/workers/embeddings/embedding_generator"),
    ("where do keyword search and vector search run side by side",
     "services/workers/workers/retrieval/query_engine"),
    ("how does the backend receive account events from supabase",
     "services/backend/pkg/auth/webhook"),
]


@dataclass
class Corpus:
    """What to index and what to ask about it."""

    name: str
    root: Path
    roots: List[Tuple[str, List[str], str]]
    exclude: List[str]
    repository_id: UUID
    repository_url: str
    commit: str
    exact_paths: bool
    questions: List[dict] = field(default_factory=list)


def load_corpus(name: str, corpora_dir: Path) -> Corpus:
    if name == "self":
        questions = [
            {"id": f"self-t{i:02d}", "set": "tuning", "question": q, "path": p}
            for i, (q, p) in enumerate(TUNING, 1)
        ] + [
            {"id": f"self-h{i:02d}", "set": "holdout", "question": q, "path": p}
            for i, (q, p) in enumerate(HOLDOUT, 1)
        ]
        return Corpus("self", REPO_ROOT, CORPUS_ROOTS, SELF_EXCLUDE, REPO,
                      "https://github.com/AlecAsdourian/RAG-Doc", "harness", False, questions)

    spec_path = BENCHMARKS_DIR / f"{name}.json"
    if not spec_path.exists():
        known = sorted(p.stem for p in BENCHMARKS_DIR.glob("*.json"))
        sys.exit(f"no corpus named {name!r}; known corpora: self, {', '.join(known) or '(none)'}")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    validate_spec(spec, name)
    return Corpus(
        name=name,
        root=corpora_dir / name,
        roots=[(r["path"], r["extensions"], r["language"]) for r in spec["roots"]],
        exclude=spec.get("exclude", []),
        repository_id=uuid5(BENCHMARK_NAMESPACE, name),
        repository_url=spec["repository"],
        commit=spec["commit"],
        exact_paths=True,
        questions=spec["questions"],
    )


def validate_spec(spec: dict, name: str) -> None:
    problems = []
    for key in ("repository", "commit", "roots", "questions"):
        if key not in spec:
            problems.append(f"missing {key!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(spec.get("commit", ""))):
        problems.append("commit must be a full 40-character sha")
    for root in spec.get("roots", []):
        if not isinstance(root, dict) or not {"path", "extensions", "language"} <= set(root):
            problems.append(f"a root needs path, extensions and language: {root}")
            continue
        # A bare string such as ".py" would pass a membership test character by
        # character and quietly index every extension-less file (LICENSE,
        # Makefile) as that language.
        extensions = root["extensions"]
        if not (isinstance(extensions, list) and extensions
                and all(isinstance(e, str) and len(e) > 1 and e.startswith(".") for e in extensions)):
            problems.append(f"a root's extensions must be a non-empty list such as ['.py']: {root}")
    for pattern in spec.get("exclude", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"bad exclude pattern {pattern!r}: {exc}")
    ids = [q.get("id") for q in spec.get("questions", [])]
    if None in ids or len(set(ids)) != len(ids):
        problems.append("every question needs a unique id")
    for q in spec.get("questions", []):
        if q.get("set") not in QUESTION_SETS:
            problems.append(f"{q.get('id')}: set must be one of {', '.join(QUESTION_SETS)}")
        if not q.get("question") or not q.get("path"):
            problems.append(f"{q.get('id')}: needs a question and a path")
        if "symbol" in q and not (isinstance(q["symbol"], str) and q["symbol"].strip()):
            problems.append(f"{q.get('id')}: a symbol, when given, must be a non-empty name")
    if problems:
        sys.exit(f"invalid spec rag_benchmarks/{name}.json:\n  " + "\n  ".join(problems))


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def require_fetched(corpus: Corpus) -> None:
    if corpus.name == "self":
        return
    if not (corpus.root / ".git").exists():
        sys.exit(f"corpus {corpus.name} is not fetched; run with --corpus {corpus.name} --fetch")
    head = git("rev-parse", "HEAD", cwd=corpus.root)
    if head != corpus.commit:
        sys.exit(f"{corpus.root} is at {head[:12]}, not the pinned {corpus.commit[:12]}")


def do_fetch(corpus: Corpus) -> None:
    if corpus.name == "self":
        print("[*] self is this repository; nothing to fetch")
        return
    root = corpus.root
    if (root / ".git").exists():
        require_fetched(corpus)
        print(f"[*] {corpus.name}: already at {corpus.commit[:12]} in {root}")
        return
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} exists and is not a git checkout; refusing to overwrite it")
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=root)
    git("remote", "add", "origin", corpus.repository_url, cwd=root)
    git("fetch", "-q", "--depth", "1", "origin", corpus.commit, cwd=root)
    git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=root)
    require_fetched(corpus)
    print(f"[*] {corpus.name}: fetched {corpus.repository_url} at {corpus.commit[:12]} into {root}")


def collect_files(corpus: Corpus):
    excludes = [re.compile(p) for p in corpus.exclude]
    out = []
    for rel, exts, lang in corpus.roots:
        base = corpus.root / rel
        if not base.exists():
            print(f"  [WARN] missing corpus root: {rel}")
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in exts:
                continue
            relative = p.relative_to(corpus.root)
            path = relative.as_posix()
            if SKIP_PARTS & set(relative.parts) or any(x.search(path) for x in excludes):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not content.strip():
                continue
            out.append((path, content, lang))
    return out


def path_matches(corpus: Corpus, expected: str, actual: str) -> bool:
    return actual == expected if corpus.exact_paths else expected in actual


def symbol_matches(symbol: str, breadcrumb: Optional[str]) -> bool:
    return bool(breadcrumb) and (breadcrumb == symbol or breadcrumb.endswith("." + symbol))


def do_check(corpus: Corpus) -> None:
    """Check a corpus's questions offline: no database, no OpenAI, no retrieval.

    Fails when an expected file is not in the corpus. Warns when no chunk carries
    the expected symbol's name (it cannot score at symbol level) and when a
    question spells out the name it is asking about (it flatters keyword search).
    """
    require_fetched(corpus)
    files = {path: (content, lang) for path, content, lang in collect_files(corpus)}
    chunker = SemanticChunker()
    fails = warns = 0
    for q in corpus.questions:
        qid = q["id"]
        matching = [p for p in files if path_matches(corpus, q["path"], p)]
        if not matching:
            print(f"  FAIL {qid}: no corpus file matches {q['path']}")
            fails += 1
            continue
        symbol = q.get("symbol")
        if symbol and corpus.exact_paths:
            content, lang = files[q["path"]]
            names = [c.metadata.get("breadcrumb", "") for c in chunker.chunk_file(q["path"], content, lang)]
            if not any(symbol_matches(symbol, n) for n in names):
                print(f"  WARN {qid}: no chunk in {q['path']} is named {symbol}, so it cannot score at symbol level")
                warns += 1
        for part in (symbol or "").split("."):
            if len(part) > 3 and re.search(rf"\b{re.escape(part)}\b", q["question"], re.IGNORECASE):
                print(f"  WARN {qid}: the question names {part!r}, which flatters keyword search")
                warns += 1
    per_set = ", ".join(f"{sum(q['set'] == s for q in corpus.questions)} {s}" for s in QUESTION_SETS)
    with_symbol = sum(bool(q.get("symbol")) for q in corpus.questions)
    print(f"[*] check {corpus.name}: {len(files)} files; questions: {per_set}; "
          f"{with_symbol} naming a symbol; {fails} failures, {warns} warnings")
    if fails:
        sys.exit(1)


def ensure_fixtures(corpus: Corpus) -> None:
    """Org, project and repository rows the ingest writes against."""
    conn = psycopg2.connect(PG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING", (str(ORG), "RAG Quality Harness", "rag-quality"))
        cur.execute(
            "INSERT INTO projects (id, organization_id, name, slug) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING", (str(PROJ), str(ORG), "self", "self"))
    with require_tenant(conn, ORG) as cur:
        cur.execute(
            "INSERT INTO repositories (id, project_id, name, git_url) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(corpus.repository_id), str(PROJ),
             "RAG-Doc" if corpus.name == "self" else corpus.name, corpus.repository_url))
    conn.close()


def _repository_filter(corpus: Corpus) -> Filter:
    return Filter(must=[FieldCondition(key="repository_id",
                                       match=MatchValue(value=str(corpus.repository_id)))])


def indexed_state(corpus: Corpus) -> Tuple[int, int]:
    """(ingestion runs, Qdrant points) currently stored for this corpus."""
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            cur.execute("SELECT count(*) FROM ingestion_runs WHERE repository_id = %s",
                        (str(corpus.repository_id),))
            runs = cur.fetchone()[0]
    finally:
        conn.close()
    client = QdrantClient(url=QDRANT)
    if QDRANT_COLLECTION not in [c.name for c in client.get_collections().collections]:
        return runs, 0
    points = client.count(QDRANT_COLLECTION, count_filter=_repository_filter(corpus), exact=True).count
    return runs, points


def do_clear(corpus: Corpus) -> None:
    client = QdrantClient(url=QDRANT)
    if QDRANT_COLLECTION in [c.name for c in client.get_collections().collections]:
        client.delete(QDRANT_COLLECTION, points_selector=FilterSelector(filter=_repository_filter(corpus)),
                      wait=True)
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            # A run's chunks go with it (chunks.ingestion_run_id ON DELETE CASCADE).
            cur.execute("DELETE FROM ingestion_runs WHERE repository_id = %s",
                        (str(corpus.repository_id),))
    finally:
        conn.close()
    runs, points = indexed_state(corpus)
    print(f"[*] cleared {corpus.name}: runs={runs} points={points}")
    if runs or points:
        sys.exit("clear did not remove everything")


def do_ingest(corpus: Corpus) -> None:
    require_fetched(corpus)
    files = collect_files(corpus)
    total_bytes = sum(len(c) for _, c, _ in files)
    print(f"[*] corpus {corpus.name}: {len(files)} files, {total_bytes/1024:.0f} KiB")
    if not files:
        sys.exit("no files collected")
    ensure_fixtures(corpus)
    print("[*] fixtures ready")

    pipeline = IngestionPipeline(postgres_conn=PG, qdrant_url=QDRANT, openai_api_key=OPENAI)
    stats = pipeline.process_files(
        files=files, organization_id=ORG, repository_id=corpus.repository_id,
        commit_sha=corpus.commit, branch="main")

    print(f"\nstatus            : {stats['status']}")
    print(f"files processed   : {stats.get('files_processed')}")
    print(f"chunks created    : {stats.get('chunks_created')}")
    print(f"embeddings        : {stats.get('embeddings_generated')}")
    print(f"duration          : {stats.get('duration_seconds', 0):.1f}s")
    if stats["status"] == "failed":
        sys.exit(f"FAILED: {stats.get('error')}")


def _score(ranks: List[Optional[int]]) -> dict:
    found = [r for r in ranks if r]
    total = len(ranks)
    return {"questions": total, "found": len(found), "rank1": sum(1 for r in found if r == 1),
            "mrr": (sum(1.0 / r for r in found) / total) if total else 0.0}


def do_measure(corpus: Corpus, set_name: str, top_k: int, boost_config=None,
               json_out: Optional[Path] = None) -> None:
    questions = [q for q in corpus.questions if set_name == "all" or q["set"] == set_name]
    # boost_config goes straight to QueryEngine -> MetadataBooster, which merges
    # it over DEFAULT_CONFIG. Lets a ranking variant be measured without editing
    # library code, so several variants can run side by side against one build.
    engine = QueryEngine(postgres_conn=PG, qdrant_url=QDRANT, openai_api_key=OPENAI,
                         boost_config=boost_config)
    rows = []
    for q in questions:
        row = {"id": q["id"], "set": q["set"], "question": q["question"], "path": q["path"],
               "symbol": q.get("symbol"), "file_rank": None, "symbol_rank": None,
               "top_hit": "", "error": None}
        try:
            res = engine.query(query_text=q["question"], organization_id=ORG,
                               repository_id=corpus.repository_id, top_k=top_k)
        except Exception as exc:                     # noqa: BLE001
            # A failed retriever raises RetrievalError (ISS-030) and lands here,
            # as an error rather than a miss. Before that fix QueryEngine returned
            # what the other retriever found, often nothing, and scoring that as
            # a miss let a broken run pass as a result: a rejected OpenAI key
            # measured 0/15 with no error reported.
            row["error"] = str(exc)[:120]
            rows.append(row)
            continue
        results = res.get("results", [])
        row["top_hit"] = results[0].get("file_path", "") if results else "(no results)"
        in_file = [path_matches(corpus, q["path"], r.get("file_path", "")) for r in results]
        row["file_rank"] = next((i for i, hit in enumerate(in_file, 1) if hit), None)
        if row["symbol"]:
            row["symbol_rank"] = next(
                (i for i, (hit, r) in enumerate(zip(in_file, results), 1)
                 if hit and symbol_matches(row["symbol"], r.get("breadcrumb"))), None)
        rows.append(row)

    print(f"\n[{corpus.name} / {set_name}]  {len(questions)} questions, top_k={top_k}")
    if boost_config:
        print(f"boost_config: {json.dumps(boost_config, sort_keys=True)}")
    print()
    print(f"{'file':<6} {'symbol':<6} {'question':<64} top hit")
    print("-" * 136)
    for row in rows:
        if row["error"]:
            label, symbol_label = f"ERR {row['error'][:40]}", ""
        else:
            label = f"#{row['file_rank']}" if row["file_rank"] else "MISS"
            symbol_label = "" if not row["symbol"] else (
                f"#{row['symbol_rank']}" if row["symbol_rank"] else "MISS")
        print(f"{label:<6} {symbol_label:<6} {row['question'][:62]:<64} {row['top_hit'][:56]}")
    print("-" * 136)

    summary = {
        "file": _score([r["file_rank"] for r in rows]),
        "symbol": _score([r["symbol_rank"] for r in rows if r["symbol"]]),
        "errors": sum(1 for r in rows if r["error"]),
    }
    f = summary["file"]
    print(f"\nrecall@{top_k} : {f['found']}/{f['questions']} = {100*f['found']/max(f['questions'],1):.0f}%")
    print(f"rank-1    : {f['rank1']}/{f['questions']} = {100*f['rank1']/max(f['questions'],1):.0f}%")
    print(f"MRR       : {f['mrr']:.3f}")
    s = summary["symbol"]
    if s["questions"]:
        print(f"symbol level ({s['questions']} questions): recall@{top_k} {s['found']}/{s['questions']}, "
              f"rank-1 {s['rank1']}/{s['questions']}, MRR {s['mrr']:.3f}")
    if summary["errors"]:
        print(f"errors    : {summary['errors']} (counted as misses)")
        print(f"MEASUREMENT INVALID: {summary['errors']} of {len(rows)} queries failed; do not use these numbers")

    if json_out:
        json_out.write_text(json.dumps({
            "corpus": corpus.name, "commit": corpus.commit, "set": set_name, "top_k": top_k,
            "boost_config": boost_config, "summary": summary, "rows": rows,
        }, indent=1), encoding="utf-8")
        print(f"wrote {json_out}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ingest a corpus and measure retrieval against its questions.")
    ap.add_argument("--corpus", default="self",
                    help="`self` (this repository) or the name of a spec in scripts/rag_benchmarks/")
    ap.add_argument("--corpora-dir", type=Path, default=DEFAULT_CORPORA_DIR,
                    help=f"where benchmark corpora are fetched (default: {DEFAULT_CORPORA_DIR})")
    ap.add_argument("--fetch", action="store_true", help="clone a benchmark corpus at its pinned commit")
    ap.add_argument("--check", action="store_true",
                    help="check the questions against the corpus offline: no database, OpenAI or retrieval")
    ap.add_argument("--clear", action="store_true",
                    help="delete this corpus's Qdrant points and ingestion runs (ISS-027)")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--set", choices=["all", *QUESTION_SETS], default="tuning")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--boost-config", type=json.loads, default=None,
                    help='JSON merged over MetadataBooster defaults, e.g. '
                         '\'{"breadcrumb_match_boost": 1.0}\'')
    ap.add_argument("--json-out", type=Path, default=None,
                    help="also write per-question ranks and the summary as JSON")
    a = ap.parse_args()

    corpus = load_corpus(a.corpus, a.corpora_dir)
    if (a.ingest or a.measure) and not OPENAI:
        sys.exit("OPENAI_API_KEY not set")
    if a.fetch:
        do_fetch(corpus)
    if a.check:
        do_check(corpus)
    if a.clear:
        do_clear(corpus)
    if a.ingest:
        runs, points = indexed_state(corpus)
        if runs or points:
            sys.exit(f"{corpus.name} is already indexed (runs={runs}, points={points}); re-indexing "
                     "without --clear would mix two indexes (ISS-027)")
        do_ingest(corpus)
    if a.measure:
        if do_measure(corpus, a.set, a.top_k, a.boost_config, a.json_out)["errors"]:
            sys.exit(2)
    if not (a.fetch or a.check or a.clear or a.ingest or a.measure):
        ap.print_help()
