# skill_1_research — Weekend scanner (Phase A)

You find the best stock candidates for the coming week, size them, and verify they
can realistically contribute to the monthly 100% goal. You learn from past
postmortems and victories which sources and signals to trust most.

## Goal-pace check (do this first)

Read `progress_tracking` in strategy.json:
- `monthly_goal`, `current_return`, `required_weekly_return`, `month_start_value`, `current_value`
- Compute **remaining return needed** = 100% − current_return (e.g. if at +12%, need +88% more)
- Compute **weeks left** = rough calendar weeks remaining in the month
- Derive **minimum viable weekly return** = remaining_return / weeks_left
- Derive **minimum viable per-position move** = to contribute meaningfully, a 30%-sized
  position needs to move at least: (minimum_viable_weekly_return / 0.30). Round up.
  Example: need 40% weekly → position must move at least 13% in the week.
- Use this **move threshold** as a hard floor: any candidate whose realistic upside
  (based on recent ATR or % range over the last 5 trading days) falls below the threshold
  goes to watchlist only — not a buy candidate.

## Open position review (do this second, before scanning new candidates)

Read open positions from the Robinhood MCP (`get_equity_positions`). For **every** held position:

### Step A — Re-score existing positions
Run each held position through the **exact same** pipeline as new candidates:
1. **EMA gate**: compute the current EMA signal. If the position is now in SELL zone (55 EMA above all of 8/13/21), flag it as `SELL_SIGNAL` — recommend immediate exit regardless of reallocation.
2. **Momentum / move-potential**: recompute 5-day % range and recent momentum direction.
3. **Source scoring**: re-run the 0–100 confidence score (news, social, fundamentals, macro). Use the same weights from `source_performance`.
4. **Assign current confidence band**:
   - 90–100 → max allocation 30% of total portfolio
   - 75–89 → max allocation 20%
   - 60–74 → max allocation 15%
   - Below 60 → flag as `TRIM_TO_ZERO` (confidence has decayed below the trade floor)

### Step B — Check portfolio weight vs. confidence band
Compute each position's **current weight** = `position_market_value / total_portfolio_value × 100`.

Compare current weight to the band maximum from Step A:
- If `current_weight > band_maximum` → **over-weighted**: recommend a trim down to the band maximum.
- If confidence band has decayed (e.g., was 90+ at entry, now 65) → recommend trimming to the new band's maximum.
- If flagged `TRIM_TO_ZERO` or `SELL_SIGNAL` → recommend full exit.

### Step C — Free up capital
Sum the trim/exit proceeds from Step B. Add this to the available settled cash when sizing new picks. Clearly label it: "freeing $X from [SYMBOL] trim" in the output.

### Step D — Rank everything together
Before allocating, build **one combined ranked list** of:
- Existing positions (with re-scored confidence) marked as `[HOLD]`, `[TRIM]`, or `[EXIT]`
- New candidates marked as `[NEW]`

Sort by confidence score descending. The top N positions by confidence score should receive capital; lower-ranked positions (whether new or existing) lose allocation. This ensures the portfolio is always weighted toward the highest-conviction ideas, not just toward whatever was bought first.

---

## Candidate universe

Scan BOTH sources to build the candidate pool:

### A. Static high-beta universe (always scan these)
Check the EMA signal for every ticker in these groups via `signals.py` or Robinhood MCP quotes:

**Semiconductors / AI hardware**
NVDA, AMD, SMCI, ARM, AVGO, MRVL, MU, QCOM, INTC, TSM, LRCX, AMAT, KLAC, ON

**Software / Cloud / AI**
META, MSFT, GOOGL, PLTR, SNOW, NET, DDOG, CRWD, MDB, GTLB, AI, BBAI, SOUN

**High-beta tech / fintech / crypto-adjacent**
TSLA, COIN, HOOD, MSTR, RBLX, SOFI, UPST, AFRM, SQ, PYPL

**Biotech / high-volatility**
MRNA, NVAX, ACMR, EDIT, CRSP, RXRX, ARKG (ETF)

**Leveraged ETFs (use only if EMA signal is strong AND confidence ≥ 85)**
TQQQ, SOXL, FNGU

**Energy / commodities**
XOM, CVX, OXY, FANG, SLB, FCX, GOLD

### B. Dynamic MCP scan (always run alongside A)
1. Call `get_popular_lists` — pull the top-movers, most-watched, and trending lists.
2. Call `search` for terms like "momentum", "breakout", "52-week high" to surface
   names not on the static list.
3. Call `get_watchlists` and `get_watchlist_items` to include any symbols you've
   previously flagged.
4. Add any new names from B to the candidate pool before scoring.

## Evaluation pipeline (run for every candidate)

### Step 1 — EMA gate
Compute the EMA signal for the candidate (use `signals.py` output from the prompt,
or fetch via Robinhood MCP quotes to compute manually):
- **ENTER_LONG / BUY**: 55 EMA has just crossed below all of 8/13/21 → strong buy candidate
- **HOLD (BUY zone)**: 55 EMA already below all of 8/13/21 and spread is still widening → holdable, can enter on a dip
- **APPROACHING** (within ~2% separation between 55 and the next EMA): note as "watch" — if the cross happens intraweek, it qualifies
- **NEUTRAL or SELL zone**: skip entirely — do not buy into a downtrend

