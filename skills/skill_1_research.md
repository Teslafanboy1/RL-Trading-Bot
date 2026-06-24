# skill_1_research — Weekend scanner (Phase A)

You find the best stock candidates for the coming week, size them, and verify they
can realistically contribute to the monthly 100% goal. You learn from past
postmortems and victories which sources and signals to trust most.

## Goal-pace check (do this first — INFORMATIONAL ONLY)

Read `progress_tracking` and `goal_framing` in strategy.json. The 100%/month figure is an
**aspirational ceiling, not a sizing input** (see goal_framing). Compute current return and
weeks left for context — but do **NOT** derive a "minimum viable per-position move" and use it
as a hard floor. That old rule was a lottery-ticket selector: it forced the book toward names
that *could* move 20%+/week, which is exactly how the −11.84% SOXL and −4.99% AMD chases got in
(together 73% of all loss dollars).

Instead, rank candidates by **risk-adjusted quality**: a clean ribbon setup + a durable catalyst,
sized under the risk model. A high-quality 8–12% setup that can't single-handedly hit the goal is
still a BUY — compounding clean wins is the plan. The real scoreboard is **beat SPY with
controlled drawdown**; never raise per-trade risk to chase the ceiling.

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

Scan BOTH sources to build the candidate pool. The scanner (Section B) sweeps the
entire US equity universe — Section A is the anchor set you always check regardless.

### A. Static anchor universe (always scan these)
Check the EMA signal for every ticker in these groups via `signals.py` or Robinhood MCP quotes:

**Semiconductors / AI hardware**
NVDA, AMD, SMCI, ARM, AVGO, MRVL, MU, QCOM, INTC, TSM, LRCX, AMAT, KLAC, ON

**Software / Cloud / AI**
META, MSFT, GOOGL, PLTR, SNOW, NET, DDOG, CRWD, MDB, GTLB, AI, BBAI, SOUN

**High-beta tech / fintech / crypto-adjacent**
TSLA, COIN, HOOD, MSTR, RBLX, SOFI, UPST, AFRM, SQ, PYPL

**Healthcare / pharma / biotech**
LLY, ABBV, REGN, ISRG, DXCM, HIMS, VKTX, MRNA, NVAX, ACMR, EDIT, CRSP, RXRX, ARKG

**Financials / payments**
JPM, GS, V, MA, AXP, NU, APP

**Consumer / e-commerce**
AMZN, SHOP, MELI, SE, CPNG

**Defense / aerospace / industrial**
LMT, RTX, GE, BA, AXON, HEI

**International ADRs (US-listed, high-beta)**
ASML, BIDU, BABA, SE, MELI, NU

**Energy / commodities**
XOM, CVX, OXY, FANG, SLB, FCX, GOLD

**Sector ETFs (liquid, EMA-friendly — treat as any other pick; no LR002 penalty)**
XLK, XLF, XLE, XLV, XBI, ARKK, GLD, SLV

**Leveraged ETFs (use only if EMA signal is strong AND confidence ≥ 85)**
TQQQ, SOXL, FNGU
— LR002: apply a −10 confidence penalty, require a same-day (≤2 trading days) signal — no
stale-momentum entries — size by the leverage haircut (÷ leverage) with the tighter
leverage-adjusted stop, and count them at their leverage toward the sector cap.

**Cheap-momentum options-sleeve universe (price < $30 — scan for the MOMENTUM OPTIONS WATCH output only)**
SOFI, PLUG, RIOT, MARA, F, NIO, LCID, RIVN, AMC, CHPT, RUN, PATH, DKNG, IONQ, RKLB, ACHR, JOBY, BBAI, SOUN, LUNR, GME, WBD, SNAP, PTON, CCL, VALE, GOLD, KGC, UEC, CIFR, WULF, BTBT, HUT
— These are checked **only** to feed the paper-options momentum sleeve (see Output → MOMENTUM OPTIONS WATCH). Validating their catalyst here — inside the one daily research pass — is what lets the options shadow run with **no extra model calls**. A name here qualifies for the WATCH list only if it shows real multi-timeframe momentum (up meaningfully over 1w/1m/6m) **and** you can name a durable catalyst. Do **not** add them to the equity `### #N` picks unless they independently pass the full equity scoring above.

