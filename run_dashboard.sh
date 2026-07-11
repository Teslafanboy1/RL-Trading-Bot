#!/usr/bin/env bash
# TradeCommand dashboard launcher.
#   bash run_dashboard.sh              # serve on http://127.0.0.1:8787
#   bash run_dashboard.sh --set-pin    # set/replace the trade PIN
#
# Remote access comes through Tailscale (see dashboard/README.md):
#   tailscale serve --bg --https=443 http://127.0.0.1:8787
# The server itself binds loopback ONLY — nothing is exposed to the LAN.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate

# one-time icon generation (no-op if present)
[ -f dashboard/static/icons/icon-192.png ] || python3 dashboard/gen_icons.py

if [ "${1:-}" = "--set-pin" ]; then
  exec python3 dashboard/server.py --set-pin
fi

echo "TradeCommand — http://localhost:8787"
echo "  (phone: https://<mac-name>.<tailnet>.ts.net once 'tailscale serve' is on)"
# -u so alerts/poller errors reach the log immediately when run detached
exec python3 -u dashboard/server.py "$@"
