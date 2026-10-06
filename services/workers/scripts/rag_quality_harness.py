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

    # A scratch database, never compose's: --ingest and --clear refuse port 5434
    # (compose's mapping, and the default DATABASE_URL) unless --allow-compose is
    # passed deliberately. Migrate the scratch database and create rag_doc_app
    # the way tests/isolation/conftest.py does; --measure then runs as that role
    # with `?options=-c%20role%3Drag_doc_app` on the DSN.
    export DATABASE_URL=postgresql://user:pass@127.0.0.1:<scratch-port>/db
    export OPENAI_API_KEY=sk-...

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

RE-INDEXING NEEDS --clear (ISS-027). Earlier runs' chunks stay searchable, so
--ingest refuses a corpus that is already indexed unless --clear comes with it.
--clear deletes that corpus's ingestion runs and, through the cascade, their
chunks and vectors, and nothing else.

--ingest AND --clear REFUSE COMPOSE'S POSTGRES. DATABASE_URL defaults to
compose's Postgres on port 5434, which is not a scratch store: the harness
writes and deletes. Both flags exit before touching anything when the
configured port is compose's, unless --allow-compose is passed deliberately
(22-03).

RECORDING A RUN FOR THE STORAGE-MOVE EQUIVALENCE GATE (22-03). The gate compares
retrieval over the same chunks and the same query vectors under two read paths,
so a recorded run needs more than final ranks:

    --query-vectors FILE   question id -> its ada-002 vector. A missing question
                           is embedded ONCE and written back; every run reads the
                           file, so both sides of the gate ask the same vector.
    --record FILE.jsonl    per question: QueryEngine's trace (both legs, fused,
                           boosted, top), the ranks, the SHA-256 of the query
                           vector used, and the measuring connection's role
                           (rolsuper and rolbypassrls must be false). Refuses a
                           superuser connection.
    --exact FILE           the exact-search reference: each question's nearest
                           chunks by cosine distance with index scans off, from
                           the cached vector, plus every chunk tied with the 50th.

    scripts/rag_benchmarks/compare_runs.py judges two recorded runs under the
    rule in .planning/phases/22-repository-clone-ingestion/22-03-equivalence.md.
    (The Qdrant point set it also reads, --qdrant-ids, was recorded before Qdrant
    was retired and cannot be recorded again; the committed files are the record.
    Two runs recorded since are judged with `compare_runs.py --no-qdrant`.)

