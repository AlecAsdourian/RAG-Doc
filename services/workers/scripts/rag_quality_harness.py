#!/usr/bin/env python3
"""Does retrieval actually work? Ingest this repository, then measure.

WHY THIS EXISTS. Three of nine phases are done and the core experience had
never been measured end to end. `test_ingestion.py` ingests three hardcoded
files and prints that it worked; that is a smoke test, not evidence. This
ingests a real corpus and scores retrieval against questions whose answers were
established by reading the code.

WHAT IT MEASURES. For each question, where the file that actually contains the
answer lands in the results. Reported three ways:

    recall@k  the right file appears anywhere in the top k
    rank-1    the right file is the very first result
    MRR       mean reciprocal rank -- 1/rank averaged, 0 when missed

MRR is the number to tune against: moving an answer from rank 5 to rank 2
counts, which rank-1 and recall@k both ignore.

TWO QUESTION SETS, AND WHY. Tuning weights against the same questions you score
with fits the weights to the questions, not to retrieval. So:

    tuning   the original 25. Look at these while changing ranking.
    holdout  15 more, written and verified against the code BEFORE any
             retrieval result for them was seen. Check these only after a
             change is made, never while making it.

A change that lifts `tuning` and not `holdout` has been overfitted.

READ SMALL DIFFERENCES WITH CARE. The measurement is deterministic -- re-running
a configuration reproduces every rank exactly -- so a difference between two
configurations is a real ranking change. But on 15 questions one answer moving
from #1 to #2 shifts MRR by 0.033, so a one- or two-question gap is weak
evidence that a change generalises.

KNOWN SOFTNESS in `tuning`, left in deliberately. An expectation matches any
file whose path contains it, and as of 2026-09-13 five tuning questions accept
more than one file: `pkg/auth/` (11 files), `workers/chunker/` (6),
`workers/embeddings/` (3), and both `handlers/github_webhook` questions, which
also match `github_webhook_events.go`. That flatters recall. They are unchanged
so the recorded baseline stays comparable. Every `holdout` expectation matches
exactly one file.

USAGE
    cd services/workers
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --ingest
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --measure --set tuning
    ./venv/Scripts/python.exe scripts/rag_quality_harness.py --measure --set holdout \\
        --boost-config '{"breadcrumb_match_boost": 1.0}'

Ranking knobs usable here: the BOOST_* environment variables QueryEngine reads,
and --boost-config, which reaches every MetadataBooster weight including the
ones no environment variable exposes.

RE-INGESTING THE SAME REPOSITORY IS NOT SAFE YET (ISS-027). Earlier runs' vectors
stay searchable, so clear the harness repository's Qdrant points and ingestion
runs before --ingest, or the measurement mixes two indexes.

Ingestion costs OpenAI credits; measurement is cheap and re-runnable.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

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
# written by this project, with answers checkable by reading it.
CORPUS_ROOTS = [
    ("services/backend/pkg", {".go"}, "go"),
    ("services/workers/workers", {".py"}, "python"),
]
SKIP_PARTS = {"venv", "node_modules", "__pycache__", ".git", "testdata"}

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

SETS = {"tuning": TUNING, "holdout": HOLDOUT, "all": TUNING + HOLDOUT}


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


def do_measure(set_name, top_k, boost_config=None):
    questions = SETS[set_name]
    # boost_config goes straight to QueryEngine -> MetadataBooster, which merges
    # it over DEFAULT_CONFIG. Lets a ranking variant be measured without editing
    # library code, so several variants can run side by side against one build.
    engine = QueryEngine(postgres_conn=PG, qdrant_url=QDRANT, openai_api_key=OPENAI,
                         boost_config=boost_config)
    rows = []
    for question, expected in questions:
        try:
            res = engine.query(query_text=question, organization_id=ORG,
                               repository_id=REPO, top_k=top_k)
            paths = [r.get("file_path", "") for r in res.get("results", [])]
        except Exception as exc:                     # noqa: BLE001
            rows.append((None, question, f"ERR {str(exc)[:40]}", ""))
            continue
        rank = next((i + 1 for i, p in enumerate(paths) if expected in p), None)
        rows.append((rank, question, "", paths[0] if paths else "(no results)"))

    print(f"\n[{set_name}]  {len(questions)} questions, top_k={top_k}")
    if boost_config:
        print(f"boost_config: {json.dumps(boost_config, sort_keys=True)}")
    print()
    print(f"{'rank':<6} {'question':<64} top hit")
    print("-" * 130)
    for rank, q, err, top in rows:
        label = err or (f"#{rank}" if rank else "MISS")
        print(f"{label:<6} {q[:62]:<64} {top[:56]}")
    print("-" * 130)

    total = len(rows)
    found = [r for r, *_ in rows if r]
    recall = len(found) / total
    at1 = sum(1 for r in found if r == 1) / total
    mrr = sum(1.0 / r for r in found) / total
    print(f"\nrecall@{top_k} : {len(found)}/{total} = {100*recall:.0f}%")
    print(f"rank-1    : {sum(1 for r in found if r == 1)}/{total} = {100*at1:.0f}%")
    print(f"MRR       : {mrr:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--set", choices=sorted(SETS), default="tuning")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--boost-config", type=json.loads, default=None,
                    help='JSON merged over MetadataBooster defaults, e.g. '
                         '\'{"breadcrumb_match_boost": 1.0}\'')
    a = ap.parse_args()
    if not OPENAI:
        sys.exit("OPENAI_API_KEY not set")
    if a.ingest:
        do_ingest()
    if a.measure:
        do_measure(a.set, a.top_k, a.boost_config)
    if not (a.ingest or a.measure):
        ap.print_help()
