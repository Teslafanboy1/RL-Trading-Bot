"""Engine B — Both-Ways Trend (PAPER). Operator plan 2026-09-23.

One ranked pool of stocks, commodities, crypto and INVERSE ETFs. The account is
limited-margin (no borrowing), so it cannot short directly; an inverse ETF in a
rising trend IS the short side. When the market falls, SQQQ/SOXS/SPXU trend up
and win the ranking on their own — no regime switch to tune.

Pure: closes in, weights out. Callers pass closes through YESTERDAY only (same
lag discipline as rotation_engine) — the engine never sees the bar it trades.
"""

STOCKS = ["NVDA", "AMD", "AVGO", "MU", "TSM", "PLTR", "META", "TSLA", "CRWD",
          "MRVL", "NET", "SHOP", "COIN", "HOOD", "QQQ", "SPY", "IWM"]
COMMODITIES = ["GLD", "SLV", "USO", "CPER", "UNG", "DBA", "URA"]
CRYPTO = ["IBIT", "ETHA"]
INVERSE = ["SQQQ", "SOXS", "SPXU", "SRTY"]
UNIVERSE = STOCKS + COMMODITIES + CRYPTO + INVERSE

TOP_N = 3
SMA_LEN = 100
REBALANCE_EVERY = 5       # trading days; weekly was the best risk-adjusted cadence in edge_lab4
LOOKBACKS = {"1w": 5, "1m": 21, "6m": 126}
WEIGHTS = {"1w": 0.3, "1m": 0.5, "6m": 0.2}   # same blend as rotation_engine's rank


def _ret(c, n):
    if len(c) <= n or not c[-n - 1]:
        return None
    return c[-1] / c[-n - 1] - 1


def score(closes):
    """Momentum score, or None if the asset is not in an up-trend. An asset is
    eligible only above its SMA and with a positive 1-month return."""
    if not closes or len(closes) < max(SMA_LEN, LOOKBACKS["6m"]) + 1:
        return None
    sma = sum(closes[-SMA_LEN:]) / SMA_LEN
    r = {k: _ret(closes, n) for k, n in LOOKBACKS.items()}
    if any(v is None for v in r.values()):
        return None
    if closes[-1] <= sma or r["1m"] <= 0:
        return None
    return sum(WEIGHTS[k] * r[k] for k in WEIGHTS)


def target_book(hist, top_n=TOP_N):
    """{sym: closes through yesterday} -> {"weights": {sym: w}, "ranked": [...]}.
    Equal weight across the top_n eligible names; nothing eligible -> all cash
    (a falling market with no inverse ETF trending yet is a reason to wait)."""
    scored = []
    for sym, c in hist.items():
        s = score(c)
        if s is not None:
            scored.append((s, sym))
    scored.sort(reverse=True)
    picks = [sym for _, sym in scored[:top_n]]
    w = round(1.0 / len(picks), 6) if picks else 0.0
    return {"weights": {s: w for s in picks},
            "ranked": [(sym, round(s, 4)) for s, sym in scored]}


def side_of(sym):
    return "short" if sym in INVERSE else "long"
