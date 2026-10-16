#!/bin/sh
# Install (or update) Hangar on macOS or Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/jobski/Hangar/main/install.sh | sh
#
# A private fork works too: pass a token that can read it in GITHUB_TOKEN (sent with the download and the clone).
#
# Settings (environment variables):
#   HANGAR_DIR=~/hangar      where it goes          HANGAR_REF=main     branch or tag to install
#   HANGAR_NO_START=1        do not start it         HANGAR_NO_PATH=1        do not put `hangar` on your PATH
# It does not set Hangar to start at login: security tools such as SentinelOne quarantine LaunchAgents made by
# scripts. Start it with scripts/start.sh after a restart (or add that to Login Items yourself).
# Running it again updates an existing install in place; config.yaml, profiles and stats are kept.
set -eu

REPO=${HANGAR_REPO:-jobski/Hangar}
REF=${HANGAR_REF:-main}
DIR=${HANGAR_DIR:-$HOME/hangar}
PORT=${HANGAR_PORT:-9000}
BIN=$HOME/.local/bin
MARK="# added by the Hangar installer"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---- what we need
APT="sudo apt install git python3 python3-venv curl lsof"
command -v curl >/dev/null 2>&1 || die "curl is missing. On Debian/Ubuntu run: $APT"
command -v lsof >/dev/null 2>&1 || die "lsof is missing. On Debian/Ubuntu run: $APT"
command -v git >/dev/null 2>&1 || die "git is missing. On macOS run: xcode-select --install; on Debian/Ubuntu run: $APT"
PY=${PYTHON:-}
if [ -z "$PY" ]; then
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PY=$(command -v "$c"); break
        fi
    done
fi
[ -n "$PY" ] || die "Python 3.10 or newer is needed. On macOS: brew install python@3.12 (or xcode-select --install); on Debian/Ubuntu run: $APT"
say "Using $("$PY" --version) at $PY"

# ---- get the code (a token in GITHUB_TOKEN reaches a private repo; it is not stored in the clone)
URL="https://github.com/$REPO.git"
auth() {
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        git -c "http.extraHeader=Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')" "$@"
    else
        git "$@"
    fi
}
# the Hangar on the port is this install when it accepts this install's token (each install makes its own)
ours() {
    [ -f "$DIR/data/token.txt" ] || return 1
    curl -fs -o /dev/null -H "Authorization: Bearer $(cat "$DIR/data/token.txt")" "http://127.0.0.1:$PORT/api/jobs?limit=1"
}
RESTART=""
if [ -d "$DIR/.git" ]; then
    say "Updating $DIR to $REF"
    git -C "$DIR" config core.fileMode false          # executable bits set here are not "local changes"
    ours && RESTART=1
    auth -C "$DIR" fetch --quiet origin "$REF"
    git -C "$DIR" checkout --quiet "$REF" 2>/dev/null || git -C "$DIR" checkout --quiet -B "$REF" "origin/$REF"
    if ! git -C "$DIR" merge --quiet --ff-only "origin/$REF" 2>/dev/null; then
        # not a fast-forward: either real local edits (kept), or the repo's history was rewritten upstream (nothing
        # local to lose: config.yaml, data/ and models are not tracked), in which case take GitHub's version
        if git -C "$DIR" diff --quiet && git -C "$DIR" diff --cached --quiet; then
            git -C "$DIR" reset --quiet --hard "origin/$REF" && say "Took the repository's rewritten history"
        else
            warn "local changes in $DIR; kept them and did not update"
        fi
    fi
elif [ -e "$DIR" ]; then
    die "$DIR exists and is not a Hangar checkout; set HANGAR_DIR to install somewhere else"
else
    say "Downloading Hangar ($REF) into $DIR"
    auth clone --quiet --branch "$REF" "$URL" "$DIR" || die "could not clone $URL (private repo? set GITHUB_TOKEN)"
    git -C "$DIR" config core.fileMode false
fi
cd "$DIR"

# ---- Python environment, config.yaml, token
PYTHON="$PY" sh scripts/setup.sh
chmod +x hangar scripts/*.sh install.sh uninstall.sh 2>/dev/null || true

# ---- `hangar` on the PATH
if [ -z "${HANGAR_NO_PATH:-}" ]; then
    mkdir -p "$BIN"
    ln -sf "$DIR/hangar" "$BIN/hangar"
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *)
            for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
                if [ -f "$rc" ] || [ "$rc" = "$HOME/.zshrc" ]; then
                    grep -qF "$MARK" "$rc" 2>/dev/null || printf '\nexport PATH="$HOME/.local/bin:$PATH"  %s\n' "$MARK" >> "$rc"
                fi
            done
            warn "Open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\") to use the hangar command" ;;
    esac
fi

# ---- start it (an updated Hangar that was running is restarted, so the new code runs)
if [ -n "$RESTART" ]; then
    say "Restarting Hangar to run the update (engines keep running)"
    PID=$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    [ -n "$PID" ] && kill "$PID" 2>/dev/null
    i=0
    while [ $i -lt 15 ] && lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do sleep 1; i=$((i + 1)); done
fi
if [ -z "${HANGAR_NO_START:-}" ] || [ -n "$RESTART" ]; then
    sh scripts/start.sh
    i=0
    while [ $i -lt 30 ] && ! curl -fs "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; do sleep 1; i=$((i + 1)); done
fi

TOKEN=$(cat data/token.txt 2>/dev/null || true)
say "Hangar is installed in $DIR"
echo "    Dashboard: http://localhost:$PORT/?token=$TOKEN"
echo "    Settings:  $DIR/config.yaml   (apply with: scripts/stop.sh && scripts/start.sh)"
echo "    After a restart: sh $DIR/scripts/start.sh"
echo "    Remove:    sh $DIR/uninstall.sh"
