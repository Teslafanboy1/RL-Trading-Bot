# Autonomous self-improving trading agent

A fully autonomous trading agent. Claude drives everything — research, execution,
learning, and strategy updates — using the Robinhood Agentic MCP to buy and sell
stocks with real money, plus web search for news / Reddit / RSS.

- Platform: Robinhood Agentic Account (true cash, account `696283985`)
- Starting capital: $91
- Settlement: T+1 — after a sell, cash takes 1 business day to settle before it
  can be redeployed. Never buy with unsettled funds.
- North star: 100% monthly portfolio return.

## The ribbon strategy (core buy/sell signal)

Four **plain EMA** lines matching the TradingView chart the strategy is read from:
blue = EMA(8), green = EMA(13), yellow = EMA(21), red = EMA(55). Lengths are measured in
**bars** at the active chart interval (`SIGNAL_INTERVAL`): on a 30m chart, 55 bars = 55
half-hours. (The chart's "Three Moving Averages [AdventTrading]" indicator is labeled
"TEMA" but its Pine source computes plain `ema()` — do **not** use triple-EMA here; it
overshoots in rallies and inverts the signal, which liquidated the book on 2026-06-15.)

- **BUY**  — the 55 (red) crosses below ALL of 8/13/21 and becomes the LOWEST line.
- **SELL** — the 55 (red) crosses above ALL of 8/13/21 and becomes the HIGHEST line.
  For a held position, SELL **state** itself triggers the sell — no fresh cross required.
- **HOLD** — while red stays the lowest line, hold the position.
- **WAIT** — while red is above all 3 (downtrend), do nothing.

## When the agent sells

Three independent sell triggers — any one fires a sell:

1. **Ribbon EXIT** — the 55 (red EMA) sits above all of 8/13/21: SELL state on a held
   position sells immediately, whether the cross happened this bar or an earlier one.
2. **Stop-loss** — position falls ≥10% below entry price. Hard rule, overrides everything.
3. **Thesis break / confidence decay** — intraday news check finds a thesis-breaking event
   (bad earnings, CEO departure, regulatory reversal, sector shock) or re-scores confidence
   below 60. This fires every cycle and is the only mechanism that can sell before the
   lagging EMA or stop-loss reacts.

## The 4 phases

**A — Pre-market research (8:55 AM ET, every trading day)** (`skill_1_research.md`)

Runs automatically 35 minutes before market open. When you start the script while the
market is closed, press `w` to enter the daily schedule — it wakes at **4:25 AM ET** for
a tiny preflight system check (which deliberately *anchors* the rolling 5-hour Claude
session window so it expires just before the bell), runs research at **8:55 AM ET**
inside that window, then starts the trading loop at 9:30 AM on a **fresh** window.
Uses `claude-opus-4-8`. See "Usage governor" in `CLAUDE.md` for why the timing matters.

1. Goal-pace check: compute how much each position needs to move to matter.
2. **Open-position review**: re-score every held position 0–100 from scratch. Compare each
   position's weight to its confidence band maximum. Trim/exit over-weighted or
   confidence-decayed positions; free that capital for redeployment.
3. Scan 60+ candidates (static high-beta universe + live MCP movers/watchlists).
4. Score each candidate 0–100 from EMA signal, momentum, and multi-source agreement.
5. Rank all existing positions and new candidates together by confidence; allocate
   top-down so capital always sits in the highest-conviction ideas.

Output → `research/weekend_picks_YYYY-MM-DD.md` — automatically injected into every
subsequent execution cycle so the trading loop acts on the picks immediately at open.
Each pick must be written as a `### #N — SYMBOL | Confidence: XX/100` heading: the agent
parses that exact format both to save the file and to extract the tickers execution
watches. A research run that produces no parseable picks is never silently dropped — the
agent logs a `WARNING` and preserves the raw text at `research/unsaved_*.md` so the plan
is recoverable instead of execution quietly falling back to the prior day's picks.

**B — Execution, Mon + intraweek** (`skill_2_execution.md`)
- Every cycle: thesis integrity check on all held positions (web search for breaking news;
  sell immediately if thesis broken or confidence decays below 60).
