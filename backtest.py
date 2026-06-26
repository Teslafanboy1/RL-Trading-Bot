"""
backtest.py — walk-forward backtest of the live EMA-ribbon strategy.

Why this exists
---------------
Before this, the only evidence the ribbon strategy had any edge was the live
trade log: n=5 "clean" trades, 20% win rate, -1.64% expectancy (strategy.json
-> expectancy). Five trades tells us NOTHING statistically — it can't separate
"no edge" from "unlucky". You cannot responsibly size, leverage, or add options
on top of an edge you have not measured. This module measures it.

It reuses signals.py's EXACT functions (ema, classify_signal,
detect_transition, LENGTHS, MIN_BARS, WARMUP_OK) so the backtest signal is
identical to the one the live bot trades — a backtest that computes the signal
differently than production is worthless. It mirrors the live trade rules:
  - ENTER_LONG transition  -> open long
  - SELL state while held   -> exit (ema_exit), matching agent's state-based sell
  - close <= entry*(1-stop) -> exit (stop_loss), leverage-adjusted per LR002

Honesty guards (no look-ahead, no free lunch):
  - The signal is computed on bar i; fills happen at the bar i+1 close. You
    cannot trade on a bar's close using a signal derived from that same close.
  - The first WARMUP_OK bars are skipped so the EMA(55) seed has washed out
    before any trade (matches signals.WARMUP_OK semantics).
  - Per-side transaction cost (COST_BPS, default 5bps) is charged on every fill.
  - A buy-and-hold benchmark is computed per symbol — if the signal can't beat
    just holding the name, it has no edge worth trading.

Data: free Yahoo history (closes + timestamps). Intraday history is short — at
30m you only get ~60d of bars, which is itself a finding: you CANNOT get a
statistically meaningful intraday backtest from free data. Default interval is
1d / 10y for a real sample; override with BACKTEST_INTERVAL / BACKTEST_RANGE.
A data/<SYMBOL>.csv (Close column) overrides Yahoo, same as signals.py.

CLI:
    python3 backtest.py SPY MU CAT              # default 1d/10y
    BACKTEST_INTERVAL=1h BACKTEST_RANGE=2y python3 backtest.py SPY
    python3 backtest.py --universe               # the strategy's full universe
    python3 backtest.py SPY --md                 # also write a markdown report
"""

import os
import ssl
import csv
import json
import math
import urllib.request

import signals  # reuse the EXACT live signal math
import momentum_screen as mscreen  # reuse the operator's multi-timeframe momentum edge

ROOT = os.path.dirname(os.path.abspath(__file__))

# Default to a long DAILY window: a real statistical sample. Intraday ranges are
# capped by Yahoo (1h<=730d, 30m/15m<=60d) and noted in the report.
INTERVAL = os.environ.get("BACKTEST_INTERVAL", "1d")
RANGE = os.environ.get("BACKTEST_RANGE") or {
    "1d": "10y", "1h": "2y", "30m": "60d", "15m": "60d", "5m": "60d", "1m": "7d",
}.get(INTERVAL, "10y")
COST_BPS = float(os.environ.get("BACKTEST_COST_BPS", "5"))   # per side
DEFAULT_STOP = 0.10                                          # base stop (LR002)

# Sizing layer (portfolio overlay — see sizing()). VOL_LOOKBACK bars of trailing
# returns define each name's volatility; positions are weighted to equalise risk
# (inverse-vol), and an optional time-series overlay scales gross exposure toward
# TARGET_VOL_ANNUAL, capped at MAX_LEVERAGE. None of this changes WHICH trades
# fire — only how many dollars each gets.
VOL_LOOKBACK = int(os.environ.get("BACKTEST_VOL_LOOKBACK", "20"))
TARGET_VOL_ANNUAL = float(os.environ.get("BACKTEST_TARGET_VOL", "0.15"))
MAX_LEVERAGE = float(os.environ.get("BACKTEST_MAX_LEVERAGE", "3.0"))
INVVOL_CAP_X = float(os.environ.get("BACKTEST_INVVOL_CAP_X", "3.0"))  # per-name cap, xequal

# Regime filter: only take long entries when the broad market (SPY) is above its
# own long-term trend. A pure trend system bleeds in chop; gating entries to
# "market risk-on" is the classic fix. Length in BARS at the active interval.
REGIME_SYMBOL = os.environ.get("BACKTEST_REGIME_SYMBOL", "SPY")
REGIME_MA_LEN = int(os.environ.get("BACKTEST_REGIME_MA", "200"))

# A representative slice of the strategy's universe for --universe runs (kept in
# sync with strategy.json factor_exposure_limits sector buckets + run.sh).
UNIVERSE = [
    "SPY", "QQQ", "IWM",
    "NVDA", "AMD", "AVGO", "MU", "AMAT", "TSM", "SMH",
    "META", "MSFT", "GOOGL", "PLTR", "CRWD", "SNOW",
    "COIN", "HOOD", "MSTR", "SOFI",
    "CAT", "DE", "GE", "XOM", "CVX", "FCX",
    "SOXL", "TQQQ", "FNGU",          # leveraged — exercises the tighter stop
]
LEVERAGE = {  # mirror strategy.json position_sizing.leverage_factors
    "SOXL": 3, "SOXS": 3, "TQQQ": 3, "SQQQ": 3, "FNGU": 3, "FNGD": 3,
    "SPXL": 3, "UPRO": 3, "TNA": 3, "TZA": 3, "TECL": 3, "LABU": 3,
    "UVXY": 2, "SSO": 2, "QLD": 2,
}

# --------------------------------------------------- leveraged-momentum sleeve
# The "max credible growth" small-account design (see momentum_sleeve()): take the
# operator's real edge — multi-timeframe momentum (momentum_screen.py) — and express
# it through 3x index ETFs (the only AFFORDABLE leverage for a $400 cash account:
# fractional shares, no option premium/theta/IV-crush/spread). Long-only, trend-
# gated, trailing-stop exit ("let winners run"). The leverage IS the ETF; we never
# stack margin on top. Liquid 3x ETFs with long Yahoo history:
LEV_UNIVERSE = ["TQQQ", "SOXL", "SPXL", "UPRO", "TECL", "FAS", "TNA", "LABU", "FNGU", "UDOW"]

# Max-aggression CONCENTRATED ROTATION universe (see backtest_rotation): high-beta
# single names + leveraged ETFs, so the engine can rotate into whichever is strongest.
AGGRO_UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MU", "AMAT", "SMCI", "MRVL", "TSM",
    "PLTR", "COIN", "MSTR", "HOOD", "SOFI", "SNOW", "CRWD", "NET", "DDOG",
    "META", "TSLA", "SHOP", "RBLX",
    "TQQQ", "SOXL", "FNGU", "TECL", "LABU", "FAS", "TNA",
]

MOM_TRAIL = float(os.environ.get("BACKTEST_MOM_TRAIL", "0.25"))     # giveback from peak
MOM_TREND_LEN = int(os.environ.get("BACKTEST_MOM_TREND", "200"))    # SMA trend gate (bars)
# Entry gate tuned for 3x INDEX ETFs (not the cheap-single-stock defaults in
# momentum_screen): anchor = 1-month momentum positive & meaningful, every shorter
# timeframe non-negative, plus a 200-bar trend filter applied in backtest_momentum.
MOM_CFG = {
    "targets": {"1w": 0.0, "1m": 0.08, "6m": 0.15, "1y": 0.25},
    "anchor_timeframe": "1m",
    "require_all_positive": True,
    "positive_timeframes": ["1m", "6m"],
    "min_score": 40,
}


def effective_stop(symbol):
    """Leverage-adjusted stop, identical rule to agent.effective_stop_pct:
    base/leverage for >=2x daily-reset ETFs, tighter only."""
    lf = LEVERAGE.get(symbol.upper(), 1)
    return round(DEFAULT_STOP / lf, 4) if lf > 1 else DEFAULT_STOP


