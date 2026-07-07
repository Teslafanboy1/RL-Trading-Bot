"""edge_lab3.py — third research sprint: 14 NEW mechanisms vs the RX-2 incumbent.

All challengers are modifications of the generalized rotation loop below, then
throttled by the SAME lagged vol-target x RISKX chain as RX-2, so every
comparison is apples-to-apples. Signals strictly from data <= t (external maps
lagged one day). Pre-registered acceptance bar: Sharpe >= incumbent + 0.15 AND
maxDD <= incumbent + 5pts, then halves + salted-universe survival.

 1  ATRMOM  — rank by ATR-normalized momentum (return / ATR14) not raw return
 2  HI52    — rank by proximity to 52-week high (George-Hwang)
 3  ACCEL   — rank by momentum acceleration (1m return now minus 1m a month ago)
 4  VOLCONF — eligibility also requires volume accumulation (vol MA20 > MA60)
 5  DECORR  — 2nd slot picks the LEAST-correlated of the top-5 (breadth)
 6  PULLBK  — enter leaders only 3-10%% below their 20d high (no chase)
 7  DEFENS  — risk-off capital rotates to best of GLD/TLT/XLU/XLP, not cash
 8  VVIX    — extra de-risk when vol-of-vol (^VVIX) is in its top quintile
 9  OVEREXT — early exit when price > 20d SMA + 4*ATR (parabolic trim)
10  GAPSIZE — weight names by 1/overnight-gap volatility (gap-risk parity)
11  TRENDAGE— only trends 10-150 days old (avoid late-cycle leaders)
12  WEEKDAY — new entries only Mon-Tue (avoid pre-weekend whipsaw)
13  THRUST  — Zweig-style breadth thrust => full exposure for 40 bars
14  SECREL  — leader must beat its OWN sector ETF over 1m (idiosyncratic strength)
"""

import os
import sys
import math
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import backtest as bt                      # noqa: E402

_s2 = importlib.util.spec_from_file_location(
    "lab2", os.path.join(ROOT, "research", "edge_lab2.py"))
lab2 = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(lab2)
lab = lab2.lab

COST = bt.COST_BPS / 10000.0
U = bt.AGGRO_UNIVERSE
SALTED = list(dict.fromkeys(U + ["PTON", "ZM", "DOCU", "TDOC", "LCID", "RIVN",
    "PLUG", "NIO", "AMC", "GME", "BYND", "ROKU", "SNAP", "PINS", "LYFT",
    "CHPT", "RUN", "SPCE", "WISH", "OPEN", "BB", "CLOV"]))


# ------------------------------------------------------------- shared data
def prep_all(universe):
    preps = {}
    for s in universe:
        p = bt._prep_symbol(s, bt.MOM_TREND_LEN, bt.MOM_CFG)
        if p:
            preps[s] = p
    return preps


def ohlc_maps(universe):
    """Per-symbol {date: (o,h,l,c,v)} maps for ATR/gap/volume mechanisms."""
    out = {}
    for s in universe:
        d = lab.fetch_ohlc(s)
        if not d:
            continue
        m = {}
        for i in range(len(d["ts"])):
            m[d["ts"][i]] = (d["open"][i], d["high"][i], d["low"][i],
                             d["close"][i], d["volume"][i], i)
        out[s] = (m, d)
    return out


def atr_series(d, length=14):
    """{date: ATR14/close} (normalized ATR) from an OHLC dict."""
    out = {}
    trs = []
    for i in range(1, len(d["ts"])):
        tr = max(d["high"][i] - d["low"][i],
                 abs(d["high"][i] - d["close"][i - 1]),
                 abs(d["low"][i] - d["close"][i - 1]))
        trs.append(tr)
        if len(trs) >= length and d["close"][i] > 0:
            out[d["ts"][i]] = sum(trs[-length:]) / length / d["close"][i]
    return out


def lag_map(m, dates):
    out = {}
    prev = None
    for d in dates:
        if prev is not None and prev in m:
            out[d] = m[prev]
        prev = d
    return out