- Smart skip: if all EMA signals are flat, no stop-loss is triggered, and the
  last **execution** cycle was less than `NEWS_CHECK_HOURS` (default 4h) ago, skip the
  model call entirely (0 tokens). Otherwise call the model to execute buys/sells.
  (The gate keys off the last execution cycle, not the last model call of any kind,
  so the pre-market research run at 9:23 AM can't suppress the 9:30 open.)
- Buys require: EMA BUY/HOLD + settled cash + passing news check + no blackout window.

**C — Midweek re-score (Wed, 12:00 PM ET)** (`skill_3_midweek.md`)

Fires automatically once at noon on Wednesday during market hours. Uses `claude-opus-4-8`.

- Re-scores every open position 0–100 from scratch (not qualitative — same full formula
  as weekend research: EMA health, news search, momentum).
- Compares current portfolio weights to confidence bands; trims/exits over-weighted or
  decayed positions and redeploys freed capital via the Robinhood MCP in the same cycle.
- Reports the explicit T+1 settlement schedule.

Output → `research/midweek_review_YYYY-MM-DD.md` (existence of this file prevents the
review from re-firing on subsequent 15-minute polls the same day).

**D — Post-trade analysis (19:35 ET maintenance drain)** (`skill_4_postmortem.md` / `skill_4b_victory.md`)
- Loss → postmortem (root cause, which source misled, preventive rule).
- Win → victory (repeatable feature, guard against crediting luck, source weight nudge).
- Followed by strategy + skill rewrite and confidence calibration.
- A close during market hours **queues** the analysis (`logs/analysis_queue.jsonl`)
  instead of running it inline: it is an Opus + web-search call, and firing it the
  moment a position closes spent execution-window budget on retrospection. The drain
  runs after the close, in a session window of its own, along with the skill_5
  strategy rewrites. A close outside market hours is analysed immediately.

## Capital allocation (of total portfolio value)

Applied portfolio-wide, not just to new cash — so existing positions are always sized correctly:

| Confidence | Max allocation |
|---|---|
| 90–100 | 30% |
| 75–89 | 20% |
| 60–74 | 15% |
| Below 60 | Exit if held / skip if new |

Always keep a 10% cash reserve. Max 30% in any single position.

## Run

```bash
pip install -r requirements.txt
bash run.sh                        # advisory mode — read-only, no real orders
EXECUTION_MODE=live bash run.sh    # live mode — places real orders
```

No `ANTHROPIC_API_KEY` needed — the `claude` CLI supplies the model and the authorized
`robinhood-cli` MCP connection. Set `CLAUDE_BIN` if the CLI is not in PATH.

Key env vars:

| Variable | Default | Effect |
|---|---|---|
| `EXECUTION_MODE` | `advisory` | `live` arms real orders |
| `SIGNAL_INTERVAL` | `1h` | Bar width for EMA |
| `POLL_MINUTES` | `15` | Cycle frequency during market hours |
| `NEWS_CHECK_HOURS` | `4` | Force a news/thesis check at least every N hours even on flat EMA days |
| `MODEL` | `claude-opus-4-8` | Research / postmortem calls |
| `CHECK_MODEL` | `claude-haiku-4-5-20251001` | Routine market-hours checks |

## File structure

```
skills/
  skill_0_orchestrator.md       coordinates every cycle
  skill_1_research.md           weekend scanner + open-position reallocation
  skill_2_execution.md          Mon + intraweek executor + thesis integrity check
  skill_3_midweek.md            Wednesday full re-score + reallocation
  skill_4_postmortem.md         loss analyst
  skill_4b_victory.md           win analyst
  skill_5_strategy_rewriter.md  rewrites strategy.json + skills after every trade
  skill_6_pattern_detector.md   quarterly systemic review
  history/                      versioned skill-file snapshots — rollback anytime
strategy/
  strategy.json                 config + learned state (versioned)
  history/                      snapshot on every change — rollback anytime
research/                       weekend_picks / midweek_review / agent runs / skill5_run logs
                                + strategy_rewrite_queue.md (rewrite work queue)
postmortems/                    postmortem_NNN.md / victory_NNN.md
agent.py                        core loop, scheduling, skip logic, MCP wiring
signals.py                      ribbon computation, plain EMA 8/13/21/55 (Yahoo or local CSV)
trade_log.json                  open positions, closed trades, learning links
```

## The learning loop

After every close: loss → skill_4, win → skill_4b. Each close also appends an entry to
`research/strategy_rewrite_queue.md`. At the end of each cycle, `process_strategy_rewrite_queue()`
pulls one un-done entry and runs **skill_5**, which updates `strategy.json` and any skill that
needs improving. The model runs headless with no file-write tool, so `agent.py` parses skill_5's
output text and applies the edits itself (the last fenced ```json block becomes the new
`strategy.json`; `## SKILL FILE UPDATE` blocks rewrite skill files). One entry per cycle, and the
whole step is isolated in try/except so a bad rewrite can never crash the trading loop.

Source weights update after every trade; confidence scores are calibrated against real outcomes.
A core rule changes only after 3+ similar outcomes. Every change is versioned for rollback —
strategy snapshots in `strategy/history/`, skill-file snapshots in `skills/history/` (`version_skill_file()`
snapshots a skill before each edit, a baseline `v001` of every skill is written on first run, and
`rollback_skill()` restores any version). Rollback = swap a snapshot back. Quarterly, skill_6 reads all postmortems at once for the biggest strategic pivots.
