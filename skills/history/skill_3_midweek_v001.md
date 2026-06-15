# skill_3_midweek — Wednesday validator (Phase C)

Mid-week portfolio review. You re-score every open position from scratch, check
portfolio weights against confidence bands, execute trims and exits, redeploy freed
capital into better opportunities, and write a structured review file. This is the
same rigor as weekend research — not a qualitative gut-check.

## Step 1 — Pull live state
Read from the Robinhood MCP:
- `get_equity_positions` — current shares, avg cost per position
- `get_portfolio` — total portfolio value and settled buying power
- `get_equity_quotes` — live prices for all held symbols

Compute for each position:
- **Current market value** = shares × last price
- **Current portfolio weight** = market value / total portfolio value × 100
- **Unrealized P&L** = (last price − entry price) / entry price × 100

## Step 2 — Re-score every open position (0–100)

Run the **full scoring formula** from `skill_1_research.md` on each held symbol.
Do NOT carry forward the entry confidence score — re-derive it fresh from current data.

### EMA check
Compute the EMA signal for the symbol:
- `SELL` (red above all others) → flag `SELL_SIGNAL` — exit regardless of other factors
- `BUY/HOLD` (red still lowest) → continue scoring
- Lines converging (gap between red and next EMA shrinking vs. last week) → deduct 10 pts and flag as "trend weakening"

### News and sentiment check (web search)
Search `"[SYMBOL] news this week"` and the thesis invalidation condition from the picks file:
- Thesis-breaking event (earnings miss, CEO exit, regulatory reversal, key catalyst reversed) → score 0, exit immediately
- Significant negative shift (analyst downgrade, negative sector rotation) → deduct 20 pts
- No material change → no adjustment
- New positive catalyst since entry → add up to +15 pts

### Momentum check
- 5-day % range and direction (is the stock still moving toward the exit target?)
- If the stock has stalled or reversed without an EMA EXIT: deduct 10 pts
- If the stock is accelerating toward the exit target: add +10 pts

### Confidence band assignment
Map the re-scored value to its allocation band:
- 90–100 → max 30% of total portfolio
- 75–89 → max 20%
- 60–74 → max 15%
- Below 60 → `TRIM_TO_ZERO` — exit the position

## Step 3 — Weight vs. band check

For each position, compare **current weight** to its new **band maximum**:
- `current_weight > band_maximum` → over-weighted → recommend trim to band maximum
- `band_maximum` dropped due to confidence decay → recommend trim to new band maximum
- `TRIM_TO_ZERO` or `SELL_SIGNAL` → full exit
- `current_weight ≤ band_maximum` and confidence ≥ 60 → HOLD, no action

## Step 4 — Free capital and rank

Sum all trim/exit proceeds. Add to settled buying power.

Build a **combined ranked list** (same as weekend research Step D):
- Existing positions tagged `[HOLD]`, `[TRIM]`, or `[EXIT]` with re-scored confidence
- Any high-confidence redeployment candidates from the weekend picks file or a fresh scan

Sort by confidence descending. Capital flows top-down.

## Step 5 — Execute via Robinhood MCP

Execute all decisions in this order:
1. **EMA EXITs and TRIM_TO_ZERO positions first** — sell via `place_equity_order` at market
2. **Over-weighted trims** — sell the excess shares to bring position to band maximum
3. **Redeploy freed capital** into the top-ranked unowned candidate if:
   - EMA signal is BUY/HOLD
   - Confidence ≥ 75 (no weak redeployments)
   - Cash will be settled before the buy (T+1 check)
   - No earnings within 5 days, no Fed within 3 days

Report all actions in `actions_taken` so the learning loop fires.

## Step 6 — Settlement schedule

State explicitly:
- What cash is settled **right now**
- What cash settles **tomorrow** (from any sells executed today)
- What cash settles **later this week**

This prevents the execution skill from planning buys against unsettled funds.

## Output

Write `research/midweek_review_YYYY-MM-DD.md`:

### Header
- Total portfolio value, settled cash, date
- Weekly progress: current return vs. 100% monthly goal, weeks left

### Position verdicts table
| Symbol | Entry | Current | P&L% | Re-scored conf | Weight | Band max | Action |
|--------|-------|---------|------|----------------|--------|----------|--------|
One row per open position. Action = HOLD / TRIM $X / EXIT.

### Reasoning per position
For each non-HOLD position: 2–3 sentences on what changed (news, EMA, momentum,
weight vs. band) and why the action was chosen.

### Executed orders
List every MCP order placed this cycle with order ID.

### Settlement schedule
Explicit table: what cash is free now, tomorrow, and end of week.

### Redeployment
If capital was freed and redeployed: the target symbol, confidence, allocation, and
the thesis in one sentence. If no redeployment was made, explain why (e.g., no
qualifying EMA signal, all capital in reserve pending a better setup).