def throttle(port, rx_map, tv=0.50, extra_scale=None, override_full=None):
    """Lagged vol-target x RISKX chain (identical to RX-2), with optional extra
    per-day scale map (VVIX) and full-exposure override map (THRUST) — both
    already lagged by the caller."""
    dates = sorted(port)
    rets = [port[d] for d in dates]
    gate = lag_map(rx_map, dates)
    out = {}
    last_g = 1.0
    for i, d in enumerate(dates):
        if i < 21:
            out[d] = port[d]
            continue
        win = rets[i - 21:i]
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / (len(win) - 1))
        rv = sd * math.sqrt(252)
        sc = min(1.0, tv / rv) if rv > 0 else 1.0
        if d in gate:
            last_g = gate[d]
        s = sc * last_g
        if extra_scale and d in extra_scale:
            s *= extra_scale[d]
        if override_full and override_full.get(d):
            s = 1.0
        out[d] = s * port[d]
    return out


# --------------------------------------------------- generalized rotation loop
def rotate(preps, top_n=2, score_fn=None, elig_fn=None, entry_ok_fn=None,
           weight_fn=None, early_exit_fn=None, entry_days=None):
    """Rotation with hooks. Defaults reproduce the incumbent exactly.
    Day-d P&L uses weights fixed at the prior close (no same-day reweighting)."""
    stop = {s: bt.effective_stop(s) for s in preps}
    dates = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    held = {}                      # sym -> {entry, peak, w}
    port = {}
    for di, d in enumerate(dates):
        day = 0.0
        for sym, pos in held.items():
            r = preps[sym]["ret"].get(d)
            if r is not None:
                day += pos["w"] * r
            c = preps[sym]["close"].get(d)
            if c is not None:
                pos["peak"] = max(pos["peak"], c)
        events = 0
        for sym in list(held):
            c = preps[sym]["close"].get(d)
            if c is None:
                continue
            pos = held[sym]
            base_exit = (c <= pos["peak"] * (1 - bt.MOM_TRAIL)
                         or c <= pos["entry"] * (1 - stop[sym])
                         or not preps[sym]["elig"].get(d, False))
            extra_exit = early_exit_fn(sym, d) if early_exit_fn else False
            if base_exit or extra_exit:
                del held[sym]
                events += 1
        can_enter = True
        if entry_days is not None:
            import datetime as _dt
            wd = _dt.date.fromisoformat(d).weekday()
            can_enter = wd in entry_days
        if len(held) < top_n and can_enter:
            def sc(s):
                if score_fn:
                    return score_fn(s, d)
                return preps[s]["score"].get(d, -9.9)
            cand = [s for s in preps
                    if s not in held and preps[s]["elig"].get(d, False)
                    and (elig_fn is None or elig_fn(s, d))
                    and (entry_ok_fn is None or entry_ok_fn(s, d))]
            cand.sort(key=sc, reverse=True)
            if weight_fn is None or len(held) + len(cand) <= 1:
                for s in cand[:top_n - len(held)]:
                    c = preps[s]["close"].get(d)
                    if c:
                        held[s] = {"entry": c, "peak": c, "w": 0.0}
                        events += 1
            else:                                    # DECORR-style second pick
                picks = weight_fn(list(held), cand, d, top_n)
                for s in picks:
                    c = preps[s]["close"].get(d)
                    if c and s not in held:
                        held[s] = {"entry": c, "peak": c, "w": 0.0}
                        events += 1
        n = max(top_n, len(held)) or 1
        for pos in held.values():                    # weights apply from tomorrow
            pos["w"] = 1.0 / n
        port[d] = day - events * COST / n
    return port


