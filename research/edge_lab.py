"""edge_lab.py — walk-forward lab for the 2026-07 strategy redesign.

Every candidate sleeve for the new architecture is proven (or killed) here on real
Yahoo OHLC history before a line of live code is written. Honesty guards match
backtest.py: decisions on bar t use only data through t; fills happen at the NEXT
open (or close where stated) with per-side cost; leveraged ETFs are real traded
ETFs (their decay is in the data, not simulated); no shorting (cash account).

Sleeves under test
------------------
CORE  — regime-throttled leveraged core: hold a 3x index ETF only when
        (a) SPY > SMA200 (trend), (b) VIX term structure in contango
        (VIX < VIX3M — crash regimes invert it), (c) scaled to a vol target
        (target_ann_vol / realized_vol, capped 1.0), (d) drawdown ratchet:
        after a 15% sleeve DD, exposure halves until a new equity high.
        The COMBINATION is the design: each filter is public, the joint
        gate + throttle + ratchet tuned to daily-reset 3x mechanics is ours.
GAP   — event-gap continuation (PEAD proxy): a liquid name gaps up >=6% at the
        open and HOLDS it into the close (close >= open) -> buy next open,
        exit on trailing stop / hard stop / 5-bar time stop.
ROT   — the existing concentrated top-3 momentum rotation (backtest.py), with
        a portfolio-level vol-target + VIX-gate overlay to see whether the ~69%
        max DD can be cut without giving up the return.
NIGHT — overnight-only QQQ (buy close, sell next open) as a control: a widely
        cited anomaly that must survive costs + T+1 to earn a slot (expect: no).

Usage:  python3 research/edge_lab.py [core|gap|rot|night|combo|all]
Data:   cached in research/_cache/*.json (delete to refetch).
"""

import os
import sys
import json
import math
import random
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import signals  # noqa: E402  (ssl context reuse)

CACHE = os.path.join(ROOT, "research", "_cache")
COST_BPS = float(os.environ.get("LAB_COST_BPS", "5"))      # per side, single names
COST_BPS_ETF = float(os.environ.get("LAB_COST_BPS_ETF", "3"))  # liquid index ETFs
RANGE = os.environ.get("LAB_RANGE", "10y")
PPY = 252

# ------------------------------------------------------------------ data
def fetch_ohlc(symbol, rng=None):
    """{ts, open, high, low, close, volume} daily arrays, oldest-first, cached."""
    rng = rng or RANGE
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{symbol.replace('^','_')}_{rng}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval=1d&includePrePost=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 edge-lab/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=signals._ssl_context()) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        out = {"ts": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        for i, t in enumerate(res["timestamp"]):
            c = q["close"][i]
            o = q["open"][i]
            if c is None or o is None:
                continue
            import datetime as dt
            out["ts"].append(dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"))
            out["open"].append(float(o))
            out["high"].append(float(q["high"][i] or c))
            out["low"].append(float(q["low"][i] or c))
            out["close"].append(float(c))
            out["volume"].append(float(q["volume"][i] or 0))
    except Exception as e:
        print(f"  fetch failed {symbol}: {e}")
        return None
    if not out["close"]:
        return None
    with open(path, "w") as f:
        json.dump(out, f)
    return out


# ------------------------------------------------------------------ metrics
def sma(vals, n):
    out, run, win = [], 0.0, []
    for v in vals:
        win.append(v); run += v
        if len(win) > n:
            run -= win.pop(0)
        out.append(run / n if len(win) == n else None)
    return out


def realized_vol(closes, i, lb=20):
    """Annualized stdev of the lb daily returns ENDING at bar i (inclusive)."""
    if i < lb + 1:
        return None
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - lb + 1, i + 1)]
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(PPY)


