#!/usr/bin/env bash
# The backend, as docs/local-development.md runs it: its .env sourced (with the
# file's CRLF line endings stripped, or every value would keep a trailing \r),
# then DATABASE_URL overridden to the scratch database as rag_doc_app, the
# internal listener on a free loopback port, the public one on another, and
# Redis pointed at this session's scratch Redis. Nothing here echoes a value.
set -euo pipefail
LIVE=/tmp/rag2205-ScfByQaa/live
. "$LIVE/ports.env"
cd /c/Users/Alec/Desktop/code/testtGSD/.claude/worktrees/agent-ab4bd33d2c92fa0bd/services/backend
set -a
. <(tr -d '\r' < /c/Users/Alec/Desktop/code/testtGSD/services/backend/.env)
set +a
DATABASE_URL="$(cat "$LIVE/app_dsn")"
export DATABASE_URL
export INTERNAL_ADDR="127.0.0.1:${INTERNAL_PORT}"
export PORT="${PUBLIC_PORT}"
export REDIS_URL="redis://127.0.0.1:64710/14"
export LOG_FORMAT=json
exec "$LIVE/backend.exe" > "$LIVE/backend.log" 2>&1