def gap_weighted(preps, gapvol, top_n=2):
    """GAPSIZE: same loop but weights inverse to trailing overnight-gap vol."""
    port = rotate(preps, top_n=top_n)   # membership identical to incumbent
    # rebuild with gap-parity weights: replay, adjusting only weights
    stop = {s: bt.effective_stop(s) for s in preps}
    dates = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    held = {}
    out = {}
    for d in dates:
        day = 0.0
        for sym, pos in held.items():
            r = preps[sym]["ret"].get(d)
            if r is not None:
                day += pos["w"] * r
            c = preps[sym]["close"].get(d)
            if c is not None:
                pos["peak"] = max(pos["peak"], c)
        events = 0
        for sym in list(held):
            c = preps[sym]["close"].get(d)
            if c is None:
                continue
            pos = held[sym]
            if (c <= pos["peak"] * (1 - bt.MOM_TRAIL)
                    or c <= pos["entry"] * (1 - stop[sym])
                    or not preps[sym]["elig"].get(d, False)):
                del held[sym]
                events += 1
        if len(held) < top_n:
            cand = sorted(((preps[s]["score"].get(d, -9.9), s) for s in preps
                           if s not in held and preps[s]["elig"].get(d, False)),
                          reverse=True)
            for _, s in cand[:top_n - len(held)]:
                c = preps[s]["close"].get(d)
                if c:
                    held[s] = {"entry": c, "peak": c, "w": 0.0}
                    events += 1
        if held:
            inv = {}
            for s in held:
                gv = gapvol.get(s, {}).get(d)
                inv[s] = 1.0 / gv if gv and gv > 0 else 1.0
            tot = sum(inv.values())
            budget = len(held) / top_n if top_n else 1.0   # keep cash slots cash
            for s, pos in held.items():
                pos["w"] = min(budget * inv[s] / tot, 0.6)
        out[d] = day - events * COST / max(top_n, 1)
    return out


def report(name, s):
    st = lab.series_stats(s)
    ms = lab.monthly_stats(s)
    br = lab.bootstrap_risk(s)
    print(f"{name:26s} CAGR {st['cagr']*100:+6.1f}%  Shp {st['sharpe']:.2f}  "
          f"DD {st['max_dd']*100:3.0f}%  wm {ms['worst']*100:+6.1f}%  "
          f"mo>=25% {ms['ge25']*100:3.0f}%  P(DD50) {(br or {}).get('p_dd50_1y',0)*100:4.1f}%")
    return st


