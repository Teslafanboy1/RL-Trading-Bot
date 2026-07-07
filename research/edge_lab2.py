"""edge_lab2.py — second research sprint: four NON-TEXTBOOK mechanisms layered on
the proven top-2 momentum rotation, each walk-forward tested, keep-what-wins.

  RISKX — cross-asset risk-appetite composite throttle (HYG/IEF, XLY/XLP,
          CPER/GLD, BTC 1m momentum, IWM/SPY breadth) instead of / with VIX.
  DISPN — dispersion-adaptive concentration: top-1 when momentum dispersion is
          high (winners separating), top-3 when low (all-beta tape).
  ONSH  — overnight-share quality filter: penalize leaders whose trailing gains
          came mostly from overnight gaps (retail-attention + gap-risk proxy).
  TOM   — turn-of-month exposure cycling (full risk only around month turns).

Same honesty rules as edge_lab.py: signals from data <= t, next-day earning,
per-side costs on rotation events, expanding-window (past-only) quantiles.
"""

import os
import sys
import math
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import backtest as bt                      # noqa: E402 — rotation prep + universe

_spec = importlib.util.spec_from_file_location(
    "edge_lab", os.path.join(ROOT, "research", "edge_lab.py"))
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)

COST = bt.COST_BPS / 10000.0
PPY = 252


# ------------------------------------------------------------- shared helpers
def vol_throttle_and_vix(series_dates, rets, tv):
    """The proven overlay math (edge_lab): per-day scale from trailing vol + VIX
    term structure. Returns {date: scale}."""
    vix = lab.fetch_ohlc("^VIX"); vix3m = lab.fetch_ohlc("^VIX3M")
    v3 = {vix3m["ts"][i]: vix3m["close"][i] for i in range(len(vix3m["ts"]))}
    vg = {}
    for i in range(len(vix["ts"])):
        t = vix["ts"][i]
        if t in v3 and v3[t] > 0:
            vg[t] = vix["close"][i] < v3[t]
    out = {}; lv = None
    for i, d in enumerate(series_dates):
        if i < 21:
            out[d] = 1.0
            continue
        win = rets[i - 21:i]
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / (len(win) - 1))
        rv = sd * math.sqrt(PPY)
        sc = min(1.0, tv / rv) if rv > 0 else 1.0
        if d in vg:
            lv = vg[d]
        if lv is False:
            sc = min(sc, 0.5)
        out[d] = sc
    return out


def ratio_mom(num_sym, den_sym, lb=21):
    """{date: 1 if the num/den ratio is above its value lb bars ago} (risk-on)."""
    a = lab.fetch_ohlc(num_sym); b = lab.fetch_ohlc(den_sym)
    if not a or not b:
        return {}
    bmap = {b["ts"][i]: b["close"][i] for i in range(len(b["ts"]))}
    dates, ratio = [], []
    for i in range(len(a["ts"])):
        t = a["ts"][i]
        if t in bmap and bmap[t] > 0:
            dates.append(t)
            ratio.append(a["close"][i] / bmap[t])
    out = {}
    for i in range(lb, len(dates)):
        out[dates[i]] = 1 if ratio[i] > ratio[i - lb] else 0
    return out


def self_mom(sym, lb=21):
    d = lab.fetch_ohlc(sym)
    if not d:
        return {}
    out = {}
    for i in range(lb, len(d["ts"])):
        out[d["ts"][i]] = 1 if d["close"][i] > d["close"][i - lb] else 0
    return out


def riskx_scale():
    """Cross-asset risk-appetite composite -> {date: exposure scale}.
    5 components, each 0/1 risk-on; carry-forward last known value.
    score>=4 -> 1.0, 3 -> 0.75, 2 -> 0.5, <=1 -> 0.25."""
    comps = [ratio_mom("HYG", "IEF"), ratio_mom("XLY", "XLP"),
             ratio_mom("CPER", "GLD"), ratio_mom("IWM", "SPY"),
             self_mom("BTC-USD")]
    all_dates = sorted(set().union(*[set(c) for c in comps if c]))
    out = {}
    last = [None] * len(comps)
    for d in all_dates:
        for k, c in enumerate(comps):
            if d in c:
                last[k] = c[d]
        known = [x for x in last if x is not None]
        if not known:
            continue
        score = sum(known) + (len(comps) - len(known)) * 0.5  # unknown = neutral
        out[d] = 1.0 if score >= 4 else 0.75 if score >= 3 else 0.5 if score >= 2 else 0.25
    return out