# --------------------------------------------------------------------- data
def _fetch_history(symbol):
    """(timestamps, closes) oldest-first. Local CSV first (Close column), then
    Yahoo. Timestamps are epoch seconds (None for CSV rows without a date)."""
    path = os.path.join(ROOT, "data", f"{symbol.upper()}.csv")
    if os.path.exists(path):
        closes, ts = [], []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                ck = next((k for k in row if k.lower() == "close"), None)
                if ck is None:
                    break
                try:
                    closes.append(float(row[ck]))
                except (ValueError, TypeError):
                    continue
                dk = next((k for k in row if k.lower() == "date"), None)
                ts.append(row.get(dk) if dk else None)
        if closes:
            return ts, closes, "local_csv"

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval={INTERVAL}&includePrePost=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 backtest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=signals._ssl_context()) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        res = data["chart"]["result"][0]
        stamps = res["timestamp"]
        closes_raw = res["indicators"]["quote"][0]["close"]
    except Exception as e:
        return [], [], f"error:{type(e).__name__}"
    ts, closes = [], []
    for t, c in zip(stamps, closes_raw):
        if c is not None:
            ts.append(t)
            closes.append(float(c))
    return ts, closes, f"yahoo({INTERVAL})"


# ----------------------------------------------------------------- regime
_REGIME_CACHE = {}


def _sma(values, length):
    """Simple moving average; out[i] is the mean of the trailing `length` closes,
    or None until enough history exists."""
    out, run = [], 0.0
    from collections import deque
    win = deque()
    for v in values:
        win.append(v)
        run += v
        if len(win) > length:
            run -= win.popleft()
        out.append(run / length if len(win) == length else None)
    return out


def load_regime():
    """date_str -> bool risk_on, where risk_on means REGIME_SYMBOL closed above
    its REGIME_MA_LEN-bar SMA that bar. Cached per (symbol,len,interval,range).
    Returns {} if the regime series can't be fetched (caller treats missing as
    risk-on so the filter never silently blocks every trade)."""
    key = (REGIME_SYMBOL, REGIME_MA_LEN, INTERVAL, RANGE)
    if key in _REGIME_CACHE:
        return _REGIME_CACHE[key]
    ts, closes, source = _fetch_history(REGIME_SYMBOL)
    risk = {}
    if closes:
        ma = _sma(closes, REGIME_MA_LEN)
        for i in range(len(closes)):
            d = _fmt_ts(ts, i)
            if d is not None and ma[i] is not None:
                risk[d] = closes[i] > ma[i]
    _REGIME_CACHE[key] = risk
    return risk


# --------------------------------------------------------------- simulation
def _states(closes):
    """State + transition at every bar, using signals.py's exact math.

    EMA[i] depends only on closes[0..i], so computing the full series once and
    indexing is identical to recomputing on closes[:i+1] each step — O(n) not
    O(n^2). Returns lists aligned to closes (index 0 has no prior, NO_ACTION)."""
    lines = {name: signals.ema(closes, length)
             for name, length in signals.LENGTHS.items()}
    states, trans = [], []
    prev = None
    for i in range(len(closes)):
        st = signals.classify_signal({k: lines[k][i] for k in lines})
        states.append(st)
        trans.append("NO_ACTION" if prev is None
                     else signals.detect_transition(prev, st))
        prev = st
    return states, trans


def backtest_symbol(symbol, regime=None):
    """Walk-forward single-symbol backtest. Returns a stats dict (ok=False on
    insufficient data). Trades are sequential (one position at a time), entries
    and ema-exits fill at the NEXT bar close, stop exits at the breaching close.

    regime: optional date_str->bool risk_on map (from load_regime()). When given,
    an ENTER_LONG is only taken if the market was risk-on at the signal bar; a
    date missing from the map is treated as risk-on (never silently blocks)."""
    ts, closes, source = _fetch_history(symbol)
    n = len(closes)
    if n < signals.MIN_BARS + 5:
        return {"symbol": symbol, "ok": False, "source": source, "bars": n,
                "note": f"need >= {signals.MIN_BARS + 5} bars, got {n}"}

    states, trans = _states(closes)
    stop = effective_stop(symbol)
    cost = COST_BPS / 10000.0
    trades = []
    in_pos = False
    entry_px = entry_i = 0.0
    blocked = 0     # ENTER_LONG signals skipped by the regime filter

    # Skip warmup so the EMA(55) seed has washed out before the first trade.
    start = max(signals.WARMUP_OK, 1)
    for i in range(start, n - 1):       # -1: need bar i+1 to fill on
        if not in_pos:
            if trans[i] == "ENTER_LONG":
                if regime is not None and not regime.get(_fmt_ts(ts, i), True):
                    blocked += 1
                    continue
                in_pos = True
                entry_px = closes[i + 1] * (1 + cost)   # fill next bar + slippage
                entry_i = i + 1
        else:
            stop_px = entry_px * (1 - stop)
            if closes[i] <= stop_px:                    # stop breached -> exit now
                exit_px = closes[i] * (1 - cost)
                trades.append(_close(symbol, entry_px, exit_px, entry_i, i, ts, "stop_loss"))
                in_pos = False
            elif states[i] == "SELL":                   # state-based ema exit
                exit_px = closes[i + 1] * (1 - cost)
                trades.append(_close(symbol, entry_px, exit_px, entry_i, i + 1, ts, "ema_exit"))
                in_pos = False
    if in_pos:                                          # mark open trade to last close
        exit_px = closes[-1] * (1 - cost)
        trades.append(_close(symbol, entry_px, exit_px, entry_i, n - 1, ts, "open_eod"))

    return _summarize(symbol, trades, closes, source, n, stop, blocked, ts)


def _close(symbol, entry_px, exit_px, ei, xi, ts, reason, side="long"):
    # Short P&L is the mirror of long: you profit when price falls.
    ret = ((exit_px - entry_px) if side == "long" else (entry_px - exit_px)) / entry_px
    return {"symbol": symbol, "entry": round(entry_px, 4), "exit": round(exit_px, 4),
            "ret": ret, "bars_held": xi - ei, "reason": reason, "side": side,
            "_ei": ei, "_xi": xi,
            "entry_ts": _fmt_ts(ts, ei), "exit_ts": _fmt_ts(ts, xi)}


def _daily_returns(trades, closes, ts):
    """date_str -> strategy return on that bar, summed from holding periods. A bar
    j inside (entry_i, exit_i] contributes the bar's pct change (negated for short
    positions). Used to build a comparable return stream per strategy."""
    out = {}
    for t in trades:
        sign = 1.0 if t.get("side", "long") == "long" else -1.0
        for j in range(t["_ei"] + 1, t["_xi"] + 1):
            if j < 1 or j >= len(closes) or closes[j - 1] == 0:
                continue
            d = _fmt_ts(ts, j)
            if d is not None:
                out[d] = out.get(d, 0.0) + sign * (closes[j] / closes[j - 1] - 1)
    return out


def _fmt_ts(ts, i):
    if i >= len(ts) or ts[i] is None:
        return None
    v = ts[i]
    if isinstance(v, str):
        return v
    import datetime as _dt
    return _dt.datetime.fromtimestamp(v, _dt.timezone.utc).strftime("%Y-%m-%d")


