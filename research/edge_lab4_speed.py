"""edge_lab4_speed.py — does trading RX-3 FASTER actually earn more?

Operator question (2026-08-14): "the bot made no progress in 2 weeks — can RX-3
go shorter timeframe / closer to day trading so we get more returns?"

That is two separate questions, and this harness answers both against 10y of
daily data before a dollar moves:

  Q1  What is the RISKX + vol throttle actually COSTING?  (RX-3 vs full-deploy)
  Q2  Does a SHORTER momentum lookback (a "faster" ribbon) beat the current
      1m-anchored one, after the extra turnover cost it creates?

Method — the point of this file is that it does NOT reimplement the strategy.
It drives `rotation_engine.target_book()` itself, the exact function the live
desk calls, walking one day at a time with closes strictly through YESTERDAY
(the same lag discipline; three same-day look-ahead bugs were caught in
research before, so the harness must not be able to reintroduce one). Only the
LOOKBACK CONFIG is swapped between variants, via a scoped monkeypatch of
momentum_screen.LOOKBACK_BARS — rotation_engine.py itself is never edited,
because its whole value is being provably the same math the backtest ran.

Costs: COST_BPS per side charged on every dollar of turnover, so a faster
variant has to pay for the churn it creates before it can look better.

Run:
    python3 research/edge_lab4_speed.py
    SPEED_COST_BPS=10 python3 research/edge_lab4_speed.py     # pessimistic fills
"""

import json
import math
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import momentum_screen as mscreen          # noqa: E402
import rotation_engine as rex              # noqa: E402
import signals                             # noqa: E402

COST_BPS = float(os.environ.get("SPEED_COST_BPS", "5"))
RANGE = os.environ.get("SPEED_RANGE", "10y")
CACHE = os.path.join(ROOT, "research", ".speed_cache.json")
PPY = 252

# The live RX-3 universe + everything the engine needs to compute RISKX/DEFENS.
UNIVERSE = ["NVDA", "AMD", "AVGO", "MU", "AMAT", "SMCI", "MRVL", "TSM", "PLTR",
            "COIN", "MSTR", "HOOD", "SOFI", "SNOW", "CRWD", "NET", "DDOG",
            "META", "TSLA", "SHOP", "RBLX", "TQQQ", "SOXL", "FNGU", "TECL",
            "LABU", "FAS", "TNA"]
COMPONENTS = [s for pair in rex.RISKX_COMPONENTS for s in pair if s]
DEFENSIVE = list(rex.DEFENSIVE_ASSETS)
LEVERAGED = {"TQQQ": 3, "SOXL": 3, "FNGU": 3, "TECL": 3, "LABU": 3, "FAS": 3,
             "TNA": 3}


