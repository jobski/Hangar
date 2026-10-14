#!/bin/sh
# Remove Hangar from macOS or Linux:
#
#   sh ~/hangar/uninstall.sh                 (or: curl -fsSL https://raw.githubusercontent.com/jobski/Hangar/main/uninstall.sh | sh)
#
# Stops the engines it runs and the manager, removes the `hangar` command, the PATH line the installer added and any
# LaunchAgent an earlier version made, and deletes the install folder. Your config.yaml, profiles.yaml and data/ (token, stats) are first
# copied to ~/hangar-backup-<date>; --purge skips that. Model files (models.dir) and engine folders are never touched.
#
#   --yes     do not ask        --purge   no backup        HANGAR_DIR=...   a different install folder
set -eu

DIR=${HANGAR_DIR:-$HOME/hangar}
PORT=${HANGAR_PORT:-9000}
MARK="# added by the Hangar installer"
YES=""; PURGE=""
for a in "$@"; do
    case "$a" in --yes|-y) YES=1 ;; --purge) PURGE=1 ;; *) echo "unknown option $a" >&2; exit 2 ;; esac
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ -f "$DIR/manager/__init__.py" ] || { echo "No Hangar install in $DIR (set HANGAR_DIR)." >&2; exit 1; }
if [ -z "$YES" ]; then
    printf 'Remove Hangar from %s%s? [y/N] ' "$DIR" "$([ -n "$PURGE" ] && echo ' including its config and stats')"
    # piped from curl, stdin is the script: ask the terminal
    read -r ans </dev/tty || ans=""
    case "$ans" in y|Y|yes|YES) ;; *) echo "Nothing removed."; exit 0 ;; esac
fi

cd "$DIR"
DIR=$(pwd -P)
# the Hangar on the port is this install when it accepts this install's token (each install makes its own)
ours() {
    [ -f "$DIR/data/token.txt" ] || return 1
    curl -fs -o /dev/null -H "Authorization: Bearer $(cat "$DIR/data/token.txt")" "http://127.0.0.1:$PORT/api/jobs?limit=1"
}
# only what belongs to this install: another Hangar (a second copy, or a different port) is left alone
PID=$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
if [ -n "$PID" ] && ours; then
    say "Stopping the engines and the manager"
    ./hangar stop all >/dev/null 2>&1 || true
    kill "$PID" 2>/dev/null || true
fi

say "Removing the command, the PATH line and any old LaunchAgent"
AGENT="$HOME/Library/LaunchAgents/dev.hangar.manager.plist"
if [ -f "$AGENT" ] && grep -qF "$DIR" "$AGENT"; then
    launchctl bootout "gui/$(id -u)/dev.hangar.manager" 2>/dev/null || true
    rm -f "$AGENT"
fi
LINK="$HOME/.local/bin/hangar"
if [ -L "$LINK" ] && case "$(readlink "$LINK")" in "$DIR"/*) true ;; *) false ;; esac; then rm -f "$LINK"; fi
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    if [ -f "$rc" ] && grep -qF "$MARK" "$rc"; then
        grep -vF "$MARK" "$rc" > "$rc.hangar-tmp" && cat "$rc.hangar-tmp" > "$rc" && rm -f "$rc.hangar-tmp"
    fi
done

if [ -z "$PURGE" ]; then
    BK="$HOME/hangar-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK"
    for f in config.yaml profiles.yaml data; do [ -e "$f" ] && cp -R "$f" "$BK/"; done
    say "Kept your settings and stats in $BK"
fi
cd "$HOME"
rm -rf "$DIR"
say "Hangar removed. Model files and engine folders (e.g. ~/models, ~/llama.cpp) were left in place."