### Step 2 — Momentum / move-potential check
For each EMA-qualified candidate:
- Fetch recent price history: last close, 5-day high/low, and estimated ATR
- Compute **5-day % range** = (5d_high − 5d_low) / 5d_low × 100
- Compute **recent momentum** = (last_close − 5d_low) / 5d_low × 100 (positive = rising)
- Check if the 5-day % range ≥ the **move threshold** calculated in the goal-pace check
- Stocks that move too little for the goal are watchlist-only, not buys this cycle

### Step 3 — Source scoring (0–100 confidence)
Weight by `source_performance` in strategy.json (not static weights when performance data exists):
- **News** (web search): recent catalysts, analyst upgrades, positive earnings surprise, sector tailwinds
- **Social** (Reddit WSB / r/investing / r/stocks): sentiment direction, post volume trend
- **RSS** (Bloomberg / Reuters / MarketWatch): institutional coverage, macro relevance
- **Fundamentals** (Robinhood MCP): revenue growth, margins, short interest, insider buys
- **Macro**: Fed calendar, sector rotation signals, CPI/jobs data this week

Confidence formula (combine):
- Strong EMA signal (ENTER_LONG vs HOLD): +10 pts
- 5-day % range ≥ 2× move threshold: +15 pts
- Recent momentum positive (stock rising into the signal): +10 pts
- 2+ sources agree on bullish thesis: +20 pts base, +10 for each additional agreeing source
- No earnings within 5 days: +5 pts (earnings within 5 days = blackout → skip)
- No Fed meeting within 3 days: +3 pts
- High short interest (>15%): +5 pts (squeeze potential)
- Deduct 20 pts if only 1 source is bullish with no corroboration

### Step 4 — Blackout check
- Earnings within 5 calendar days → **skip** (move to watchlist with earnings date noted)
- Fed meeting / major macro event within 3 days → **skip**

### Step 5 — Portfolio-wide capital allocation
Allocate across the **combined ranked list** from the open position review (Step D above).
Capital pool = settled cash + any proceeds freed from trims/exits in Step C.

Allocation rules (applied to **total portfolio value**, not just new cash):
- 90–100 confidence → up to 30% of total portfolio value
- 75–89 confidence → up to 20% of total portfolio value
- 60–74 confidence → up to 15% of total portfolio value
- Below 60 → watchlist only (or exit if currently held)
- Always keep 10% cash reserve
- Max 30% per position at any time

Work top-down through the ranked list: assign each position its band maximum, deduct from
the available capital pool, stop when the pool is exhausted or all candidates are sized.
Existing positions already at or below their band maximum need no new capital — just confirm
their continued hold. Positions above their band maximum get a trim order.

**Target at least 5 qualified candidates before allocating.** If fewer than 5 pass the
EMA gate, expand the search: run additional web searches for "stocks breaking out this week",
"EMA crossover setups", "high momentum stocks", and pull any Robinhood MCP trending data
you haven't checked yet. Do not stop at 1–2 unless the market truly has no qualifying signals.

## Output

Write `research/weekend_picks_YYYY-MM-DD.md` with:

### Header
- Current portfolio value, settled cash, open positions
- Monthly goal progress: `current_return` vs `monthly_goal`, weeks left
- Move threshold used this cycle (e.g. "each position needs to move ≥12% to matter")

### Open position reassessment
For each currently held position, a one-line verdict:
- Re-scored confidence, current portfolio weight vs. band maximum, action: `HOLD` / `TRIM $X` / `EXIT`
- Brief reason (e.g. "confidence decayed from 82→58, over band at 28% weight")

### Ranked picks (ranked by confidence × move_potential score)
**FORMAT IS MANDATORY.** Each qualified candidate MUST be its own heading written
EXACTLY as:

`### #N — SYMBOL (Company Name) | Confidence: XX/100`

(e.g. `### #1 — HOOD (Robinhood Markets) | Confidence: 78/100`). The agent parses
this exact heading to (a) save this file at all and (b) extract the tickers that
execution watches. If you write the picks as prose, a single table, or any other
format, **the picks are silently dropped, the file is never saved, and execution
runs on a stale day-old picks file.** Every pick gets its own `### #N — SYMBOL`
heading — no exceptions, including pre-market prep runs with only a handful of picks.

Under each pick heading include:
- Confidence score (0–100), EMA state (ENTER_LONG / HOLD / APPROACHING)
- 5-day % range, recent momentum direction
- Entry target, exit target, stop-loss level (entry × 0.90)
- Dollar allocation and share count
- Sources that drove the thesis (and their current accuracy weights)
- The ONE thing that would invalidate this thesis

### Watchlist
- Symbols that passed EMA but missed the move threshold or confidence floor
- Symbols in blackout (with the earnings/Fed date)
- Approaching-crossover names with the trigger level to watch

### Deployment summary
- Trim/exit actions: "Trimming BTSG by $X (from 28% → 20% band), freeing $X"
- New buys: "Buying [SYMBOL] at market / limit $X — $Y allocated"
- "Net deployed: $X across N positions (N existing + N new), holding $Y in reserve"
- Which actions are executable Monday open vs. needing limit orders at entry target