# ------------------------------------------------------------------ data
def _fetch(symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={RANGE}&interval=1d&includePrePost=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 edgelab/4"})
    with urllib.request.urlopen(req, timeout=30, context=signals._ssl_context()) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    res = data["chart"]["result"][0]
    out = {}
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[time.strftime("%Y-%m-%d", time.gmtime(t))] = float(c)
    return out


def load_data(symbols):
    """{sym: {date: close}} with a disk cache — a sweep re-runs many variants
    over the same history and must not re-hammer Yahoo for each one."""
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    fresh = cache.get("_range") == RANGE and (time.time() - cache.get("_ts", 0)) < 86400
    out, missing = {}, []
    for s in symbols:
        if fresh and s in cache.get("data", {}):
            out[s] = cache["data"][s]
        else:
            missing.append(s)
    for i, s in enumerate(missing):
        try:
            out[s] = _fetch(s)
            print(f"  fetched {s:<6} {len(out[s])} bars ({i+1}/{len(missing)})")
        except Exception as e:
            print(f"  SKIP {s}: {type(e).__name__}")
        time.sleep(0.25)
    try:
        with open(CACHE, "w") as f:
            json.dump({"_range": RANGE, "_ts": time.time(), "data": out}, f)
    except Exception:
        pass
    return out


def align(data):
    """(dates, {sym: [close|None aligned to dates]}). Forward-filled after a
    symbol's first real bar so a missing day never shortens a lookback."""
    dates = sorted(set().union(*[set(d) for d in data.values()]))
    series = {}
    for sym, by_date in data.items():
        row, last = [], None
        for d in dates:
            v = by_date.get(d)
            if v is not None:
                last = v
            row.append(last)
        series[sym] = row
    return dates, series


# ------------------------------------------------------------------ sim
def _hist(series, sym, i, need=260):
    """Closes for `sym` strictly BEFORE index i (i.e. through yesterday).
    Returns None until the symbol has `need` real bars — an engine fed a short
    series silently degrades its own gates."""
    row = series.get(sym)
    if not row or i < need or row[i - 1] is None:
        return None
    win = [c for c in row[max(0, i - 400):i] if c is not None]
    return win if len(win) >= need else None


def simulate(dates, series, *, lookbacks, targets, top_n, full_deploy,
             trend_len, rebalance_every=1, cost_bps=COST_BPS, label=""):
    """Walk the history one day at a time through the LIVE engine.

    On each rebalance day the target book is computed from closes through
    YESTERDAY and earns the return from today onward — never today's own bar.
    Returns a stats dict, or None if the history was too thin.
    """
    # Scoped config swap: the engine reads these module globals. Restored in
    # `finally` so one variant can never contaminate the next.
    old_lb, old_cfg, old_trend = mscreen.LOOKBACK_BARS, rex.MOM_CFG, rex.TREND_LEN
    cfg = dict(rex.MOM_CFG)
    cfg["targets"] = targets
    try:
        mscreen.LOOKBACK_BARS = lookbacks
        rex.MOM_CFG = cfg
        rex.TREND_LEN = trend_len

        weights, equity, curve, sleeve, turn_total, rebals = {}, 1.0, {}, [], 0.0, 0
        warm = max(trend_len, max(lookbacks.values())) + 5
        for i in range(warm, len(dates)):
            d = dates[i]
            # 1) today's P&L on yesterday's book
            day = 0.0
            for sym, w in weights.items():
                row = series[sym]
                if row[i] is not None and row[i - 1]:
                    day += w * (row[i] / row[i - 1] - 1.0)
            equity *= (1 + day)
            sleeve.append(day)
            curve[d] = day

            # 2) rebalance on schedule, from data through yesterday only
            if (i - warm) % rebalance_every:
                continue
            uni = {s: h for s in UNIVERSE if (h := _hist(series, s, i))}
            if len(uni) < 5:
                continue
            comp = {s: h for s in COMPONENTS if (h := _hist(series, s, i))}
            dfs = {s: h for s in DEFENSIVE if (h := _hist(series, s, i))}
            tb = rex.target_book(
                uni, comp, dfs, sleeve[-rex.VOL_LOOKBACK:],
                held=list(weights),
                leverage_factor=lambda s: LEVERAGED.get(s, 1),
                full_deploy=full_deploy, top_n=top_n)
            new = {**tb["weights"], **tb["defensive"]}
            turn = sum(abs(new.get(s, 0) - weights.get(s, 0))
                       for s in set(new) | set(weights))
            equity *= (1 - turn * cost_bps / 10000.0)
            curve[d] = curve.get(d, 0.0) - turn * cost_bps / 10000.0
            turn_total += turn
            rebals += 1
            weights = new

        if len(curve) < 250:
            return None
        ordered = sorted(curve)
        vals = [curve[d] for d in ordered]

        # How often would risk_guard's kill-switch actually fire? It halts and
        # force-flattens on a >=25% drawdown from the MONTH's peak equity, and
        # then refuses to trade until the operator deletes the HALT file by hand.
        # A backtest CAGR that ignores this is fiction for any variant whose
        # drawdowns routinely exceed it, so count the trips.
        halts, months, eq2, mpeak, cur_month, tripped = 0, 0, 1.0, 1.0, None, False
        for d in ordered:
            if d[:7] != cur_month:
                cur_month, mpeak, tripped = d[:7], eq2, False
                months += 1
            eq2 *= (1 + curve[d])
            mpeak = max(mpeak, eq2)
            if not tripped and mpeak > 0 and (1 - eq2 / mpeak) >= 0.25:
                halts += 1
                tripped = True

        yrs = len(vals) / PPY
        cagr = equity ** (1 / yrs) - 1 if yrs > 0 and equity > 0 else -1.0
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1))
        sharpe = (mu / sd) * math.sqrt(PPY) if sd > 0 else 0.0
        peak, dd = 1.0, 0.0
        eq = 1.0
        for v in vals:
            eq *= (1 + v)
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
        return {"label": label, "final": equity, "cagr": cagr, "sharpe": sharpe,
                "maxdd": dd, "turnover_yr": turn_total / yrs, "rebals": rebals,
                "years": yrs, "days": len(vals), "halts": halts,
                "halt_months_pct": halts / months if months else 0.0}
    finally:
        mscreen.LOOKBACK_BARS, rex.MOM_CFG, rex.TREND_LEN = old_lb, old_cfg, old_trend


