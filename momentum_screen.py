"""momentum_screen.py — multi-timeframe momentum screener (Phase B+ shadow input).

The Phase B options engine (options_shadow.py) is a generic paper-trade EXECUTION
layer: give it an underlying + a validated contract and it tracks spread-aware P&L.
What it was missing is a SIGNAL that matches the operator's actual edge — not the
EMA ribbon, but *multi-timeframe momentum on cheap stocks with a real catalyst*:

    "stocks up ~15%/week, ~30%/month, ~50%/6mo, ~100%/year — and there should be
     a REASON they are going up like that."

This module is the deterministic half of that edge: from a daily close series it
computes the return over each timeframe, scores how strongly a name is trending
across ALL of them (persistence, not a single-bar pop), and gates on price so the
options it implies are actually affordable for a small account. The *why* (catalyst
validation) is a model call in agent.py — this module only finds the candidates that
are worth paying the model to research.

PURE standard-library, deterministic, no I/O, no network — the daily closes are
injected by the caller (agent.py reuses signals._fetch_yahoo at 1d/1y). Everything
here is unit-testable with synthetic price arrays. It is never the trading path:
it only proposes underlyings for the READ-ONLY shadow pass.

Trading-day lookbacks (≈ calendar): 1w=5, 1m=21, 6m=126, 1y=252 bars.
"""

# Trading-day bar counts per timeframe (Yahoo 1d series ≈ trading days, not calendar).
LOOKBACK_BARS = {"1w": 5, "1m": 21, "6m": 126, "1y": 252}

# Default targets — the operator's stated edge. A name hitting all of these is a
# textbook multi-timeframe momentum leader; the score measures how close it gets.
DEFAULT_TARGETS = {"1w": 0.15, "1m": 0.30, "6m": 0.50, "1y": 1.00}

# Score weights per timeframe. The 1-month is the anchor (current but less noisy
# than the 1-week pop); the 1-year confirms it's a durable trend, not a dead-cat.
DEFAULT_WEIGHTS = {"1w": 0.20, "1m": 0.35, "6m": 0.25, "1y": 0.20}


# ---------------------------------------------------------------- returns
def period_return(closes, lookback_bars):
    """Simple % return over the last `lookback_bars` (e.g. 0.30 = +30%). Returns
    None if the series is too short or the reference price is non-positive — a
    missing timeframe is "unknown", never silently treated as 0%."""
    if not closes or lookback_bars <= 0 or len(closes) <= lookback_bars:
        return None
    past = closes[-1 - lookback_bars]
    last = closes[-1]
    if past is None or last is None or past <= 0:
        return None
    return last / past - 1.0


def multi_timeframe_returns(closes, lookbacks=None):
    """Return {timeframe: pct or None} for every configured lookback."""
    lookbacks = lookbacks or LOOKBACK_BARS
    return {tf: period_return(closes, bars) for tf, bars in lookbacks.items()}


# ---------------------------------------------------------------- score / gate
def momentum_score(returns, targets=None, weights=None):
    """0–100 score: weighted average of each timeframe's progress toward its target,
    each capped at 1.5x target so one explosive timeframe can't mask weakness in the
    others. Timeframes with no data (None) are dropped and the remaining weights are
    renormalised, so a name with only 3 months of history still scores on what exists."""
    targets = targets or DEFAULT_TARGETS
    weights = weights or DEFAULT_WEIGHTS
    num = 0.0
    wsum = 0.0
    for tf, ret in returns.items():
        if ret is None:
            continue
        target = targets.get(tf)
        w = weights.get(tf)
        if not target or not w:
            continue
        progress = max(0.0, min(ret / target, 1.5))  # 0..1.5, negative clamped to 0
        num += w * progress
        wsum += w
    if wsum <= 0:
        return 0.0
    # progress of 1.0 (hit target) maps to ~67/100; 1.5 (blew past) → 100.
    return round(min(100.0, (num / wsum) / 1.5 * 100.0), 1)


