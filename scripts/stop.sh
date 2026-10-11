#!/bin/sh
# Stop the manager. The engines it runs keep running; it picks them up again when it starts.
PORT=${HANGAR_PORT:-9000}
PID=$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
if [ -z "$PID" ]; then echo "The manager is not running."; exit 0; fi
kill "$PID"
echo "Hangar stopped (engines left running)."
