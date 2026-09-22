#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8477}"
if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "port $PORT already in use; stopping the old instance"
  lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi
echo "Interview Copilot -> http://127.0.0.1:$PORT"
exec ./.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port "$PORT" --log-level warning