def series_stats(series):
    """series: {date: ret}. Full stat block."""
    dates = sorted(series)
    vals = [series[d] for d in dates]
    n = len(vals)
    if n < 20:
        return None
    mu = sum(vals) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / (n - 1))
    downside = [min(0.0, x) for x in vals]
    dsd = math.sqrt(sum(x * x for x in downside) / (n - 1))
    eq, peak, dd = 1.0, 1.0, 0.0
    for v in vals:
        eq *= (1 + v)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    yrs = n / PPY
    cagr = eq ** (1 / yrs) - 1 if eq > 0 else -1.0
    return {"n_days": n, "start": dates[0], "end": dates[-1],
            "cagr": cagr, "ann_vol": sd * math.sqrt(PPY),
            "sharpe": (mu / sd * math.sqrt(PPY)) if sd > 0 else 0.0,
            "sortino": (mu / dsd * math.sqrt(PPY)) if dsd > 0 else 0.0,
            "max_dd": dd, "total": eq - 1.0,
            "worst_day": min(vals), "best_day": max(vals)}


def monthly_stats(series):
    months = {}
    for d in sorted(series):
        months.setdefault(d[:7], []).append(series[d])
    mret = []
    for ym in sorted(months):
        eq = 1.0
        for r in months[ym]:
            eq *= (1 + r)
        mret.append((ym, eq - 1))
    vals = [x for _, x in mret]
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(vals), "mean": sum(vals) / len(vals), "median": s[len(s) // 2],
            "best": s[-1], "worst": s[0],
            "pos": sum(1 for x in vals if x > 0) / len(vals),
            "ge5": sum(1 for x in vals if x >= 0.05) / len(vals),
            "ge10": sum(1 for x in vals if x >= 0.10) / len(vals),
            "ge25": sum(1 for x in vals if x >= 0.25) / len(vals),
            "_series": mret}


def yearly_table(series):
    years = {}
    for d in sorted(series):
        years.setdefault(d[:4], []).append(series[d])
    out = []
    for y in sorted(years):
        eq = 1.0
        for r in years[y]:
            eq *= (1 + r)
        out.append((y, eq - 1))
    return out


def regime_split(series, spy):
    """Split daily returns by SPY>SMA200 (bull) vs below (bear) as of the PRIOR day."""
    ma = sma(spy["close"], 200)
    risk = {}
    for i in range(len(spy["ts"])):
        if ma[i] is not None:
            risk[spy["ts"][i]] = spy["close"][i] > ma[i]
    dates = sorted(series)
    bull = {}; bear = {}
    prev_state = None
    for d in dates:
        st = risk.get(d, prev_state)
        if st is not None:
            (bull if st else bear)[d] = series[d]
            prev_state = st
    return bull, bear


def bootstrap_risk(series, horizon_days=252, trials=2000, block=10, seed=7):
    """Stationary block bootstrap: P(drawdown ever exceeds X within a 1y horizon)
    and P(any month >= +25%). Uses the empirical daily returns — fat tails included
    to the extent the sample has them (it can't know about worse-than-sample events;
    treat as a LOWER bound on tail risk)."""
    vals = [series[d] for d in sorted(series)]
    if len(vals) < 100:
        return None
    rng = random.Random(seed)
    ruin50 = ruin30 = 0
    hit25 = 0
    for _ in range(trials):
        path = []
        while len(path) < horizon_days:
            j = rng.randrange(len(vals))
            path.extend(vals[j:j + block])
        path = path[:horizon_days]
        eq, peak, mdd = 1.0, 1.0, 0.0
        for r in path:
            eq *= (1 + r)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
        if mdd >= 0.50:
            ruin50 += 1
        if mdd >= 0.30:
            ruin30 += 1
        # monthly slices of 21 days
        got = False
        for k in range(0, horizon_days - 20, 21):
            m = 1.0
            for r in path[k:k + 21]:
                m *= (1 + r)
            if m - 1 >= 0.25:
                got = True
                break
        if got:
            hit25 += 1
    return {"p_dd50_1y": ruin50 / trials, "p_dd30_1y": ruin30 / trials,
            "p_month_ge25_1y": hit25 / trials}