def passes_gate(returns, cfg):
    """Hard qualification gate, separate from the (continuous) score. A candidate
    qualifies only if:
      • the ANCHOR timeframe (default 1m) meets its target — fresh, current momentum;
      • no required timeframe is NEGATIVE (consistent uptrend, not a bounce in a
        downtrend) when require_all_positive is set;
      • the overall score clears min_score.
    Returns (ok: bool, reason: str)."""
    targets = {**DEFAULT_TARGETS, **(cfg.get("targets") or {})}
    anchor = cfg.get("anchor_timeframe", "1m")
    anchor_ret = returns.get(anchor)
    if anchor_ret is None:
        return False, f"no_data:{anchor}"
    anchor_target = targets.get(anchor, DEFAULT_TARGETS.get(anchor, 0.30))
    if anchor_ret < anchor_target:
        return False, f"anchor_below_target:{anchor}={round(anchor_ret, 3)}<{anchor_target}"

    if cfg.get("require_all_positive", True):
        for tf in cfg.get("positive_timeframes", ["1w", "1m", "6m"]):
            r = returns.get(tf)
            if r is not None and r < 0:
                return False, f"negative_timeframe:{tf}={round(r, 3)}"

    score = momentum_score(returns, targets, cfg.get("weights"))
    if score < cfg.get("min_score", 55):
        return False, f"score_below_min:{score}<{cfg.get('min_score', 55)}"
    return True, "ok"


def affordable_underlying(price, cfg):
    """Cheap-stock gate. A $100→$1000 account can only afford options on low-priced
    underlyings (an ATM NVDA call is hundreds of dollars; an ATM call on a $6 stock
    is a few dollars). Skip names priced above max_underlying_price. Returns
    (ok, reason)."""
    cap = cfg.get("max_underlying_price")
    if cap is None:
        return True, "ok"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False, "bad_price"
    if p <= 0:
        return False, "bad_price"
    if p > cap:
        return False, f"too_pricey:{round(p, 2)}>{cap}"
    return True, "ok"


# ---------------------------------------------------------------- screen
def evaluate(symbol, closes, last_price, cfg):
    """Full per-candidate evaluation. Returns a dict the caller can rank/log:
    {symbol, last_price, returns, score, qualified, reason, affordable, affordable_reason}.
    Never raises on bad data — a candidate that can't be evaluated is simply
    qualified=False with the reason why."""
    try:
        returns = multi_timeframe_returns(closes, cfg.get("lookbacks"))
    except Exception:
        returns = {tf: None for tf in LOOKBACK_BARS}
    score = momentum_score(returns, {**DEFAULT_TARGETS, **(cfg.get("targets") or {})},
                           cfg.get("weights"))
    aff_ok, aff_reason = affordable_underlying(last_price, cfg)
    gate_ok, gate_reason = passes_gate(returns, cfg)
    qualified = bool(gate_ok and aff_ok)
    reason = "ok" if qualified else (gate_reason if not gate_ok else aff_reason)
    return {
        "symbol": symbol,
        "last_price": round(float(last_price), 4) if last_price else None,
        "returns": {tf: (round(r, 4) if r is not None else None)
                    for tf, r in returns.items()},
        "score": score,
        "qualified": qualified,
        "reason": reason,
        "affordable": aff_ok,
        "affordable_reason": aff_reason,
    }


def screen(candidates, cfg):
    """Rank a batch of candidates by momentum score, highest first.

    `candidates` = {symbol: {"closes": [...], "last_price": float}}. Returns the list
    of evaluation dicts (all candidates, qualified flagged), sorted by score desc, so
    the caller can take the top-N qualified names to send for catalyst validation."""
    evals = []
    for sym, d in candidates.items():
        evals.append(evaluate(sym, d.get("closes") or [], d.get("last_price"), cfg))
    evals.sort(key=lambda e: e["score"], reverse=True)
    return evals


def qualified(evals):
    """Just the qualified candidates, score order preserved."""
    return [e for e in evals if e.get("qualified")]
