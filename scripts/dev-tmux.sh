#!/bin/bash
# Start all development services in a tmux session
# Usage: bash scripts/dev-tmux.sh
#
# Docker runs: postgres, qdrant, redis, backend (Go), rag-api (Python)
# Native runs: frontend (Vite) — for fast HMR during UI development
#
# Panes layout:
#   ┌─────────────────────────────┐
#   │   Docker services (all)     │
#   ├─────────────────────────────┤
#   │   Frontend (npm run dev)    │
#   └─────────────────────────────┘
#
# Ctrl+b then arrow keys to switch panes
# Ctrl+b then d to detach (services keep running)
# tmux attach -t coderag to reattach

SESSION="coderag"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Kill existing session if running
tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"

# Create session — docker compose for infra + backend + workers
tmux new-session -d -s "$SESSION" -n "dev" -c "$ROOT"
tmux send-keys -t "$SESSION" "docker compose up --build postgres qdrant redis backend rag-api" C-m

# Split for frontend (native for fast HMR)
sleep 1
tmux split-window -v -t "$SESSION" -c "$ROOT/services/frontend"
tmux send-keys -t "$SESSION" "npm run dev" C-m

# Give frontend pane more space (60% bottom)
tmux resize-pane -t "$SESSION:dev.1" -y 60%

# Select the docker pane
tmux select-pane -t "$SESSION:dev.0"

# Attach
tmux attach -t "$SESSION"