def _summarize(symbol, trades, closes, source, n, stop, blocked=0, ts=None):
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    nt = len(rets)
    win_rate = len(wins) / nt if nt else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(rets) / nt if nt else 0.0
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    # Compounded equity through sequential trades (start $1).
    equity = 1.0
    curve = [1.0]
    for r in rets:
        equity *= (1 + r)
        curve.append(equity)
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    # Sharpe of per-trade returns (not annualized — sample size varies). A crude
    # but honest read on consistency: mean / stdev of trade returns.
    sharpe = 0.0
    if nt > 1:
        mu = expectancy
        var = sum((r - mu) ** 2 for r in rets) / (nt - 1)
        sd = math.sqrt(var)
        sharpe = (mu / sd) if sd > 0 else 0.0

    buy_hold = (closes[-1] - closes[0]) / closes[0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    return {
        "symbol": symbol, "ok": True, "source": source, "bars": n, "stop": stop,
        "trades": nt, "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": expectancy, "profit_factor": profit_factor,
        "strategy_return": equity - 1.0, "buy_hold_return": buy_hold,
        "max_drawdown": max_dd, "sharpe_per_trade": sharpe,
        "exit_reasons": reasons, "blocked": blocked, "_trades": trades,
        "_daily": _daily_returns(trades, closes, ts) if ts is not None else {},
        "_closes": closes, "_ts": ts,
    }


# ------------------------------------------------------ short / market-neutral
def backtest_short(symbol):
    """The ribbon, mirrored to the SHORT side: short a confirmed downtrend (entry
    on the EXIT transition into SELL state), cover when it flips to BUY or when a
    stop is hit (price RISES stop% above entry). Same fill/cost model. In a bull
    market this sleeve usually loses outright — its value is as a HEDGE that cuts
    the long book's market correlation and drawdown, which marketneutral() tests.
    Short stop is the base stop (no leverage tightening — symmetric to long)."""
    ts, closes, source = _fetch_history(symbol)
    n = len(closes)
    if n < signals.MIN_BARS + 5:
        return {"symbol": symbol, "ok": False, "source": source, "bars": n,
                "note": f"need >= {signals.MIN_BARS + 5} bars, got {n}"}
    states, trans = _states(closes)
    stop = effective_stop(symbol)
    cost = COST_BPS / 10000.0
    trades = []
    in_pos = False
    entry_px = entry_i = 0.0
    start = max(signals.WARMUP_OK, 1)
    for i in range(start, n - 1):
        if not in_pos:
            if trans[i] == "EXIT":                       # entered SELL state -> short
                in_pos = True
                entry_px = closes[i + 1] * (1 - cost)    # short fill: receive bid, slip down
                entry_i = i + 1
        else:
            stop_px = entry_px * (1 + stop)              # short stop: price rises
            if closes[i] >= stop_px:
                trades.append(_close(symbol, entry_px, closes[i] * (1 + cost),
                                     entry_i, i, ts, "stop_loss", side="short"))
                in_pos = False
            elif states[i] == "BUY":                     # trend flipped up -> cover
                trades.append(_close(symbol, entry_px, closes[i + 1] * (1 + cost),
                                     entry_i, i + 1, ts, "cover", side="short"))
                in_pos = False
    if in_pos:
        trades.append(_close(symbol, entry_px, closes[-1] * (1 + cost),
                             entry_i, n - 1, ts, "open_eod", side="short"))
    return _summarize(symbol, trades, closes, source, n, stop, 0, ts)


# ----------------------------------------------------- mean-reversion sleeve
# A second, intentionally UNCORRELATED strategy. The ribbon is pure momentum —
# it buys strength and gets chopped up in range-bound tape. Mean reversion does
# the opposite: it buys short-term oversold DIPS, but only in names still in a
# long-term uptrend (the trend filter that avoids catching falling knives). When
# momentum chops, mean reversion tends to earn — that's the diversification.
# Classic Connors RSI(2) construction.
MR_RSI_LEN = int(os.environ.get("BACKTEST_MR_RSI_LEN", "2"))
MR_RSI_ENTRY = float(os.environ.get("BACKTEST_MR_RSI_ENTRY", "10"))
MR_TREND_LEN = int(os.environ.get("BACKTEST_MR_TREND", "200"))
MR_EXIT_MA = int(os.environ.get("BACKTEST_MR_EXIT_MA", "5"))
MR_MAX_HOLD = int(os.environ.get("BACKTEST_MR_MAX_HOLD", "10"))


def rsi(closes, length):
    """Wilder's RSI; out[i] in [0,100], None until `length` deltas exist."""
    if len(closes) < length + 1:
        return [None] * len(closes)
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, length + 1):       # seed: simple average of first `length`
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / length, losses / length
    out[length] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (length - 1) + max(d, 0.0)) / length
        avg_l = (avg_l * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def backtest_meanrev(symbol):
    """Walk-forward mean-reversion backtest. Entry: RSI(MR_RSI_LEN) < MR_RSI_ENTRY
    while close > SMA(MR_TREND_LEN) (uptrend). Exit: close > SMA(MR_EXIT_MA), or
    stop-loss, or MR_MAX_HOLD bars elapsed. Same fill/cost/stop model as the
    momentum backtest. Returns the same stats shape (reuses _summarize)."""
    ts, closes, source = _fetch_history(symbol)
    n = len(closes)
    if n < MR_TREND_LEN + 5:
        return {"symbol": symbol, "ok": False, "source": source, "bars": n,
                "note": f"need >= {MR_TREND_LEN + 5} bars, got {n}"}

    rsis = rsi(closes, MR_RSI_LEN)
    trend = _sma(closes, MR_TREND_LEN)
    exit_ma = _sma(closes, MR_EXIT_MA)
    stop = effective_stop(symbol)
    cost = COST_BPS / 10000.0
    trades = []
    in_pos = False
    entry_px = entry_i = 0.0

    start = max(MR_TREND_LEN, MR_RSI_LEN + 1)
    for i in range(start, n - 1):
        if not in_pos:
            if (rsis[i] is not None and trend[i] is not None
                    and rsis[i] < MR_RSI_ENTRY and closes[i] > trend[i]):
                in_pos = True
                entry_px = closes[i + 1] * (1 + cost)
                entry_i = i + 1
        else:
            stop_px = entry_px * (1 - stop)
            if closes[i] <= stop_px:
                trades.append(_close(symbol, entry_px, closes[i] * (1 - cost),
                                     entry_i, i, ts, "stop_loss"))
                in_pos = False
            elif (exit_ma[i] is not None and closes[i] > exit_ma[i]) or (i - entry_i) >= MR_MAX_HOLD:
                reason = "mr_revert" if (exit_ma[i] is not None and closes[i] > exit_ma[i]) else "time_stop"
                trades.append(_close(symbol, entry_px, closes[i + 1] * (1 - cost),
                                     entry_i, i + 1, ts, reason))
                in_pos = False
    if in_pos:
        trades.append(_close(symbol, entry_px, closes[-1] * (1 - cost),
                             entry_i, n - 1, ts, "open_eod"))
    return _summarize(symbol, trades, closes, source, n, stop, 0, ts)


# --------------------------------------------------- leveraged-momentum sleeve
def _mtf_returns_at(closes, i, lookbacks):
    """{timeframe: pct or None} as of bar i, using only closes[..i] (no look-ahead).
    O(1) per bar — avoids re-slicing the series each step (which would be O(n^2))."""
    out = {}
    for tf, lb in lookbacks.items():
        j = i - lb
        out[tf] = (closes[i] / closes[j] - 1.0) if j >= 0 and closes[j] > 0 else None
    return out


def backtest_momentum(symbol, trail=None, trend_len=None, cfg=None):
    """Multi-timeframe-momentum entry + trailing-stop exit, for the (typically 3x)
    leveraged sleeve. Mirrors the live let-winners-run design:
      ENTRY  — momentum gate passes (momentum_screen.passes_gate) AND close is above
               its trend_len SMA (don't buy a 3x ETF in a downtrend — that's the
               2022-TQQQ-to-zero trap).
      EXIT   — leverage-adjusted hard stop from entry (LR002), OR a trailing stop
               (give back `trail` from the post-entry peak), OR a trend break
               (close < SMA). Trailing stop is the PRIMARY momentum exit.
    Same next-bar-fill / per-side cost / no-look-ahead honesty guards and the same
    _summarize output shape as backtest_symbol, so every report/sizing path reuses it."""
    trail = MOM_TRAIL if trail is None else trail
    trend_len = MOM_TREND_LEN if trend_len is None else trend_len
    cfg = cfg or MOM_CFG
    ts, closes, source = _fetch_history(symbol)
    n = len(closes)
    need = max(trend_len, mscreen.LOOKBACK_BARS["1y"]) + 5
    if n < need:
        return {"symbol": symbol, "ok": False, "source": source, "bars": n,
                "note": f"need >= {need} bars, got {n}"}
    sma = _sma(closes, trend_len)
    stop = effective_stop(symbol)
    cost = COST_BPS / 10000.0
    trades = []
    in_pos = False
    entry_px = entry_i = peak = 0.0
    for i in range(need, n - 1):                 # -1: need bar i+1 to fill on
        if not in_pos:
            rets = _mtf_returns_at(closes, i, mscreen.LOOKBACK_BARS)
            gate_ok, _ = mscreen.passes_gate(rets, cfg)
            if gate_ok and sma[i] is not None and closes[i] > sma[i]:
                in_pos = True
                entry_px = closes[i + 1] * (1 + cost)
                entry_i = i + 1
                peak = closes[i + 1]
        else:
            peak = max(peak, closes[i])
            if closes[i] <= entry_px * (1 - stop):           # hard stop (breaching close)
                trades.append(_close(symbol, entry_px, closes[i] * (1 - cost), entry_i, i, ts, "stop_loss"))
                in_pos = False
            elif closes[i] <= peak * (1 - trail):            # trailing stop (breaching close)
                trades.append(_close(symbol, entry_px, closes[i] * (1 - cost), entry_i, i, ts, "trailing_stop"))
                in_pos = False
            elif sma[i] is not None and closes[i] < sma[i]:  # trend break (next-bar exit)
                trades.append(_close(symbol, entry_px, closes[i + 1] * (1 - cost), entry_i, i + 1, ts, "trend_break"))
                in_pos = False
    if in_pos:
        trades.append(_close(symbol, entry_px, closes[-1] * (1 - cost), entry_i, n - 1, ts, "open_eod"))
    return _summarize(symbol, trades, closes, source, n, stop, 0, ts)


# ----------------------------------------------------------------- reporting
def _pct(x):
    return f"{x*100:+.1f}%"


def _row(s):
    if not s["ok"]:
        return f"  {s['symbol']:<6} SKIP — {s['note']} (source={s['source']})"
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (f"  {s['symbol']:<6} n={s['trades']:<3} win={s['win_rate']*100:4.0f}% "
            f"exp/trade={_pct(s['expectancy']):>7} PF={pf:>5} "
            f"strat={_pct(s['strategy_return']):>8} B&H={_pct(s['buy_hold_return']):>8} "
            f"maxDD={s['max_drawdown']*100:4.0f}% sharpe={s['sharpe_per_trade']:+.2f}")


def _pooled(ok):
    """(n_trades, win_rate, expectancy, profit_factor, beat_count) over a result set."""
    trades = [t for r in ok for t in r["_trades"]]
    if not trades:
        return 0, 0.0, 0.0, 0.0, 0
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    gl = -sum(r for r in rets if r <= 0)
    gw = sum(wins)
    pf = (gw / gl) if gl > 0 else float("inf")
    beat = sum(1 for r in ok if r["strategy_return"] > r["buy_hold_return"])
    return len(trades), len(wins) / len(rets), sum(rets) / len(rets), pf, beat


def run(symbols, write_md=False, regime=False):
    tag = " +REGIME" if regime else ""
    print(f"Backtest{tag} — interval={INTERVAL} range={RANGE} cost={COST_BPS}bps/side "
          f"warmup={signals.WARMUP_OK} bars"
          + (f" | regime={REGIME_SYMBOL}>SMA{REGIME_MA_LEN}\n" if regime else "\n"))
    reg = load_regime() if regime else None
    if regime and not reg:
        print(f"  WARNING: could not load {REGIME_SYMBOL} regime series — filter inert.\n")
    results = [backtest_symbol(s, regime=reg) for s in symbols]
    ok = [r for r in results if r["ok"]]
    for r in results:
        print(_row(r))

    # Pooled stats across all trades — the real edge read.
    all_trades = [t for r in ok for t in r["_trades"]]
    nt, wr, exp, pf, beat = _pooled(ok)
    print("\n" + "=" * 78)
    if nt:
        print(f"POOLED: {nt} trades across {len(ok)} symbols | "
              f"win_rate={wr*100:.1f}% | expectancy/trade={_pct(exp)} | "
              f"profit_factor={'inf' if pf==float('inf') else f'{pf:.2f}'}")
        print(f"        beat buy-and-hold on {beat}/{len(ok)} symbols")
        verdict = ("POSITIVE edge" if exp > 0 and pf > 1.1 else
                   "NO measurable edge" if exp <= 0 else "MARGINAL — within noise")
        print(f"        VERDICT: {verdict} "
              f"(n={nt}; need ~30+ trades/symbol for confidence)")
    else:
        print("POOLED: no trades generated — signal never fired ENTER_LONG in window.")
    print("=" * 78)

    if write_md:
        _write_md(results, all_trades)
    return results


def compare(symbols):
    """A/B the regime filter against the no-filter baseline on the same symbols
    and print the delta — the whole point of building the filter."""
    base = [backtest_symbol(s) for s in symbols]
    reg_map = load_regime()
    if not reg_map:
        print(f"WARNING: could not load {REGIME_SYMBOL} regime series — comparison aborted.")
        return
    filt = [backtest_symbol(s, regime=reg_map) for s in symbols]
    bok = [r for r in base if r["ok"]]
    fok = [r for r in filt if r["ok"]]
    n_b, wr_b, exp_b, pf_b, beat_b = _pooled(bok)
    n_f, wr_f, exp_f, pf_f, beat_f = _pooled(fok)
    blocked = sum(r.get("blocked", 0) for r in fok)

    def pf_s(x):
        return "inf" if x == float("inf") else f"{x:.2f}"
    print(f"\nA/B — regime filter ({REGIME_SYMBOL} > SMA{REGIME_MA_LEN}), "
          f"interval={INTERVAL} range={RANGE}, {len(bok)} symbols\n")
    print(f"  {'metric':<22}{'BASELINE':>12}{'+REGIME':>12}{'delta':>12}")
    print(f"  {'-'*58}")
    print(f"  {'trades':<22}{n_b:>12}{n_f:>12}{n_f-n_b:>+12}")
    print(f"  {'win_rate':<22}{wr_b*100:>11.1f}%{wr_f*100:>11.1f}%{(wr_f-wr_b)*100:>+11.1f}%")
    print(f"  {'expectancy/trade':<22}{_pct(exp_b):>12}{_pct(exp_f):>12}{_pct(exp_f-exp_b):>12}")
    pf_delta = (f"{pf_f-pf_b:>+12.2f}"
                if float('inf') not in (pf_b, pf_f) else f"{'—':>12}")
    print(f"  {'profit_factor':<22}{pf_s(pf_b):>12}{pf_s(pf_f):>12}{pf_delta}")
    print(f"  {'beat buy&hold':<22}{f'{beat_b}/{len(bok)}':>12}"
          f"{f'{beat_f}/{len(fok)}':>12}{beat_f-beat_b:>+12}")
    print(f"\n  {blocked} ENTER_LONG signals blocked by the filter "
          f"(taken in risk-off regime in the baseline).")
    verdict = ("HELPS — higher expectancy AND more names beat buy&hold"
               if exp_f > exp_b and beat_f > beat_b else
               "HELPS expectancy/consistency" if exp_f > exp_b else
               "HURTS — filter removed more good trades than bad"
               if exp_f < exp_b else "NEUTRAL")
    print(f"  VERDICT: {verdict}")


def _write_md(results, all_trades):
    import datetime as _dt
    stamp = _dt.datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(ROOT, "research", f"backtest_{INTERVAL}_{stamp}.md")
    ok = [r for r in results if r["ok"]]
    lines = [f"# Backtest — {stamp}", "",
             f"- interval `{INTERVAL}`, range `{RANGE}`, cost `{COST_BPS}`bps/side, "
             f"warmup `{signals.WARMUP_OK}` bars",
             "- Rules: ENTER_LONG entry, SELL-state exit, leverage-adjusted stop. "
             "Fills next-bar close; no look-ahead.", "",
             "| Symbol | Trades | Win% | Exp/trade | PF | Strategy | Buy&Hold | MaxDD | Sharpe |",
             "|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for s in ok:
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        lines.append(f"| {s['symbol']} | {s['trades']} | {s['win_rate']*100:.0f}% | "
                     f"{_pct(s['expectancy'])} | {pf} | {_pct(s['strategy_return'])} | "
                     f"{_pct(s['buy_hold_return'])} | {s['max_drawdown']*100:.0f}% | "
                     f"{s['sharpe_per_trade']:+.2f} |")
    if all_trades:
        rets = [t["ret"] for t in all_trades]
        wins = [r for r in rets if r > 0]
        exp = sum(rets) / len(rets)
        lines += ["", f"**Pooled:** {len(all_trades)} trades, "
                  f"win_rate {len(wins)/len(rets)*100:.1f}%, expectancy/trade {_pct(exp)}."]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nwrote {os.path.relpath(path, ROOT)}")


_PERIODS_PER_YEAR = {"1d": 252, "1h": 1638, "30m": 3276, "15m": 6552}


def _aggregate_daily(ok):
    """Equal-weight portfolio daily-return series across symbols: on each date,
    mean of the per-symbol strategy returns (a symbol flat that day contributes
    0). date_str -> portfolio return."""
    nsym = len(ok) or 1
    agg = {}
    for r in ok:
        for d, v in r["_daily"].items():
            agg[d] = agg.get(d, 0.0) + v
    return {d: v / nsym for d, v in agg.items()}


def _corr(a, b):
    """Pearson correlation over the union of dates (missing = 0/flat)."""
    dates = set(a) | set(b)
    if len(dates) < 3:
        return 0.0
    xs = [a.get(d, 0.0) for d in dates]
    ys = [b.get(d, 0.0) for d in dates]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0


def _ann_sharpe(series):
    """Annualized Sharpe of a daily-return series (0 risk-free)."""
    vals = list(series.values())
    if len(vals) < 2:
        return 0.0
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mu / sd) * math.sqrt(_PERIODS_PER_YEAR.get(INTERVAL, 252))


def _spy_daily():
    """SPY daily-return series (market proxy) for beta/correlation tests."""
    ts, closes, _ = _fetch_history("SPY")
    out = {}
    for j in range(1, len(closes)):
        d = _fmt_ts(ts, j)
        if d is not None and closes[j - 1]:
            out[d] = closes[j] / closes[j - 1] - 1
    return out


def _maxdd_from_daily(series):
    """Max drawdown of the equity curve implied by a daily-return series."""
    eq, peak, dd = 1.0, 1.0, 0.0
    for d in sorted(series):
        eq *= (1 + series[d])
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return dd


# ----------------------------------------------------------- sizing layer
def _vol_series(closes, ts, lookback):
    """date_str -> trailing realized volatility (stdev of the prior `lookback`
    bar returns, STRICTLY past so weighting a bar uses only info known before it
    — no look-ahead). Bars without enough history are omitted."""
    out = {}
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))
            if closes[i - 1]]
    # rets[k] corresponds to bar k+1; align vol-as-of-bar i to data ending at i-1.
    for i in range(lookback + 1, len(closes)):
        win = [closes[j] / closes[j - 1] - 1 for j in range(i - lookback, i)
               if closes[j - 1]]
        if len(win) < 2:
            continue
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / (len(win) - 1))
        d = _fmt_ts(ts, i)
        if d is not None and sd > 0:
            out[d] = sd
    return out


