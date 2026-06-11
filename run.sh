#!/usr/bin/env bash
# Launcher — runs the agent via the `claude` CLI (no API key needed).
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
command -v "${CLAUDE_BIN:-claude}" >/dev/null || { echo "claude CLI not found (set CLAUDE_BIN)"; exit 1; }
export SIGNAL_INTERVAL="${SIGNAL_INTERVAL:-30m}"      # 1-week chart
export WATCHLIST="${WATCHLIST:-SPY,BTSG,CAT}"
export EXECUTION_MODE="${EXECUTION_MODE:-advisory}"   # set 'live' to arm real orders
echo "interval=$SIGNAL_INTERVAL watchlist=$WATCHLIST mode=$EXECUTION_MODE"
exec python3 agent.py