### B. Dynamic MCP scan — sweep the full US equity universe (always run alongside A)

These three scanner calls together cover the entire US market. Run all three; merge
the resulting tickers into the candidate pool before scoring. Any name that surfaces
here AND passes the EMA gate is a valid candidate regardless of whether it is in
Section A.

**1. Daily gainers preset** — top movers across all US equities today:
```
create_scan(preset="DAILY_GAINERS")   # then run_scan(scan_id=...)
```
Take the top 20 tickers by % gain. These are the names the whole market is watching.

**2. Multi-day momentum scan** — sustained breakouts, not just one-day pops:
```
create_scan(
  preset="INITIAL",
  title="Multi-day momentum",
  filters=[
    {"filter_type": "FILTER_TYPE_PERCENT_CHANGE", "predicate": "PREDICATE_GREATER_THAN",
     "values": ["5"], "interval": "5d", "plot": "close"},
    {"filter_type": "FILTER_TYPE_VOLUME", "predicate": "PREDICATE_GREATER_THAN",
     "values": ["500000"], "interval": "1d"}
  ]
)
```
Take the top 20 by 5-day % change. This catches names that have been building for days.

**3. High options volume scan** — unusual institutional positioning:
```
create_scan(preset="HIGH_OPTIONS_VOLUME_IV")   # then run_scan(scan_id=...)
```
Take the top 15 by options volume. Unusual options activity is an early signal before
price moves; it also identifies names with liquid enough contracts for the options sleeve.

**4. Trending / watchlist sweep:**
- Call `get_popular_watchlists` — pull Robinhood's curated trending, most-watched, and
  sector-rotation lists. Add any ticker not already in the pool.
- Call `get_watchlists` + `get_watchlist_items` to include any symbols previously flagged.

**Merge and de-dupe:** combine all scanner results + Section A into one flat list. Run
the EMA gate (Step 1 below) on every ticker in the merged list. Only EMA-qualified
names advance to Step 2+.

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

### Step 5 — Portfolio-wide capital allocation (risk-based + factor-capped)
Allocate across the **combined ranked list** from the open position review (Step D above).
Capital pool = settled cash + any proceeds freed from trims/exits in Step C.

**Size by RISK, not flat bands** (`position_sizing` in strategy.json):
- Target size (% of total equity) = min(confidence-band ceiling, risk_per_trade ÷ stop) ÷ leverage.
  With risk_per_trade 2% and a 10% stop this caps a normal name at **20%** of equity — the old 30%
  top band now needs a tighter stop to reach. The band (30/20/15) is a **ceiling, not the target**.
- Leveraged ETFs are divided by their leverage (a 3x name gets ~1/3 the dollars) and use the
  tighter leverage-adjusted stop.
- Below 60 confidence → watchlist only (or exit if held). Always keep the 10% cash reserve.

**Respect `factor_exposure_limits`** (the all-semis concentration is what blew up the book — SOXL,
AMD, AMAT and the NVDA/AVGO/MU/SMCI picks were all one leveraged SOX bet):
- Max **40%** of equity in any one sector (leveraged ETFs counted at leverage); max **2 names** per
  sector. If the ranked list is top-heavy in one sector, cap it and deploy the rest into the next
  sector down — or hold cash. A book diversified across factors beats five correlated semis.