def _portfolio(results, weighting, vol_maps):
    """Combine per-symbol strategy daily returns into one portfolio series under
    a weighting scheme. Gross is normalised to 1 across the names active that bar,
    so 'equal' vs 'invvol' differ ONLY in risk balance (same gross) — any Sharpe
    gap is pure risk-balancing, not leverage."""
    all_dates = sorted(set().union(*[set(r["_daily"]) for r in results])
                       if results else set())
    series = {}
    for d in all_dates:
        active = [(r["symbol"], r["_daily"][d]) for r in results if d in r["_daily"]]
        if not active:
            continue
        if weighting == "equal":
            w = {s: 1.0 / len(active) for s, _ in active}
        else:  # inverse-vol (volatility targeting, cross-sectional)
            inv = {s: (1.0 / vol_maps[s][d]) if d in vol_maps.get(s, {}) else None
                   for s, _ in active}
            present = [x for x in inv.values() if x]
            med = sorted(present)[len(present) // 2] if present else 1.0
            inv = {s: (x if x else med) for s, x in inv.items()}
            tot = sum(inv.values()) or 1.0
            w = {s: inv[s] / tot for s in inv}
            cap = INVVOL_CAP_X / len(active)          # no name hogs the book
            w = {s: min(wt, cap) for s, wt in w.items()}
            t2 = sum(w.values()) or 1.0
            w = {s: wt / t2 for s, wt in w.items()}
        series[d] = sum(w[s] * r for s, r in active)
    return series


def _vol_target_overlay(series, target_ann):
    """Scale gross exposure each bar toward a target annualised vol using the
    trailing (lagged) realised vol of the book — leverage up in calm regimes,
    down in turbulent ones. Vol clustering makes this a genuine Sharpe lever, not
    just a scale change. Leverage capped at MAX_LEVERAGE."""
    ppy = _PERIODS_PER_YEAR.get(INTERVAL, 252)
    dates = sorted(series)
    rets = [series[d] for d in dates]
    out = {}
    win = max(VOL_LOOKBACK, 10)
    for i, d in enumerate(dates):
        if i < win:
            out[d] = series[d]
            continue
        w = rets[i - win:i]                           # strictly past
        mu = sum(w) / len(w)
        sd = math.sqrt(sum((x - mu) ** 2 for x in w) / (len(w) - 1))
        realized_ann = sd * math.sqrt(ppy)
        lev = min(target_ann / realized_ann, MAX_LEVERAGE) if realized_ann > 0 else 1.0
        out[d] = lev * series[d]
    return out


def _series_stats(series):
    """Annualised return / vol / Sharpe + max drawdown of a daily-return series."""
    ppy = _PERIODS_PER_YEAR.get(INTERVAL, 252)
    vals = [series[d] for d in sorted(series)]
    if len(vals) < 2:
        return {"ret": 0.0, "vol": 0.0, "sharpe": 0.0, "dd": 0.0}
    mu = sum(vals) / len(vals)
    sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1))
    ann_ret, ann_vol = mu * ppy, sd * math.sqrt(ppy)
    return {"ret": ann_ret, "vol": ann_vol,
            "sharpe": (ann_ret / ann_vol) if ann_vol > 0 else 0.0,
            "dd": _maxdd_from_daily(series)}


