"""edge_lab5_moonshot.py — what would it take to earn 20% A MONTH?

Operator target (2026-09-23): "returns need to be at least 20% a month".
20%/mo compounds to ~790%/yr. Instead of arguing about it, this harness pushes
the LIVE engine (`rotation_engine.target_book`, same function the desk calls,
same lag discipline as edge_lab4) to its most aggressive settings and adds
portfolio leverage on top, then reports the number that matters for that
target: how many months actually cleared +20%, and how often the account
would have been wiped out getting there.

Leverage is modelled as a daily-rebalanced multiple L of the book's return,
with a borrow cost on the (L-1) borrowed part. That is roughly what a 2x/3x
ETF, or a margin loan, delivers. The real account is limited-margin with NO
borrowing, so L>1 is a hypothetical of what leveraged instruments would do —
it is optimistic (no ETF decay beyond the daily reset, no margin calls).

Run:
    python3 research/edge_lab5_moonshot.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edge_lab4_speed as lab4             # noqa: E402
import rotation_engine as rex              # noqa: E402

BORROW_APR = float(os.environ.get("MOON_BORROW_APR", "0.08"))
TARGET_MO = 0.20
PPY = 252


def daily_curve(dates, series, *, top_n, full_deploy, every=1):
    """{date: book return that day, net of 5bps/side costs}. Mirrors
    lab4.simulate() exactly, but returns the curve instead of summary stats."""
    weights, sleeve, curve = {}, [], {}
    warm = 257
    for i in range(warm, len(dates)):
        day = 0.0
        for sym, w in weights.items():
            row = series[sym]
            if row[i] is not None and row[i - 1]:
                day += w * (row[i] / row[i - 1] - 1.0)
        sleeve.append(day)
        curve[dates[i]] = day
        if (i - warm) % every:
            continue
        uni = {s: h for s in lab4.UNIVERSE if (h := lab4._hist(series, s, i))}
        if len(uni) < 5:
            continue
        comp = {s: h for s in lab4.COMPONENTS if (h := lab4._hist(series, s, i))}
        dfs = {s: h for s in lab4.DEFENSIVE if (h := lab4._hist(series, s, i))}
        tb = rex.target_book(uni, comp, dfs, sleeve[-rex.VOL_LOOKBACK:],
                             held=list(weights),
                             leverage_factor=lambda s: lab4.LEVERAGED.get(s, 1),
                             full_deploy=full_deploy, top_n=top_n)
        new = {**tb["weights"], **tb["defensive"]}
        turn = sum(abs(new.get(s, 0) - weights.get(s, 0))
                   for s in set(new) | set(weights))
        curve[dates[i]] -= turn * lab4.COST_BPS / 10000.0
        weights = new
    return curve


def stats(curve, lev):
    ordered = sorted(curve)
    carry = (lev - 1) * BORROW_APR / PPY
    eq, peak, dd, ruined = 1.0, 1.0, 0.0, False
    months, m_start, cur = {}, 1.0, None
    for d in ordered:
        if d[:7] != cur:
            if cur is not None:
                months[cur] = eq / m_start - 1
            cur, m_start = d[:7], eq
        r = max(-1.0, lev * curve[d] - carry)
        eq *= (1 + r)
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak if peak else 1)
        if eq <= 0.10:          # lost 90% of the starting stake
            ruined = True
    months[cur] = eq / m_start - 1
    mo = list(months.values())
    yrs = len(ordered) / PPY
    cagr = eq ** (1 / yrs) - 1 if eq > 0 else -1.0
    geo_mo = (1 + cagr) ** (1 / 12) - 1 if cagr > -1 else -1.0
    return {
        "cagr": cagr, "geo_mo": geo_mo, "final": eq, "maxdd": dd,
        "hit20": sum(m >= TARGET_MO for m in mo) / len(mo),
        "median_mo": sorted(mo)[len(mo) // 2],
        "worst_mo": min(mo), "best_mo": max(mo),
        "loss_mo": sum(m < 0 for m in mo) / len(mo),
        "ruined": ruined, "years": yrs,
    }


def main():
    syms = sorted(set(lab4.UNIVERSE + lab4.COMPONENTS + lab4.DEFENSIVE))
    data = lab4.load_data(syms)
    data = {s: d for s, d in data.items() if len(d) > 300}
    dates, series = lab4.align(data)
    print(f"edge_lab5 — MOONSHOT. {dates[0]} → {dates[-1]}, "
          f"{len(data)} symbols, borrow {BORROW_APR:.0%}/yr\n")
    print(f"  {'variant':<32}{'CAGR':>8}{'avg/mo':>8}{'median':>8}"
          f"{'≥20% mo':>9}{'loss mo':>9}{'worst mo':>10}{'maxDD':>8}"
          f"{'$100→':>12}  ruin")
    books = [
        ("RX-3 live (throttled top2)", dict(top_n=2, full_deploy=False)),
        ("full-deploy top4", dict(top_n=4, full_deploy=True)),
        ("full-deploy top2", dict(top_n=2, full_deploy=True)),
        ("full-deploy top1 (all-in)", dict(top_n=1, full_deploy=True)),
    ]
    rows = []
    for name, kw in books:
        curve = daily_curve(dates, series, **kw)
        for lev in (1.0, 1.5, 2.0, 3.0):
            s = stats(curve, lev)
            label = f"{name} x{lev:g}"
            rows.append((label, s))
            print(f"  {label:<32}{s['cagr']*100:>7.1f}%{s['geo_mo']*100:>7.1f}%"
                  f"{s['median_mo']*100:>7.1f}%{s['hit20']*100:>8.0f}%"
                  f"{s['loss_mo']*100:>8.0f}%{s['worst_mo']*100:>9.1f}%"
                  f"{s['maxdd']*100:>7.0f}%{100*s['final']:>12,.0f}  "
                  f"{'YES' if s['ruined'] else 'no'}")
        print()
    best = max(rows, key=lambda r: r[1]["geo_mo"])
    need = (1 + TARGET_MO) ** 12 - 1
    print(f"Target 20%/mo = {need*100:,.0f}%/yr. Best variant: {best[0]} at "
          f"{best[1]['geo_mo']*100:.1f}%/mo ({best[1]['cagr']*100:.0f}%/yr), "
          f"maxDD {best[1]['maxdd']*100:.0f}%.")


if __name__ == "__main__":
    main()