def print_block(name, series, spy=None, boot=True):
    st = series_stats(series)
    if not st:
        print(f"{name}: insufficient data")
        return
    ms = monthly_stats(series)
    print(f"\n=== {name}  ({st['start']} → {st['end']}, {st['n_days']}d) ===")
    print(f"  CAGR {st['cagr']*100:+.1f}%  vol {st['ann_vol']*100:.1f}%  "
          f"Sharpe {st['sharpe']:.2f}  Sortino {st['sortino']:.2f}  maxDD {st['max_dd']*100:.0f}%  "
          f"total {st['total']*100:+.0f}%")
    print(f"  monthly: mean {ms['mean']*100:+.1f}%  med {ms['median']*100:+.1f}%  "
          f"best {ms['best']*100:+.1f}%  worst {ms['worst']*100:+.1f}%  pos {ms['pos']*100:.0f}%")
    print(f"  months >=+5%: {ms['ge5']*100:.0f}%   >=+10%: {ms['ge10']*100:.0f}%   "
          f">=+25%: {ms['ge25']*100:.0f}%")
    print(f"  years: " + "  ".join(f"{y}:{r*100:+.0f}%" for y, r in yearly_table(series)))
    if spy:
        bull, bear = regime_split(series, spy)
        sb, sr = series_stats(bull), series_stats(bear)
        if sb and sr:
            print(f"  regime: BULL(SPY>200sma) CAGR {sb['cagr']*100:+.1f}% DD {sb['max_dd']*100:.0f}%"
                  f"  |  BEAR CAGR {sr['cagr']*100:+.1f}% DD {sr['max_dd']*100:.0f}%")
    if boot:
        br = bootstrap_risk(series)
        if br:
            print(f"  bootstrap 1y: P(DD>=50%) {br['p_dd50_1y']*100:.1f}%   "
                  f"P(DD>=30%) {br['p_dd30_1y']*100:.1f}%   P(a month >=+25%) {br['p_month_ge25_1y']*100:.0f}%")


# ------------------------------------------------------------------ CORE sleeve
def core_sleeve(etf="TQQQ", target_vol=0.40, ratchet_dd=0.15, ratchet_scale=0.5,
                rebal_band=0.10, use_vix_gate=True, use_trend=True, quiet=False):
    """Regime-throttled leveraged core. Daily weight in [0,1] of sleeve capital in
    the 3x ETF, rest cash. Signals from bar t data; weight applies to bar t+1's
    close-to-close return; weight CHANGES cost COST_BPS_ETF per side on the traded
    delta. Returns {date: ret}."""
    d = fetch_ohlc(etf)
    spy = fetch_ohlc("SPY")
    vix = fetch_ohlc("^VIX")
    vix3m = fetch_ohlc("^VIX3M")
    if not d or not spy:
        return None
    spy_ma = sma(spy["close"], 200)
    spy_ok = {spy["ts"][i]: (spy_ma[i] is not None and spy["close"][i] > spy_ma[i])
              for i in range(len(spy["ts"]))}
    vmap = {}
    if vix and vix3m and use_vix_gate:
        v3 = {vix3m["ts"][i]: vix3m["close"][i] for i in range(len(vix3m["ts"]))}
        for i in range(len(vix["ts"])):
            t = vix["ts"][i]
            if t in v3 and v3[t] > 0:
                vmap[t] = vix["close"][i] < v3[t]          # contango = calm
    cost = COST_BPS_ETF / 10000.0
    out = {}
    w = 0.0
    eq, peak = 1.0, 1.0
    last_spy_ok = last_vix_ok = None
    for i in range(1, len(d["ts"])):
        t_prev, t = d["ts"][i - 1], d["ts"][i]
        # --- return earned today from yesterday's weight
        r = d["close"][i] / d["close"][i - 1] - 1
        day = w * r
        # --- decide tomorrow's weight from data through today
        last_spy_ok = spy_ok.get(t, last_spy_ok)
        if t in vmap:
            last_vix_ok = vmap[t]
        gate = (not use_trend or bool(last_spy_ok)) and (not use_vix_gate or last_vix_ok is not False)
        rv = realized_vol(d["close"], i, 20)
        scale = min(1.0, target_vol / rv) if (rv and rv > 0) else 0.0
        target = scale if gate else 0.0
        # drawdown ratchet on the sleeve's own equity
        eq *= (1 + day)
        peak = max(peak, eq)
        if peak > 0 and (peak - eq) / peak >= ratchet_dd:
            target *= ratchet_scale
        # rebalance band: trade only when the drift is material
        if abs(target - w) > rebal_band or (target == 0.0 and w > 0.0):
            day -= abs(target - w) * cost
            w = target
        out[t] = day
    if not quiet:
        print(f"[core {etf}] target_vol={target_vol} vix_gate={use_vix_gate} "
              f"trend={use_trend} ratchet={ratchet_dd}/{ratchet_scale}")
    return out


