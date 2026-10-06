#!/usr/bin/env bash
# run_auto_update.sh
#
# Runs run_validator.sh and restarts it whenever new commits are detected
# on the remote main branch.  All arguments are forwarded to run_validator.sh.
#
# Usage:
#   pm2 start scripts/run_auto_update.sh --name niome-validator --no-autorestart \
#       -- --wallet.name <NAME> --wallet.hotkey <HOTKEY> [--wandb.api_key <KEY>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BRANCH="main"
CHECK_INTERVAL=60   # seconds between git-fetch checks
VENV_DIR="$REPO_DIR/.venv"
VALIDATOR_PID=""

cd "$REPO_DIR"

log() {
    echo "[auto-update $(date '+%Y-%m-%d %H:%M:%S')] $*"
}

start_validator() {
    log "Starting run_validator.sh …"
    bash "$SCRIPT_DIR/run_validator.sh" "$@" &
    VALIDATOR_PID=$!
    log "run_validator.sh PID: $VALIDATOR_PID"
}

stop_validator() {
    if [[ -n "$VALIDATOR_PID" ]] && kill -0 "$VALIDATOR_PID" 2>/dev/null; then
        log "Stopping run_validator.sh (PID $VALIDATOR_PID) …"
        kill "$VALIDATOR_PID"
        wait "$VALIDATOR_PID" 2>/dev/null || true
        VALIDATOR_PID=""
    fi
}

trap 'stop_validator; exit 0' SIGINT SIGTERM EXIT

start_validator "$@"

while true; do
    sleep "$CHECK_INTERVAL"

    log "Checking for updates on origin/$BRANCH …"
    git fetch origin "$BRANCH" --quiet

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "origin/$BRANCH")

    if [ "$LOCAL" != "$REMOTE" ]; then
        log "Update detected ($LOCAL -> $REMOTE). Pulling and restarting …"
        stop_validator

        git pull --rebase origin "$BRANCH"

        if "$VENV_DIR/bin/pip" install -e . --quiet; then
            log "Package reinstalled."
        else
            log "WARNING: pip install failed, continuing with existing install."
        fi

        start_validator "$@"
    else
        log "No changes detected."
    fi
done