def sizing(symbols):
    """Compare position-sizing models on the long momentum sleeve: equal-weight
    (baseline) vs inverse-vol (equal risk per name) vs inverse-vol + a
    time-series vol-target overlay. Same trades throughout — only the dollar
    weights change. Reports whether sizing actually lifts risk-adjusted return."""
    results = [r for r in (backtest_symbol(s) for s in symbols)
               if r["ok"] and r["_ts"] is not None]
    if not results:
        print("no usable symbols")
        return
    vol_maps = {r["symbol"]: _vol_series(r["_closes"], r["_ts"], VOL_LOOKBACK)
                for r in results}

    eq = _portfolio(results, "equal", vol_maps)
    iv = _portfolio(results, "invvol", vol_maps)
    ivt = _vol_target_overlay(iv, TARGET_VOL_ANNUAL)
    books = [("equal-weight", eq), ("inverse-vol", iv),
             (f"inv-vol + {int(TARGET_VOL_ANNUAL*100)}%vol-target", ivt)]

    print(f"\nSizing layer — long momentum sleeve, interval={INTERVAL} "
          f"range={RANGE}, {len(results)} symbols")
    print(f"  vol_lookback={VOL_LOOKBACK} bars, per-name cap={INVVOL_CAP_X}x equal, "
          f"max_leverage={MAX_LEVERAGE}x\n")
    print(f"  {'sizing model':<28}{'ann.ret':>9}{'ann.vol':>9}{'Sharpe':>9}{'maxDD':>8}")
    print(f"  {'-'*63}")
    for name, s in books:
        st = _series_stats(s)
        print(f"  {name:<28}{st['ret']*100:>8.1f}%{st['vol']*100:>8.1f}%"
              f"{st['sharpe']:>9.2f}{st['dd']*100:>7.0f}%")

    base, iv_st = _series_stats(eq), _series_stats(iv)
    # Fractional-Kelly: full-Kelly per-period leverage ~ mean/variance of the book.
    vals = [iv[d] for d in sorted(iv)]
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / (len(vals) - 1)
    kelly = (mu / var) if var > 0 else 0.0
    print(f"\n  Inverse-vol vs equal-weight: Sharpe {base['sharpe']:.2f} -> "
          f"{iv_st['sharpe']:.2f} ({iv_st['sharpe']-base['sharpe']:+.2f}), "
          f"maxDD {base['dd']*100:.0f}% -> {iv_st['dd']*100:.0f}%")
    print(f"  Measured edge implies full-Kelly gross ~{kelly:.1f}x; "
          f"1/4-Kelly ~{kelly/4:.1f}x (sets return/risk SCALE, not Sharpe — "
          f"keep <=1x live).")
    verdict = ("inverse-vol raises Sharpe — adopt risk-balanced sizing"
               if iv_st['sharpe'] > base['sharpe'] + 0.05 else
               "inverse-vol cuts drawdown at similar Sharpe — defensible"
               if iv_st['dd'] < base['dd'] * 0.9 else
               "no material gain over equal-weight on this universe")
    print(f"  VERDICT: {verdict}")