WHAT A RECORD MEASURED (22.2-01). Every run header names the chunker version,
the chunk-set digest and row count of the corpus's rows with the run's model
(read as the measuring connection, under the tenant), the rows per model, the
corpus's commit and the vector tolerance (`scripts/rag_benchmarks/
chunk_digest.py` defines the first two). --ingest prints the same digest
computed offline from the files it is about to ingest, so a header can be
checked against it. --self-root DIR reads `self` from DIR, so a chunker change
(the chunker is part of `self`) is measured before and after on one tree;
--self-commit names DIR's commit when it is an export with no .git.
--vector-tolerance (default 1e-5, 22-03's; Phase 22.2 passes QD2's 2e-6) sets
--exact's tie tail and is recorded in the header.

Ingestion costs OpenAI credits; measurement is cheap and re-runnable.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid5

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import parse_dsn

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent / "rag_benchmarks"))

import chunk_digest  # noqa: E402  (what a record measured: the digest and the chunker version)
from scoring import aggregate, path_matches, ranks, symbol_matches  # noqa: E402
from workers.chunker.semantic_chunker import SemanticChunker  # noqa: E402
from workers.db import require_tenant  # noqa: E402
from workers.pipeline.ingestion_pipeline import IngestionPipeline  # noqa: E402
from workers.retrieval.query_engine import QueryEngine  # noqa: E402
from workers.retrieval.vector_retriever import vector_literal  # noqa: E402

# The default is compose's Postgres. --ingest and --clear refuse it without
# --allow-compose (refuse_compose below); --measure only reads.
DEFAULT_PG = "postgresql://coderag:coderag@127.0.0.1:5434/coderag"
PG = os.getenv("DATABASE_URL", DEFAULT_PG)
OPENAI = os.getenv("OPENAI_API_KEY")

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
    # Until 22-03 this expected qdrant_writer; vectors are stored by the
    # Postgres writer now (the same file the previous question expects).
    ("how are vectors stored for similarity search",
     "services/workers/workers/storage/postgres_writer"),
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


def tree_commit(root: Path) -> Optional[str]:
    """The HEAD of the git checkout whose top level is `root`, or None.

    None for an export without `.git`, and for a directory that is only inside
    some other checkout: that checkout's HEAD would name the wrong tree.
    """
    try:
        top = git("rev-parse", "--show-toplevel", cwd=root)
        if Path(top).resolve() != Path(root).resolve():
            return None
        return git("rev-parse", "HEAD", cwd=root)
    except (subprocess.CalledProcessError, OSError):
        return None


def load_corpus(name: str, corpora_dir: Path, self_root: Path = REPO_ROOT,
                self_commit: Optional[str] = None) -> Corpus:
    """The corpus to index and its questions.

    `self` is read from `self_root` (default: this checkout). The `self` corpus
    holds `workers/chunker/` and `workers/parser/`, so a chunker change alters
    the source `self` is built from as well as how it is chunked; before and
    after are measured on one tree by pointing both at it (22.2-01). Its
    commit is `self_commit` when given (an export has no `.git`), else the
    tree's HEAD, else None.
    """
    if name == "self":
        questions = [
            {"id": f"self-t{i:02d}", "set": "tuning", "question": q, "path": p}
            for i, (q, p) in enumerate(TUNING, 1)
        ] + [
            {"id": f"self-h{i:02d}", "set": "holdout", "question": q, "path": p}
            for i, (q, p) in enumerate(HOLDOUT, 1)
        ]
        root = Path(self_root)
        commit = self_commit or tree_commit(root)
        return Corpus("self", root, CORPUS_ROOTS, SELF_EXCLUDE, REPO,
                      "https://github.com/AlecAsdourian/RAG-Doc", commit, False, questions)

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
        matching = [p for p in files if path_matches(corpus.exact_paths, q["path"], p)]
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


def indexed_state(corpus: Corpus) -> Tuple[int, int]:
    """(ingestion runs, chunks) currently stored for this corpus, under the harness tenant."""
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            cur.execute("SELECT count(*) FROM ingestion_runs WHERE repository_id = %s",
                        (str(corpus.repository_id),))
            runs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM chunks WHERE repository_id = %s",
                        (str(corpus.repository_id),))
            chunks = cur.fetchone()[0]
    finally:
        conn.close()
    return runs, chunks


def do_clear(corpus: Corpus) -> None:
    """Delete the corpus's ingestion runs; their chunks, vectors included, go with them."""
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            # A run's chunks go with it (chunks.ingestion_run_id ON DELETE CASCADE),
            # and since 000017 a chunk's vector is a column of the chunk.
            cur.execute("DELETE FROM ingestion_runs WHERE repository_id = %s",
                        (str(corpus.repository_id),))
    finally:
        conn.close()
    runs, chunks = indexed_state(corpus)
    print(f"[*] cleared {corpus.name}: runs={runs} chunks={chunks}")
    if runs or chunks:
        sys.exit("clear did not remove everything")


def do_ingest(corpus: Corpus) -> None:
    require_fetched(corpus)
    files = collect_files(corpus)
    total_bytes = sum(len(c) for _, c, _ in files)
    print(f"[*] corpus {corpus.name}: {len(files)} files, {total_bytes/1024:.0f} KiB, commit {corpus.commit}")
    if not files:
        sys.exit("no files collected")
    # Computed with no API call, before anything is written, so a run header's
    # database digest can be checked against it.
    digest, rows = offline_chunk_set(files)
    print(f"[*] offline: chunker version {chunk_digest.chunker_version()}, "
          f"chunk set {digest} ({rows} rows)")
    ensure_fixtures(corpus)
    print("[*] fixtures ready")

    pipeline = IngestionPipeline(postgres_conn=PG, openai_api_key=OPENAI)
    # ingestion_runs.commit_sha is NOT NULL; a `self` tree whose commit is
    # unknown (an export, no --self-commit) is ingested as "harness", as before.
    stats = pipeline.process_files(
        files=files, organization_id=ORG, repository_id=corpus.repository_id,
        commit_sha=corpus.commit or "harness", branch="main")

    print(f"\nstatus            : {stats['status']}")
    print(f"files processed   : {stats.get('files_processed')}")
    print(f"chunks created    : {stats.get('chunks_created')}")
    print(f"embeddings        : {stats.get('embeddings_generated')}")
    print(f"duration          : {stats.get('duration_seconds', 0):.1f}s")
    if stats["status"] == "failed":
        sys.exit(f"FAILED: {stats.get('error')}")


# ---------------------------------------------------------------------------
# 22-03: the storage-move equivalence gate. Added BEFORE the Qdrant-era
# baseline was recorded, so both sides of the gate were measured by the same
# code, and kept for the retrieval-quality track. Everything here is
# measurement, not retrieval: the guard that keeps writes off compose, the
# cached query vectors, the pin that makes the engine use them, the identity
# of the measuring connection and the exact-search reference.
# ---------------------------------------------------------------------------

COMPOSE_POSTGRES_PORT = 5434
EXACT_LIMIT = 50
# Rows past the 50th are fetched so that every chunk tied with the 50th (within
# the vector tolerance) is recorded too.
EXACT_TAIL = 150
# The vector-score tolerance: 22-03-equivalence.md's 1e-5 by default, so a 22-03
# record re-judges unchanged. Phase 22.2 passes QD2's 2e-6 (--vector-tolerance),
# and the exact list's tie tail follows it.
DEFAULT_VECTOR_TOLERANCE = 1e-5
IDENTITY_SQL = (
    "SELECT current_user, session_user, rolsuper, rolbypassrls "
    "FROM pg_roles WHERE rolname = current_user"
)
# The exact-search reference. Deliberately its own statement, not the
# retriever's: it is the ground truth the retriever's vector leg is judged
# against, run with every index scan disabled.
EXACT_SQL = """
    SELECT id::text AS chunk_id, embedding <=> %(q)s::vector AS distance
    FROM chunks
    WHERE repository_id = %(repo)s AND embedding_model = %(model)s
    ORDER BY embedding <=> %(q)s::vector, id
    LIMIT %(limit)s
"""


class UnknownTarget(ValueError):
    """The DSN does not say where it connects, so the guard cannot decide by port."""


def dsn_port(dsn: str, env: Optional[Mapping[str, str]] = None) -> int:
    """The Postgres port the harness would connect to.

    libpq fills in what the DSN omits from its environment, so a port-less DSN
    connects to `PGPORT` when that is set (5432 otherwise), and a port-less DSN
    with `PGPORT=5434` IS compose's Postgres. `PGHOST` does the same for the
    host and is reported for context, never decided on: the guard decides by
    port alone. Raises UnknownTarget when the port cannot be known from the
    DSN and the environment: a `service=` DSN or a `PGSERVICE` filling in the
    port from a service file this guard does not read, or a multi-host DSN,
    which names more than one place to write to.
    """
    env = os.environ if env is None else env
    try:
        parsed = parse_dsn(dsn)
    except psycopg2.ProgrammingError as exc:
        raise UnknownTarget(f"DATABASE_URL could not be parsed as a DSN ({type(exc).__name__})") from exc
    if "," in str(parsed.get("host", "")) or "," in str(parsed.get("port", "")):
        raise UnknownTarget("DATABASE_URL names more than one host; the harness writes to one scratch database")
    if parsed.get("service"):
        raise UnknownTarget(
            "DATABASE_URL uses service=, so its port comes from a service file this guard does not read; "
            "put the host and port in DATABASE_URL"
        )
    port = parsed.get("port")
    if port:
        return int(port)
    if env.get("PGSERVICE"):
        raise UnknownTarget(
            "DATABASE_URL states no port and PGSERVICE is set, so libpq would take the port from a service "
            "file this guard does not read; put the port in DATABASE_URL"
        )
    if env.get("PGPORT"):
        try:
            return int(env["PGPORT"])
        except ValueError as exc:
            raise UnknownTarget("PGPORT is not a number") from exc
    return 5432


def compose_targets(pg_dsn: str, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Which of compose's stores the configuration points at, by port, and how the port was found."""
    env = os.environ if env is None else env
    port = dsn_port(pg_dsn, env)
    if port != COMPOSE_POSTGRES_PORT:
        return []
    stated = bool(parse_dsn(pg_dsn).get("port"))
    where = "" if stated else f" (DATABASE_URL states no port; PGPORT={env.get('PGPORT')}"
    if not stated and env.get("PGHOST") and not parse_dsn(pg_dsn).get("host"):
        where += f", PGHOST={env.get('PGHOST')}"
    where += "" if stated else ")"
    return [f"Postgres on port {COMPOSE_POSTGRES_PORT}{where}"]


def refuse_compose(action: str, pg_dsn: str, allow_compose: bool,
                   env: Optional[Mapping[str, str]] = None) -> None:
    """--ingest and --clear write; on compose's port they refuse unless told otherwise.

    Runs before anything is touched. Port 5434 is docker-compose.yml's Postgres
    mapping, and on a developer machine it may be bound by another project's
    container entirely; a scratch container on a free port is what the
    benchmark should use. A DSN whose target cannot be known (service files,
    several hosts) is refused whatever the flag says: --allow-compose means
    "compose, deliberately", not "somewhere, deliberately". No message echoes
    the DSN, which carries a password.
    """
    try:
        targets = compose_targets(pg_dsn, env)
    except UnknownTarget as exc:
        sys.exit(f"--{action} refused: {exc}.")
    if targets and not allow_compose:
        sys.exit(
            f"--{action} refused: {' and '.join(targets)}: compose's (docker-compose.yml), "
            "not a scratch store. Point DATABASE_URL at a scratch container, "
            "or pass --allow-compose to write to compose deliberately."
        )


def vector_sha256(vector: Sequence[float]) -> str:
    """SHA-256 of the vector's JSON float list. Recorded per question; compare_runs.py
    refuses two runs whose hashes differ, since different vectors make every
    comparison meaningless."""
    return hashlib.sha256(json.dumps(list(vector), separators=(",", ":")).encode("ascii")).hexdigest()


class QueryVectors:
    """--query-vectors FILE: question id -> the vector it was embedded to, once.

    Both sides of the equivalence gate must ask the same question of the same
    vector, and whether ada-002 returns identical vectors across calls is not
    verified. So a question is embedded at most once, ever: a missing question
    is embedded, the file is written and read back, and the run uses what is on
    disk, exactly as every later run will.
    """

    def __init__(self, path: Path):
        self.path = path
        self.entries: Dict[str, dict] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def ensure(
        self,
        questions: List[dict],
        embed: Callable[[List[str]], List[List[float]]],
        model: str,
    ) -> int:
        """Embed the questions not yet cached, with `embed`. Returns how many were."""
        for q in questions:
            entry = self.entries.get(q["id"])
            if entry is None:
                continue
            if entry["question"] != q["question"]:
                sys.exit(f"{self.path}: {q['id']} is cached for a different question text; "
                         "refusing to reuse its vector")
            if entry["model"] != model:
                sys.exit(f"{self.path}: {q['id']} was embedded with {entry['model']} and the engine "
                         f"uses {model}; a query is never compared across models (22-CONTEXT P4)")
        missing = [q for q in questions if q["id"] not in self.entries]
        if missing:
            vectors = embed([q["question"] for q in missing])
            if len(vectors) != len(missing):
                sys.exit(f"embedding returned {len(vectors)} vectors for {len(missing)} questions")
            for q, vector in zip(missing, vectors):
                self.entries[q["id"]] = {
                    "question": q["question"],
                    "model": model,
                    "vector": [float(x) for x in vector],
                }
            self.path.write_text(json.dumps(self.entries, indent=0), encoding="utf-8")
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        return len(missing)

    def vector(self, question_id: str) -> List[float]:
        return self.entries[question_id]["vector"]


def pin_query_vector(engine: QueryEngine, question: str, vector: List[float]) -> None:
    """Make `engine` answer `question` with `vector`, and refuse to embed anything else.

    The vector retriever embeds a query through
    `self.embedding_generator.client.generate_embeddings_batch([query])[0]`.
    That exact call path is what this replaces, on the client instance, so the
    same pin works on the Qdrant read path and on the pgvector one. Any other
    text raises: an unexpected embedding call would be a measurement on a
    vector the record does not carry, and it must not pass silently.
    """
    client = engine.vector_retriever.embedding_generator.client

    def pinned(texts: List[str]) -> List[List[float]]:
        if list(texts) != [question]:
            raise RuntimeError(
                f"unexpected embedding call for {list(texts)!r}: only the cached question "
                f"{question!r} may be embedded during a recorded measurement"
            )
        return [list(vector)]

    client.generate_embeddings_batch = pinned


def connection_identity(conn) -> dict:
    """Who a connection is, and whether row-level security applies to it."""
    with conn.cursor() as cur:
        cur.execute(IDENTITY_SQL)
        current_user, session_user, rolsuper, rolbypassrls = cur.fetchone()
    if not conn.autocommit:
        conn.rollback()  # the SELECT opened a transaction; require_tenant needs it idle
    return {
        "current_user": current_user,
        "session_user": session_user,
        "rolsuper": bool(rolsuper),
        "rolbypassrls": bool(rolbypassrls),
    }


def measuring_connections(engine: QueryEngine) -> Dict[str, dict]:
    """The identity of every Postgres connection the engine measures through: both legs."""
    engine.fts_retriever.connect()
    engine.vector_retriever.connect()
    return {
        "fts": connection_identity(engine.fts_retriever.conn),
        "vector": connection_identity(engine.vector_retriever.conn),
    }


def database_facts() -> dict:
    """Where the run measured, without the password, and the server's vector settings."""
    parsed = parse_dsn(PG)
    facts = {key: parsed.get(key) for key in ("host", "port", "dbname", "options")}
    conn = psycopg2.connect(PG)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            facts["server_version"] = cur.fetchone()[0]
            # pgvector registers its parameters when its library loads, which
            # a fresh session has not done until it touches the vector type.
            cur.execute("SELECT '[1]'::vector")
            for setting in ("hnsw.ef_search", "hnsw.iterative_scan"):
                try:
                    cur.execute(f"SHOW {setting}")
                    facts[setting] = cur.fetchone()[0]
                except psycopg2.Error:
                    conn.rollback()
                    facts[setting] = None
        facts["connection"] = connection_identity(conn)
    finally:
        conn.close()
    return facts


def visible_chunks(corpus: Corpus) -> int:
    """How many of the corpus's chunks the configured connection can see under the tenant."""
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            cur.execute("SELECT count(*) FROM chunks WHERE repository_id = %s",
                        (str(corpus.repository_id),))
            return cur.fetchone()[0]
    finally:
        conn.close()


def harness_commit() -> Optional[str]:
    try:
        return git("rev-parse", "HEAD", cwd=REPO_ROOT)
    except (subprocess.CalledProcessError, OSError):
        return None


def short_plan(lines: List[str]) -> List[str]:
    """EXPLAIN output with a bound query vector abbreviated, so a record stays readable."""
    return [re.sub(r"'\[[-0-9.e,]+\]'::vector", "'[<query vector>]'::vector", line) for line in lines]


def explain_legs(engine: QueryEngine, corpus: Corpus, vector: List[float]) -> Dict[str, List[str]]:
    """EXPLAIN both production statements as the measuring role under the tenant (A5).

    The statements are the retriever modules' own constants, with a cached
    vector bound the way the retriever binds it; no copy of either.
    """
    from workers.retrieval.fts_retriever import FTS_SEARCH_SQL
    from workers.retrieval.vector_retriever import VECTOR_SEARCH_SQL

    engine.fts_retriever.connect()
    model = engine.vector_retriever.embedding_generator.model
    plans = {}
    with require_tenant(engine.fts_retriever.conn, ORG) as cur:
        cur.execute("EXPLAIN (COSTS OFF) " + VECTOR_SEARCH_SQL, {
            "q": vector_literal(vector), "repo": str(corpus.repository_id), "model": model, "limit": 50})
        plans["vector"] = short_plan([row[0] for row in cur.fetchall()])
        cur.execute("EXPLAIN (COSTS OFF) " + FTS_SEARCH_SQL, {
            "q": "where is the configuration loaded", "repo": str(corpus.repository_id), "limit": 50})
        plans["fts"] = short_plan([row[0] for row in cur.fetchall()])
    return plans


def exact_list(rows: List[Tuple[str, float]], tolerance: float) -> dict:
    """The top EXACT_LIMIT of `rows` (chunk id, distance), in order, plus every
    further row within `tolerance` of the last one: the tie tail at the cut."""
    top = [{"chunk_id": c, "distance": d} for c, d in rows[:EXACT_LIMIT]]
    tail = []
    if len(top) == EXACT_LIMIT:
        cutoff = top[-1]["distance"] + tolerance
        for c, d in rows[EXACT_LIMIT:]:
            if d > cutoff:
                break
            tail.append({"chunk_id": c, "distance": d})
    return {
        "top": top,
        "tail_ties": tail,
        "tail_complete": len(rows) < EXACT_LIMIT + EXACT_TAIL or len(tail) < EXACT_TAIL,
    }


def do_exact(corpus: Corpus, questions: List[dict], vectors: QueryVectors, model: str,
             out: Path, tolerance: float = DEFAULT_VECTOR_TOLERANCE) -> None:
    """The exact-search reference for class (b): per question, the nearest chunks by
    cosine distance with index scans off, plus every chunk tied with the 50th
    within `tolerance` (--vector-tolerance)."""
    conn = psycopg2.connect(PG)
    try:
        identity = connection_identity(conn)
        plan = None
        results = {}
        for q in questions:
            vector = vectors.vector(q["id"])
            params = {
                "q": vector_literal(vector),
                "repo": str(corpus.repository_id),
                "model": model,
                "limit": EXACT_LIMIT + EXACT_TAIL,
            }
            with require_tenant(conn, ORG) as cur:
                cur.execute("SET LOCAL enable_indexscan = off")
                cur.execute("SET LOCAL enable_bitmapscan = off")
                if plan is None:
                    cur.execute("EXPLAIN (COSTS OFF) " + EXACT_SQL, params)
                    plan = short_plan([row[0] for row in cur.fetchall()])
                cur.execute(EXACT_SQL, params)
                rows = [(chunk_id, float(distance)) for chunk_id, distance in cur.fetchall()]
            results[q["id"]] = {"query_vector_sha256": vector_sha256(vector), **exact_list(rows, tolerance)}
    finally:
        conn.close()
    out.write_text(json.dumps({
        "corpus": corpus.name, "repository_id": str(corpus.repository_id), "model": model,
        "limit": EXACT_LIMIT, "tie_tolerance": tolerance, "connection": identity,
        "plan": plan, "harness_commit": harness_commit(),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "questions": results,
    }, indent=1), encoding="utf-8")
    incomplete = sum(1 for r in results.values() if not r["tail_complete"])
    print(f"[*] exact search for {len(results)} questions of {corpus.name} as {identity['current_user']} "
          f"(rolsuper={identity['rolsuper']}) -> {out}; plan: {' / '.join(plan or [])}"
          + (f"; WARNING {incomplete} tie tails may be incomplete" if incomplete else ""))


def offline_chunk_set(files) -> Tuple[str, int]:
    """(digest, rows) of what the chunker makes of `files`, with no API call.

    The pipeline chunks the same files with the same chunker, so a run header's
    database digest must equal this (chunk_digest.py).
    """
    chunker = SemanticChunker()
    chunks = [c for path, content, lang in files for c in chunker.chunk_file(path, content, lang)]
    return chunk_digest.digest_of_chunks(chunks)


def database_chunk_set(corpus: Corpus, model: str) -> dict:
    """The corpus's stored rows as the record header names them, read as the
    configured (measuring) connection under the tenant: the digest and count of
    the rows with the run's `model`, and a count of rows per model over all of
    them (one repository can hold two model arms, 22.2-05)."""
    conn = psycopg2.connect(PG)
    try:
        with require_tenant(conn, ORG) as cur:
            cur.execute(chunk_digest.DB_ROWS_SQL, (str(corpus.repository_id), model))
            digest, rows = chunk_digest.digest_of_db_rows(cur.fetchall())
            cur.execute("SELECT embedding_model, count(*) FROM chunks WHERE repository_id = %s "
                        "GROUP BY embedding_model ORDER BY embedding_model", (str(corpus.repository_id),))
            models = {m: n for m, n in cur.fetchall()}
    finally:
        conn.close()
    return {"chunk_set_digest": digest, "chunk_rows": rows, "chunk_models": models}


def run_header(corpus: Corpus, set_name: str, top_k: int, boost_config, model: str,
               vector_tolerance: float, measured: dict) -> dict:
    """A --record file's first line. `measured` holds what was read from the
    database (`database`, `connections`, `chunks_visible`, `explain`, and
    `database_chunk_set`'s fields), so the rest is checkable without one."""
    return {
        "record": "run", "corpus": corpus.name, "commit": corpus.commit,
        # The tree the corpus was read from: a benchmark corpus's pin, or for
        # `self` the HEAD of --self-root (or --self-commit for an export).
        "corpus_commit": corpus.commit,
        "repository_id": str(corpus.repository_id), "organization_id": str(ORG),
        "set": set_name, "top_k": top_k, "boost_config": boost_config,
        # How a result's path is matched to a question's (scoring.py), so a
        # record's ranks can be recomputed from its final list.
        "exact_paths": corpus.exact_paths,
        "harness_commit": harness_commit(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "vector_backend": "pgvector",
        "embedding_model": model,
        # What was measured (22.2-01): the code that chunked, the rows read with
        # this run's model, and the tolerance its ties are read at.
        "chunker_version": chunk_digest.chunker_version(),
        "vector_tolerance": vector_tolerance,
        **measured,
    }


def do_measure(corpus: Corpus, set_name: str, top_k: int, boost_config=None,
               json_out: Optional[Path] = None, query_vectors: Optional[QueryVectors] = None,
               record: Optional[Path] = None,
               vector_tolerance: float = DEFAULT_VECTOR_TOLERANCE) -> dict:
    questions = [q for q in corpus.questions if set_name == "all" or q["set"] == set_name]
    # boost_config goes straight to QueryEngine -> MetadataBooster, which merges
    # it over DEFAULT_CONFIG. Lets a ranking variant be measured without editing
    # library code, so several variants can run side by side against one build.
    engine = QueryEngine(postgres_conn=PG, openai_api_key=OPENAI, boost_config=boost_config)
    model = engine.vector_retriever.embedding_generator.model
    if query_vectors is not None:
        # Embed whatever is missing through the engine's own client, BEFORE the
        # pin below replaces that method for the run.
        embed = engine.vector_retriever.embedding_generator.client.generate_embeddings_batch
        embedded = query_vectors.ensure(questions, embed, model)
        print(f"[*] query vectors: {len(questions) - embedded} cached, {embedded} embedded now "
              f"({model}) -> {query_vectors.path}")
    recorder = None
    if record is not None:
        if query_vectors is None:
            sys.exit("--record needs --query-vectors: the record carries the hash of the vector "
                     "each question was measured with")
        connections = measuring_connections(engine)
        for name, identity in connections.items():
            if identity["rolsuper"] or identity["rolbypassrls"]:
                sys.exit(f"--record refused: the {name} connection is {identity['current_user']} "
                         f"(rolsuper={identity['rolsuper']}, rolbypassrls={identity['rolbypassrls']}). "
                         "A superuser bypasses row-level security, so a measurement on it proves "
                         "nothing about the read path; put options=-c role=rag_doc_app in DATABASE_URL.")
        header = run_header(corpus, set_name, top_k, boost_config, model, vector_tolerance, {
            "database": database_facts(),
            "connections": connections, "chunks_visible": visible_chunks(corpus),
            **database_chunk_set(corpus, model),
            "explain": (
                explain_legs(engine, corpus, query_vectors.vector(questions[0]["id"]))
                if questions else None
            ),
        })
        recorder = record.open("w", encoding="utf-8")
        recorder.write(json.dumps(header) + "\n")
        print(f"[*] recording to {record} as {connections['fts']['current_user']} "
              f"(rolsuper={connections['fts']['rolsuper']}, "
              f"rolbypassrls={connections['fts']['rolbypassrls']}); "
              f"vector backend: {header['vector_backend']}")
        print(f"[*] header: chunker {header['chunker_version']}, chunk set {header['chunk_set_digest']} "
              f"({header['chunk_rows']} rows with {model}; rows per model {header['chunk_models']}), "
              f"corpus commit {header['corpus_commit']}, vector tolerance {vector_tolerance:g}")
    rows = []
    for q in questions:
        row = {"id": q["id"], "set": q["set"], "question": q["question"], "path": q["path"],
               "symbol": q.get("symbol"), "file_rank": None, "symbol_rank": None,
               "top_hit": "", "error": None}
        trace: Optional[dict] = {} if recorder is not None else None
        if query_vectors is not None:
            pin_query_vector(engine, q["question"], query_vectors.vector(q["id"]))
        try:
            res = engine.query(query_text=q["question"], organization_id=ORG,
                               repository_id=corpus.repository_id, top_k=top_k, trace=trace)
        except Exception as exc:                     # noqa: BLE001
            # A failed retriever raises RetrievalError (ISS-030) and lands here,
            # as an error rather than a miss. Before that fix QueryEngine returned
            # what the other retriever found, often nothing, and scoring that as
            # a miss let a broken run pass as a result: a rejected OpenAI key
            # measured 0/15 with no error reported.
            row["error"] = str(exc)[:120]
            rows.append(row)
            if recorder is not None:
                recorder.write(json.dumps({"record": "question", **row, "trace": trace,
                                           "query_vector_sha256": vector_sha256(
                                               query_vectors.vector(q["id"]))}) + "\n")
            continue
        results = res.get("results", [])
        row["top_hit"] = results[0].get("file_path", "") if results else "(no results)"
        # scoring.py's rule, the same one compare_runs.py recomputes a record's
        # ranks with from its recorded final list.
        row["file_rank"], row["symbol_rank"] = ranks(corpus.exact_paths, q["path"], row["symbol"], results)
        rows.append(row)
        if recorder is not None:
            recorder.write(json.dumps({
                "record": "question", **row,
                "query_vector_sha256": vector_sha256(query_vectors.vector(q["id"])),
                "trace": trace,
            }) + "\n")
    if recorder is not None:
        recorder.close()
        print(f"wrote {record}")

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
        "file": aggregate([r["file_rank"] for r in rows]),
        "symbol": aggregate([r["symbol_rank"] for r in rows if r["symbol"]]),
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
    ap.add_argument("--self-root", type=Path, default=REPO_ROOT,
                    help="the tree `--corpus self` is read from, for --check, --ingest and --measure "
                         "(default: this checkout); a chunker change is measured before and after on one tree")
    ap.add_argument("--self-commit", default=None,
                    help="the commit --self-root holds, for an export with no .git (default: its HEAD)")
    ap.add_argument("--vector-tolerance", type=float, default=DEFAULT_VECTOR_TOLERANCE,
                    help="absolute tolerance on vector similarity for --exact's tie tail, recorded in the "
                         "run header (default 1e-5, 22-03's; Phase 22.2 passes QD2's 2e-6)")
    ap.add_argument("--fetch", action="store_true", help="clone a benchmark corpus at its pinned commit")
    ap.add_argument("--check", action="store_true",
                    help="check the questions against the corpus offline: no database, OpenAI or retrieval")
    ap.add_argument("--clear", action="store_true",
                    help="delete this corpus's ingestion runs and, through the cascade, its chunks "
                         "and vectors (ISS-027)")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--set", choices=["all", *QUESTION_SETS], default="tuning")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--boost-config", type=json.loads, default=None,
                    help='JSON merged over MetadataBooster defaults, e.g. '
                         '\'{"breadcrumb_match_boost": 1.0}\'')
    ap.add_argument("--json-out", type=Path, default=None,
                    help="also write per-question ranks and the summary as JSON")
    ap.add_argument("--allow-compose", action="store_true",
                    help="let --ingest and --clear write to compose's Postgres (port 5434); "
                         "without it they refuse")
    ap.add_argument("--query-vectors", type=Path, default=None,
                    help="JSON cache of question id -> query vector; a missing question is "
                         "embedded once and written back, and the run uses the cached vector")
    ap.add_argument("--record", type=Path, default=None,
                    help="with --measure: write QueryEngine's trace, the ranks, the query-vector "
                         "hash and the measuring role per question, as JSON lines "
                         "(needs --query-vectors and a non-superuser DATABASE_URL)")
    ap.add_argument("--exact", type=Path, default=None,
                    help="write each question's exact-search nearest chunks (index scans off) "
                         "from the cached vectors (needs --query-vectors)")
    a = ap.parse_args()

    corpus = load_corpus(a.corpus, a.corpora_dir, self_root=a.self_root, self_commit=a.self_commit)
    if (a.ingest or a.measure) and not OPENAI:
        sys.exit("OPENAI_API_KEY not set")
    # The guard runs before anything is touched: compose's Postgres is not scratch.
    for action in ("clear", "ingest"):
        if getattr(a, action):
            refuse_compose(action, PG, a.allow_compose)
    if (a.exact or a.record) and a.query_vectors is None:
        sys.exit("--exact and --record need --query-vectors")
    query_vectors = QueryVectors(a.query_vectors) if a.query_vectors else None
    if a.fetch:
        do_fetch(corpus)
    if a.check:
        do_check(corpus)
    if a.clear:
        do_clear(corpus)
    if a.ingest:
        runs, chunks = indexed_state(corpus)
        if runs or chunks:
            sys.exit(f"{corpus.name} is already indexed (runs={runs}, chunks={chunks}); re-indexing "
                     "without --clear would mix two indexes (ISS-027)")
        do_ingest(corpus)
    if a.exact:
        questions = [q for q in corpus.questions if a.set == "all" or q["set"] == a.set]
        if not OPENAI:
            sys.exit("OPENAI_API_KEY not set (needed to embed any question the cache is missing)")
        engine = QueryEngine(postgres_conn=PG, openai_api_key=OPENAI)
        model = engine.vector_retriever.embedding_generator.model
        embedded = query_vectors.ensure(
            questions, engine.vector_retriever.embedding_generator.client.generate_embeddings_batch,
            model)
        print(f"[*] query vectors: {len(questions) - embedded} cached, {embedded} embedded now "
              f"({model}) -> {query_vectors.path}")
        do_exact(corpus, questions, query_vectors, model, a.exact, a.vector_tolerance)
    if a.measure:
        if do_measure(corpus, a.set, a.top_k, a.boost_config, a.json_out,
                      query_vectors=query_vectors, record=a.record,
                      vector_tolerance=a.vector_tolerance)["errors"]:
            sys.exit(2)
    if not (a.fetch or a.check or a.clear or a.ingest or a.measure or a.exact):
        ap.print_help()
