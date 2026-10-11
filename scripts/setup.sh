#!/bin/sh
# First-time setup on macOS/Linux: Python environment, config.yaml from the macOS example, and the API token.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
    echo "Creating the Python environment (.venv) ..."
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
if [ ! -f config.yaml ]; then
    EXAMPLE=config.example.yaml
    [ "$(uname)" = "Darwin" ] && EXAMPLE=config.example.mac.yaml
    cp "$EXAMPLE" config.yaml
    echo "Made config.yaml from $EXAMPLE - check the paths in it."
fi
chmod +x hangar scripts/*.sh
.venv/bin/python -c "from manager import config; c = config.load(); print('Token:', config.token(c))"
echo "Done. Start it with scripts/start.sh, and ./hangar autostart on to run it at login."
