# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the agent

```bash
pip install -r requirements.txt       # installs schedule; the claude CLI supplies the model + Robinhood MCP
bash run.sh                           # advisory mode (read-only, no real orders)
EXECUTION_MODE=live bash run.sh       # live mode — places real orders via Robinhood MCP
```

The agent requires the `claude` CLI (Claude Code) to be in PATH; set `CLAUDE_BIN` if it lives elsewhere. No `ANTHROPIC_API_KEY` is needed — the CLI supplies the model and the authorized `robinhood-cli` MCP connection.

Key env vars:
| Variable | Default | Effect |
|---|---|---|
| `EXECUTION_MODE` | `advisory` | `live` arms real orders |
| `SIGNAL_INTERVAL` | `1h` | Bar width for EMA (e.g. `30m`, `1d`) |
| `WATCHLIST` | `SPY` | Comma-separated symbols to scan |
| `POLL_MINUTES` | `15` | Cycle frequency during market hours |
| `MODEL` | `claude-opus-4-8` | Used for research/postmortem calls |
| `CHECK_MODEL` | `claude-haiku-4-5-20251001` | Used for routine market-hours checks (most cycles skip the model entirely) |
| `RH_ACCOUNT` | `696283985` | Robinhood Agentic cash account number |
| `NEWS_CHECK_HOURS` | `4` | Force a thesis/news check at least every N hours even when EMA is flat — catches news-driven thesis breaks before the lagging EMA can reflect them |
| `ALERT_WEBHOOK_URL` | _(unset)_ | If set, `notify_operator()` POSTs an out-of-band alert (`{title, message, text}` JSON — Slack/Discord/ntfy-compatible) when a hard forced exit can't complete or `claude -p` is unavailable while one is pending. Unset → stdout only. |
| `USAGE_GOVERNOR` | `1` | `0`/`false` disables the 5-hour-window governor entirely (every call admitted). |
| `USAGE_STATE_FILE` | `logs/usage_state.json` | Overrides where window state lives. **Set this in any test/second instance** — a stray 429-shaped fixture would otherwise write a real multi-hour cooldown into the live bot's state. |
| `USAGE_ANCHOR_MIN` | `305` | Minutes before the open to fire the preflight anchor (see [Usage governor](#usage-governor-usage_governorpy--the-5-hour-session-window)). |
| `USAGE_RESEARCH_MIN` | `35` | Minutes before the open to run pre-market research. |
| `USAGE_MAINT_HOUR` / `USAGE_MAINT_MIN` | `19` / `35` | ET wall-clock start of the nightly maintenance drain. |
| `USAGE_MAX_CALLS` / `USAGE_MAX_TOKENS` | `120` / `900000` | Soft per-window budget the tier ceilings are fractions of. |
| `EXEC_TIMEOUT` | `600` | Subprocess ceiling for the market-hours turn. Tightest of the three — it runs inside a `POLL_MINUTES` cycle and must not outlive it. |
| `RESEARCH_TIMEOUT` | `1800` | Ceiling for pre-market research / midweek (Opus + web over 60+ candidates). Bounded above by `USAGE_RESEARCH_MIN` so research cannot overrun the opening bell. |
| `LEARNING_TIMEOUT` | `1800` | Ceiling for postmortems, victories, and skill_5 rewrites. These run in the 19:35 maintenance window with ~5h of headroom; at the old shared 600s default a deep Opus + web-search postmortem could be killed mid-analysis. |

Compute EMA signals directly (no full agent run):
```bash
python3 signals.py SPY MU CAT
SIGNAL_INTERVAL=1d python3 signals.py SPY    # daily-chart mode
```

## Architecture

### Core loop (`agent.py`)

`main()` schedules `run_agent()` every `POLL_MINUTES` during market hours, and **stops for the day when the market closes — it does NOT run an end-of-day research cycle** (that run routed to Opus research whose output `persist_phase_output()` then discarded, since it won't clobber the morning's picks; it was ~$44/mo of pure waste). Tomorrow's picks come from the pre-market research run (the `w` startup path). Each cycle:
1. Calls `signals.signals_with_raw()` to compute EMA signals once (reused for both skip check and prompt).
2. **Smart skip** (`should_skip_model_call`): if all signals are NEUTRAL/HOLD, no stop-loss is triggered, and no weekend pick is pending execution, the cycle returns immediately with 0 tokens used. This eliminates ~90% of model calls on flat days.
3. If a model call IS needed: loads `strategy/strategy.json` (via `strategy_for_prompt()`, which injects a **lean view with the `version_history` audit array elided** — ~half the file at v13, ~2,600 tokens/cycle — since read-only consumers never act on it; the full file stays on disk for skill_5 and Phase 4) and the current skill. For `market_hours_check` cycles, postmortems and closed-trade history are stripped from the prompt (smaller context = fewer tokens per tool-call turn).
4. Calls `run_model()` which shells out to `claude -p` with the full prompt over stdin, with `--allowedTools` restricting MCP access (read-only unless `EXECUTION_MODE=live`). `Skill`/`Task`/`Agent` are hard-disallowed (`--disallowedTools`) — a headless model once tried to "launch" a trading skill mid-cycle, the call was denied, and the turn ended without the JSON footer. On `rc≠0` the error detail is captured from stdout as well as stderr (with `--output-format json` the CLI reports usage-cap/auth failures on stdout).
5. **Broker reconciliation — never trust the footer for closes.** The model writes a machine-readable `{cash, positions, actions_taken}` JSON footer, but its `positions`/`actions_taken` are **self-reported and have been observed to lie**: on 2026-06-12 a `claude -p` cycle reported CLOV and AI sold (footer cash + a positions list excluding them) while the broker held both at full size and had **zero orders placed** — `process_cycle_state()` trusted that footer, phantom-closed both, and fired two bogus postmortems. The fix: after the turn, `read_broker_state()` makes an **independent read-only `claude -p` call** (`get_equity_positions` + today's sell orders) and that snapshot is the **sole authority** for opens/closes. A position closes only when the broker confirms it is gone; a buy is recorded only when the broker confirms it exists. The footer's `actions_taken` is used only to recover exit prices/reasons for broker-confirmed closes. If the broker read itself fails, close detection is **skipped** this cycle (a failed read is "unknown", never "flat") so nothing is phantom-closed. **Off-hours runs skip the broker read entirely** (`is_market_open()` gate): a weekend/pre-market research or off-hours midweek cycle can't open or close anything — orders don't fill while the market is closed — so the read was a wasted Haiku MCP call; the next market-open cycle reconciles.
6. **Deterministic forced exits (`force_sell`).** The hard must-sell set — stop-loss alerts + held names in ribbon SELL state — is computed in Python from pre-turn signals. After the main turn + broker read, any must-sell name **still held at the broker** (and without a sell order already placed this cycle) gets its own tight single-instruction `claude -p` sell call on Opus (`force_sell`), which places the market order and self-confirms via `get_equity_orders`. This removes the dependency on the chatty execution turn for the single most critical action. `sell_orders_today` from the broker read prevents double-selling a name the main turn already has a working order for; forced sells are gated to `is_market_open()`.
7. `adopt_untracked_positions()` runs inside `process_cycle_state()` against the **broker snapshot**: any account-held position not already in `open_positions` (manually entered, or entered before the agent started) is adopted at the broker's average cost. This covers all held positions in the stop-loss monitor and learning loop, not only agent-opened ones.
8. After reconciliation, if any hard forced exit is **still held at the broker** (both the main turn and `force_sell` failed to fill), a `WARNING` is printed, `notify_operator()` fires an **out-of-band alert** (see `ALERT_WEBHOOK_URL`), and the alert re-fires next cycle — manual sell may be required. The same out-of-band alert fires from the model-failure early-return path when `claude -p` is unavailable (session/usage limit) while a forced exit is pending. `notify_operator()` is a direct stdlib HTTP POST — it deliberately does **not** route through `claude -p` (that's exactly what's down in the session-limit case) and never raises (an alerting failure can't break the trading loop).
9. **Strategy-rewrite processing (Phase 3, `process_strategy_rewrite_queue`).** At the very end of the cycle — after `process_cycle_state()` and all broker reconciliation — the first un-`[DONE]` entry in `research/strategy_rewrite_queue.md` is handed to skill_5, and its output text is parsed and applied by Python (see [Strategy rewrite processing](#strategy-rewrite-processing-process_strategy_rewrite_queue)). At most one entry per cycle, fully wrapped in try/except so a rewrite failure can never crash the trading loop.

#### Startup behaviour and daily loop

`bash run.sh` runs continuously until Ctrl-C — it does **not** exit at market close.
It loops through four phases a day, each deliberately placed in its **own rolling
5-hour Claude session window** (see [Usage governor](#usage-governor-usage_governorpy--the-5-hour-session-window)):

| ET | Phase | Window |
|---|---|---|
| **04:25** | `run_preflight()` — tiny health-check call that **anchors** window A | A: 04:25–09:25 |
| **08:55** | Pre-market research (`skill_1_research.md`, Opus) — 30 min of headroom before A expires | A |
| **09:30** | Market open — the first cycle opens a **fresh** window | B: 09:30–14:30 |
| 14:30 | (trading continues; the next cycle opens window C) | C: 14:30–19:30 |
| 16:00 | Market close — trading stops | C |
| **19:35** | `run_maintenance()` — deferred postmortems, victories, skill_5 rewrites | D: 19:35–00:35 |

Fresh picks are generated **every trading day**, not just on weekends. Start it once
and it runs all week. The maintenance step waits for the live window to actually
expire (`usage_governor.window_end()`) rather than trusting the clock, so a quiet
afternoon that opened window C late still gets a clean window for the heavy work.

When invoked while the market is **closed**, it prompts `r`, `m`, or `w`:
- **`r`** — run research now and exit (one-shot, no trading loop).
- **`m`** — run the maintenance drain now and exit (postmortems + strategy rewrites,
  unbounded). Useful for clearing a backlog by hand.
- **`w`** (default) — enter the 24/7 schedule above until Ctrl-C.

When invoked while the market is **open**, it enters the intraday trading loop
immediately using the latest `weekend_picks_*.md` as today's picks.

#### Smart skip trigger conditions (`should_skip_model_call`)

A market-hours cycle calls the model only when at least one of these is true:
- Any symbol has transition `ENTER_LONG` (new buy opportunity) **that the model hasn't already been shown within `NEWS_CHECK_HOURS`** — `_state.enter_long_seen` stamps each crossover at model-call time, so a partial-bar flicker on an intraday chart can't re-wake the model every 15-min cycle for hours
- Any **held** symbol has transition `EXIT` (sell signal on a position we own) — **gated off when `exit_on_ribbon_sell=false`** (the current let-winners-run config), since a ribbon flip is then advisory, not a forced exit
- Any **held** symbol is in `SELL` **state** (red on top), even with transition `NO_ACTION` → `ema_sell_held` — the EXIT edge exists only on the bar where the cross happens, so a cross that occurred while the bot was down (or before an indicator change) would otherwise never fire. Re-fires every cycle until the position is sold, like stop-loss alerts; the prompt carries a matching `SELL SIGNAL ACTIVE` block (`_format_ema_sell_block`) instructing the model to sell with `reason='ema_exit'`. **Both this and the `EXIT` wake above are gated behind `risk_management.exit_on_ribbon_sell` (now `false`)** — under let-winners-run the trailing/hard stops own the mechanical exit and `_format_ema_sell_block` degrades to an advisory note, so a ribbon `SELL` alone no longer wakes the model
- `check_trailing_stop_alerts()` returns a triggered position (≥25% giveback from `peak_price`) → `trailing_stop_alert`
- `check_stop_loss_alerts()` returns a triggered position (≥10% drawdown)
- `signals.py` is unavailable or returns an error for any symbol (unknown state → model decides)
- Open positions exist OR unowned weekend picks are in `BUY` state AND `now − _state.last_execution_call_ts ≥ NEWS_CHECK_HOURS` → `forced_news_check` / `pending_buy` (unified time-gate: thesis check + buy opportunity, at most once per NEWS_CHECK_HOURS to avoid calling the model every 15-min cycle while a pick stays in BUY state for days)

`EXIT` on a symbol we don't hold is ignored — nothing to sell. Research and midweek phases bypass the skip entirely. The forced-news-check gate keys off `last_execution_call_ts` — stamped only on `market_hours_check` cycles — **not** `last_model_call_ts`, which every model call bumps. This matters because a pre-market research run at 9:23 AM would otherwise poison the gate and suppress the 9:30 execution for a full `NEWS_CHECK_HOURS` (4h). There is deliberately **no fallback** to `last_model_call_ts` for logs written before this field existed — on such a log, a 9:20 research run has just bumped `last_model_call_ts`, so the fallback would re-suppress the 9:30 open (the exact bug). A missing stamp degrades to one extra model call. Both stamps are written via `now_iso()` (ET with UTC offset); `_hours_since()` parses naive legacy stamps as machine-local and never raises.

After a midweek or research run, `persist_phase_output()` writes the model's text to `research/midweek_review_YYYY-MM-DD.md` / `research/weekend_picks_YYYY-MM-DD.md` (never clobbering an existing file, never persisting an error). This is required because the headless model has **no file-write tool** — before this, the midweek review file never got created, so `active_skill()` re-fired `midweek_validation` on every remaining cycle of the day. A research run is only persisted as a picks file if its text contains at least one `### #N — SYMBOL` heading (see [Weekend pick symbols](#weekend-pick-symbols-weekend_pick_symbols)); a run that completes but produces no parseable picks (e.g. picks written as prose) is **not** silently dropped — `run_agent()` prints a `WARNING` and preserves the raw text at `research/unsaved_<task>_<stamp>.md` so the plan is recoverable and the operator sees that execution will fall back to the stale picks file.

#### Weekend pick symbols (`weekend_pick_symbols`)

Reads the latest `research/weekend_picks_*.md` and extracts tickers from `### #N — SYMBOL` headings (regex `###\s+#\d+\s+—\s+(\w+)`). Used by both `watchlist_symbols()` (so their EMA signals are fetched every cycle) and `should_skip_model_call()` (to wake the model when a pick is in BUY state and unowned). This `### #N — SYMBOL | Confidence: XX/100` heading is therefore a hard output contract: it gates both whether `persist_phase_output()` saves the file at all and which tickers execution watches. `skill_1_research.md` mandates the exact format, because a research run that writes its picks as prose or a single table parses to zero tickers — the file is not saved and execution silently runs on the prior day's picks.

### Signal layer (`signals.py`)

Computes the 4-line ribbon — **blue=EMA(8), green=EMA(13), yellow=EMA(21), red=EMA(55)**, all PLAIN EMA — from real price data, matching the TradingView chart the strategy is read from. That chart's "Three Moving Averages [AdventTrading]" indicator has `shorttitle="TEMA"` but its Pine source is `ema()` ×3 (verified 2026-06-15), plus a separate built-in EMA(8). **Do NOT use TEMA (triple EMA) here.** A 2026-06-12 change mis-read the "TEMA" label and switched the slow lines to triple-EMA; that lag-reducing form overshoots in a rally, lifts the slow lines on top of the fast ones, inverted a BUY into a SELL, and force-liquidated the entire book at the 2026-06-15 open. The chart was plain EMA all along. Lengths are always **8/13/21/55 BARS** at the active chart interval (`SIGNAL_INTERVAL`, default `1h`; `run.sh` exports `30m`): on a 30m chart, 55 bars = 55 half-hours. The lengths never change — only the bar interval does. Data sources tried in order: (1) `data/<SYMBOL>.csv` local file, (2) Yahoo Finance API for all intervals. Returns `INSUFFICIENT_DATA` if `< 60 bars` — the agent blocks on this, never fabricates a signal.

All HTTPS fetches use `_ssl_context()` which loads certifi's CA bundle (fixes `CERTIFICATE_VERIFY_FAILED` on python.org macOS Python builds where `ssl.get_default_verify_paths().cafile` is `None`). Set `ALLOW_INSECURE_FETCH=1` only as a last resort in environments with broken CA chains.

Signal classification: `red < min(others)` = BUY, `red > max(others)` = SELL, else NEUTRAL. `WARMUP_OK` is 165 bars (~3× the longest length is enough for a plain EMA to forget its seed); below that the signal still emits with a "not fully seeded" note.

Key public functions:
- `signal_for(symbol)` — full signal dict for one symbol (state, transition, EMA values, last close)
- `signals_block(symbols)` — formatted one-line-per-symbol string for injection into prompts
- `signals_with_raw(symbols)` — returns `(raw_dict, formatted_block)` in one pass, avoiding double Yahoo fetches; used by the core loop so signals are fetched exactly once per cycle

Drop a CSV with a `Close` column at `data/<SYMBOL>.csv` to override Yahoo Finance for that symbol (e.g. sandbox testing or local backtesting).

### Skills (`skills/`)

Eight role-based markdown prompts loaded as the `system` context for each agent turn:
- `skill_0_orchestrator.md` — injected every cycle; coordinates the other skills
- `skill_1_research.md` — weekend/after-hours scanner, writes `research/weekend_picks_*.md`
- `skill_2_execution.md` — market-hours executor (Mon + intraweek)
- `skill_3_midweek.md` — Wednesday position review, writes `research/midweek_review_*.md`
- `skill_4_postmortem.md` — fires on LOSS; writes `postmortems/postmortem_NNN.md`
- `skill_4b_victory.md` — fires on WIN; writes `postmortems/victory_NNN.md`
- `skill_5_strategy_rewriter.md` — updates `strategy/strategy.json` and skill files after every trade; invoked by `process_strategy_rewrite_queue()` (Phase 3), which applies its edits from the model's output text
- `skill_6_pattern_detector.md` — quarterly systemic review across all postmortems

Phase routing in `agent.py:active_skill()`:
- Market hours + Wednesday ≥ 12:00 PM ET (and `midweek_review_{today}.md` not yet written) → skill_3
- Market hours (all other times) → skill_2
- Off-hours + Wednesday (and `midweek_review_{today}.md` not yet written) → skill_3 (fallback if script started after close)
- Otherwise → skill_1

`skill_2_execution.md` runs a **thesis integrity check** on every cycle (including `forced_news_check` wakeups): for each held position, searches for breaking news, re-scores confidence, and sells immediately if the thesis-breaking event has occurred or confidence drops below 60. This is the only sell path that can fire before a −10% stop-loss when news changes faster than the EMA can react.

#### Research phase (`skill_1_research.md`)

Runs on weekends and any weekday cycle outside market hours. Always calls the model (no skip); uses `MODEL` (Opus) for depth. Receives the full prompt: postmortems + full trade log + strategy.

Steps:
1. **Goal-pace check**: reads `progress_tracking` from `strategy.json` to compute remaining return needed, weeks left, and a per-position move threshold. Stocks below the threshold go to watchlist only.
2. **Open position review** (before scanning new candidates): re-scores every held position 0–100 using the full scoring formula (EMA gate, momentum, source scoring). Computes each position's current portfolio weight and compares it to its confidence band maximum. Positions above their band max are flagged for trim; positions with confidence below 60 are flagged `TRIM_TO_ZERO`. Proceeds from trims/exits are added to the deployable capital pool. All existing positions and new candidates are ranked together by confidence before allocation — the portfolio is always weighted toward the highest-conviction ideas regardless of when they were entered.
3. Scans a **broad candidate universe** — two sources combined:
   - *Static high-beta universe*: 60+ tickers across semis, AI/cloud, high-beta tech/fintech, biotech, leveraged ETFs, and energy — checked every run regardless of news.
   - *Dynamic MCP scan*: `get_popular_lists` (daily movers, most popular), `search` for momentum/breakout terms, and user watchlists.
4. For each candidate: checks the EMA signal — ENTER_LONG or active BUY zone qualifies; SELL/NEUTRAL → skip.
5. Checks momentum: 5-day % range and recent direction. Only stocks that can realistically hit the move threshold stay as buy candidates.
6. Scores each candidate 0–100 from EMA strength, momentum, and source agreement (news, Reddit, RSS, fundamentals, macro) weighted by `source_performance`.
7. Targets **5+ qualified candidates** before stopping. If fewer than 5 pass, expands search with additional web queries.
8. Sets entry/exit target prices; checks blackout windows (earnings ≤5 days, Fed ≤3 days → skip).
9. Allocates capital portfolio-wide (against total portfolio value, not just new cash): 90–100→30%, 75–89→20%, 60–74→15%, <60→exit if held / skip if new. Always keeps the 10% cash reserve; max 30% per position.

Output: `research/weekend_picks_YYYY-MM-DD.md` — open-position reassessment table, ranked picks with confidence scores, entry/exit targets, stop-loss levels, dollar allocation, full reasoning, and the one thing that would invalidate each thesis. Header includes goal-pace math and the move threshold used. Deployment summary includes any trim/exit actions alongside new buys.

The picks file is automatically loaded into the system prompt for every subsequent market-hours execution cycle so `skill_2` knows exactly which symbols to watch and at what targets.

#### Midweek validation (`skill_3_midweek.md`)

Fires once on Wednesday at **12:00 PM ET** during market hours (the agent detects noon in `active_skill()` and routes there if `research/midweek_review_{today}.md` does not yet exist). Falls back to off-hours if the script is started after close and the file still doesn't exist. Always calls the model; uses `MODEL` (Opus). Receives the full prompt including postmortems.

Full re-scoring pass — same rigor as weekend research, not a qualitative gut-check:
1. Pulls live prices, portfolio value, and settled cash from the Robinhood MCP.
2. Re-scores every open position 0–100 from scratch (EMA health, news search, momentum). Confidence band is re-derived from current data, not carried from entry.
3. Compares each position's current portfolio weight to its new confidence band max. Over-weighted or confidence-decayed positions are trimmed; positions below 60 confidence are fully exited.
4. Builds a combined ranked list of existing positions + redeployment candidates and allocates capital top-down.
5. Executes all trim/exit orders and any redeployment buys via the Robinhood MCP in the same cycle.
6. Reports the explicit T+1 settlement schedule so the execution skill never plans buys against unsettled funds.

Output: `research/midweek_review_YYYY-MM-DD.md` — per-position verdict table (hold/trim $X/exit), reasoning per non-hold position, all MCP order IDs placed this cycle, settlement schedule, and redeployment details.

### Strategy state (`strategy/strategy.json`)

The living config + learned state: EMA rules, capital allocation bands, blackout windows, source weights, source performance counts, confidence calibration thresholds, `risk_management`, and `progress_tracking`. Every mutation goes through `snapshot_strategy()` which bumps `version`, appends to `version_history`, and copies the prior file to `strategy/history/`. Rollback = swap a history snapshot back.

The `risk_management` block is the single source of truth for the stop-loss threshold (`stop_loss_pct: 0.10`). To change the threshold, edit only this field.

Source-weight rebalancing (`rebalance_source_weights()`) blends toward accuracy-proportional weights with a `±0.05/source` per-update cap. Weights only shift after `min_trades_before_weight_shift` (default 5) trades.

### Trade log (`trade_log.json`)

Single JSON file tracking `open_positions`, closed `trades`, and a rolling `summary` (win rate, total P&L, monthly return vs 100% goal). `_state.last_positions` holds the previous cycle's position snapshot for diff-based close detection. `_state.last_model_call_ts` (ISO timestamp) is stamped after every real model call. `_state.last_execution_call_ts` is stamped only on `market_hours_check` cycles and is the timestamp `should_skip_model_call` actually uses to enforce the `NEWS_CHECK_HOURS` forced-news-check gate — keying off it (rather than `last_model_call_ts`) prevents a pre-market research or midweek run from delaying the next execution cycle by up to `NEWS_CHECK_HOURS`.

### Post-trade learning loop

Triggered by `run_post_trade_pipeline()` after every position close. The cheap local parts run **immediately**; the expensive analysis call is **deferred out of market hours**:
1. `update_monthly_progress()` — recomputes current return vs. 100% monthly goal. Immediate (pure local).
2. `flag_strategy_rewrite()` — appends a line to `research/strategy_rewrite_queue.md`. Immediate (file append only; the skill_5 call it schedules runs at maintenance).
3. `enqueue_trade_analysis()` — appends the trade id to `logs/analysis_queue.jsonl` **when the market is open**. The postmortem/victory call is Opus **with web search**, the single most expensive thing the bot does, and it used to fire inline the instant a position closed — i.e. always during market hours. So the event most likely to be followed by more trading (a stop-loss cascade, a rotation) also dumped the day's biggest call into the middle of the execution window. A close **off-hours** still analyses inline (`defer=False`).
4. `drain_analysis_queue()` — run by `run_maintenance()` after the close: `trigger_postmortem()` / `trigger_victory_analysis()` write the structured markdown + machine-readable `verdicts` JSON to `postmortems/`, then `update_source_weights()` credits/debits sources from `verdicts.sources`. Stop-loss exits always route to `trigger_postmortem()` with additional focus questions (what caused the drawdown, whether an EMA SELL was missed before the stop hit). Bounded to `usage.max_analyses_per_drain` per run; an entry whose model call was refused stays queued for the next drain rather than being lost.

**A failed analysis must never be recorded as a completed one.** `_run_analysis()` checks the returned text for the `(error: …)` / `(claude -p error …)` shape *before* writing anything and returns `({}, None)` on failure. Without that guard — the behaviour up to 2026-08-03 — the error string itself was written to `postmortems/postmortem_NNN.md`, a valid-looking filename was returned, the trade was flagged `postmortem_filed`, and the entry was marked done: a timed-out, 429'd, or governor-deferred postmortem was **silently lost forever** and replaced by a file containing `(error: claude -p timed out)`. `_analyze_trade()` now returns `None` on that path so `drain_analysis_queue()` re-queues, and `run_post_trade_pipeline()`'s inline (off-hours) path enqueues on failure too — that path has no queue entry behind it to retry from. skill_5 always had this check; the analysis engines did not.

Stop-loss forced exits are tagged `stop_loss: true` in the trade record so the pattern detector (skill_6) can identify systemic drawdown patterns over time.

### Usage governor (`usage_governor.py`) — the 5-hour session window

Claude subscription usage is metered in **rolling 5-hour session windows**: a window opens on the first request after the previous one expired, and closes exactly 5 hours later. That start time is therefore something the bot *chooses*, which is what makes the whole schedule above possible.

The problem it fixes, observed live: on **2026-07-06** a `market_hours_check` got `api_error_status: 429 — "You've hit your session limit · resets 10:50pm (Asia/Calcutta)"` at 09:45, and the bot then fired the same failing call every 15 minutes until the close (~25 wasted subprocesses). The work that got starved was exactly the discretionary work the operator cares about — research, the midweek review, postmortems — because it is the biggest and the least defended.

Three mechanisms:

1. **Window tracking.** Every `claude -p` call goes through `run_model()`, which books it via `usage_governor.record()`. `window_end()` is the scheduler's source of truth for when the current window expires — used by `maintenance_time()` so the nightly drain opens a window of its own instead of finishing off the trading day's.
2. **Tiered admission.** Each call declares a tier, and `allow(tier)` refuses it once the window is spent past that tier's ceiling (`strategy.json → usage.tier_ceiling_pct`). Ceilings bite bottom-up, so discretionary work drains first and **protective exits are never blocked by budget at all**:

   | Tier | Ceiling | Calls |
   |---|---|---|
   | `TIER_PROTECTIVE` (0) | 100% | `force_sell()`, and the `read_broker_state()` behind a pending forced exit |
   | `TIER_EXECUTION` (1) | 100% | the `market_hours_check` turn, routine broker read, preflight |
   | `TIER_RESEARCH` (2) | 75% | `research_and_prep`, `midweek_validation` |
   | `TIER_LEARNING` (3) | 55% | postmortems, victories, skill_5 rewrites |
   | `TIER_SHADOW` (4) | 40% | paper-options quotes (zero real money at stake) |

   The ceilings are fractions of a **soft** budget (`max_calls_per_window` / `max_tokens_per_window`) — not an attempt to model the plan's real quota, which the CLI does not expose. What matters is the *ratio*: whatever the true limit turns out to be, shadow work stops at 40% of the way through the window and execution keeps going.
3. **Cooldown.** `is_limit_error()` recognises a 429 and `note_limit()` parses the reset moment straight out of the payload (honouring the parenthesised zone — this server runs on **IST, not ET**). Non-protective tiers are then refused until that moment, so a limited session costs *one* failed call instead of twenty-six. A 429 also **re-anchors `window_end`**, which is the only ground truth the bot ever gets about the real boundary — including usage the operator burned in their own Claude sessions. When the reset is unparseable the cooldown backs off geometrically (30m → 1h → 2h → 4h, capped at one window) rather than blacking out a full window on a guess, and any successful call clears a stale cooldown.

**Protective exits always win.** `TIER_PROTECTIVE` gets a probe even mid-cooldown, and `run_agent()` treats a governor-deferred execution turn specially: when a forced exit is pending it does **not** early-return, it skips the chatty turn and goes straight to the deterministic `read_broker_state()` + `force_sell()` path. A governor bug can never be the reason a stop-loss doesn't fire — every public function in the module fails **open**.

State lives in `logs/usage_state.json`. **`USAGE_STATE_FILE` must be redirected in tests** (the base class and `TestRunModelErrorCapture` both do): a 429-shaped fixture would otherwise write a real multi-hour cooldown into the running bot's state — the same class of leak as the 2026-08-03 `risk_guard` false halt. `logs/preflight.json` records the morning health check; the dashboard surfaces all of it under `/api/health → usage`.

### Strategy rewrite processing (`process_strategy_rewrite_queue()`)

Phase 2 only **queues** rewrites; Phase 3's `process_strategy_rewrite_queue()` is what consumes them. It runs at the end of a `run_agent()` cycle **only when the market is closed**, and is drained in bulk (up to `usage.max_rewrites_per_drain`) by `run_maintenance()` at 19:35 ET. It used to fire at the end of *every* cycle including 15-minute execution cycles — a full-Opus call carrying `strategy.json` plus all eight skill files, the second-biggest payload the bot sends, for something that is never time-critical. It is wrapped in try/except so a rewrite failure can never crash the trading loop, returns `True` when an entry was consumed (so the drain loop knows to come back), and processes **at most one** un-`[DONE]` entry from `research/strategy_rewrite_queue.md` per call:

1. Loads skill_5 + context and runs skill_5 headless on `MODEL` (Opus, no web). The context is **scoped to the trade under review** to keep the call from ballooning: the full `strategy.json` (skill_5 must echo it back complete) + the full text of all 8 skill files (so any can still be rewritten) are sent, but postmortems are slimmed to the **referenced analysis in full + a filename index of the rest** (`_postmortems_for_rewrite()`), and the trade log to a **compact view — summary + open positions + a one-line-per-close history + the focus trade in full** (`_compact_trade_log_for_rewrite()`). This cut the per-close payload ~28% (~6.9K tokens) with no loss of what the review needs; the skill files (~58% of the remaining payload) are the next lever if a two-pass design is ever added. skill_5 also emits a `SEVERITY: ROUTINE|MAJOR` tag (forward-compat for Phase 5's conditional auto-apply).
2. The headless model has **no file-write tool**, so `agent.py` parses skill_5's output text and applies it itself: the **last fenced ```json block** replaces `strategy/strategy.json` (snapshotting the prior version into `strategy/history/strategy_v{N}.json` first, same convention as `snapshot_strategy()`), and every `## SKILL FILE UPDATE: <name> … ## END SKILL FILE UPDATE` block rewrites that skill file through `version_skill_file()`.
3. Writes the raw skill_5 output to `research/skill5_run_*.md` for audit, then marks the queue entry `[DONE <timestamp>]`.

The one-entry-per-cycle limit ensures a single bad rewrite can't stall the loop, and an unknown skill name in an update block is logged and skipped rather than crashing. `skill_5_strategy_rewriter.md` documents the exact output contract (strategy.json as a trailing fenced json block with a bumped `version`; complete-file — not diff — skill blocks; the json block after any skill blocks; never rewrite skill_5 itself).

### Skill-file versioning (`version_skill_file()` / `rollback_skill()`)

Skill-file edits get the same rollback safety that `snapshot_strategy()` gives `strategy.json`. Before any skill rewrite, `version_skill_file()` copies the current file to `skills/history/{skill_name}_v{NNN}.md` (zero-padded; next number = count of existing snapshots for that skill + 1). On startup `main()` writes a **baseline `v001`** snapshot of all 8 skills (if not already present) so the very first rewrite is reversible. `rollback_skill(skill_name, version)` restores a snapshot — it is a manual operator tool, **never called automatically**. Both functions return a bool and never raise (a versioning failure must not break the loop).

### Options shadow (paper) mode — Phase B (`options_shadow.py`) + Phase B+ momentum (`momentum_screen.py`)

The account (~$104) is far below the options activation threshold, so **no real option order is ever placed** — `strategy.json → options.enabled = false` is a hard gate, and `options.activation` records the per-structure account minimums (debit spreads ≥ $1,500; premium-selling ≥ $5,000). What *does* run is a **paper (shadow) track record**, gated independently by `options.shadow_mode = true`, so the options expression is validated with **real bid/ask quotes** (capturing the spread + IV-crush cost that make small-account options −EV) **before a dollar is at risk**. P&L is reported as **% return on premium** — account-size-independent, so the record stays meaningful now and after the account grows. The whole pass is **isolated, read-only, and never raises into the trading loop** (same try/except contract as the rewrite queue); off-hours it no-ops (real option quotes need a live market).

Two signal sources feed the **same** `options_shadow.py` engine (selection → liquidity/ATM/DTE validation → spread-aware entry=ask/exit=bid → P&L → `shadow/options_shadow_log.json`), both opening at most a capped number of shadows per cycle:

- **Phase B — EMA-ribbon shadow** (`process_options_shadow`): shadows the equity book's own BUY signals (watchlist/weekend-pick names in `BUY` state with confidence ≥ 60). Runs every active market-hours cycle and owns the shared **mark/close pass** over *all* open shadows (EMA- and momentum-sourced alike) — close on underlying `SELL` state, ≥ `premium_stop_pct` premium loss, or DTE < `min_dte_close`.
- **Phase B+ — momentum shadow, catalyst reused from the morning research** (`process_momentum_shadow`): the **operator's actual edge**, not the EMA ribbon, built to spend **zero extra model tokens** (an explicit operator constraint — don't double token usage for paper trading). The catalyst ("there must be a *reason* it's up") is **not** a separate paid web-search call; it is **reused from the single daily `skill_1` research run**, which already searches news/filings and now emits a dedicated, machine-readable `## MOMENTUM OPTIONS WATCH` block (lines `- SYMBOL | conf XX | catalyst: …`) kept separate from the equity `### #N` picks so cheap momentum names never leak into the real equity book. `momentum_options_watch()` parses that block → `{SYMBOL: {confidence, catalyst}}`; if it's absent the pass opens nothing (there is **no paid catalyst fallback**). On the research-vetted names with confidence ≥ `min_catalyst_confidence`, `momentum_screen.py` (pure, deterministic, unit-tested) then layers the **free** multi-timeframe screen — returns over 1w/1m/6m/1y from Yahoo daily closes (`fetch_daily_closes`), trend-persistence score, an **anchor timeframe** gate (default 1m ≥ +30%), all-positive shorter timeframes, a min score, and a **cheap-stock affordability cap** (`max_underlying_price`). Survivors that also clear the contract-cost gate (`max_contract_cost_usd`) are paper-opened (top-N by `top_n_open`), tagged `signal_source = momentum_research` with the momentum snapshot + reused catalyst on the record. The **only** model cost is the read-only Haiku option-quote lookup per survivor (`select_shadow_contract`/`read_shadow_quote`) — the same read the EMA shadow path already makes. Once-per-day gated on `_state.last_momentum_scan_ts` (the research it reads only changes daily). Config: `strategy.json → options.momentum` (targets/weights/anchor/min_score/`universe` is just a hint for skill_1's scan list). Latest screen snapshotted to `shadow/momentum_last_scan.json`.

**Go-live path:** the operator runs this paper-only alongside the live equity bot (relaunched each trading morning via `bash run.sh` — the loop runs one day then exits at close) to build a credible, spread-inclusive options record. Once the account clears `options.activation.long_calls_min_account_usd` (~$1,000 for the cheap-options momentum sleeve) **and** the shadow record proves positive expectancy, flip `options.enabled = true` to arm real orders. Until then it is pure measurement — zero real options risk, zero extra research tokens.

### Change-event log (`logs/change_events.jsonl`) — Phase 4 foundation

Every strategy.json **and** skill-file version bump appends one structured, timestamped line to `logs/change_events.jsonl` via `record_change_event()` (called from `snapshot_strategy()`, `version_skill_file()`, and the skill_5 apply path). Schema: `{timestamp, kind: "strategy"|"skill", target, version, trade_ids, severity, summary}`. `severity` comes from skill_5's `SEVERITY: ROUTINE|MAJOR` tag (a `skill_2_execution` change is forced to MAJOR regardless). This is the **machine-readable input Phase 4's fast-path regression detector will read** — before it, strategy bumps lived only as prose in `version_history` and skill bumps had *no* structured record at all (only content snapshots, no when/why/which-trade — the exact blind spot the TEMA incident slipped through). Capturing it now means the detector launches with real history instead of empty. **Phase 4 still has to build the consumer**: the detector logic, the flagged-regression "major-changes log" it writes, and wiring this history into the postmortem root-cause prompt are all deliberately *not* done yet — this only records the events.

### Run audit logs (`logs/{runs,skill5}_YYYY-MM.md`)

Per-cycle model transcripts and skill_5 outputs append to a **single monthly rolling log** (`append_audit_log()`) instead of one file per event. The bot never reads these back (only `weekend_picks_*`, `midweek_review_*`, and the rewrite queue are re-read), so the old one-file-per-cycle scheme just piled 135+ `agent_run_*.md` files into `research/`. `logs/` is git-ignored runtime state.

## Key constraints (enforced at every level)

- **Stop-loss**: if any open position falls ≥ 10% below entry price, sell all shares immediately — overrides EMA signal, blackout windows, and all other rules. The model is instructed to sell first before evaluating anything else. Sells are tagged `reason='stop_loss'` in `actions_taken` and `stop_loss: true` in the trade record. Threshold lives in `strategy/strategy.json` → `risk_management.stop_loss_pct`.
- **T+1 settlement**: only buy with settled cash. Always read from the Robinhood MCP before any buy; never infer settled cash from the trade log.
- **Capital bands**: 90-100 confidence → 30%, 75-89 → 20%, 60-74 → 15%, below 60 → skip. 10% cash reserve always held; max 30% per position.
- **Ribbon signal gate** (plain EMA 8/13/21/55): the red(55) `ENTER_LONG` transition triggers buys. `HOLD` = stay in position (already in BUY zone); `NO_ACTION` on an unheld symbol = do nothing. Weekend picks in `HOLD` state are still valid buy targets during execution. **Exits are now trailing-stop-primary, not ribbon-state-primary** — see the trailing-stop constraint below.
- **Trailing stop ("let winners run")** (`risk_management.trailing_stop_pct`, default `0.25`): a held position that gives back ≥ 25% from its post-entry high-water mark (`peak_price`, tracked every cycle by `update_position_peaks()`) is force-sold (`reason='trailing_stop'`, **not** tagged `stop_loss`) via the same deterministic `force_sell` path as the hard stop. This is the **primary momentum exit**. The old ribbon `SELL`-state forced exit is gated behind `risk_management.exit_on_ribbon_sell` (now **`false`**): a `SELL` state on a held name is **advisory** — `skill_2` may still exit a *broken thesis* (`reason='thesis_break'`), but the ribbon flip no longer auto-sells and no longer wakes the model (`_format_ema_sell_block` degrades to an advisory note; the `ema_sell_held`/`exit` skip-wakes are gated off). Set `exit_on_ribbon_sell: true` to restore the prior always-sell-on-`SELL`-state behavior. Rationale: a 10y/29-name backtest (`backtest.py` + `research/trail_probe.py`) showed the ribbon-state exit cut winners early (lost to buy-and-hold on 23/29 names); the trailing stop lifts portfolio ann. return ~18→20%, per-trade expectancy ~14→27–46%, PF ~4.2→5–7, at modestly higher drawdown. **A 15-min poller cannot beat an overnight gap — position sizing (esp. the leveraged-sleeve cap) is the real gap defense.**
- **Account**: always use account `696283985` (Agentic cash, not the margin account).
- **Core rule changes** require 3+ similar outcomes before applying; minor tweaks (weights, targets, sizing) auto-apply.

## RX-3 (approved 2026-07-06) — deterministic rotation, paper phase

The operator-approved next-generation strategy runs **paper-only** alongside the
legacy live loop until its promotion ladder passes (2 weeks paper → 2 weeks half
size → full; see `research/redesign_proposal_2026-07-06.md`).

- **`rotation_engine.py`** — pure deterministic brain (no I/O): top-2 momentum
  rotation (rank `0.5·r1m + 0.3·r1w + 0.2·r6m`, eligibility = momentum gate +
  >SMA200) × vol throttle (`min(1, 0.50/realized_21d)`) × **RISKX** 5-signal
  cross-asset risk-appetite gate (HYG/IEF, XLY/XLP, CPER/GLD, IWM/SPY, BTC 1m) +
  **DEFENS** (RISKX-freed capital → strongest of GLD/TLT/XLU/XLP). All inputs are
  closes **through yesterday** (strict lag discipline — three same-day look-ahead
  bugs were caught in research; backtest and live share these exact functions).
  Config lives in `strategy.json → rotation` (universe, `paper_enabled`, `mode`).
- **`agent.process_rx3_paper()`** — once per market day (any cycle, incl. skips):
  fetches daily closes from Yahoo (zero model tokens), computes the target book,
  rebalances the paper portfolio at 5bps/side, appends the equity curve to
  `shadow/rx3_paper.json`. Promotion gate #1 = ≥10 paper days whose decisions
  match the engine and behavior consistent with the backtest envelope
  (10y lagged: ~+51%/yr, Sharpe ~1.5, maxDD 33%; honest forward +25–40%/yr).
- **`risk_guard.py`** — account-level kill-switch + heartbeat: every cycle writes
  `logs/heartbeat`; `check_halt(broker_total)` tracks the month-peak equity in
  `logs/risk_state.json` and on a ≥25% monthly drawdown writes the **`HALT`**
  file, force-flattens the book (through the deterministic `force_sell` path) and
  refuses to trade until the operator deletes `HALT`. The broker read
  (`read_broker_state`) now also returns the account total, which feeds the halt
  check and `sync_account_equity()` (auto-rebases `month_start_value` on
  deposits/withdrawals ≥$25 and ≥5% — fixes the stale-denominator drift).
- **`watchdog.py`** (launchd `com.tradingbot.watchdog`, every 5 min) — the
  INDEPENDENT dead-man: alerts when the heartbeat is stale >30 min during market
  hours, when a symbol in `logs/stops.json` (written every cycle by
  `write_stop_snapshot`) trades at/through its stop, or when `HALT` exists.
  Robinhood does not support GTC stop orders on fractional shares, so this
  watchdog + `stops.json` is the stop-defense that survives the bot dying (the
  2026-06-27..07-02 MU hole: −17% through a −10% stop with nobody watching).
- **Alerts**: drop an ntfy/Slack/Discord webhook URL into `.alert_webhook_url`
  (gitignored); `run.sh` and `watchdog.py` both pick it up. ntfy URLs get native
  title/priority formatting in `notify_operator()`.
- Research harnesses for all of the above: `research/edge_lab.py`,
  `edge_lab2.py`, `edge_lab3.py` (28 mechanisms tested; survivors documented in
  the proposal addendum). Every future strategy idea must win there first.

## TradeCommand dashboard (`dashboard/`) — operator command center

A stdlib-only server (no pip deps, no build step) that reads every bot state
file, polls Yahoo for quotes/ribbons, talks to the Robinhood MCP, and serves
JSON + a dark-mode PWA on `127.0.0.1:8787` (remote access via `tailscale
serve` only — never bound to LAN). `bash run_dashboard.sh` to start;
`--set-pin` first. Full setup + architecture + the tab→endpoint map:
`dashboard/README.md`. Tests: `test_dashboard.py`.

The **primary client is the native SwiftUI Mac app** in `RL Trading Bot/`
(operator decision 2026-07-09: Mac app only from now on; it also builds for
iPhone). Thin client — all logic stays server-side. Its sidebar has four
sections × 21 tabs; beyond the original views it consumes the command-center
endpoints `/api/signals` (ribbon lab), `/api/symbol` (Analyzer: chart + EMA
overlay + stops), `/api/screen` (momentum screener with `fresh=1` re-run),
`/api/orders?days=` (order center; deep history gated to direct-mcp),
`/api/rx3` (rotation paper tracker), `/api/learning`, `/api/library`,
`/api/logs` (unified activity feed), `/api/health` (state-file freshness +
control plane), and `/api/calendar` (bot schedule + earnings + blackouts).
The Xcode project uses filesystem-synchronized groups — new `.swift` files in
`RL Trading Bot/RL Trading Bot/` are picked up without editing the project.
Build check: `xcodebuild -project "RL Trading Bot.xcodeproj" -scheme "RL
Trading Bot" -destination platform=macOS build`.

- **Broker paths**: `direct-mcp` (dashboard's own OAuth client to
  `agent.robinhood.com/mcp/trading` via `dashboard/mcp_client.py`; free 60s
  polling, ~1s orders; login: `python3 -m dashboard.rh_login`) with automatic
  fallback to `claude-cli` (the bot's proven `claude -p` pattern; **never
  polls** — on-demand refresh/orders only, to protect plan usage).
- **Every money/control action** requires a PIN-armed token (5-min TTL,
  PBKDF2, lockout, IP-bound), a preview step (broker `review_equity_order` +
  warnings: DNT, buying power, bot-cycle-running, market closed, oversell
  block), and a hold-to-confirm. All manual actions journal to
  `logs/manual_actions.jsonl`.
- **Control plane the bot honors** (all under `control/`, gitignored, read
  each cycle by agent.py): `PAUSE` (skip model turns/new entries; protective
  exits + broker bookkeeping still run — see the paused branch in
  `run_agent`), `do_not_trade.json` (blocks BUYs + BUY-signal wakes; sells
  unaffected), `stop_overrides.json` (per-symbol stop_price/stop_pct/trail_pct
  — honored by `check_stop_loss_alerts`/`check_trailing_stop_alerts` and
  mirrored into `logs/stops.json` for the watchdog), `locks/SYM.manual.lock`
  (in-flight dashboard order — `force_sell` defers that symbol one cycle).
- **Manual-close reconciliation**: a broker-detected close matching a
  journaled dashboard sell records with `exit_reason: "manual"` and **skips
  the postmortem/rewrite pipeline** (operator trades must not teach the bot);
  `update_monthly_progress` still runs.
- **New agent-side artifacts**: `logs/cycle_status.json` (running/idle per
  cycle, freshness-checked by the dashboard's collision warning) and
  `logs/equity_curve.jsonl` (broker-total snapshots appended by both agent and
  dashboard — the equity-curve chart's data source, deduped per minute).