def scaled(base, k, floor=2):
    return {tf: max(floor, int(round(v * k))) for tf, v in base.items()}


def main():
    syms = sorted(set(UNIVERSE + COMPONENTS + DEFENSIVE))
    print(f"edge_lab4 — SPEED test. range={RANGE} cost={COST_BPS}bps/side "
          f"({len(syms)} symbols)\n")
    data = load_data(syms)
    data = {s: d for s, d in data.items() if len(d) > 300}
    dates, series = align(data)
    print(f"  {len(dates)} trading days, {dates[0]} → {dates[-1]}\n")

    base_lb = {"1w": 5, "1m": 21, "6m": 126, "1y": 252}
    base_tg = {"1w": 0.0, "1m": 0.08, "6m": 0.15, "1y": 0.25}
    rows = []

    def run(label, *, k=1.0, top_n=2, full_deploy=False, trend=200,
            every=1, scale_targets=True):
        lb = scaled(base_lb, k)
        tg = ({tf: round(v * k, 4) for tf, v in base_tg.items()}
              if scale_targets else dict(base_tg))
        r = simulate(dates, series, lookbacks=lb, targets=tg, top_n=top_n,
                     full_deploy=full_deploy, trend_len=trend,
                     rebalance_every=every, label=label)
        if r:
            r["lb"] = f"{lb['1w']}/{lb['1m']}/{lb['6m']}"
            rows.append(r)
            print(f"  {label:<34}{r['lb']:>12}  CAGR {r['cagr']*100:>7.1f}%  "
                  f"Sharpe {r['sharpe']:>5.2f}  maxDD {r['maxdd']*100:>5.1f}%  "
                  f"turn {r['turnover_yr']:>5.1f}x/yr  HALTs {r['halts']:>3} "
                  f"({r['halt_months_pct']*100:.0f}% of months)")
        else:
            print(f"  {label:<34}  (insufficient history)")

    print("── Q1: what do the RISKX + vol throttles cost? ─────────────────────")
    run("RX-3 live (throttled, top2)")
    run("RX-4 full-deploy (top2)", full_deploy=True)
    run("RX-4 full-deploy (top4)", full_deploy=True, top_n=4)

    print("\n── Q2: does a FASTER (shorter-lookback) ribbon earn more? ──────────")
    for k, name in ((0.5, "2x faster"), (0.33, "3x faster"), (0.25, "4x faster")):
        run(f"RX-3 throttled, {name}", k=k)
    for k, name in ((0.5, "2x faster"), (0.33, "3x faster"), (0.25, "4x faster")):
        run(f"RX-4 full-deploy, {name}", k=k, full_deploy=True)

    print("\n── Q2b: faster ribbon + faster trend gate (SMA100/50) ──────────────")
    run("RX-4 2x faster, SMA100", k=0.5, full_deploy=True, trend=100)
    run("RX-4 2x faster, SMA50", k=0.5, full_deploy=True, trend=50)
    run("RX-4 4x faster, SMA50", k=0.25, full_deploy=True, trend=50)

    print("\n── Q2c: does rebalancing LESS often cost anything? ─────────────────")
    run("RX-4 full-deploy, weekly rebal", full_deploy=True, every=5)

    print("\n" + "=" * 78)
    print("RANKED BY CAGR (all costs charged):")
    for r in sorted(rows, key=lambda x: -x["cagr"]):
        print(f"  {r['label']:<34} CAGR {r['cagr']*100:>7.1f}%  "
              f"Sharpe {r['sharpe']:>5.2f}  maxDD {r['maxdd']*100:>5.1f}%  "
              f"turnover {r['turnover_yr']:>5.1f}x/yr  ${100*r['final']:>10,.0f}")
    print("\nRANKED BY SHARPE (return per unit of pain):")
    for r in sorted(rows, key=lambda x: -x["sharpe"]):
        print(f"  {r['label']:<34} Sharpe {r['sharpe']:>5.2f}  "
              f"CAGR {r['cagr']*100:>7.1f}%  maxDD {r['maxdd']*100:>5.1f}%")


if __name__ == "__main__":
    main()