# ------------------------------------------------------------------ GAP sleeve
GAP_UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MU", "AMAT", "SMCI", "MRVL", "TSM", "LRCX", "KLAC",
    "META", "TSLA", "SHOP", "RBLX", "NET", "DDOG", "CRWD", "SNOW", "MDB", "PLTR",
    "COIN", "HOOD", "MSTR", "SOFI", "AFRM", "UPST", "SQ", "PYPL", "DKNG", "RIOT",
    "CELH", "ELF", "ANET", "PANW", "ZS", "TTD", "AXON", "TOST", "IONQ", "RKLB",
]


def gap_sleeve(gap_min=0.06, hold_bars=5, hard_stop=0.06, trail=0.10,
               max_concurrent=3, quiet=False, entry_mode="next_open",
               top_of_range=None):
    """Event-gap continuation. Signal day t: open[t] >= close[t-1]*(1+gap_min) AND
    close[t] >= open[t] (the gap HELD — sellers didn't fade it) AND volume[t] >=
    2x its 20d average (a real event, not noise). Entry next open t+1; exit on
    hard stop / trailing stop off the close-peak / time stop, fill next open.
    Capital model: sleeve splits into max_concurrent equal slots; a signal with no
    free slot is skipped (first-come). Returns ({date: ret}, trades)."""
    cost = COST_BPS / 10000.0
    events = []   # (entry_date_index_by_symbol …) collect per-symbol trades
    data = {}
    for s in GAP_UNIVERSE:
        d = fetch_ohlc(s)
        if d and len(d["close"]) > 260:
            data[s] = d
    trades = []
    for s, d in data.items():
        volma = sma(d["volume"], 20)
        n = len(d["ts"])
        i = 1
        while i < n - 1:
            gap = d["open"][i] / d["close"][i - 1] - 1
            held = d["close"][i] >= d["open"][i]
            vol_ok = volma[i - 1] and d["volume"][i] >= 2.0 * volma[i - 1]
            if top_of_range is not None and d["high"][i] > d["low"][i]:
                pos_in_range = (d["close"][i] - d["low"][i]) / (d["high"][i] - d["low"][i])
                held = held and pos_in_range >= top_of_range
            if gap >= gap_min and held and vol_ok:
                if entry_mode == "signal_close":
                    e = i                       # buy near the gap day's close (the
                    entry = d["close"][i] * (1 + cost)  # 15-min bot sees this live)
                else:
                    e = i + 1                   # enter next open
                    entry = d["open"][e] * (1 + cost)
                stop = entry * (1 - hard_stop)
                peak = d["close"][e]
                exit_px, exit_i, reason = None, None, None
                # signal_close entries happen AT the close of bar e — bar e's own
                # low predates the fill, so the exit scan must start at e+1.
                first_j = e if entry_mode != "signal_close" else e + 1
                for j in range(first_j, min(e + hold_bars + 1, n)):
                    if d["low"][j] <= stop:      # hard stop intraday
                        exit_px = stop * (1 - cost)
                        exit_i, reason = j, "stop"
                        break
                    peak = max(peak, d["close"][j])
                    if d["close"][j] <= peak * (1 - trail):
                        if j + 1 < n:
                            exit_px = d["open"][j + 1] * (1 - cost)
                            exit_i, reason = j + 1, "trail"
                        break
                    if j - e >= hold_bars:
                        exit_px = d["close"][j] * (1 - cost)
                        exit_i, reason = j, "time"
                        break
                if exit_px is None:
                    exit_i = min(e + hold_bars, n - 1)
                    exit_px = d["close"][exit_i] * (1 - cost)
                    reason = "eod"
                trades.append({"symbol": s, "entry_ts": d["ts"][e], "exit_ts": d["ts"][exit_i],
                               "ret": exit_px / entry - 1, "reason": reason,
                               "_ei": e, "_xi": exit_i, "_d": d})
                i = exit_i + 1                  # no overlapping trades per symbol
            else:
                i += 1
    # portfolio series: max_concurrent equal slots, first-come; a trade's exact
    # return is spread geometrically over its held bars (fill-accurate totals).
    trades.sort(key=lambda x: x["entry_ts"])
    slots = []      # exit dates of trades currently occupying a slot
    daily = {}
    for tr in trades:
        slots = [x for x in slots if x > tr["entry_ts"]]
        if len(slots) >= max_concurrent:
            tr["skipped"] = True
            continue
        slots.append(tr["exit_ts"])
        d = tr["_d"]
        if tr["_xi"] <= tr["_ei"]:              # same-bar exit (e.g. entry-day stop):
            day = d["ts"][tr["_xi"]]            # book the whole return that day
            daily[day] = daily.get(day, 0.0) + tr["ret"] / max_concurrent
            continue
        nb = tr["_xi"] - tr["_ei"]
        per_bar = (1 + tr["ret"]) ** (1 / nb) - 1
        for j in range(tr["_ei"] + 1, tr["_xi"] + 1):
            daily[d["ts"][j]] = daily.get(d["ts"][j], 0.0) + per_bar / max_concurrent
    live = [t for t in trades if not t.get("skipped")]
    if not quiet:
        rets = [t["ret"] for t in live]
        wins = [r for r in rets if r > 0]
        gl = -sum(r for r in rets if r <= 0)
        pf = sum(wins) / gl if gl > 0 else float("inf")
        print(f"[gap] signals={len(trades)} taken={len(live)} win={len(wins)/len(rets)*100:.0f}% "
              f"exp/trade={sum(rets)/len(rets)*100:+.2f}% PF={pf:.2f} "
              f"(gap>={gap_min*100:.0f}%, vol>=2x, hold<={hold_bars}d, stop {hard_stop*100:.0f}%, trail {trail*100:.0f}%)")
        by = {}
        for t in live:
            by[t["reason"]] = by.get(t["reason"], 0) + 1
        print(f"      exits: {by}")
    return daily, live


