# Strategy redesign proposal — 2026-07-06

Companion to [audit_2026-07-06.md](audit_2026-07-06.md). Backtest code:
[edge_lab.py](edge_lab.py) (new) + backtest.py (existing rotation engine).
Nothing here is implemented yet — deliverable E awaits operator approval.

---

## B. The architecture

### B.0 The honest headline first

**25%/month as a sustained average is not achievable without a credible path to
ruin, and I will not build that.** 25%/mo = 1,350%/yr compounded. For scale: TQQQ
buy-and-hold through the greatest tech bull decade ever returned 43.5%/yr — and
spent one stretch **82% underwater**. Every configuration I tested that pushes the
mean month higher than the proposal below does it by taking drawdowns in the 60–95%
zone, where a bad quarter ends the account.

What the evidence supports building — the most aggressive design I can defend:

| | Backtest (10y) | Honest forward expectation |
|---|---|---|
| CAGR | +37 to +49% | **+20 to +35%/yr** |
| Mean month | +3.4% | **+1.5 to +2.5%** |
| Months ≥ +10% | 20–27% of months | ~15–20% |
| Months ≥ +25% | 2–5% of months (it DOES print 25% months) | ~1–3 /yr |
| Worst month | −16 to −19% | assume −20% |
| Max drawdown | 45–47% | **budget 35%, hard-halt at 25% monthly** |
| P(50% drawdown in a year) | 0.1–0.4% (bootstrap) | <2% (sample can't see unseen tails) |

The forward haircut is for universe/regime bias that no backtest hygiene fully
removes. The gap between this and 25%/mo is not an engineering gap — it is the
difference between edge and fantasy, and pretending otherwise is how SOXL/AMD/MU
happened at small scale.

### B.1 Sleeve 1 — R3X: vol-throttled concentrated momentum rotation (100% of book)

The engine that survived every test. Three public ingredients whose *combination and
tuning* is the proprietary part:

1. **Cross-sectional selection** (daily, deterministic Python): momentum rank
   `0.5·r_1m + 0.3·r_1w + 0.2·r_6m` over a ~50-name high-beta liquid universe
   **plus** 3x ETFs (they compete on the same rank). Eligibility: momentum gate
   (1m ≥ +8%, no negative 1w/1m/6m, score ≥ 40) AND price > SMA200. Hold the
   **top 3** equal-weight; rotate when a holding loses eligibility or a stop hits.
   Max 2 per sector; leveraged names at 1/leverage size with leverage-tight stops
   (all existing Phase-A rails stay).
2. **Portfolio throttle** (the new part that changed the risk math): sleeve
   exposure = `min(1, 0.35 / realized_vol_21d(sleeve))`, and **halved whenever VIX >
   VIX3M** (term-structure inversion = crash regime). Residual sits in cash.
   Measured effect: keeps ~100% of the return, cuts maxDD 63%→45%, cuts
   P(DD≥50%/yr) 4.1%→0.1%, turns 2018 from −14% to +4%, and walked through the
   2020 crash at +24/+3/+1% (Jan–Mar).
3. **Exits owned by Python**: 25% trailing stop from peak, leverage-adjusted hard
   stop, eligibility-loss rotation. **Plus broker-side GTC stop orders** as the
   process-death backstop (the MU lesson).

Why this beats what's deployed: the live v25 rotation is the *unthrottled baseline*
(backtests at 63–69% maxDD) with selection delegated to a model prompt that has
already violated its own overextension rule (MU). R3X is the same alpha with the
drawdown engine fixed and the decision loop deterministic.

### B.2 What I tested and killed (so it stays dead)

| Candidate | Result | Verdict |
|---|---|---|
| Gap/PEAD continuation (40 names, 10y, full grid) | exp −0.3%/trade, PF 0.68–1.2, no robust region | **dead** |
| Overnight anomaly (QQQ, 10y) | 1.5% CAGR, Sharpe 0.25 | **dead** |
| Regime-throttled TQQQ/SOXL core | 9–14% CAGR — deleveraged beta, Sharpe < B&H | **dominated by R3X** |
| RSI(2) dip-buys via TQQQ | real edge (72% win, PF 1.38, n=80) but +0.2pt CAGR in the book, worse tails, corr +0.38 | **not in live book**; optional paper sleeve |
| Model-scored confidence as sizing input | conf-80 entries: −5%, −2.6%, −17% | **removed from sizing entirely** |

### B.3 Options

Unchanged gates ($1k activation + proven shadow expectancy), but the shadow is
redesigned to mirror what would actually go live: **calls on R3X rotation leaders
priced ≤ $2.50/contract-share**, not ATM calls on $1,000 underlyings (every record
so far is `oversized=true` noise). Options remain measurement-only.

### B.4 The model's demoted role

Selection, sizing, exits, stops: **Python only.** The model keeps three jobs:
(a) morning research *narrative* for the operator (why the leaders lead — catalyst
context, earnings dates as advisory color); (b) order *placement agent* via MCP
(until/unless a direct API exists), always followed by the existing independent
broker reconciliation; (c) postmortem/audit writing. Every rule that must bind is
code; prompts are commentary.

---

## C. Backtest proof (all real Yahoo daily data, walk-forward, next-bar fills, costs charged)

Guards: signals on bar t use data ≤ t; fills next bar; 5bps/side (10bps stressed);
leveraged-ETF decay is in the traded price series; no shorting; warmup skipped;
bootstrap = stationary block bootstrap of daily returns (2,000 trials, 1y horizon).

### C.1 Headline: R3X (top-3, throttle 0.35) — 2016-07 → 2026-07

| Universe | CAGR | Sharpe | Sortino | maxDD | worst mo | mo ≥+10% | P(DD≥50%) | P(DD≥30%) |
|---|---|---|---|---|---|---|---|---|
| High-beta 27 (as deployed) | +39.5% | 1.23 | 1.93 | 45% | −16.2% | 20% | 0.1% | 17% |
| **Salted +23 known blowups** (PTON/GME/AMC/NIO/…) | **+49.0%** | **1.34** | 2.18 | 47% | −18.9% | 27% | 0.4% | 21% |
| Sector/index ETFs only (no single-name bias) | +17.9% | 1.06 | — | 19% | −6.0% | 4% | ~0 | ~0 |
| Baseline (no throttle — what v25 runs today) | +38.4% | 1.01 | 1.55 | **63%** | −22.2% | 23% | **4.1%** | 42% |

The salted-universe row is the key evidence: adding the decade's worst blowups
*improved* results — the engine rides momentum wherever it appears and exits before
the round trip. The edge is the mechanism, not the stock list.

### C.2 Robustness

- **Parameter neighborhood** (top_n × target_vol, 8 cells): overlay raises Sharpe in
  every cell (1.01→1.16–1.26 top-3; 1.13→1.23–1.40 top-2). No cherry-picked cell.
- **Half-period split**: 2016–21 +45.6%/yr (Sharpe 1.53); 2021–26 +33.7%/yr (1.02).
- **Friction stress ×2 (10bps/side)**: CAGR 39.5→37.3% — turnover is not the fragility.
- **Regime table**: BULL (SPY>200sma) +64%/yr at 34% DD; BEAR −19.5%/yr at 41% DD.
  2022 full bear year: −25%, worst month −16.5%, no month worse. 2020 crash:
  +24.3% Jan, +3.1% Feb, +1.3% Mar (throttle + eligibility sidestepped it).
- **Monthly distribution** (120 months): mean +3.4%, median 0.0%, best +69.5%,
  worst −16.2%, 36% of months ≥+5%, 20% ≥+10%, 3% ≥+25%.

### C.3 Aggression dial (operator's choice)

| Config | CAGR | Sharpe | maxDD | worst mo | note |
|---|---|---|---|---|---|
| top-3, tv=0.25 | +29.6% | 1.16 | 37% | −14.4% | conservative |
| **top-3, tv=0.35 (recommended)** | **+39.5%** | **1.23** | **45%** | **−16.2%** | default |
| top-2, tv=0.50 (max defensible) | +61.2% | 1.40 | 45% | −18.5% | higher single-name risk; hindsight bias bites harder at top-2 |

### C.4 Known residual biases (stated, not hidden)

Survivorship in Yahoo data (delisted names absent) inflates all single-name rows —
partially bounded by the salted and ETF-only rows. The decade tested is
bull-heavy — the regime table and 2022 path are the honest bear read. Backtest
bootstrap cannot price events worse than the sample (e.g. overnight −25% index
gap). Hence the forward haircut in B.0 and the hard risk rails in D.

---

## D. Implementation plan (nothing built until you approve)

### D.1 New files

- **`rotation_engine.py`** — the deterministic R3X brain: universe config, momentum
  rank, eligibility gates, throttle math (vol target + VIX gate), target-book
  computation `{symbol: weight}`. Pure functions, no I/O, fully unit-tested; the
  backtest and the live loop call the *same* functions (no sim/live drift — the
  TEMA lesson).
- **`risk_guard.py`** — kill-switch + watchdog: reads broker equity (portfolio MCP
  read) every cycle; if month-peak-to-now drawdown ≥ 25% → flatten everything,
  write `HALT` file (loop refuses to trade while it exists), fire webhook. Also
  writes a heartbeat file every cycle.
- **`watchdog.sh` + launchd plist** — separate process (survives agent death):
  alerts via `ALERT_WEBHOOK_URL` if the heartbeat is stale >30 min during market
  hours, and if MU-class breaches appear (position < stop threshold with no order).
- **`research/edge_lab.py`** — already written (the proof harness); stays as the
  place every future idea must win before going live.

### D.2 Changes to existing files

- **`agent.py`**: replace signal plumbing with `rotation_engine` targets; after every
  fill, **place a broker-side GTC stop order** (`place_equity_order` stop type) at
  the leverage-adjusted stop, and refresh it on adds/rotations — stops must survive
  the process dying (the MU failure). Equity/sizing denominator switches to the
  broker's `get_portfolio.total_value` (fixes the $255-vs-$395 deposit drift).
  Model turn shrinks to: narrative + order placement; reconciliation stays.
- **`strategy/strategy.json`**: new `rotation` block (universe, gates, tv, top_n),
  `risk_management.monthly_halt_dd: 0.25`, keep all Phase-A rails. Version-bumped
  via the existing snapshot machinery.
- **`skills/skill_1_research.md`**: rewritten — the model annotates the Python-chosen
  leaders (catalyst context, earnings dates), it no longer picks the book.
- **`options_shadow`**: contract selector re-pointed at affordable rotation leaders.
- **`run.sh`**: exports for the new config; `SIGNAL_INTERVAL` becomes irrelevant to
  selection (daily engine) — the intraday loop only monitors stops/throttle.

### D.3 Risk controls (the non-negotiables, all enforced in code)

1. Per-position: risk-based sizing (existing 2%-risk model), leverage-adjusted
   stops, **broker-side GTC stops**, 25% trailing stop.
2. Portfolio: max 3 names, max 2/sector, leveraged sleeve ≤ 25% notional,
   vol-throttle + VIX de-risking, 5% min cash.
3. Account: **monthly drawdown halt at −25% → flatten + HALT file + webhook**;
   manual file removal required to resume. Watchdog heartbeat. `ALERT_WEBHOOK_URL`
   must be set before go-live (5-minute ntfy.sh setup — this is a launch blocker).
4. Process: T+1-aware rotation (fund new entries from the throttle's cash buffer
   first); earnings-date blackout read from the MCP calendar deterministically.

### D.4 Paper→live promotion

1. **Week 1–2: full shadow.** R3X runs paper-only next to the current live book
   (which stays under its existing rails); every simulated order logged with real
   quotes. Promotion gate: shadow behavior matches backtest expectations (target
   book matches the engine's picks, throttle values sane, zero missed stops).
2. **Live at half throttle** (tv=0.175) for 2 weeks. Gate: no risk-rail breach,
   realized slippage <15bps/side.
3. **Full config.** Options sleeve stays shadow until account ≥$1k AND its own
   record is positive (unchanged).
4. Any HALT trigger, stop-breach >1.5× design risk, or sim/live divergence →
   automatic demotion one step and a postmortem before re-promotion.

### D.5 Immediate items regardless of approval

- **MU breach**: bot is live and should force-sell at the open (~−14%). Confirm it
  fires; the loss is already incurred — the lesson is D.2 #1 and #3.
- Fix `summary.current_value` deposit drift (bookkeeping bug, not strategy).
- Set `ALERT_WEBHOOK_URL`.
- Fix the one failing test (`test_full_flow_opens_momentum_shadow` — stub drift).

---

## ADDENDUM (same day, second research sprint) — RX-2 supersedes the above configs

**Correction first:** a paranoia pass found a same-day look-ahead in how the
VIX gate (and the experimental overlays) were applied — a gate computed from
day-t closes scaled day-t's own return. All configs re-run with a strict
one-day signal lag. Corrected numbers: R3X top-3/0.35 is **+32.6%/yr at 49%
maxDD** (previously overstated as +39.5%/45%); top-2/0.50-VIX is **+50.8%/yr
at 49% DD** (was +61.2%/45%). Every number below is look-ahead-free.

**The invention that survived: RISKX** — a 5-component cross-asset
risk-appetite composite used as the exposure throttle on the concentrated
momentum rotation: HYG/IEF (credit), XLY/XLP (consumer), CPER/GLD
(copper/gold), IWM/SPY (breadth), BTC 1-month momentum. Each component is a
public risk-on/off proxy; using their composite to throttle a concentrated
single-name momentum rotation is, to my knowledge, unpublished. Ablations:
removing any one component keeps CAGR within ~±5pts — no single input carries it.

**Also invented and honestly killed in this sprint:** dispersion-adaptive
concentration (its +77% was a weighting look-ahead; lagged it *subtracts*),
overnight-share quality filter (neutral), turn-of-month cycling (−12pts CAGR).

### RX-2 (top-2 rotation, tv=0.50 vol throttle × RISKX gate) — 10y, lagged, costs charged

| Metric | Value |
|---|---|
| CAGR | **+46.3%** (Sharpe 1.39, Sortino 2.34) |
| Max drawdown | **37%** (vs 49% for the VIX version at +50.8%) |
| Worst month | −13.8% |
| Monthly | mean +3.8%, median 0.0%, best +67.7%, 47% positive |
| Months ≥+10% / ≥+25% | 18% / 6% — P(≥+25% month within a year) ≈ 40% |
| Bootstrap 1y | P(DD≥30%) 9.3%, **P(DD≥50%) 0.0%** |
| Halves | 2016–21 +42.9% (DD 24%) / 2021–26 +49.8% (DD 34%) |
| 2022 bear | −19% |

Barbell variant (85% RX-2 + 15% top-1): +47.6%/yr, DD 34%, slightly more
+25% months (7%), slightly lower Sharpe — optional dial, not required.

**Honest forward expectation** after universe/regime haircuts: **+25–35%/yr,
mean month +2–3%**, with the same floor guarantees (broker-side stops, −25%
monthly halt, sector caps). Implementation delta vs the plan in D: the
throttle reads 6 extra free Yahoo series (HYG, IEF, XLY, XLP, CPER, GLD,
BTC-USD, IWM) — no new infrastructure.
