#!/bin/sh
# Run the manager in the background. Logs go to logs/manager.log. --foreground runs it in this terminal instead.
cd "$(dirname "$0")/.."
PORT=${HANGAR_PORT:-9000}
mkdir -p logs
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Something already listens on port $PORT (pid $(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN | head -1))."
    exit 0
fi
if [ "$1" = "--foreground" ]; then
    exec .venv/bin/python -m manager serve
fi
nohup .venv/bin/python -m manager serve >logs/manager.log 2>logs/manager.err.log </dev/null &
echo "Hangar started: http://localhost:$PORT"
