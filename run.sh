#!/usr/bin/env bash
# Launcher — runs the agent via the `claude` CLI (no API key needed).
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
command -v "${CLAUDE_BIN:-claude}" >/dev/null || { echo "claude CLI not found (set CLAUDE_BIN)"; exit 1; }
# Out-of-band alert channel (ntfy/Slack/Discord). Drop the URL into
# .alert_webhook_url (gitignored) once; the bot and watchdog.py both pick it up.
[ -z "${ALERT_WEBHOOK_URL:-}" ] && [ -f .alert_webhook_url ] && export ALERT_WEBHOOK_URL="$(cat .alert_webhook_url)"
export SIGNAL_INTERVAL="${SIGNAL_INTERVAL:-30m}"      # 1-week chart
export WATCHLIST="${WATCHLIST:-SPY,NET,SNOW,COIN}"
export EXECUTION_MODE="${EXECUTION_MODE:-advisory}"   # set 'live' to arm real orders
echo "interval=$SIGNAL_INTERVAL watchlist=$WATCHLIST mode=$EXECUTION_MODE"
# -u (unbuffered) so per-cycle status + critical warnings (forced-exit-not-
# completed, session-limit, manual-sell-required) reach the log immediately when
# the agent is run detached via nohup; line-buffering hid them before.
exec python3 -u agent.py
