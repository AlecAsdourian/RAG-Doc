#!/usr/bin/env bash
# The worker: `python -m workers`, the workers .env sourced for OPENAI_API_KEY,
# DATABASE_URL overridden to the scratch database as rag_doc_app, and
# INTERNAL_API_URL at the backend's internal listener. It never sees the App
# key. Nothing echoes a value.
set -euo pipefail
LIVE=/tmp/rag2205-ScfByQaa/live
. "$LIVE/ports.env"
cd /c/Users/Alec/Desktop/code/testtGSD/.claude/worktrees/agent-ab4bd33d2c92fa0bd/services/workers
set -a
. /c/Users/Alec/Desktop/code/testtGSD/services/workers/.env
set +a
DATABASE_URL="$(cat "$LIVE/app_dsn")"
export DATABASE_URL
export INTERNAL_API_URL="http://127.0.0.1:${INTERNAL_PORT}"
export WORKER_WORKDIR='C:\Users\Alec\AppData\Local\Temp\rag2205-ScfByQaa\live\workdir'
unset REDIS_URL QDRANT_URL
exec /tmp/rag2205-ScfByQaa/venv313/Scripts/python -m workers > "$LIVE/worker.log" 2>&1