# ------------------------------------------------------------------ ROT overlay
def rotation_overlay(target_vol=0.35, use_vix_gate=True):
    """The existing backtest.py top-3 concentrated rotation, with a portfolio-level
    vol-target + VIX-contango overlay: scale tomorrow's exposure by
    min(1, target/realized_20d) and force 0.5x when VIX backwardated. Measures
    whether the overlay cuts the ~69% DD without killing the return."""
    import backtest as bt
    port = bt.backtest_rotation(bt.AGGRO_UNIVERSE, top_n=3)
    if not port:
        return None, None
    vix = fetch_ohlc("^VIX")
    vix3m = fetch_ohlc("^VIX3M")
    vgate = {}
    if vix and vix3m and use_vix_gate:
        v3 = {vix3m["ts"][i]: vix3m["close"][i] for i in range(len(vix3m["ts"]))}
        for i in range(len(vix["ts"])):
            t = vix["ts"][i]
            if t in v3 and v3[t] > 0:
                vgate[t] = vix["close"][i] < v3[t]
    dates = sorted(port)
    rets = [port[d] for d in dates]
    out = {}
    last_v = None
    for i, dte in enumerate(dates):
        if i < 21:
            out[dte] = rets[i]
            continue
        win = rets[i - 21:i]                    # strictly past
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / (len(win) - 1))
        rv = sd * math.sqrt(PPY)
        scale = min(1.0, target_vol / rv) if rv > 0 else 1.0
        if dte in vgate:
            last_v = vgate[dte]
        if last_v is False:
            scale = min(scale, 0.5)
        out[dte] = scale * rets[i]
    return port, out