**Concentrate — `position_sizing.max_concurrent_positions` (currently 5):** hold **at most 5 names**.
Deploy **top-down**: fill the highest-confidence idea to its risk-based size before funding the next.
Do **not** dilute into marginal 60–74 names while a 90+ name still has capacity under the per-name and
sector caps. If 5 names are already strong, stop — concentration into conviction is the point. This is
deliberately higher-variance than a wide book; the per-name (30%), sector (40%), and cash-reserve (10%)
caps are what keep it survivable.

**Capped leverage sleeve — `factor_exposure_limits.leveraged_sleeve_max_pct` (currently 25%):** total
**NOTIONAL** in >1x daily-reset ETFs (TQQQ/SOXL/FNGU/…) must stay **≤ 25% of the account**. Measured at
notional (not leverage-adjusted) so a 60% sleeve crash costs ~15% of the account, not the account. A 3x
ETF can gap 60%+ overnight and **no stop beats a gap** — this cap is the only real defense, so never
exceed it even if a leveraged name scores highest. LR002 (÷leverage sizing, tighter stop, ≤2-day signal
staleness, −10 confidence) still applies on top.

Work top-down through the ranked list: assign each position its risk-based size, **check the sector cap,
the ≤5-name concentration limit, AND the ≤25% leveraged-sleeve cap before committing**, deduct from the
pool, and stop when the pool is exhausted, the reserve is
reached, or all candidates are sized. Existing positions at/below their risk-based size need no new
capital; those above it or breaching a sector cap get a trim order.

**Target at least 5 qualified candidates before allocating.** If fewer than 5 pass the
EMA gate, expand the search: run additional web searches for "stocks breaking out this week",
"EMA crossover setups", "high momentum stocks", and pull any Robinhood MCP trending data
you haven't checked yet. Do not stop at 1–2 unless the market truly has no qualifying signals.

## Output

Write `research/weekend_picks_YYYY-MM-DD.md` with:

### Header
- Current portfolio value, settled cash, open positions
- Goal-pace context: `current_return` vs the 100% ceiling, weeks left — **informational only**
- Risk budget in force: per-trade risk %, the resulting max non-leveraged size, and the sector cap

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

### MOMENTUM OPTIONS WATCH
The cheap-momentum names (price < $30, from the options-sleeve universe + any cheap dynamic-scan movers) that show **real multi-timeframe momentum AND a durable catalyst** — these feed the paper-options momentum sleeve. The agent parses this block verbatim, so the format is a **hard contract**: this exact `## MOMENTUM OPTIONS WATCH` heading, then ONE line per name:

`- SYMBOL | conf XX | catalyst: <one line on WHY it is moving and whether it can continue>`

- `conf XX` is your 0–100 confidence the move is real and durable (the same scoring rigor as an equity pick — it IS the catalyst validation the options sleeve relies on). Only list names you'd score ≥ 60.
- `catalyst:` must name a concrete, durable reason (earnings/guidance beat, FDA/contract win, sector tailwind, catalyst-backed squeeze). If the move is an unexplained or pure-meme pump, **do not list it** — an unexplained spike is a reject.
- List 0–6 names, best first. If none qualify, write the heading followed by `- (none this week)`.
- These are SEPARATE from the equity `### #N` picks and do **not** trigger equity buys; they only tell the paper-options engine which cheap momentum names already have a vetted catalyst, so it spends no extra tokens re-researching them.

Example:
```
## MOMENTUM OPTIONS WATCH
- CIFR | conf 88 | catalyst: crypto miner expanding into AI-datacenter leasing; signed HPC hosting deal, sector tailwind intact
- WULF | conf 72 | catalyst: TeraWulf AI/HPC hosting contract ramp; powered-datacenter scarcity bid
- (none else this week)
```

### Deployment summary
- Trim/exit actions: "Trimming BTSG by $X (from 28% → 20% band), freeing $X"
- New buys: "Buying [SYMBOL] at market / limit $X — $Y allocated"
- "Net deployed: $X across N positions (N existing + N new), holding $Y in reserve"
- Which actions are executable Monday open vs. needing limit orders at entry target
