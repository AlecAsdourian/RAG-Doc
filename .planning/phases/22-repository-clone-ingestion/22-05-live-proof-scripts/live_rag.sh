#!/usr/bin/env bash
# The RAG API: the workers .env sourced for OPENAI_API_KEY, then DATABASE_URL
# overridden to the scratch database as rag_doc_app. No REDIS_URL, so the
# (broken, ISS-021) semantic cache is not attempted. Nothing echoes a value.
set -euo pipefail
LIVE=/tmp/rag2205-ScfByQaa/live
. "$LIVE/ports.env"
cd /c/Users/Alec/Desktop/code/testtGSD/.claude/worktrees/agent-ab4bd33d2c92fa0bd/services/workers
set -a
. /c/Users/Alec/Desktop/code/testtGSD/services/workers/.env
set +a
DATABASE_URL="$(cat "$LIVE/app_dsn")"
export DATABASE_URL
unset REDIS_URL QDRANT_URL
exec /tmp/rag2205-ScfByQaa/venv313/Scripts/python -m uvicorn api.main:app --host 127.0.0.1 --port "${RAG_PORT}" > "$LIVE/rag.log" 2>&1
