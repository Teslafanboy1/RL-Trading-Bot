"""Engine A — Crowd Hunter (PAPER options). Operator plan 2026-09-23.

The operator's thesis: people pile into whatever is going up, and options are
how they bet on it. So this engine follows the crowd in, and fades it when the
crowd's favourite cracks:

  CALL  a stock the crowd is buying TODAY (big up day on heavy volume) that is
        also in a real up-trend (above its 50-day average, up over the month).
  PUT   a stock the crowd already ran up hard (+40% in a month) that is now
        breaking DOWN on heavy volume — the exhaustion trade.

Candidates come from Yahoo's free top-movers screens (zero model tokens). The
Brain (one Claude call a day, with web search) then has to find a REAL reason
for the move — no catalyst, no trade. Contracts are quoted at the real bid/ask
so the spread, which is what usually kills small options trades, is paid.

Pure: no I/O. agent.process_desk_engine_a owns the fetches and model calls.
"""
from datetime import date

MIN_PRICE = 3.0            # below this, option chains are thin or absent
MAX_PRICE = 150.0          # above this, one contract dwarfs the account
CALL_MIN_CHANGE = 5.0      # % up today
PUT_MAX_CHANGE = -5.0      # % down today
PUT_MIN_RUN_1M = 0.40      # the crowd must already have run it up this much
MIN_RVOL = 2.0             # today's volume vs 3-month average
SMA_LEN = 50

MAX_OPEN = 3
MAX_OPENS_PER_DAY = 1
MIN_CATALYST_CONF = 60
DTE_MIN, DTE_MAX = 14, 45
MAX_SPREAD_PCT = 0.15      # of mid; wider than this and the spread eats the edge
MAX_CONTRACT_USD = 150.0

TAKE_PROFIT = 1.00         # +100% on premium
PREMIUM_STOP = 0.50        # -50% on premium
MIN_DTE_HOLD = 7           # close before the last week of time decay
MAX_HOLD_DAYS = 14         # calendar days: a crowd move that stalls is over


def _ret(closes, n):
    if not closes or len(closes) <= n or not closes[-n - 1]:
        return None
    return closes[-1] / closes[-n - 1] - 1


def classify(mover, closes):
    """One mover -> ("call"|"put", reason) or (None, reason).

    mover: {symbol, price, change_pct, volume, avg_volume}
    closes: daily closes through YESTERDAY (the screen's own price is today's).
    """
    try:
        px = float(mover["price"])
        chg = float(mover["change_pct"])
        vol = float(mover.get("volume") or 0)
        avg = float(mover.get("avg_volume") or 0)
    except (KeyError, TypeError, ValueError):
        return None, "unparseable"
    if not (MIN_PRICE <= px <= MAX_PRICE):
        return None, "price_out_of_band"
    rvol = vol / avg if avg > 0 else 0.0
    if rvol < MIN_RVOL:
        return None, "no_crowd"
    if not closes or len(closes) < SMA_LEN + 1:
        return None, "short_history"
    sma = sum(closes[-SMA_LEN:]) / SMA_LEN
    r1m = _ret(closes, 21)
    if r1m is None:
        return None, "short_history"
    if chg >= CALL_MIN_CHANGE:
        if closes[-1] > sma and r1m > 0:
            return "call", "crowd_buying_uptrend"
        return None, "pop_without_trend"
    if chg <= PUT_MAX_CHANGE:
        if r1m >= PUT_MIN_RUN_1M:
            return "put", "crowd_favorite_cracking"
        return None, "drop_without_prior_run"
    return None, "quiet"


def rank(movers, closes_by_sym):
    """All qualifying setups, strongest crowd signal first:
    [{symbol, side, price, change_pct, rvol, reason}]."""
    out, seen = [], set()
    for m in movers:
        sym = m.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        side, reason = classify(m, closes_by_sym.get(sym))
        if not side:
            continue
        avg = float(m.get("avg_volume") or 0)
        rvol = float(m.get("volume") or 0) / avg if avg else 0.0
        out.append({"symbol": sym, "side": side, "price": float(m["price"]),
                    "change_pct": round(float(m["change_pct"]), 2),
                    "rvol": round(rvol, 2), "reason": reason})
    out.sort(key=lambda c: c["rvol"] * abs(c["change_pct"]), reverse=True)
    return out


def contract_cfg():
    """Selection rules in the shape options_shadow.validate_contract expects."""
    return {"selection": {"target_dte_min": DTE_MIN, "target_dte_max": DTE_MAX,
                          "max_bid_ask_pct_of_mid": MAX_SPREAD_PCT}}


def should_exit(pos, bid, today=None):
    """(close?, reason) for an open paper option. Sells at the BID."""
    today = today or date.today()
    try:
        entry = float(pos["entry_premium"])
        b = float(bid)
    except (KeyError, TypeError, ValueError):
        return False, ""
    if entry > 0 and b >= entry * (1 + TAKE_PROFIT):
        return True, "take_profit"
    if entry > 0 and b <= entry * (1 - PREMIUM_STOP):
        return True, "premium_stop"
    try:
        exp = date.fromisoformat(str(pos["expiry"])[:10])
        if (exp - today).days < MIN_DTE_HOLD:
            return True, "dte_expiry"
    except (KeyError, ValueError):
        pass
    try:
        opened = date.fromisoformat(str(pos["entry_date"])[:10])
        if (today - opened).days >= MAX_HOLD_DAYS:
            return True, "time_stop"
    except (KeyError, ValueError):
        pass
    return False, ""
