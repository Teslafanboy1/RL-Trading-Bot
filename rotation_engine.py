"""rotation_engine.py — the deterministic RX-3 brain (approved 2026-07-06).

RX-3 = top-2 concentrated momentum rotation
       x portfolio vol throttle (target 50% ann. vol on the sleeve's own returns)
       x RISKX 5-signal cross-asset risk-appetite gate
       + DEFENS: capital freed by RISKX rotates into the strongest defensive
         asset (GLD/TLT/XLU/XLP) instead of sitting idle
       + residual cash swept to T-bills (SGOV) by the caller.

Proven in research/edge_lab*.py (10y walk-forward, lagged signals, costs):
~+51%/yr, Sharpe ~1.5, maxDD 33%; salted-universe floor ~+39%. Honest forward
expectation +25-40%/yr. This module is the SAME math the backtest ran — pure
functions, no I/O, no network; the caller injects closes so live and backtest
can never drift apart (the TEMA lesson).

Lag discipline: every function takes "closes through yesterday's close" and
returns a decision to execute TODAY. The caller must never feed it intraday
data for the decision series.

Selection (per backtest.py MOM_CFG semantics):
  momentum rank = 0.5*r_1m + 0.3*r_1w + 0.2*r_6m
  eligible      = momentum gate (1m >= +8%, 1w/1m/6m all non-negative,
                  screen score >= 40) AND close > SMA200
  hold top-2 equal-weight; exits by trailing stop / hard stop / lost
  eligibility are enforced by the caller's position machinery (same rules as
  risk_management), rotation fills the freed slot with the next leader.
"""

import math

import momentum_screen as mscreen

# --- selection config (mirrors backtest.py MOM_CFG — the tested values)
MOM_CFG = {
    "targets": {"1w": 0.0, "1m": 0.08, "6m": 0.15, "1y": 0.25},
    "anchor_timeframe": "1m",
    "require_all_positive": True,
    "positive_timeframes": ["1m", "6m"],
    "min_score": 40,
}
RANK_WEIGHTS = {"1m": 0.5, "1w": 0.3, "6m": 0.2}
TREND_LEN = 200
TOP_N = 2

# --- throttle config (RX-3 tested values)
TARGET_VOL = 0.50          # ann. vol target on the sleeve's own daily returns
VOL_LOOKBACK = 21
PPY = 252

# --- RISKX composite: (numerator, denominator|None) ratio-momentum components.
# denominator None => plain 1m self-momentum. Each contributes 1 when risk-on.
RISKX_COMPONENTS = [("HYG", "IEF"), ("XLY", "XLP"), ("CPER", "GLD"),
                    ("IWM", "SPY"), ("BTC-USD", None)]
RISKX_LOOKBACK = 21

# --- DEFENS sleeve
DEFENSIVE_ASSETS = ["GLD", "TLT", "XLU", "XLP"]
DEFENS_MIN_FREED = 0.20    # deploy defensives only when RISKX frees >20%
DEFENS_LOOKBACK = 21


# ------------------------------------------------------------------ helpers
def _ret(closes, lb):
    """Simple return over the last lb bars, or None if the series is short."""
    if not closes or len(closes) <= lb or closes[-1 - lb] is None:
        return None
    past = closes[-1 - lb]
    return closes[-1] / past - 1.0 if past > 0 else None


def sma_last(closes, length):
    """SMA of the trailing `length` closes, or None if short."""
    if len(closes) < length:
        return None
    win = closes[-length:]
    return sum(win) / length


# ------------------------------------------------------------------ selection
def momentum_rank(closes):
    """Uncapped blended momentum strength (the rotation's rank score).
    Distinct from momentum_screen's capped 0-100 score, which saturates."""
    r = {tf: _ret(closes, mscreen.LOOKBACK_BARS[tf]) for tf in ("1w", "1m", "6m")}
    return sum(RANK_WEIGHTS[tf] * (r[tf] or 0.0) for tf in RANK_WEIGHTS)


def is_eligible(closes, cfg=None):
    """Momentum gate + trend filter. closes = daily closes through yesterday."""
    cfg = cfg or MOM_CFG
    rets = mscreen.multi_timeframe_returns(closes)
    gate_ok, reason = mscreen.passes_gate(rets, cfg)
    if not gate_ok:
        return False, reason
    ma = sma_last(closes, TREND_LEN)
    if ma is None:
        return False, "insufficient_history_for_trend"
    if closes[-1] <= ma:
        return False, "below_sma200"
    return True, "ok"


def select_leaders(universe_closes, top_n=TOP_N, held=None, sector_of=None,
                   max_per_sector=2):
    """Rank eligible names and return the target holdings list (<= top_n).

    universe_closes — {symbol: [daily closes through yesterday]}
    held            — symbols currently held; a held name that is still
                      eligible and still in the top ranks is kept (reduces
                      churn: rotation only replaces names that dropped out).
    sector_of       — optional callable(symbol)->sector for the 2/sector rail.
    """
    held = set(held or [])
    scored = []
    for sym, closes in universe_closes.items():
        ok, _ = is_eligible(closes)
        if ok:
            scored.append((momentum_rank(closes), sym))
    scored.sort(reverse=True)

    picks, sector_count = [], {}

    def try_add(sym):
        if sector_of:
            sec = sector_of(sym)
            if sector_count.get(sec, 0) >= max_per_sector:
                return False
            sector_count[sec] = sector_count.get(sec, 0) + 1
        picks.append(sym)
        return True

    eligible_syms = [s for _, s in scored]
    # keep still-eligible holds that remain in the top 2*top_n ranks
    for sym in eligible_syms[: top_n * 2]:
        if sym in held and len(picks) < top_n:
            try_add(sym)
    for sym in eligible_syms:
        if len(picks) >= top_n:
            break
        if sym not in picks:
            try_add(sym)
    return picks, scored


