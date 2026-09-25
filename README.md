# Autonomous trading agent

An autonomous trading bot for a small Robinhood account. It started in June 2026 as a
fully LLM-driven trader (Claude doing research, execution and self-rewriting). After
two months of live results and backtests, it now works like this:

- **A deterministic momentum rotation (RX-3) trades the real account.** It's plain
  Python, and it's the same math the backtest ran.
- **Claude is the transport and the watchdog.** It reads the broker and places one
  named order at a time. It never decides what to buy or sell on the live book.
- **New ideas run on paper first** (the Claude Desk engines), each graded on a
  scorecard before any real money goes behind them.

| | |
|---|---|
| Broker | Robinhood Agentic account `696283985`, **limited margin** (unsettled funds spendable; no borrowing, no leverage) |
| Runs on | Oracle VM (`trading-bot-deploy.service`); Mac app + dashboard as the operator console |
| Live strategy | RX-3 rotation, full-deploy, **top 4** names |
| Risk rails | −10% hard stop · 25% trailing stop · 10% monthly-drawdown kill-switch · independent watchdog |
| Model access | `claude` CLI (Claude Code) with the `robinhood-cli` MCP. No API key needed. |

`CLAUDE.md` is the full engineering reference, including the incident history behind
each rule.

## How the live book trades (RX-3)

`rotation_engine.py` is a pure function. Once per market day it ranks a fixed universe
on momentum (`0.5·r1m + 0.3·r1w + 0.2·r6m`, must be above its 200-day average) using
closes **through yesterday only**, then picks a target book. The live config is
`full_deploy=true, top_n=4`: 100% invested, split across the four strongest names.
Leveraged 3x ETFs get a size haircut.

Every 5 minutes during market hours, `agent.run_rotation_cycle()`:

1. writes a heartbeat, checks the `HALT` kill-switch and the operator `PAUSE`
2. reads the broker (authoritative; a failed read places **nothing**)
3. fires **protective exits first**: hard stop (−10% from entry) and trailing stop
   (−25% from the post-entry high)
4. reconciles the book toward the day's target: sells, then re-reads buying power and
   buys with the proceeds in the same cycle
5. records fills from the broker, never from what the model says it did

Buys are capped by the broker's `buying_power` taken verbatim, with a daily order cap
and a `do_not_trade` list that blocks buys but never exits.

**What the research says** (all in `research/`, 10y backtests):
- Trading faster or on shorter timeframes makes it worse (`edge_lab4_speed.py`). The
  edge is multi-week momentum persistence.
- The lever that moves returns is how much capital is deployed, not how often it
  trades.
- 20%/month is not reachable. The best of 16 leveraged variants averaged ~2.8%/month
  at a 93% drawdown (`edge_lab5_moonshot.py`). Unlevered top-4 full-deploy (~26%/yr,
  ~50% max drawdown over 10y) is the best sane config.
- Gaps beat stops: a stop can't sell while the market is closed, so stops have
  filled around −14.5% on a −10% trigger. Position sizing is the gap defense.

## Safety layers

- **Kill-switch** (`risk_guard.py`): a ≥10% drawdown from the month's peak equity
  writes `HALT`, flattens the book, and refuses to trade until the operator deletes it.
- **Watchdog** (`watchdog.py`, every 5 min, independent of the bot): alerts on a stale
  heartbeat, a symbol trading through its stop, a `HALT`, or **BLIND**. BLIND means a
  failed morning health check or repeated failed broker reads; it was added after
  the bot's Claude login expired on 2026-08-31 and it traded blind for three weeks.
- **Alerts**: put an ntfy / Slack / Discord webhook URL in `.alert_webhook_url`
  (gitignored). Without it, alerts only reach a log file.
- **Operator controls** (`control/`): `PAUSE`, `do_not_trade.json`,
  `stop_overrides.json`, per-symbol manual locks. All are honored every cycle.

## Paper engines — Claude Desk

These run alongside the live book with zero real orders. Every entry is pre-registered
(entry / stop / target / expiry) on `shadow/desk_scorecard.jsonl` and graded daily.

- **Engine B — both-ways trend** (`desk/engine_b.py`): momentum across stocks,
  commodities, crypto ETFs and inverse ETFs, top 3, rebalanced weekly. Runs long-only
  and with-inverse variants side by side.
- **Engine A — Crowd Hunter** (`desk/engine_a.py`): paper options. It buys calls on
  stocks surging on heavy volume in an up-trend and puts on crowd favourites that are
  cracking. One Claude web-search call a day must confirm a real catalyst. Contracts
  are priced at the real bid/ask.
- **RX-4 paper tracker** (`shadow/rx4_paper.json`): a full-deploy top-2 comparison book.

## Run

```bash
pip install -r requirements.txt
bash run.sh                        # advisory mode — read-only, no real orders
EXECUTION_MODE=live bash run.sh    # live mode — places real orders
bash run_dashboard.sh              # operator dashboard on 127.0.0.1:8787
```

RX-3 live is armed by `strategy.json → rotation.mode: "live"` **and**
`rotation.live.enabled: true`. Set either to false and the legacy discretionary LLM loop
returns on the next cycle.

`strategy/strategy.json` is deploy-protected: the VM's copy wins. Change live settings
on the VM with the scripts, which bump the version, snapshot history and log the change:

```bash
python3 scripts/apply_rx4_sizing.py --full-deploy --top-n 4
```

```bash
python3 scripts/apply_stop_loss.py --show
```

Key env vars:

| Variable | Default | Effect |
|---|---|---|
| `EXECUTION_MODE` | `advisory` | `live` arms real orders |
| `POLL_MINUTES` | `5` | Cycle frequency during market hours (buys stop-breach latency, not more trades) |
| `MODEL` | `claude-opus-4-8` | Research / postmortem / Engine A catalyst calls |
| `CHECK_MODEL` | `claude-haiku-4-5-20251001` | Routine broker reads and order placement |
| `ALERT_WEBHOOK_URL` | _(unset)_ | Out-of-band alerts (or use `.alert_webhook_url`) |

## File structure

```
agent.py              core loop: scheduling, broker reconciliation, RX-3 live, desk passes
rotation_engine.py    RX-3 brain: pure, deterministic, identical to the backtest
risk_guard.py         heartbeat + monthly-drawdown kill-switch
watchdog.py           independent dead-man / stop / BLIND alerts
usage_governor.py     keeps Claude usage inside the rolling 5-hour window
desk/                 Claude Desk paper engines (A, B), scorecard, playbook
options_shadow.py     paper-options quote + P&L engine
signals.py            EMA ribbon (8/13/21/55, plain EMA) — used by the legacy loop
strategy/             strategy.json (live config + learned state) + version history
skills/               prompts for the legacy discretionary loop + learning loop
scripts/              operator scripts for deploy-protected config changes
research/             backtests and edge labs (every new idea must win here first)
shadow/               paper books and the desk scorecard
dashboard/            TradeCommand server (JSON API + PWA)
RL Trading Bot/       native SwiftUI Mac/iPhone operator app
```

## The legacy discretionary loop (retired while RX-3 is live)

The original design is still in the code and comes back if RX-3 live is disarmed:
pre-market Opus research (`skill_1`) picks stocks, an execution turn (`skill_2`) trades
them on an EMA-ribbon signal, and every closed trade triggers a postmortem or victory
analysis that feeds a strategy/skill rewriter (`skill_5`). Stop-loss and trailing-stop
exits on the live book still go through that postmortem pipeline. Mechanical rotation
exits don't.
