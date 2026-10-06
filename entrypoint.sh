#!/usr/bin/env bash
# entrypoint.sh
#
# Installs PM2 (if not present) and starts the validator watcher under PM2.
#
# Usage:
#   chmod +x entrypoint.sh
#   ./entrypoint.sh --wallet <NAME> --wallet-hotkey <HOTKEY> [--wandb.api_key <KEY>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
    echo "[entrypoint $(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ---------------------------------------------------------------------------
# Parse args or collect credentials interactively
# ---------------------------------------------------------------------------

WALLET_NAME=""
WALLET_HOTKEY=""
WANDB_API_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --wallet)        WALLET_NAME="$2";   shift 2 ;;
        --wallet-hotkey) WALLET_HOTKEY="$2"; shift 2 ;;
        --wandb.api_key) WANDB_API_KEY="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$WALLET_NAME" && -z "$WALLET_HOTKEY" ]]; then
    read -rp "Validator's wallet name: " WALLET_NAME
    while [[ -z "$WALLET_NAME" ]]; do
        echo "Validator's wallet name cannot be empty."
        read -rp "Validator's wallet name: " WALLET_NAME
    done

    read -rp "Validator's wallet hotkey: " WALLET_HOTKEY
    while [[ -z "$WALLET_HOTKEY" ]]; do
        echo "Validator's wallet hotkey cannot be empty."
        read -rp "Validator's wallet hotkey: " WALLET_HOTKEY
    done

    read -rp "WANDB API Key (leave blank to skip): " WANDB_API_KEY
else
    [[ -z "$WALLET_NAME" ]]   && { echo "Error: --wallet is required.";        exit 1; }
    [[ -z "$WALLET_HOTKEY" ]] && { echo "Error: --wallet-hotkey is required."; exit 1; }
fi

VALIDATOR_ARGS=(--wallet "$WALLET_NAME" --wallet-hotkey "$WALLET_HOTKEY")
if [[ -n "$WANDB_API_KEY" ]]; then
    VALIDATOR_ARGS+=(--wandb.api_key "$WANDB_API_KEY")
fi

# ---------------------------------------------------------------------------

# Install PM2 globally if not already installed
if ! command -v pm2 &>/dev/null; then
    echo "[entrypoint] PM2 not found. Installing via npm …"
    if ! command -v npm &>/dev/null; then
        echo "[entrypoint] npm not found. Installing Node.js via apt …"
        sudo apt-get update -qq
        sudo apt-get install -y -qq nodejs npm
        echo "[entrypoint] Node.js/npm installed."
    fi
    npm install -g pm2
    echo "[entrypoint] PM2 installed."
else
    echo "[entrypoint] PM2 already installed: $(pm2 --version)"
fi

echo "[entrypoint] Starting niome-validator via PM2 …"
if pm2 describe niome-validator &>/dev/null; then
    echo "[entrypoint] Deleting existing niome-validator process …"
    pm2 delete niome-validator
fi
pm2 start "$SCRIPT_DIR/scripts/run_auto_update.sh" \
    --name niome-validator \
    --no-autorestart \
    -- "${VALIDATOR_ARGS[@]}"

pm2 logs niome-validator