def marketneutral(symbols):
    """Test the long/short (market-neutral) book: does adding the short ribbon
    sleeve to the long book cut market correlation and drawdown enough to raise
    risk-adjusted return? This is the construction the evidence pointed to after
    regime + mean-rev both failed (both stayed long the same beta)."""
    longs = [r for r in (backtest_symbol(s) for s in symbols) if r["ok"]]
    shorts = [r for r in (backtest_short(s) for s in symbols) if r["ok"]]
    nl, wl, el, pl, _ = _pooled(longs)
    ns, ws, es, ps, _ = _pooled(shorts)

    def pf_s(x):
        return "inf" if x == float("inf") else f"{x:.2f}"
    print(f"\nLong/short (market-neutral) — interval={INTERVAL} range={RANGE}, "
          f"{len(symbols)} symbols\n")
    print(f"  {'metric':<20}{'LONG (BUY)':>13}{'SHORT (SELL)':>14}")
    print(f"  {'-'*47}")
    print(f"  {'trades':<20}{nl:>13}{ns:>14}")
    print(f"  {'win_rate':<20}{wl*100:>12.1f}%{ws*100:>13.1f}%")
    print(f"  {'expectancy/trade':<20}{_pct(el):>13}{_pct(es):>14}")
    print(f"  {'profit_factor':<20}{pf_s(pl):>13}{pf_s(ps):>14}")

    long_d = _aggregate_daily(longs)
    short_d = _aggregate_daily(shorts)
    combo = {d: long_d.get(d, 0.0) + short_d.get(d, 0.0)
             for d in set(long_d) | set(short_d)}
    spy_d = _spy_daily()
    cL, cC = _corr(long_d, spy_d), _corr(combo, spy_d)
    sL, sC = _ann_sharpe(long_d), _ann_sharpe(combo)
    ddL, ddC = _maxdd_from_daily(long_d), _maxdd_from_daily(combo)
    print(f"\n  {'book':<20}{'LONG-ONLY':>13}{'LONG+SHORT':>14}")
    print(f"  {'-'*47}")
    print(f"  {'corr to SPY':<20}{cL:>+13.2f}{cC:>+14.2f}")
    print(f"  {'ann. Sharpe':<20}{sL:>+13.2f}{sC:>+14.2f}")
    print(f"  {'max drawdown':<20}{ddL*100:>12.0f}%{ddC*100:>13.0f}%")

    helps_sharpe = sC > sL
    cuts_beta = abs(cC) < abs(cL) - 0.15
    cuts_dd = ddC < ddL * 0.8
    if es <= 0 and not (cuts_beta and (helps_sharpe or cuts_dd)):
        v = ("short sleeve loses money AND doesn't improve the book "
             "(beta/dd/Sharpe) enough to justify the borrow/margin cost — skip")
    elif helps_sharpe and cuts_beta:
        v = "ADD IT — lower market beta AND higher Sharpe (real diversification)"
    elif cuts_beta and cuts_dd:
        v = "hedge value — cuts beta & drawdown materially even if Sharpe ~flat"
    elif helps_sharpe:
        v = "Sharpe improves but beta barely moves — marginal"
    else:
        v = "no meaningful improvement — skip"
    print(f"\n  VERDICT: {v}")


def combine(symbols):
    """Run the momentum ribbon and the mean-reversion sleeve, report each
    standalone, measure their correlation, and show a 50/50 combined book — the
    test of whether the second sleeve actually earns its place."""
    mom = [r for r in (backtest_symbol(s) for s in symbols) if r["ok"]]
    mr = [r for r in (backtest_meanrev(s) for s in symbols) if r["ok"]]
    nm, wm, em, pm, bm = _pooled(mom)
    nr, wr, er, pr, br = _pooled(mr)

    def pf_s(x):
        return "inf" if x == float("inf") else f"{x:.2f}"
    print(f"\nSleeve comparison — interval={INTERVAL} range={RANGE}, "
          f"{len(symbols)} symbols\n")
    print(f"  {'metric':<20}{'MOMENTUM':>12}{'MEAN-REV':>12}")
    print(f"  {'-'*44}")
    print(f"  {'trades':<20}{nm:>12}{nr:>12}")
    print(f"  {'win_rate':<20}{wm*100:>11.1f}%{wr*100:>11.1f}%")
    print(f"  {'expectancy/trade':<20}{_pct(em):>12}{_pct(er):>12}")
    print(f"  {'profit_factor':<20}{pf_s(pm):>12}{pf_s(pr):>12}")

    mom_d = _aggregate_daily(mom)
    mr_d = _aggregate_daily(mr)
    corr = _corr(mom_d, mr_d)
    combo = {d: 0.5 * mom_d.get(d, 0.0) + 0.5 * mr_d.get(d, 0.0)
             for d in set(mom_d) | set(mr_d)}
    s_mom, s_mr, s_combo = _ann_sharpe(mom_d), _ann_sharpe(mr_d), _ann_sharpe(combo)
    print(f"\n  Annualized Sharpe   momentum={s_mom:+.2f}  mean-rev={s_mr:+.2f}  "
          f"50/50={s_combo:+.2f}")
    print(f"  Correlation of daily returns (momentum vs mean-rev): {corr:+.2f}")

    verdict = []
    if er <= 0:
        verdict.append("mean-rev has NO standalone edge — do not add")
    else:
        verdict.append("mean-rev has positive standalone edge")
        if corr < 0.4 and s_combo >= max(s_mom, s_mr):
            verdict.append("AND is uncorrelated → combined Sharpe improves → ADD IT")
        elif corr < 0.4:
            verdict.append(f"AND is uncorrelated (corr {corr:+.2f}) but combo Sharpe "
                           f"{s_combo:+.2f} didn't beat best sleeve — marginal")
        else:
            verdict.append(f"but correlation {corr:+.2f} is too high to diversify much")
    print(f"  VERDICT: {'; '.join(verdict)}")


def _full_calendar(results):
    """Sorted union of every trading date across the result set (held or flat)."""
    dates = set()
    for r in results:
        for k in range(len(r["_ts"] or [])):
            d = _fmt_ts(r["_ts"], k)
            if d:
                dates.add(d)
    return sorted(dates)


