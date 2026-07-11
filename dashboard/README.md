# TradeCommand — investing command center

Reads every state file the trading bot writes, polls live market data, talks to
the Robinhood MCP, and serves a dark-mode PWA you can use from the Mac and the
iPhone. **It can place real orders and flip bot controls** — everything
dangerous sits behind your PIN.

## One-time setup (~15 min)

### 1. PIN (required before any control works)
```bash
bash run_dashboard.sh --set-pin
```
Stored as a salted PBKDF2 hash in `dashboard/.auth.json` (chmod 600,
gitignored). 5 wrong attempts → 15-minute lockout.

### 2. Start the server
```bash
bash run_dashboard.sh          # http://localhost:8787 (loopback ONLY)
```
Optional — keep it running across reboots:
```bash
cp dashboard/com.tradingbot.dashboard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tradingbot.dashboard.plist
```
Note: the Mac must be awake for real-time data (System Settings → prevent
sleep on power, or run `caffeinate -s` — same constraint the bot already has).

### 3. Tailscale (phone access, HTTPS, from anywhere)
1. Install Tailscale on the Mac (App Store or `brew install --cask tailscale`)
   and on the iPhone (App Store). Sign both into the same tailnet.
2. In the [admin console](https://login.tailscale.com/admin/dns): enable
   **MagicDNS** and **HTTPS certificates**.
3. On the Mac:
   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8787
   ```
4. iPhone Safari → `https://<mac-name>.<tailnet>.ts.net` → Share →
   **Add to Home Screen**. That's the PWA.

The server binds `127.0.0.1` only; the *only* ways in are localhost and your
tailnet. No LAN or internet exposure.

### 4. Robinhood broker path (choose automatically)
```bash
python3 -m dashboard.rh_login        # one-time browser OAuth to agent.robinhood.com
python3 -m dashboard.rh_login --check
```
- **Works** → `direct-mcp` mode: ~1s deterministic orders, free 60s account
  polling, zero Claude-plan usage. Tokens in `dashboard/.rh_tokens.json`
  (chmod 600, gitignored).
- **Rejected** (Robinhood may not allow third-party OAuth clients) → the
  dashboard silently uses `claude-cli` mode: the bot's proven `claude -p` path.
  Reads then come from cached bot snapshots + the *Broker refresh* button
  (each refresh/order costs a small model call; there is deliberately **no
  background polling** in this mode).

The active mode shows in the top-bar `broker:` chip.

## Using it

- **Arm** (🔒 chip or any danger action) → PIN → 5-minute armed window.
- **Orders**: New order / Close / Add → *Preview* (broker pre-trade review +
  warnings: do-not-trade list, buying power vs T+1 settled cash, bot cycle
  running, market closed) → **hold-to-confirm**. Every action lands in the
  manual journal (`logs/manual_actions.jsonl`) — Console tab answers "did I
  do that or did the bot".
- **Pause bot** (`control/PAUSE`): bot skips model turns and new entries but
  still runs stop-loss/trailing forced exits, kill-switch checks, and broker
  bookkeeping every cycle. Resume deletes the file.
- **Halt / Clear halt**: writes/deletes the same `HALT` file as the
  kill-switch. ⚠ While halted the bot does **nothing** — including its own
  stop enforcement; only the independent watchdog still alerts.
- **Stop overrides** (Risk tab): per-symbol absolute stop price, stop %, or
  trailing %. Honored by the bot's alert checks next cycle and mirrored into
  the watchdog's `logs/stops.json`.
- **Do-not-trade** (Risk tab): blocks bot BUYs on a symbol (sells still
  allowed) and stops BUY-signal wakeups for it.

## Clients & API

The **native Mac app** (`RL Trading Bot/`, SwiftUI, also builds for iPhone) is
the primary client — the web PWA in `dashboard/static/` still works but is no
longer the main surface. The app's sidebar is organized into four sections
backed by these GET endpoints (all JSON, read-open on the tailnet):

| Section | Tab → endpoint |
|---|---|
| Trading | Overview `/api/overview` · Positions `/api/overview`+`/api/risk` · Orders `/api/orders?days=` · Market `/api/market` · Analyzer `/api/symbol?sym=&interval=` |
| Intelligence | Thinking `/api/thinking` · Signals `/api/signals` · Screener `/api/screen?symbols=&fresh=` · News `/api/news` · Calendar `/api/calendar` |
| Strategy & Learning | Strategy `/api/strategy` · Learning `/api/learning` · Library `/api/library` · Shadow `/api/shadow` · RX-3 `/api/rx3` |
| Operations | Performance `/api/performance` · Trades `/api/trades` · Risk `/api/risk` · Activity `/api/logs` · Health `/api/health` · Console `/api/actions`+`/api/alerts` |

Notes: `/api/orders?days=N` (N>1) fetches deeper order history live —
direct-mcp broker path only (the claude-cli path would cost a model call per
refresh, so it stays snapshot-of-today). `/api/screen?fresh=1` bypasses the
10-minute screen cache. `/api/symbol` overlays the bot's exact EMA(8/13/21/55)
ribbon plus stop/trail/entry lines when the symbol is held. Document text
comes from `/api/postmortem?file=` and `/api/research_file?file=`.

## How manual actions and the bot coordinate

Chosen model: **journal + reconcile**. Your orders fire immediately; the bot's
broker-truth reconciliation absorbs them next cycle (your sell → position
closed with `exit_reason: manual`, **no postmortem fired**; your buy → adopted
at broker cost). While your order is in flight the dashboard holds a
per-symbol lock (`control/locks/`) that the bot's force_sell defers to for one
cycle; the confirm screen warns when a bot cycle is running right now.

## Files this adds

| Path | What |
|---|---|
| `dashboard/server.py` | stdlib HTTP server + API + pollers (`--set-pin`, `--port`) |
| `dashboard/readers.py` | parsers for every bot state file (missing-safe) |
| `dashboard/quotes.py` | Yahoo quotes/ribbons/news with TTL caches (reuses signals.py) |
| `dashboard/broker.py` | DirectMCP + ClaudeCLI drivers behind one facade |
| `dashboard/mcp_client.py` | stdlib MCP streamable-HTTP client + OAuth 2.1 login |
| `dashboard/controls.py` | PAUSE/HALT/DNT/stop-overrides/locks + PIN armory + journal |
| `dashboard/static/` | the PWA (vanilla JS, hand-rolled SVG charts, no build step) |
| `control/`, `logs/manual_actions.jsonl`, `logs/equity_curve.jsonl`, `logs/cycle_status.json` | runtime control/journal files (gitignored) |

`python3 test_dashboard.py` runs the dashboard + control-plane suite.