# ------------------------------------------------------------------ DIP sleeve
def rsi(closes, length=2):
    out = [None] * len(closes)
    if len(closes) < length + 1:
        return out
    g = l = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0); l += max(-d, 0.0)
    ag, al = g / length, l / length
    out[length] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (length - 1) + max(d, 0.0)) / length
        al = (al * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def dip_sleeve(trade_etf="TQQQ", sig="SPY", rsi_buy=10, rsi_sell=65, max_hold=5,
               hard_stop=0.12, trend_len=200, quiet=False):
    """Index-dip mean reversion expressed through a 3x ETF: when SPY (the SIGNAL
    series — broad, mean-reverting) closes with RSI(2) < rsi_buy while ABOVE its
    200d SMA (dip in an uptrend, not a crash), buy the 3x ETF at the NEXT open;
    exit at next open after RSI(2) > rsi_sell, or hard stop on the ETF, or
    max_hold bars. Deliberately anti-correlated with trend sleeves: it is long
    exactly when momentum sleeves are being stopped out."""
    sd = fetch_ohlc(sig)
    td = fetch_ohlc(trade_etf)
    if not sd or not td:
        return None, []
    tix = {t: i for i, t in enumerate(td["ts"])}
    r2 = rsi(sd["close"], 2)
    ma = sma(sd["close"], trend_len)
    cost = COST_BPS_ETF / 10000.0
    daily = {}
    trades = []
    in_pos = False
    entry_px = entry_i = 0
    n = len(sd["ts"])
    for i in range(trend_len, n - 1):
        t_next = sd["ts"][i + 1]
        if not in_pos:
            if (r2[i] is not None and ma[i] is not None
                    and r2[i] < rsi_buy and sd["close"][i] > ma[i]):
                j = tix.get(t_next)
                if j is None:
                    continue
                in_pos = True
                entry_px = td["open"][j] * (1 + cost)
                entry_i = j
        else:
            j = tix.get(sd["ts"][i])
            if j is None:
                continue
            exit_now = None
            if td["close"][j] <= entry_px * (1 - hard_stop):
                exit_now = (td["close"][j] * (1 - cost), j, "stop")
            elif r2[i] is not None and r2[i] > rsi_sell:
                k = tix.get(t_next)
                if k is not None:
                    exit_now = (td["open"][k] * (1 - cost), k, "revert")
            elif j - entry_i >= max_hold:
                exit_now = (td["close"][j] * (1 - cost), j, "time")
            if exit_now:
                px, xi, why = exit_now
                ret = px / entry_px - 1
                trades.append({"entry_ts": td["ts"][entry_i], "exit_ts": td["ts"][xi],
                               "ret": ret, "reason": why})
                nb = max(1, xi - entry_i)
                pb = (1 + ret) ** (1 / nb) - 1
                for k in range(entry_i + 1, xi + 1):
                    daily[td["ts"][k]] = daily.get(td["ts"][k], 0.0) + pb
                if xi == entry_i:
                    daily[td["ts"][xi]] = daily.get(td["ts"][xi], 0.0) + ret
                in_pos = False
    if not quiet and trades:
        rets = [t["ret"] for t in trades]
        wins = [r for r in rets if r > 0]
        gl = -sum(r for r in rets if r <= 0)
        pf = sum(wins) / gl if gl > 0 else float("inf")
        why = {}
        for t in trades:
            why[t["reason"]] = why.get(t["reason"], 0) + 1
        print(f"[dip {trade_etf}|{sig}] n={len(rets)} win={len(wins)/len(rets)*100:.0f}% "
              f"exp/trade={sum(rets)/len(rets)*100:+.2f}% PF={pf:.2f} exits={why}")
    return daily, trades


# ------------------------------------------------------------------ NIGHT control
def night_sleeve(sym="QQQ"):
    """Overnight-only: buy close t, sell open t+1, half capital (T+1 alternation).
    Cost 2bps/side (tight ETF). The classic 'night effect' — tested as a control."""
    d = fetch_ohlc(sym)
    if not d:
        return None
    cost = 2 / 10000.0
    out = {}
    for i in range(1, len(d["ts"])):
        r = d["open"][i] / d["close"][i - 1] - 1 - 2 * cost
        out[d["ts"][i]] = 0.5 * r               # half capital each night (settlement)
    return out


# ------------------------------------------------------------------ COMBO
def combo(weights=None):
    """The proposed book: CORE + GAP + (optionally) NIGHT/cash. Correlations and
    combined stats. Rebalanced daily to fixed weights (small acct, fractional)."""
    weights = weights or {"core": 0.50, "gap": 0.35, "cash": 0.15}
    core = core_sleeve(quiet=True)
    gap, _ = gap_sleeve(quiet=True)
    spy = fetch_ohlc("SPY")
    dates = sorted(set(core) | set(gap))
    series = {}
    for dte in dates:
        series[dte] = (weights.get("core", 0) * core.get(dte, 0.0)
                       + weights.get("gap", 0) * gap.get(dte, 0.0))
    # correlation
    common = sorted(set(core) & set(gap))
    xs = [core[d] for d in common]
    ys = [gap[d] for d in common]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    corr = cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0
    print(f"\ncorr(core, gap) daily = {corr:+.2f}   weights = {weights}")
    print_block("COMBO", series, spy)
    return series


# ------------------------------------------------------------------ main
if __name__ == "__main__":
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    spy = fetch_ohlc("SPY")
    if which in ("core", "all"):
        s = core_sleeve()
        if s:
            print_block("CORE (TQQQ regime-throttled)", s, spy)
        s2 = core_sleeve(etf="SOXL", quiet=True)
        if s2:
            print_block("CORE variant (SOXL)", s2, spy, boot=False)
        # ablations
        s3 = core_sleeve(use_vix_gate=False, quiet=True)
        if s3:
            print_block("CORE ablation: no VIX gate", s3, spy, boot=False)
        s4 = core_sleeve(target_vol=99.0, quiet=True)   # no vol throttle
        if s4:
            print_block("CORE ablation: no vol target", s4, spy, boot=False)
        bh = {}
        d = fetch_ohlc("TQQQ")
        for i in range(1, len(d["ts"])):
            bh[d["ts"][i]] = d["close"][i] / d["close"][i - 1] - 1
        print_block("TQQQ buy&hold (reference)", bh, spy, boot=False)
    if which in ("gap", "all"):
        g, tr = gap_sleeve()
        if g:
            print_block("GAP continuation (3 slots)", g, spy)
    if which in ("rot", "all"):
        base, ov = rotation_overlay()
        if base:
            print_block("ROTATION top-3 (as-deployed v25)", base, spy)
            print_block("ROTATION + vol-target/VIX overlay", ov, spy)
    if which in ("dip", "all"):
        dsr, dtr = dip_sleeve()
        if dsr:
            print_block("DIP (TQQQ on SPY RSI2<10 in uptrend)", dsr, spy)
        d2, _ = dip_sleeve(trade_etf="QQQ", quiet=True)
        if d2:
            print_block("DIP variant (QQQ, unleveraged)", d2, spy, boot=False)
    if which in ("night", "all"):
        n = night_sleeve()
        if n:
            print_block("NIGHT (QQQ overnight, control)", n, spy, boot=False)
    if which in ("combo", "all"):
        combo()
    if which in ("spy", "all"):
        bh = {}
        for i in range(1, len(spy["ts"])):
            bh[spy["ts"][i]] = spy["close"][i] / spy["close"][i - 1] - 1
        print_block("SPY buy&hold (benchmark)", bh, None, boot=False)
