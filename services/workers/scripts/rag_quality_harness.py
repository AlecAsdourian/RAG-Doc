#!/usr/bin/env python3
"""Does retrieval actually work? Ingest this repository, then measure.

WHY THIS EXISTS. Three of nine phases are done and the core experience has
never been measured end to end. `test_ingestion.py` ingests three hardcoded
files and prints that it worked; that is a smoke test, not evidence. This
ingests a real corpus and scores retrieval against questions whose answers were
established by reading the code.

WHAT IT MEASURES. For each question, whether the file that actually contains the
answer appears in the top-k results. That is deliberately a low bar -- it asks
"did retrieval surface the right place", not "was the generated answer good" --
because the low bar is the one that has to hold before anything above it matters.

USAGE
    cd services/workers
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --ingest
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --measure

Split deliberately: ingestion costs OpenAI credits, measurement is cheap and
re-runnable while tuning.
"""

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.db import require_tenant  # noqa: E402
from workers.pipeline.ingestion_pipeline import IngestionPipeline  # noqa: E402
from workers.retrieval.query_engine import QueryEngine  # noqa: E402

PG = os.getenv("DATABASE_URL", "postgresql://coderag:coderag@127.0.0.1:5434/coderag")
QDRANT = os.getenv("QDRANT_URL", "http://localhost:6333")
OPENAI = os.getenv("OPENAI_API_KEY")

# Stable ids so --ingest and --measure agree across runs.
ORG = UUID("0ca11117-0000-4000-8000-00000000f001")
PROJ = UUID("0ca11117-0000-4000-8000-00000000f002")
REPO = UUID("0ca11117-0000-4000-8000-00000000f003")

# Corpus: this repository's own Go backend and Python workers. Real code,
# written by this project, with answers I can check by reading it.
CORPUS_ROOTS = [
    ("services/backend/pkg", {".go"}, "go"),
    ("services/workers/workers", {".py"}, "python"),
]
SKIP_PARTS = {"venv", "node_modules", "__pycache__", ".git", "testdata"}

# ---------------------------------------------------------------------------
# The questions. Each answer was established by reading the code, and the
# expected file is where the answer actually lives. Phrased the way someone
# would ask, not using the identifier as a keyword, because keyword-echo
# questions flatter FTS and prove nothing about retrieval.
# ---------------------------------------------------------------------------
QUESTIONS = [
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


def collect_files():
    root = Path(__file__).resolve().parents[3]
    out = []
    for rel, exts, lang in CORPUS_ROOTS:
        base = root / rel
        if not base.exists():
            print(f"  [WARN] missing corpus root: {rel}")
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in exts:
                continue
            if SKIP_PARTS & set(p.parts):
                continue
            if p.name.startswith("test_") or p.name.endswith("_test.go"):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not content.strip():
                continue
            out.append((str(p.relative_to(root)).replace("\\", "/"), content, lang))
    return out


def ensure_fixtures():
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
            (str(REPO), str(PROJ), "RAG-Doc", "https://github.com/AlecAsdourian/RAG-Doc"))
    conn.close()


def do_ingest():
    files = collect_files()
    total_bytes = sum(len(c) for _, c, _ in files)
    print(f"[*] corpus: {len(files)} files, {total_bytes/1024:.0f} KiB")
    if not files:
        sys.exit("no files collected")
    ensure_fixtures()
    print("[*] fixtures ready")

    pipeline = IngestionPipeline(postgres_conn=PG, qdrant_url=QDRANT, openai_api_key=OPENAI)
    stats = pipeline.process_files(
        files=files, organization_id=ORG, repository_id=REPO,
        commit_sha="harness", branch="main")

    print(f"\nstatus            : {stats['status']}")
    print(f"files processed   : {stats.get('files_processed')}")
    print(f"chunks created    : {stats.get('chunks_created')}")
    print(f"embeddings        : {stats.get('embeddings_generated')}")
    print(f"duration          : {stats.get('duration_seconds', 0):.1f}s")
    if stats["status"] == "failed":
        sys.exit(f"FAILED: {stats.get('error')}")


def do_measure(top_k):
    engine = QueryEngine(postgres_conn=PG, qdrant_url=QDRANT, openai_api_key=OPENAI)
    hits = misses = 0
    rows = []
    for question, expected in QUESTIONS:
        try:
            res = engine.query(query_text=question, organization_id=ORG,
                               repository_id=REPO, top_k=top_k)
            paths = [r.get("file_path", "") for r in res.get("results", [])]
        except Exception as exc:                     # noqa: BLE001
            rows.append(("ERR", question, str(exc)[:60], ""))
            misses += 1
            continue
        rank = next((i + 1 for i, p in enumerate(paths) if expected in p), None)
        if rank:
            hits += 1
            rows.append(("HIT", question, f"rank {rank}", paths[0]))
        else:
            misses += 1
            rows.append(("MISS", question, "not in top-%d" % top_k,
                         paths[0] if paths else "(no results)"))

    print(f"\n{'':4} {'question':<62} {'result':<14} top hit")
    print("-" * 130)
    for status, q, detail, top in rows:
        print(f"{status:<4} {q[:60]:<62} {detail:<14} {top[:44]}")

    total = hits + misses
    print("-" * 130)
    print(f"\nrecall@{top_k}: {hits}/{total} = {100*hits/total:.0f}%")
    at1 = sum(1 for s, _, d, _ in rows if s == "HIT" and d == "rank 1")
    print(f"rank-1   : {at1}/{total} = {100*at1/total:.0f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    a = ap.parse_args()
    if not OPENAI:
        sys.exit("OPENAI_API_KEY not set")
    if a.ingest:
        do_ingest()
    if a.measure:
        do_measure(a.top_k)
    if not (a.ingest or a.measure):
        ap.print_help()
