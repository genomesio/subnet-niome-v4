#!/usr/bin/env bash
# autoupdate_validator.sh
#
# Runs the validator and automatically restarts it whenever changes are
# detected on the remote main branch.  All arguments are forwarded to
# neurons/validator.py.
#
# Usage:
#   pm2 start run_validator.sh --name niome_validator --no-autorestart \
#       -- --wallet <NAME> --wallet-hotkey <HOTKEY> [--wandb.api_key <KEY>]
#
#   Or run interactively (prompts for credentials):
#   ./run_validator.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
VALIDATOR_PID=""

cd "$REPO_DIR"

log() {
    echo "[autoupdate $(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ---------------------------------------------------------------------------
# Collect required arguments — from CLI args or interactively
# ---------------------------------------------------------------------------

WALLET_NAME=""
WALLET_HOTKEY=""
WANDB_API_KEY=""

# Parse --wallet, --wallet-hotkey, --wandb.api_key from script arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wallet)        WALLET_NAME="$2";   shift 2 ;;
        --wallet-hotkey) WALLET_HOTKEY="$2"; shift 2 ;;
        --wandb.api_key) WANDB_API_KEY="$2"; shift 2 ;;
        *) shift ;;
    esac
done

if [[ -z "$WALLET_NAME" ]]; then
    echo "ERROR: --wallet is required." >&2; exit 1
fi

if [[ -z "$WALLET_HOTKEY" ]]; then
    echo "ERROR: --wallet-hotkey is required." >&2; exit 1
fi

VALIDATOR_ARGS=(
    --netuid 55
    --network finney
    --wallet "$WALLET_NAME"
    --wallet-hotkey "$WALLET_HOTKEY"
)
if [[ -n "$WANDB_API_KEY" ]]; then
    VALIDATOR_ARGS+=(--wandb.api_key "$WANDB_API_KEY")
fi

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

export PYTHONPATH="${PYTHONPATH:-}:$REPO_DIR"
log "PYTHONPATH set to: $PYTHONPATH"

if [[ "${VIRTUAL_ENV:-}" != "$VENV_DIR" ]]; then
    log "Project venv is not active."

    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating virtual environment at $VENV_DIR …"
        python3 -m venv "$VENV_DIR"
    fi

    log "Activating virtual environment …"
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"

    log "Syncing python packages …"
    uv sync
else
    log "Project venv already active: $VIRTUAL_ENV"
fi

# ---------------------------------------------------------------------------
# Download and prepare Chromosome 11 reference data
# ---------------------------------------------------------------------------

CHR11_DIR="$REPO_DIR/data"
CHR11_FILE="$CHR11_DIR/chr11.fa"
CHR11_URL="https://ftp.ensembl.org/pub/release-116/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.chromosome.11.fa.gz"

if [[ ! -f "$CHR11_FILE" ]]; then
    log "Chromosome 11 reference not found. Downloading from Ensembl…"
    mkdir -p "$CHR11_DIR"
    
    log "Step 1: Downloading GRCh38 Chromosome 11 assembly…"
    wget -q --show-progress -O "$CHR11_DIR/chr11.fa.gz" "$CHR11_URL"
    
    log "Step 2: Unzipping and renaming to chr11.fa…"
    gunzip -f "$CHR11_DIR/chr11.fa.gz"
    
    log "Chromosome 11 reference prepared at $CHR11_FILE"
else
    log "Chromosome 11 reference already exists at $CHR11_FILE"
fi

# ---------------------------------------------------------------------------

start_validator() {
    log "Starting validator with args: $*"
    "$VENV_DIR/bin/python" "$REPO_DIR/neurons/validator.py" "$@" &
    VALIDATOR_PID=$!
    log "Validator PID: $VALIDATOR_PID"
}

stop_validator() {
    if [[ -n "$VALIDATOR_PID" ]] && kill -0 "$VALIDATOR_PID" 2>/dev/null; then
        log "Stopping validator (PID $VALIDATOR_PID) …"
        kill "$VALIDATOR_PID"
        wait "$VALIDATOR_PID" 2>/dev/null || true
        VALIDATOR_PID=""
    fi
}

# Clean up child process when this script is stopped
trap 'stop_validator; exit 0' SIGINT SIGTERM EXIT

start_validator "${VALIDATOR_ARGS[@]}"

# Wait for the validator process to finish
wait "$VALIDATOR_PID"
