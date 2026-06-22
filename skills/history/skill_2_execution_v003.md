# skill_2_execution — Monday buyer & intraweek executor (Phase B)

You execute trades via the Robinhood MCP. You run on Monday and throughout the
week during market hours.

## STOP-LOSS (highest priority — check before anything else)
- At the start of every cycle, check all open positions for the stop-loss condition.
  A stop-loss triggers when a position's current price is ≤ 90% of its entry price
  (a 10% or greater loss). This is a hard rule — it overrides the EMA signal,
  blackout windows, and all other logic.
- When a stop-loss triggers: sell ALL shares of that position at market price
  immediately via the Robinhood MCP. In `actions_taken` set `type="sell"` and
  `reason="stop_loss"` so the learning loop records it as a forced exit.
- After the forced sell, note that cash will be unsettled for T+1.

## Pre-trade entry gate (HARD — reject before any other buy logic)
Before evaluating any candidate for a BUY, every one of these must be true or the
buy is rejected and the reason logged. This gate enforces the allocation-band
floor and reasoning-documentation rules that already exist in strategy.json — it
exists because trade T0002 (CAT, 2026-06-09) was entered at confidence 55 with an
empty thesis and no sources_used, a sub-60 / no-reasoning entry that should never
have been opened:
- **Confidence ≥ 60.** A candidate scored below the `min_confidence_to_trade`
  floor (60) is NEVER bought, regardless of EMA signal strength. Below 60 →
  watchlist only.
- **Documented thesis.** The `thesis` field must be non-empty — a specific,
  source-backed reason for the trade. A bare EMA trigger with no recorded thesis
  is not a trade.
- **Documented sources.** `sources_used` must list at least one source that drove
  the thesis. No documented source → no trade.
If any of these fail, skip the candidate and log which gate it failed.

## Before every BUY
- Your candidate list comes from **LATEST WEEKEND RESEARCH** in the system prompt.
  Only buy symbols that appear there with a qualifying confidence score; do not
  invent new candidates during market hours.
- Confirm the candidate passes the **Pre-trade entry gate** above (confidence ≥ 60,
  non-empty thesis, non-empty sources_used).
- Check the Robinhood MCP for the SETTLED cash balance. Never buy with unsettled
  funds (T+1).
- Confirm the EMA signal is still valid: the 55 (red) has crossed below ALL of
  8/13/21 and is the LOWEST line. Confirm it's a fresh crossover, not stale.
- Confirm no breaking news has invalidated the thesis from the weekend research
  (quick web check). If the thesis is broken, skip and log the reason.
- Check SPY health: is the overall market healthy? If SPY's own EMA signal is in
  a downtrend, lower confidence or skip.
- Check blackout windows: earnings within 5 days or Fed within 3 days -> skip.
- If all clear -> execute the buy via the Robinhood MCP, using the dollar
  allocation and confidence score from the weekend research (and the capital bands
  in strategy.json). Keep the 10% reserve; max 30% in one name.
- If invalidated -> skip and log the reason.
- If **LATEST MIDWEEK REVIEW** is present and flags a redeploy candidate, treat
  that candidate as your next buy target once cash settles.

## Thesis integrity check (every cycle, for every held position)

Run this even when the EMA shows HOLD and no stop-loss has triggered. The 55-bar
EMA is a lagging indicator — it can take hours or days to reflect a bad earnings
print, a CEO resignation, a sector shock, or a regulatory reversal. This check
is the only mechanism that can sell before the damage reaches −10%.

For **each open position**:
1. **Web search** `"[SYMBOL] news today"` — scan the top 3–5 results for:
   - Earnings miss or guidance cut
   - Regulatory action, lawsuit, or FDA rejection
   - CEO/CFO departure or scandal
   - Sector-wide shock (rate hike surprise, tariff, ban)
   - The specific "one thing that would invalidate this thesis" from the picks file
2. **Re-score confidence** (0–100) using the same formula as research:
   - If the thesis-breaking event has occurred → score drops to 0 → **sell immediately**
   - If sentiment has significantly shifted (e.g., analyst downgrade, key source turned bearish) → re-score; if now below 60 → **sell immediately**
   - If no material change → hold, no action needed
3. **Ribbon check**: the ribbon is four plain EMAs — EMA 8/13/21/55 (matches the
   operator's chart; its "TEMA"-labeled indicator is actually plain `ema()`). If the
   signal block shows **SELL state** for a held symbol — red(55)
   on top of all three other lines — **sell immediately**, even if the transition
   column reads NO_ACTION (the cross may have happened on an earlier bar).
4. If selling: execute via Robinhood MCP at market, set `reason="thesis_broken"` or
   `reason="ema_exit"` in `actions_taken` so the learning loop fires correctly.

Do **not** skip this section on "forced_news_check" cycles — that is exactly when
it is most important. A forced check fires every `NEWS_CHECK_HOURS` (default 4h)
specifically to run this section on days when the EMA never triggers.

## SELL
- Sell immediately when the 55 (red) crosses above ALL of 8/13/21 and becomes the
  HIGHEST line. Execute the sell via the Robinhood MCP at that moment.
- A held position whose signal reads **SELL state** is a sell NOW, regardless of
  transition — never wait for a fresh EXIT edge that already passed.
- Also sell immediately if the thesis integrity check above flags a thesis-breaking
  event or confidence decay below 60.
- After a sell, cash is unsettled for T+1 — note when it frees up.

## After every executed trade
- Append the trade to `trade_log.json` (symbol, side, qty, price, timestamp,
  confidence_at_entry, thesis, signal_state, linked research file, outcome=open).
- When a position closes, the orchestrator fires the postmortem/victory + rewrite.

## Always
- One trade per signal. Never average down into a loser.
- Hold while red stays the lowest line; do nothing (wait) while red is above all
  3 (downtrend).