if __name__ == "__main__":
    rx = lab2.riskx_scale()
    preps = prep_all(U)
    om = ohlc_maps(U)
    closes_full = {s: om[s][1] for s in om}

    # ---- shared per-symbol derived maps
    atr = {s: atr_series(d) for s, (m, d) in om.items()}
    hi52, accel, volok, pull, gapvol, age, dret = {}, {}, {}, {}, {}, {}, {}
    for s, (m, d) in om.items():
        n = len(d["ts"])
        h, a, v, p, g, ag, dr = {}, {}, {}, {}, {}, {}, {}
        vol20 = lab.sma(d["volume"], 20)
        vol60 = lab.sma(d["volume"], 60)
        sma200 = lab.sma(d["close"], 200)
        gaps = []
        cross = None
        for i in range(1, n):
            t = d["ts"][i]
            dr[t] = d["close"][i] / d["close"][i - 1] - 1
            lo = max(0, i - 252)
            hh = max(d["close"][lo:i + 1])
            h[t] = d["close"][i] / hh if hh > 0 else 0        # 1.0 = at 52w high
            if i >= 42:
                r_now = d["close"][i] / d["close"][i - 21] - 1
                r_prev = d["close"][i - 21] / d["close"][i - 42] - 1
                a[t] = r_now - r_prev
            if vol20[i] and vol60[i]:
                v[t] = vol20[i] > vol60[i]
            hi20 = max(d["close"][max(0, i - 20):i + 1])
            p[t] = (hi20 - d["close"][i]) / hi20 if hi20 > 0 else 0  # % below 20d high
            if d["close"][i - 1] > 0:
                gaps.append(d["open"][i] / d["close"][i - 1] - 1)
            if len(gaps) >= 63:
                w = gaps[-63:]
                mu = sum(w) / len(w)
                g[t] = math.sqrt(sum((x - mu) ** 2 for x in w) / (len(w) - 1))
            if sma200[i] is not None:
                above = d["close"][i] > sma200[i]
                prev_above = (sma200[i - 1] is not None
                              and d["close"][i - 1] > sma200[i - 1])
                if above and not prev_above:
                    cross = i
                ag[t] = (i - cross) if (above and cross is not None) else None
        hi52[s], accel[s], volok[s], pull[s] = h, a, v, p
        gapvol[s], age[s], dret[s] = g, ag, dr

    def corr63(s1, s2, d):
        r1, r2 = dret.get(s1, {}), dret.get(s2, {})
        ds = sorted(set(r1) & set(r2))
        ds = [x for x in ds if x <= d][-63:]
        if len(ds) < 30:
            return 0.0
        xs = [r1[x] for x in ds]; ys = [r2[x] for x in ds]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((a_ - mx) * (b_ - my) for a_, b_ in zip(xs, ys))
        vx = sum((a_ - mx) ** 2 for a_ in xs)
        vy = sum((b_ - my) ** 2 for b_ in ys)
        return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0

    # sector map for SECREL
    SEC = {}
    for s in ["NVDA","AMD","AVGO","MU","AMAT","SMCI","MRVL","TSM","SOXL"]: SEC[s] = "SMH"
    for s in ["PLTR","SNOW","CRWD","NET","DDOG","META","TQQQ","FNGU","TECL"]: SEC[s] = "XLK"
    for s in ["COIN","MSTR","HOOD","SOFI","FAS"]: SEC[s] = "XLF"
    for s in ["TSLA","SHOP","RBLX"]: SEC[s] = "XLY"
    sec_ret = {}
    for etf in set(SEC.values()) | {"SPY"}:
        d = lab.fetch_ohlc(etf)
        if d:
            m = {}
            for i in range(21, len(d["ts"])):
                m[d["ts"][i]] = d["close"][i] / d["close"][i - 21] - 1
            sec_ret[etf] = m

    incumbent = throttle(rotate(preps, 2), rx)
    inc = report("RX-2 incumbent", incumbent)

    tests = []
    tests.append(("1 ATRMOM", rotate(preps, 2, score_fn=lambda s, d:
        (preps[s]["score"].get(d, -9.9) / atr[s][d]) if atr.get(s, {}).get(d) else -9.9)))
    tests.append(("2 HI52", rotate(preps, 2, score_fn=lambda s, d:
        hi52.get(s, {}).get(d, -9.9))))
    tests.append(("3 ACCEL", rotate(preps, 2, score_fn=lambda s, d:
        accel.get(s, {}).get(d, -9.9))))
    tests.append(("4 VOLCONF", rotate(preps, 2, elig_fn=lambda s, d:
        volok.get(s, {}).get(d, True))))
    def decorr_pick(held, cand, d, top_n):
        picks = []
        pool = cand[:5]
        if not held and pool:
            picks.append(pool[0]); pool = pool[1:]
        anchor = (held + picks)[0] if (held or picks) else None
        while len(held) + len(picks) < top_n and pool:
            best = min(pool, key=lambda s: corr63(anchor, s, d))
            picks.append(best); pool.remove(best)
        return picks
    tests.append(("5 DECORR", rotate(preps, 2, weight_fn=decorr_pick)))
    tests.append(("6 PULLBK", rotate(preps, 2, entry_ok_fn=lambda s, d:
        0.03 <= pull.get(s, {}).get(d, 0) <= 0.10)))
    # 7 DEFENS: risk-off sleeve in defensive momentum instead of cash
    def_ret = {}
    for etf in ["GLD", "TLT", "XLU", "XLP"]:
        d0 = lab.fetch_ohlc(etf)
        if d0:
            mm, rr = {}, {}
            for i in range(21, len(d0["ts"])):
                mm[d0["ts"][i]] = d0["close"][i] / d0["close"][i - 21] - 1
                rr[d0["ts"][i]] = d0["close"][i] / d0["close"][i - 1] - 1
            def_ret[etf] = (mm, rr)
    base_port = rotate(preps, 2)
    dts = sorted(base_port)
    thr_series = throttle(base_port, rx)
    defens = {}
    gate_l = lag_map(rx, dts)
    last_g = 1.0
    for d in dts:
        if d in gate_l:
            last_g = gate_l[d]
        r = thr_series[d]
        freed = max(0.0, 1.0 - last_g)
        if freed > 0.2:
            best, bm = None, 0.0
            for etf, (mm, rr) in def_ret.items():
                v = mm.get(d)
                if v is not None and v > bm:
                    best, bm = etf, v
            if best:
                r += freed * def_ret[best][1].get(d, 0.0)
        defens[d] = r
    # 8 VVIX
    vv = lab.fetch_ohlc("^VVIX")
    vvx = {}
    if vv:
        hist = []
        for i in range(len(vv["ts"])):
            hist.append(vv["close"][i])
            if len(hist) > 252:
                past = sorted(hist[-1260:-1] if len(hist) > 1260 else hist[:-1])
                q80 = past[int(0.8 * len(past))]
                vvx[vv["ts"][i]] = 0.5 if vv["close"][i] >= q80 else 1.0
    # 9 OVEREXT
    sma20 = {}
    for s, (m, d) in om.items():
        ma = lab.sma(d["close"], 20)
        sma20[s] = {d["ts"][i]: ma[i] for i in range(len(d["ts"])) if ma[i]}
    def overext(s, d):
        c = preps[s]["close"].get(d)
        ma = sma20.get(s, {}).get(d)
        a = atr.get(s, {}).get(d)
        return bool(c and ma and a and c > ma + 4 * a * c)
    tests.append(("9 OVEREXT", rotate(preps, 2, early_exit_fn=overext)))
    tests.append(("10 GAPSIZE", gap_weighted(preps, gapvol)))
    tests.append(("11 TRENDAGE", rotate(preps, 2, elig_fn=lambda s, d:
        (lambda x: x is not None and 10 <= x <= 150)(age.get(s, {}).get(d)))))
    tests.append(("12 WEEKDAY", rotate(preps, 2, entry_days={0, 1})))
    # 13 THRUST: breadth of universe above 20d SMA
    breadth = {}
    all_d = sorted(set().union(*[set(p["close"]) for p in preps.values()]))
    for d in all_d:
        above = tot = 0
        for s in preps:
            c = preps[s]["close"].get(d)
            ma = sma20.get(s, {}).get(d)
            if c and ma:
                tot += 1
                if c > ma:
                    above += 1
        if tot >= 10:
            breadth[d] = above / tot
    thrust_on = {}
    bd = sorted(breadth)
    for i, d in enumerate(bd):
        if breadth[d] > 0.60:
            for j in range(max(0, i - 10), i):
                if breadth[bd[j]] < 0.35:
                    for k in range(i, min(i + 40, len(bd))):
                        thrust_on[bd[k]] = True
                    break
    tests.append(("14 SECREL", rotate(preps, 2, elig_fn=lambda s, d:
        (preps[s]["score"].get(d) is not None and
         sec_ret.get(SEC.get(s, "SPY"), {}).get(d) is not None and
         closes_full.get(s) is not None and
         (lambda om_=om[s][0].get(d): om_ is not None and om_[5] >= 21 and
          om[s][1]["close"][om_[5]] / om[s][1]["close"][om_[5]-21] - 1
          > sec_ret[SEC.get(s, "SPY")][d])()))))

    print()
    results = {}
    for name, port in tests:
        results[name] = report(name, throttle(port, rx))
    results["7 DEFENS"] = report("7 DEFENS", defens)
    results["8 VVIX"] = report("8 VVIX", throttle(base_port, rx,
        extra_scale=lag_map(vvx, dts) if vvx else None))
    results["13 THRUST"] = report("13 THRUST", throttle(base_port, rx,
        override_full=lag_map(thrust_on, dts)))