# ------------------------------------------------- rotation with pluggable bits
def rotation(symbols, top_n=2, dynamic_n=None, score_adj=None,
             trail=None, trend_len=None, cfg=None):
    """Re-implementation of backtest.py's rotation loop with two hooks:
      dynamic_n(d)   -> today's top_n (DISPN)
      score_adj(s,d) -> adjusted rank score for symbol s on date d (ONSH)
    Same fill/cost/no-lookahead semantics as bt.backtest_rotation."""
    trail = trail if trail is not None else bt.MOM_TRAIL
    trend_len = trend_len or bt.MOM_TREND_LEN
    cfg = cfg or bt.MOM_CFG
    preps = {}
    for s in symbols:
        p = bt._prep_symbol(s, trend_len, cfg)
        if p:
            preps[s] = p
    if not preps:
        return None
    stop = {s: bt.effective_stop(s) for s in preps}
    dates = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    held = {}
    port = {}
    for d in dates:
        tn = dynamic_n(d) if dynamic_n else top_n
        day = 0.0
        for sym, pos in held.items():
            r = preps[sym]["ret"].get(d)
            if r is not None:
                day += r / max(tn, len(held))
            c = preps[sym]["close"].get(d)
            if c is not None:
                pos["peak"] = max(pos["peak"], c)
        events = 0
        for sym in list(held):
            c = preps[sym]["close"].get(d)
            if c is None:
                continue
            pos = held[sym]
            if (c <= pos["peak"] * (1 - trail) or c <= pos["entry"] * (1 - stop[sym])
                    or not preps[sym]["elig"].get(d, False)):
                del held[sym]
                events += 1
        while len(held) > tn:                      # concentration tightened today
            weakest = min(held, key=lambda s: preps[s]["score"].get(d, -9))
            del held[weakest]
            events += 1
        if len(held) < tn:
            def sc(s):
                base = preps[s]["score"].get(d, -9.9)
                return score_adj(s, d, base) if score_adj else base
            cand = sorted(((sc(s), s) for s in preps
                           if s not in held and preps[s]["elig"].get(d, False)),
                          reverse=True)
            for _, s in cand[:tn - len(held)]:
                c = preps[s]["close"].get(d)
                if c:
                    held[s] = {"entry": c, "peak": c}
                    events += 1
        port[d] = day - events * COST / max(tn, 1)
    return port


def apply_scale(port, scale_map, floor_scale=1.0):
    out = {}
    last = 1.0
    for d in sorted(port):
        if d in scale_map:
            last = scale_map[d]
        out[d] = port[d] * min(last, floor_scale)
    return out