# ------------------------------------------------------------------ RISKX
def riskx_score(component_closes):
    """0..1 risk-appetite scale from the 5-component composite.

    component_closes — {"HYG": [...], "IEF": [...], ...} closes through
    yesterday. A component is risk-on (1) when its ratio (or self) is above its
    value RISKX_LOOKBACK bars ago; missing data counts 0.5 (neutral).
    Mapping (tested): >=4 -> 1.0, >=3 -> 0.75, >=2 -> 0.5, else 0.25."""
    score = 0.0
    for num, den in RISKX_COMPONENTS:
        a = component_closes.get(num)
        b = component_closes.get(den) if den else None
        if not a or len(a) <= RISKX_LOOKBACK or (den and (not b or len(b) <= RISKX_LOOKBACK)):
            score += 0.5
            continue
        if den:
            now = a[-1] / b[-1] if b[-1] > 0 else None
            then = (a[-1 - RISKX_LOOKBACK] / b[-1 - RISKX_LOOKBACK]
                    if b[-1 - RISKX_LOOKBACK] > 0 else None)
        else:
            now, then = a[-1], a[-1 - RISKX_LOOKBACK]
        if now is None or then is None:
            score += 0.5
        elif now > then:
            score += 1.0
    if score >= 4:
        return 1.0
    if score >= 3:
        return 0.75
    if score >= 2:
        return 0.5
    return 0.25


# ------------------------------------------------------------------ throttle
def vol_scale(sleeve_daily_returns, target_vol=TARGET_VOL, lookback=VOL_LOOKBACK):
    """min(1, target/realized) from the sleeve's own trailing daily returns
    (STRICTLY past — the caller passes returns through yesterday). 1.0 until
    enough history exists (matches backtest warmup behavior)."""
    r = [x for x in sleeve_daily_returns if x is not None][-lookback:]
    if len(r) < lookback:
        return 1.0
    mu = sum(r) / len(r)
    var = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
    rv = math.sqrt(var) * math.sqrt(PPY)
    return min(1.0, target_vol / rv) if rv > 0 else 1.0


# ------------------------------------------------------------------ DEFENS
def defensive_pick(defensive_closes, lookback=DEFENS_LOOKBACK):
    """Strongest defensive asset by 1m momentum, only if positive. Returns
    (symbol|None, momentum). closes through yesterday (lag discipline)."""
    best, bm = None, 0.0
    for sym, closes in defensive_closes.items():
        r = _ret(closes, lookback)
        if r is not None and r > bm:
            best, bm = sym, r
    return best, bm


# ------------------------------------------------------------------ target book
def target_book(universe_closes, component_closes, defensive_closes,
                sleeve_daily_returns, held=None, sector_of=None,
                leverage_factor=None, full_deploy=False, top_n=None):
    """The whole RX-3 allocation in one call. Returns a dict:

      {"weights": {SYM: w, ...},          # rotation leaders
       "defensive": {SYM: w} | {},        # DEFENS sleeve
       "cash": w,                         # residual (sweep to SGOV)
       "scale": s, "riskx": g, "vol_scale": v,
       "leaders": [...], "scored": [(rank, sym), ...]}

    Weights sum to 1.0. Leveraged names get their weight divided by
    leverage_factor(sym) (the existing LR002 haircut), freed weight -> cash.
    All inputs are closes through YESTERDAY; execute at today's prices.

    full_deploy (RX-4, operator-directed 2026-08-04 hypothetical): skip the
    RISKX/vol-throttle sizing and the DEFENS carve-out entirely, always
    putting 100% into the top-N leaders (leverage haircut still applies —
    that's a per-name safety rule, not a buying-power throttle).

    top_n (RX-4, operator-directed 2026-08-04): how many leaders to hold,
    overriding the module default TOP_N (2). RX-4 uses a wider spread (6) so
    "use the whole portfolio" doesn't mean "all of it on 2 names" — same
    momentum ranking and same max_per_sector cap as RX-3, just filling more
    slots off the same ranked list before the money runs out."""
    n = top_n or TOP_N
    g = riskx_score(component_closes)
    v = vol_scale(sleeve_daily_returns)
    scale = 1.0 if full_deploy else g * v
    leaders, scored = select_leaders(universe_closes, top_n=n, held=held, sector_of=sector_of)

    weights = {}
    if leaders:
        per = scale * (len(leaders) / n) / len(leaders)  # unfilled slot stays cash
        for sym in leaders:
            lf = float(leverage_factor(sym)) if leverage_factor else 1.0
            weights[sym] = round(per / lf if lf > 1 else per, 4)

    defensive = {}
    if not full_deploy:
        freed = max(0.0, 1.0 - g)          # RISKX-freed fraction only (tested form)
        if freed > DEFENS_MIN_FREED:
            pick, _ = defensive_pick(defensive_closes)
            if pick:
                defensive[pick] = round(freed, 4)

    invested = sum(weights.values()) + sum(defensive.values())
    return {"weights": weights, "defensive": defensive,
            "cash": round(max(0.0, 1.0 - invested), 4),
            "scale": round(scale, 4), "riskx": g, "vol_scale": round(v, 4),
            "leaders": leaders, "scored": scored[:10]}