def _eqw_calendar_series(results):
    """Equal-capital-bucket portfolio: each of the N names gets 1/N of capital and
    earns 0 on days it holds nothing. Includes flat days (full trading calendar) so
    CAGR/Sharpe correctly penalise time spent in cash — the honest comparison to a
    fully-invested buy-and-hold benchmark."""
    n = len(results) or 1
    dates = _full_calendar(results)
    return {d: sum(r["_daily"].get(d, 0.0) for r in results) / n for d in dates}


def _cagr(series):
    """Compound annual growth rate of a daily-return series over its calendar span."""
    dates = sorted(series)
    if len(dates) < 2:
        return 0.0
    eq = 1.0
    for d in dates:
        eq *= (1 + series[d])
    yrs = len(dates) / _PERIODS_PER_YEAR.get(INTERVAL, 252)
    return eq ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else 0.0


def _monthly_stats(series):
    """Compound each calendar month, then summarise the distribution of monthly
    returns — the table that directly answers 'can it hit X% per month?'."""
    from collections import defaultdict
    months = defaultdict(list)
    for d in sorted(series):
        months[d[:7]].append(series[d])
    mret = []
    for ym in sorted(months):
        eq = 1.0
        for r in months[ym]:
            eq *= (1 + r)
        mret.append(eq - 1)
    if not mret:
        return None
    s = sorted(mret)
    pos = [x for x in mret if x > 0]
    return {"n": len(mret), "mean": sum(mret) / len(mret),
            "median": s[len(s) // 2], "best": s[-1], "worst": s[0],
            "pct_pos": len(pos) / len(mret),
            "hit_5": sum(1 for x in mret if x >= 0.05) / len(mret),
            "hit_10": sum(1 for x in mret if x >= 0.10) / len(mret)}


# ----------------------------------------------- concentrated momentum rotation
def _prep_symbol(sym, trend_len, cfg):
    """Pre-compute per-date close, bar return, raw momentum rank-score, and an
    eligibility flag (momentum gate + above-trend) for the rotation simulator.
    Returns None if the symbol lacks enough history. Rank-score is the UNCAPPED
    blended momentum (1m/1w/6m) so the strongest names rank highest — distinct from
    momentum_screen's capped 0-100 score, which saturates and can't discriminate."""
    ts, closes, source = _fetch_history(sym)
    n = len(closes)
    need = max(trend_len, mscreen.LOOKBACK_BARS["1y"]) + 2
    if n < need:
        return None
    sma = _sma(closes, trend_len)
    p = {"close": {}, "ret": {}, "score": {}, "elig": {}}
    for k in range(n):
        d = _fmt_ts(ts, k)
        if d is None:
            continue
        p["close"][d] = closes[k]
        if k >= 1 and closes[k - 1]:
            p["ret"][d] = closes[k] / closes[k - 1] - 1
        if k >= need:
            r = _mtf_returns_at(closes, k, mscreen.LOOKBACK_BARS)
            p["score"][d] = (0.5 * (r["1m"] or 0) + 0.3 * (r["1w"] or 0)
                             + 0.2 * (r["6m"] or 0))         # uncapped strength rank
            gate_ok, _ = mscreen.passes_gate(r, cfg)
            p["elig"][d] = bool(gate_ok and sma[k] is not None and closes[k] > sma[k])
    return p


def backtest_rotation(symbols, top_n=3, trail=None, trend_len=None, cfg=None):
    """Concentrated momentum-ROTATION portfolio: each day hold the `top_n` strongest
    eligible names (momentum gate + above-trend), equal weight, cash in any unfilled
    slot. Exit a held name on a trailing stop (giveback `trail` from peak), the
    leverage-adjusted hard stop, or loss of eligibility (momentum/trend broke) — then
    rotate the freed slot into the next strongest leader. This is the live let-winners-
    run edge run at MAX aggression: no diversification drag, always in the leaders.
    Per-side cost charged on every entry/exit. No look-ahead: returns on day i come
    from names held INTO day i; entries decided on close i earn from i+1. Returns a
    date->portfolio-return series (same shape the report/stat helpers consume)."""
    trail = MOM_TRAIL if trail is None else trail
    trend_len = MOM_TREND_LEN if trend_len is None else trend_len
    cfg = cfg or MOM_CFG
    cost = COST_BPS / 10000.0
    preps = {}
    for s in symbols:
        pr = _prep_symbol(s, trend_len, cfg)
        if pr:
            preps[s] = pr
    if not preps:
        return None
    stop = {s: effective_stop(s) for s in preps}
    dates = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    held = {}                                    # sym -> {entry, peak}
    port = {}
    for d in dates:
        day_ret = 0.0
        for sym, pos in held.items():            # 1) today's P&L from names held into today
            r = preps[sym]["ret"].get(d)
            if r is not None:
                day_ret += r / top_n
            c = preps[sym]["close"].get(d)
            if c is not None:
                pos["peak"] = max(pos["peak"], c)
        events = 0                               # entries+exits today (each costs one side)
        for sym in list(held):                   # 2) exits on close d
            c = preps[sym]["close"].get(d)
            if c is None:
                continue
            pos = held[sym]
            if (c <= pos["peak"] * (1 - trail) or c <= pos["entry"] * (1 - stop[sym])
                    or not preps[sym]["elig"].get(d, False)):
                del held[sym]
                events += 1
        if len(held) < top_n:                    # 3) rotate freed slots into the strongest
            cand = sorted(((preps[s]["score"].get(d, -9.9), s) for s in preps
                           if s not in held and preps[s]["elig"].get(d, False)), reverse=True)
            for sc, s in cand[:top_n - len(held)]:
                c = preps[s]["close"].get(d)
                if c:
                    held[s] = {"entry": c, "peak": c}
                    events += 1
        port[d] = day_ret - events * cost / top_n
    return port


def momentum_sleeve(symbols):
    """THE leveraged-momentum sleeve test. For each (3x) ETF: momentum-entry +
    trailing-stop strategy vs simply buy-and-holding that same ETF vs the EMA ribbon
    on it. Then an equal-bucket portfolio: CAGR, max drawdown, Sharpe, the monthly-
    return distribution, and the implied Kelly leverage — all against a plain
    buy-and-hold SPY benchmark over the identical window. The drawdown column is the
    point: 3x leverage is a return multiplier AND a ruin multiplier."""
    mom = [r for r in (backtest_momentum(s) for s in symbols) if r["ok"]]
    rib = {r["symbol"]: r for r in (backtest_symbol(s) for s in symbols) if r["ok"]}
    if not mom:
        print("no usable symbols — each leveraged ETF needs ~1y+ of daily history.")
        return
    print(f"\nLeveraged-momentum sleeve — interval={INTERVAL} range={RANGE}, "
          f"{len(mom)} ETFs, trail={int(MOM_TRAIL*100)}% trend=SMA{MOM_TREND_LEN}")
    print(f"  entry: 1m≥{int(MOM_CFG['targets']['1m']*100)}% & all-positive & >SMA{MOM_TREND_LEN};"
          f"  exit: lev-adj stop / trailing / trend-break, cost={COST_BPS}bps/side\n")
    print(f"  {'ETF':<6}{'n':>4}{'win':>6}{'exp/t':>9}{'MOM ret':>11}{'maxDD':>8}"
          f"{'B&H ret':>11}{'B&H DD':>8}{'ribbon':>10}")
    print(f"  {'-'*73}")
    for r in sorted(mom, key=lambda x: x["strategy_return"], reverse=True):
        bh_dd = _bh_maxdd(r["_closes"])
        rr = rib.get(r["symbol"])
        rib_s = _pct(rr["strategy_return"]) if rr else "—"
        print(f"  {r['symbol']:<6}{r['trades']:>4}{r['win_rate']*100:>5.0f}%"
              f"{_pct(r['expectancy']):>9}{_pct(r['strategy_return']):>11}"
              f"{r['max_drawdown']*100:>7.0f}%{_pct(r['buy_hold_return']):>11}"
              f"{bh_dd*100:>7.0f}%{rib_s:>10}")

    # Equal-bucket portfolio + benchmark over the SAME window.
    port = _eqw_calendar_series(mom)
    p_st = _series_stats(port)
    start, end = (sorted(port)[0], sorted(port)[-1]) if port else ("", "")
    spy_full = _spy_daily()
    spy_win = {d: v for d, v in spy_full.items() if start <= d <= end}
    spy_cagr, spy_dd = _cagr(spy_win), _maxdd_from_daily(spy_win)
    spy_bh = 1.0
    for d in sorted(spy_win):
        spy_bh *= (1 + spy_win[d])

    vals = [port[d] for d in sorted(port)]
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / (len(vals) - 1) if len(vals) > 1 else 0.0
    kelly = (mu / var) if var > 0 else 0.0
    ms = _monthly_stats(port)

    print(f"\n  Equal-bucket portfolio ({len(mom)} ETFs, 1/N each), {start} → {end}")
    print(f"  {'-'*73}")
    print(f"  {'':<22}{'STRATEGY':>14}{'buy&hold SPY':>16}")
    print(f"  {'CAGR':<22}{_pct(_cagr(port)):>14}{_pct(spy_cagr):>16}")
    print(f"  {'ann. Sharpe':<22}{p_st['sharpe']:>14.2f}{_series_stats(spy_win)['sharpe']:>16.2f}")
    print(f"  {'MAX DRAWDOWN':<22}{p_st['dd']*100:>13.0f}%{spy_dd*100:>15.0f}%")
    if ms:
        print(f"\n  Monthly returns ({ms['n']} months): mean {_pct(ms['mean'])}, "
              f"median {_pct(ms['median'])}, best {_pct(ms['best'])}, worst {_pct(ms['worst'])}")
        print(f"  Positive months: {ms['pct_pos']*100:.0f}%   |   "
              f"months that hit ≥+10%: {ms['hit_10']*100:.0f}%")
    print(f"\n  Implied full-Kelly leverage on this (already-3x) sleeve ≈ {kelly:.1f}x; "
          f"¼-Kelly ≈ {kelly/4:.1f}x.")
    print(f"  → The 3x ETF IS the leverage. On a cash account you cap ADDED leverage "
          f"at 1.0x (no margin).")
    print(f"\n  READ THIS: the gaudy CAGR is inseparable from the MAX DRAWDOWN and is "
          f"\n  regime-dependent — {start[:4]}–{end[:4]} was the biggest equity bull run in history.")


def rotation_report(symbols, top_ns=(1, 2, 3)):
    """THE max-aggression test: concentrated momentum rotation at varying
    concentration (top_n=1 is all-in the single strongest name). Reports CAGR /
    Sharpe / max drawdown and — the whole point — the MONTHLY return distribution:
    how often it actually hits +5% and +10%, and how ugly the bad months get.
    Benchmarked against buy-and-hold SPY over the same window."""
    print(f"\nConcentrated momentum ROTATION — interval={INTERVAL} range={RANGE}, "
          f"{len(symbols)} names, trail={int(MOM_TRAIL*100)}% trend=SMA{MOM_TREND_LEN}")
    print(f"  hold the top_n strongest eligible names (gate+trend); rotate on "
          f"stop/trail/trend-break. cost={COST_BPS}bps/side.\n")
    print(f"  {'concen.':<10}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>7}{'mo.mean':>9}"
          f"{'mo.med':>8}{'worst':>8}{'≥+5%':>7}{'≥+10%':>7}{'pos':>6}")
    print(f"  {'-'*78}")
    span = None
    for tn in top_ns:
        port = backtest_rotation(symbols, top_n=tn)
        if not port:
            continue
        st = _series_stats(port)
        ms = _monthly_stats(port)
        if span is None:
            ds = sorted(port)
            span = (ds[0], ds[-1])
        label = "all-in (1)" if tn == 1 else f"top-{tn}"
        print(f"  {label:<10}{_pct(_cagr(port)):>8}{st['sharpe']:>8.2f}"
              f"{st['dd']*100:>6.0f}%{_pct(ms['mean']):>9}{_pct(ms['median']):>8}"
              f"{_pct(ms['worst']):>8}{ms['hit_5']*100:>6.0f}%{ms['hit_10']*100:>6.0f}%"
              f"{ms['pct_pos']*100:>5.0f}%")
    if span:
        spy = {d: v for d, v in _spy_daily().items() if span[0] <= d <= span[1]}
        sst, sms = _series_stats(spy), _monthly_stats(spy)
        print(f"  {'-'*78}")
        print(f"  {'SPY B&H':<10}{_pct(_cagr(spy)):>8}{sst['sharpe']:>8.2f}"
              f"{sst['dd']*100:>6.0f}%{_pct(sms['mean']):>9}{_pct(sms['median']):>8}"
              f"{_pct(sms['worst']):>8}{sms['hit_5']*100:>6.0f}%{sms['hit_10']*100:>6.0f}%"
              f"{sms['pct_pos']*100:>5.0f}%")
        print(f"\n  window {span[0]} → {span[1]}. '≥+10%' = share of months that hit "
              f"your target. Read it next to 'worst' and 'maxDD'\n  — the same "
              f"concentration that buys good months buys the bad ones. Regime: a bull decade.")


def _bh_maxdd(closes):
    """Buy-and-hold max drawdown of a close series."""
    peak, dd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        dd = max(dd, (peak - c) / peak)
    return dd


if __name__ == "__main__":
    import sys
    args = list(sys.argv[1:])
    write_md = "--md" in args
    regime = "--regime" in args
    do_compare = "--compare" in args
    do_combine = "--combine" in args
    do_meanrev = "--meanrev" in args
    do_neutral = "--neutral" in args
    do_short = "--short" in args
    do_sizing = "--sizing" in args
    do_momentum = "--momentum" in args
    do_rotation = "--rotation" in args
    universe = "--universe" in args
    flags = {"--md", "--regime", "--compare", "--universe", "--combine",
             "--meanrev", "--neutral", "--short", "--sizing", "--momentum", "--rotation"}
    syms = UNIVERSE if universe else (
        [a.upper() for a in args if a not in flags] or ["SPY"])
    if do_rotation:
        # --rotation defaults to the high-beta + leveraged aggression universe.
        rotation_report([a.upper() for a in args if a not in flags] or AGGRO_UNIVERSE)
    elif do_momentum:
        # --momentum defaults to the leveraged-ETF universe when no symbols given.
        momentum_sleeve([a.upper() for a in args if a not in flags] or LEV_UNIVERSE)
    elif do_sizing:
        sizing(syms)
    elif do_neutral:
        marketneutral(syms)
    elif do_short:
        print(f"Short-ribbon backtest — interval={INTERVAL} range={RANGE}\n")
        res = [backtest_short(s) for s in syms]
        for r in res:
            print(_row(r))
        nt, wr, exp, pf, beat = _pooled([r for r in res if r["ok"]])
        print(f"\nPOOLED: {nt} trades | win_rate={wr*100:.1f}% | "
              f"expectancy/trade={_pct(exp)} | "
              f"profit_factor={'inf' if pf==float('inf') else f'{pf:.2f}'}")
    elif do_combine:
        combine(syms)
    elif do_compare:
        compare(syms)
    elif do_meanrev:
        print(f"Mean-reversion backtest — interval={INTERVAL} range={RANGE} "
              f"RSI({MR_RSI_LEN})<{MR_RSI_ENTRY} in uptrend, exit>SMA{MR_EXIT_MA}\n")
        res = [backtest_meanrev(s) for s in syms]
        for r in res:
            print(_row(r))
        nt, wr, exp, pf, beat = _pooled([r for r in res if r["ok"]])
        print(f"\nPOOLED: {nt} trades | win_rate={wr*100:.1f}% | "
              f"expectancy/trade={_pct(exp)} | "
              f"profit_factor={'inf' if pf==float('inf') else f'{pf:.2f}'}")
    else:
        run(syms, write_md=write_md, regime=regime)