def dispersion_n(symbols, trend_len=None, cfg=None):
    """{date: top_n} from cross-sectional momentum dispersion. disp = score gap
    between the leader and the median ELIGIBLE name; expanding past-only
    terciles map to n: high disp -> 1, mid -> 2, low -> 3."""
    trend_len = trend_len or bt.MOM_TREND_LEN
    cfg = cfg or bt.MOM_CFG
    preps = {}
    for s in symbols:
        p = bt._prep_symbol(s, trend_len, cfg)
        if p:
            preps[s] = p
    dates = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    disp = {}
    for d in dates:
        scores = sorted((p["score"].get(d) for p in preps.values()
                         if p["elig"].get(d, False) and p["score"].get(d) is not None),
                        reverse=True)
        if len(scores) >= 3:
            disp[d] = scores[0] - scores[len(scores) // 2]
    out = {}
    hist = []
    for d in sorted(disp):
        hist.append(disp[d])
        if len(hist) < 60:
            out[d] = 2
            continue
        past = sorted(hist[:-1])
        lo = past[len(past) // 3]
        hi = past[2 * len(past) // 3]
        v = disp[d]
        out[d] = 1 if v >= hi else 2 if v >= lo else 3
    return out


def overnight_share_adj(symbols):
    """score_adj hook: penalize names whose trailing-21d LOG return is mostly
    overnight gaps. share>0.75 -> score*0.6; share<0.25 (intraday-driven,
    institutional accumulation profile) -> score*1.1. Neutral otherwise."""
    ohlc = {s: lab.fetch_ohlc(s) for s in symbols}
    share = {}
    for s, d in ohlc.items():
        if not d:
            continue
        m = {}
        n = len(d["ts"])
        for i in range(22, n):
            on = sum(math.log(d["open"][j] / d["close"][j - 1])
                     for j in range(i - 20, i + 1)
                     if d["close"][j - 1] > 0 and d["open"][j] > 0)
            tot = (math.log(d["close"][i] / d["close"][i - 21])
                   if d["close"][i - 21] > 0 else 0.0)
            if tot > 0.02:                        # meaningful up-move only
                m[d["ts"][i]] = max(0.0, min(1.5, on / tot))
        share[s] = m

    def adj(sym, date, base):
        v = share.get(sym, {}).get(date)
        if v is None:
            return base
        if v > 0.75:
            return base * 0.6
        if v < 0.25:
            return base * 1.1
        return base
    return adj


def tom_scale(dates_all):
    """Turn-of-month: last 4 + first 3 trading days = 1.0, else 0.7."""
    by_month = {}
    for d in dates_all:
        by_month.setdefault(d[:7], []).append(d)
    out = {}
    months = sorted(by_month)
    for m in months:
        ds = sorted(by_month[m])
        window = set(ds[:3] + ds[-4:])
        for d in ds:
            out[d] = 1.0 if d in window else 0.7
    return out


def report(name, s):
    st = lab.series_stats(s); ms = lab.monthly_stats(s); br = lab.bootstrap_risk(s)
    print(f"{name:36s} CAGR {st['cagr']*100:+6.1f}%  Shp {st['sharpe']:.2f}  "
          f"DD {st['max_dd']*100:3.0f}%  wm {ms['worst']*100:+6.1f}%  "
          f"mo>=10% {ms['ge10']*100:3.0f}%  mo>=25% {ms['ge25']*100:3.0f}%  "
          f"P(DD50) {(br or {}).get('p_dd50_1y', 0)*100:4.1f}%")


if __name__ == "__main__":
    U = bt.AGGRO_UNIVERSE
    base = rotation(U, top_n=2)
    dates = sorted(base)
    rets = [base[d] for d in dates]
    thr = vol_throttle_and_vix(dates, rets, 0.50)
    baseline = {d: thr[d] * base[d] for d in dates}
    report("BASELINE top-2/0.5 (VIX+vol)", baseline)

    # 1) RISKX composite replaces VIX half-gate (vol throttle kept)
    rx = riskx_scale()
    vix_free = {}
    for i, d in enumerate(dates):                  # vol-only throttle
        if i < 21:
            vix_free[d] = base[d]; continue
        win = rets[i-21:i]; mu = sum(win)/len(win)
        sd = math.sqrt(sum((x-mu)**2 for x in win)/(len(win)-1)); rv = sd*math.sqrt(PPY)
        vix_free[d] = min(1.0, 0.5/rv if rv > 0 else 1.0) * base[d]
    riskx = apply_scale(vix_free, rx)
    report("RISKX composite throttle", riskx)

    # 2) DISPN dispersion-adaptive concentration (then same VIX+vol overlay)
    dn = dispersion_n(U)
    dyn = rotation(U, dynamic_n=lambda d: dn.get(d, 2))
    ddates = sorted(dyn); drets = [dyn[d] for d in ddates]
    dthr = vol_throttle_and_vix(ddates, drets, 0.50)
    dispn = {d: dthr[d] * dyn[d] for d in ddates}
    report("DISPN adaptive top-N", dispn)

    # 3) ONSH overnight-share filter on selection (same overlay)
    adj = overnight_share_adj(U)
    on = rotation(U, top_n=2, score_adj=adj)
    odates = sorted(on); orets = [on[d] for d in odates]
    othr = vol_throttle_and_vix(odates, orets, 0.50)
    onsh = {d: othr[d] * on[d] for d in odates}
    report("ONSH overnight-share filter", onsh)

    # 4) TOM turn-of-month cycling on the baseline
    tm = tom_scale(dates)
    tom = {d: baseline[d] * tm.get(d, 1.0) for d in dates}
    report("TOM turn-of-month cycling", tom)
