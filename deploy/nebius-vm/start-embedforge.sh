#!/usr/bin/env bash
# Run the EmbedForge IDE on the VM, using the Nemotron server started by serve-nemotron.sh. The IDE listens on all
# interfaces, so a password is required: the browser asks for it (any user name).
set -euo pipefail

: "${EMBEDFORGE_PASSWORD:?Set EMBEDFORGE_PASSWORD first: the IDE will be reachable from the internet}"
export SELFHOSTED_BASE_URL="${SELFHOSTED_BASE_URL:-http://127.0.0.1:8001/v1}"
export SELFHOSTED_MODEL="${SELFHOSTED_MODEL:-nemotron-3-super}"
export EMBEDFORGE_HOST="${EMBEDFORGE_HOST:-0.0.0.0}"
export EMBEDFORGE_PORT="${EMBEDFORGE_PORT:-8000}"
export EMBEDFORGE_WORKSPACE="${EMBEDFORGE_WORKSPACE:-$HOME/embedforge-demo}"   # a clean workspace for the demo
mkdir -p "$EMBEDFORGE_WORKSPACE"

# the repository root, where .env (TAVILY_API_KEY) and the virtual environment are
cd "$(dirname "$0")/../.."
curl -fsS "$SELFHOSTED_BASE_URL/models" >/dev/null || { echo "No model server at $SELFHOSTED_BASE_URL: start serve-nemotron.sh first."; exit 1; }
exec .venv/bin/embedforge
