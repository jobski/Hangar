#!/bin/sh
# Stop this manager only; running engines are adopted again on its next start.
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m manager.local --stop-manager
