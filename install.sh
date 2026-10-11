#!/bin/sh
# Install (or update) Hangar on macOS or Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/jobski/hangar/main/install.sh | sh
#
# A private fork works too: pass a token that can read it in GITHUB_TOKEN (sent with the download and the clone).
#
# Settings (environment variables):
#   HANGAR_DIR=~/hangar      where it goes          HANGAR_REF=main     branch or tag to install
#   HANGAR_NO_START=1        do not start it         HANGAR_NO_AUTOSTART=1   do not start it at login
#   HANGAR_NO_PATH=1         do not put `hangar` on your PATH
# Running it again updates an existing install in place; config.yaml, profiles and stats are kept.
set -eu

REPO=${HANGAR_REPO:-jobski/hangar}
REF=${HANGAR_REF:-main}
DIR=${HANGAR_DIR:-$HOME/hangar}
PORT=${HANGAR_PORT:-9000}
BIN=$HOME/.local/bin
MARK="# added by the Hangar installer"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---- what we need
command -v git >/dev/null 2>&1 || die "git is missing. On macOS run: xcode-select --install"
PY=${PYTHON:-}
if [ -z "$PY" ]; then
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PY=$(command -v "$c"); break
        fi
    done
fi
[ -n "$PY" ] || die "Python 3.10 or newer is needed. On macOS: brew install python@3.12 (or xcode-select --install)"
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
if [ -d "$DIR/.git" ]; then
    say "Updating $DIR to $REF"
    auth -C "$DIR" fetch --quiet origin "$REF"
    git -C "$DIR" checkout --quiet "$REF" 2>/dev/null || git -C "$DIR" checkout --quiet -B "$REF" "origin/$REF"
    git -C "$DIR" merge --quiet --ff-only "origin/$REF" || warn "local changes in $DIR; kept them and did not update"
elif [ -e "$DIR" ]; then
    die "$DIR exists and is not a Hangar checkout; set HANGAR_DIR to install somewhere else"
else
    say "Downloading Hangar ($REF) into $DIR"
    auth clone --quiet --branch "$REF" "$URL" "$DIR" || die "could not clone $URL (private repo? set GITHUB_TOKEN)"
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

# ---- start it, and at every login
if [ -z "${HANGAR_NO_START:-}" ]; then
    sh scripts/start.sh
    i=0
    while [ $i -lt 30 ] && ! curl -fs "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; do sleep 1; i=$((i + 1)); done
    if [ -z "${HANGAR_NO_AUTOSTART:-}" ]; then
        ./hangar autostart on || warn "could not turn on start-at-login; try: hangar autostart on"
    fi
fi

TOKEN=$(cat data/token.txt 2>/dev/null || true)
say "Hangar is installed in $DIR"
echo "    Dashboard: http://localhost:$PORT/?token=$TOKEN"
echo "    Settings:  $DIR/config.yaml   (apply with: scripts/stop.sh && scripts/start.sh)"
echo "    Remove:    sh $DIR/uninstall.sh"
