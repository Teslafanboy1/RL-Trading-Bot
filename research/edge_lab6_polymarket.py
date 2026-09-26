"""edge_lab6_polymarket.py — can prediction markets carry the 10%/month target?

Operator (2026-09-26): "you seem to be doing nothing over the weekend. Do you
know polymarkets exist ... at least 10 percent a month". Equities are closed on
weekends; Polymarket trades 24/7 and is CFTC-regulated for US residents
(Polymarket US, KYC, API). Before any money goes near it, this harness measures
whether a MECHANICAL Polymarket strategy has an edge, on real prices.

The one mechanical edge prediction markets are known for is the favourite-
longshot bias: longshots are over-priced, so near-certain favourites are
(slightly) under-priced. The trade that harvests it: shortly before a market's
scheduled end, buy the side priced at 80–99.5c and hold it to resolution. It
wins small and often, and loses everything on the rare upset — so the only
honest test is on a large sample of resolved markets, with fees and spread.

Data (free, public, no account): every binary Yes/No market that closed in the
last year with >= PM_MIN_VOL volume (gamma-api), plus its YES token's hourly
price for the 14 days before it closed (clob prices-history). Cached in
research/_cache/polymarket/.

Honesty rules, same as edge_lab4/5:
  * decisions only use what was knowable then — a market is tradable at t only
    if it is still open (t < closedTime) and its SCHEDULED endDate (published
    at listing) is within the horizon; the outcome is used only to settle.
  * entry pays the half-spread (PM_SPREAD, default 1c on top of the hourly
    price) plus Polymarket's real taker fee, rate*p*(1-p) per share, read from
    each market's own feeSchedule.
  * one position per event (Polymarket lists mutually-exclusive markets under
    one event — "Fed cuts 25bp", "Fed cuts 50bp" — which are not independent
    bets).

Run:
    python3 research/edge_lab6_polymarket.py              # unbiased sample (~20 min cold)
    PM_MODE=headline python3 research/edge_lab6_polymarket.py   # the biased one, for contrast

VERDICT (2026-09-26, 23,314 markets, 2025-09..2026-09): NO EDGE.
  * Polymarket is well calibrated. Favourites priced 80-99.5c win within
    ~0.3-0.8 points of their price; after a 1c spread and the real taker fee
    every favourite-harvest variant LOSES, -0.4% to -3% per bet. Best-case
    month (100% deployed, instant recycling) is negative in every cell.
  * The one positive cell (50-60c favourites, +1.8%/bet) does not survive:
    first half of the year -1.5%/bet, one-bet-per-event +0.3%/bet (±1.4pt
    edge, i.e. zero); the crypto slice is a BTC-downtrend bet, not a market edge.
  * TRAP, measured: the headline sample (top markets by volume) showed
    favourites losing 3-6 points vs price. That is selection bias — final
    volume is CAUSED by upsets ($300k+ markets: favourites -2.5pt; <$10k:
    +1.7pt). Never filter prediction-market backtests on final volume.
  * Not tested, and cannot be backtested honestly: an LLM forecaster picking
    mispriced markets (the model already knows how past markets resolved).
    The only honest test is forward: pre-registered calls on the desk scorecard.
"""

import calendar
import datetime as dt
import bisect
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import _ssl_context             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "research", "_cache", "polymarket")
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"

START = os.environ.get("PM_START", "2025-09-01")
END = os.environ.get("PM_END", "2026-09-20")
# "sample" (default): the COMPLETE population of resolved markets ending on
# every PM_SAMPLE_EVERY-th day. "headline": top markets by volume per month —
# kept only to show the bias it has (see fetch_markets).
MODE = os.environ.get("PM_MODE", "sample")
SAMPLE_EVERY = int(os.environ.get("PM_SAMPLE_EVERY", "7"))
MIN_VOL = float(os.environ.get("PM_MIN_VOL",
                               "1000" if MODE == "sample" else "50000"))
# the complete sample is ~111k markets; price histories for a seeded random
# subset keep the run to ~20 minutes without biasing which markets are kept
MAX_SAMPLE = int(os.environ.get("PM_MAX_SAMPLE", "25000"))
# 5-to-60-minute crypto "Up or Down" windows: a once-a-day scan can never be
# in them, so they are dropped by TITLE (known at listing, not the outcome)
_INTRADAY = re.compile(r"up or down.*\d{1,2}(:\d\d)?\s*(am|pm)", re.I)
SPREAD = float(os.environ.get("PM_SPREAD", "0.01"))
WINDOW_DAYS = 14
DAY = 86400
TARGET_MO = 0.10
START_EQUITY = 374.0            # the live account, 2026-09-25 close

_SSL = _ssl_context()


def _get_json(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "edge-lab6"})
            with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _ts(s):
    """Gamma timestamps come as '2026-06-15T00:00:00Z' or '2026-06-18 00:32:19+00'."""
    if not s:
        return None
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _month_windows():
    y, mo = int(START[:4]), int(START[5:7])
    while f"{y:04d}-{mo:02d}-01" < END:
        last = calendar.monthrange(y, mo)[1]
        yield (max(START, f"{y:04d}-{mo:02d}-01"),
               min(END, f"{y:04d}-{mo:02d}-{last:02d}"))
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)


def _parse_market(m):
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if len(outcomes) != 2 or len(tokens) != 2:
        return None                            # outcome[0] plays the "YES" role
    if prices not in (["1", "0"], ["0", "1"]):
        return None                            # voided / 50-50 / unresolved
    end_ts, close_ts = _ts(m.get("endDate")), _ts(m.get("closedTime"))
    if not end_ts or not close_ts:
        return None
    ev = (m.get("events") or [{}])[0]
    fee = m.get("feeSchedule") or {}
    return {
        "id": m.get("id"),
        "q": m.get("question", ""),
        "event": str(ev.get("id") or m.get("id")),
        "yes_token": tokens[0],
        "yes_won": prices[0] == "1",
        "end_ts": end_ts,
        "close_ts": close_ts,
        "vol": m.get("volumeNum") or 0,
        "fee_rate": float(fee.get("rate") or 0) if m.get("feesEnabled") else 0.0,
    }


def _gamma_window(lo, hi, *, order_by_volume=False):
    """Every resolved market with endDate in [lo, hi] (ISO strings). Returns
    (markets, capped) — gamma refuses offsets past ~2,100 with HTTP 422."""
    out, offset = [], 0
    while True:
        params = {"closed": "true", "limit": 100, "offset": offset,
                  "end_date_min": lo, "end_date_max": hi,
                  "volume_num_min": int(MIN_VOL)}
        if order_by_volume:
            params.update(order="volumeNum", ascending="false")
        try:
            page = _get_json(f"{GAMMA}?{urllib.parse.urlencode(params)}", tries=2)
        except Exception:
            return out, True
        if not page:
            return out, False
        offset += len(page)
        out.extend(page)


def _fetch_sample():
    """The COMPLETE population of resolved markets ending on every
    SAMPLE_EVERY-th day. A window that hits the paging cap is split into
    6-hour slices, and a slice that still caps is reported, never truncated
    silently."""
    out, seen = [], set()
    day = dt.date.fromisoformat(START)
    last = dt.date.fromisoformat(END)
    while day <= last:
        d = day.isoformat()
        raw, capped = _gamma_window(d + "T00:00:00Z", d + "T23:59:59Z")
        if capped:
            raw = []
            for h in range(0, 24, 6):
                part, c2 = _gamma_window(f"{d}T{h:02d}:00:00Z",
                                         f"{d}T{h + 5:02d}:59:59Z")
                if c2:
                    print(f"  WARNING {d} {h:02d}h slice still capped")
                raw.extend(part)
        n0 = len(out)
        for m in raw:
            pm = _parse_market(m)
            if pm and pm["id"] not in seen:
                seen.add(pm["id"])
                out.append(pm)
        print(f"  gamma {d}: {len(raw)} markets, {len(out) - n0} usable")
        day += dt.timedelta(days=SAMPLE_EVERY)
    return out


def _fetch_headline():
    """Top ~2,100 markets BY VOLUME per month. Biased: final volume is
    partly caused by the outcome (an upset draws traders), so this sample
    over-represents the markets where the favourite lost."""
    out, seen = [], set()
    for lo, hi in _month_windows():
        raw, _ = _gamma_window(lo, hi + "T23:59:59Z", order_by_volume=True)
        for m in raw:
            pm = _parse_market(m)
            if pm and pm["id"] not in seen:
                seen.add(pm["id"])
                out.append(pm)
        print(f"  gamma {lo[:7]}: {len(raw)} scanned, {len(out)} usable so far")
    return out


def fetch_markets():
    path = os.path.join(CACHE_DIR, f"markets_{MODE}{SAMPLE_EVERY if MODE == 'sample' else ''}"
                                   f"_{START}_{END}_{int(MIN_VOL)}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    out = _fetch_sample() if MODE == "sample" else _fetch_headline()
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f)
    return out


def _history(m):
    path = os.path.join(CACHE_DIR, "px", f"{m['id']}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    end = int(m["close_ts"])
    q = urllib.parse.urlencode({
        "market": m["yes_token"], "startTs": end - WINDOW_DAYS * DAY,
        "endTs": end, "fidelity": 60,
    })
    try:
        hist = _get_json(f"{CLOB}?{q}").get("history", [])
    except Exception:
        return None                             # not cached: retried next run
    pts = [[int(p["t"]), float(p["p"])] for p in hist]
    with open(path, "w") as f:
        json.dump(pts, f)
    return pts


def attach_histories(markets):
    os.makedirs(os.path.join(CACHE_DIR, "px"), exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        hists = list(ex.map(_history, markets))
    out = []
    for m, h in zip(markets, hists):
        if h and len(h) >= 12:
            m["px"] = h
            out.append(m)
    return out


def _price_at(px, t, max_stale=6 * 3600):
    """Last YES price at or before t (binary search), None if stale/absent."""
    lo, hi = 0, len(px) - 1
    if px[0][0] > t:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if px[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return px[lo][1] if t - px[lo][0] <= max_stale else None


def _decision_times():
    t0 = _ts(START + "T14:00:00Z") + WINDOW_DAYS * DAY
    t1 = _ts(END + "T14:00:00Z")
    out, t = [], t0
    while t <= t1:
        out.append(t)
        t += DAY
    return out


def index(markets):
    """Markets sorted by scheduled end, so a scan only touches the ones whose
    endDate falls inside its horizon."""
    ms = sorted(markets, key=lambda m: m["end_ts"])
    return [m["end_ts"] for m in ms], ms


def candidates(idx, t, *, lo, hi, horizon_days):
    """Markets tradable at t whose favourite is priced in [lo, hi]."""
    ends, ms = idx
    out = []
    for m in ms[bisect.bisect_right(ends, t):
                bisect.bisect_right(ends, t + horizon_days * DAY)]:
        if not (m["px"][0][0] <= t < m["close_ts"]):
            continue
        p_yes = _price_at(m["px"], t)
        if p_yes is None:
            continue
        fav_yes = p_yes >= 0.5
        p = p_yes if fav_yes else 1 - p_yes
        if lo <= p <= hi:
            out.append((m, fav_yes, p))
    return out


def _cost_per_share(m, p):
    entry = min(p + SPREAD, 0.999)
    return entry + m["fee_rate"] * entry * (1 - entry)


def per_trade(idx, times, *, lo, hi, horizon_days):
    """First qualifying entry per market, held to resolution."""
    seen, trades = set(), []
    for t in times:
        for m, fav_yes, p in candidates(idx, t, lo=lo, hi=hi, horizon_days=horizon_days):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            won = m["yes_won"] == fav_yes
            cost = _cost_per_share(m, p)
            trades.append({"m": m, "p": p, "won": won,
                           "ret": (1.0 / cost - 1.0) if won else -1.0,
                           "hold": (m["close_ts"] - t) / DAY})
    return trades


def portfolio(idx, times, *, lo, hi, horizon_days, frac):
    """Daily scan, `frac` of equity per new position (one per event), marked
    daily at the hourly price, settled at closedTime. Returns month-end equity."""
    cash, book, month_eq = START_EQUITY, [], {}
    for t in times:
        # settle anything that closed since the last scan
        still = []
        for pos in book:
            if pos["m"]["close_ts"] <= t:
                cash += pos["shares"] if pos["won"] else 0.0
            else:
                still.append(pos)
        book = still

        def mark():
            v = cash
            for pos in book:
                p_yes = _price_at(pos["m"]["px"], t, max_stale=10 * DAY)
                if p_yes is None:
                    p_yes = pos["p_entry_yes"]
                v += pos["shares"] * (p_yes if pos["fav_yes"] else 1 - p_yes)
            return v

        equity = mark()
        held_events = {pos["m"]["event"] for pos in book}
        for m, fav_yes, p in sorted(candidates(idx, t, lo=lo, hi=hi,
                                               horizon_days=horizon_days),
                                    key=lambda c: -c[2]):
            if m["event"] in held_events or cash < 1.0:
                continue
            spend = min(frac * equity, cash)
            shares = spend / _cost_per_share(m, p)
            cash -= spend
            held_events.add(m["event"])
            book.append({"m": m, "fav_yes": fav_yes, "shares": shares,
                         "won": m["yes_won"] == fav_yes,
                         "p_entry_yes": p if fav_yes else 1 - p})
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        month_eq[(d.year, d.month)] = mark()
    return month_eq


def _monthly(month_eq):
    keys = sorted(month_eq)
    rets, prev = [], START_EQUITY
    for k in keys:
        rets.append(month_eq[k] / prev - 1.0)
        prev = month_eq[k]
    return keys, rets


def _max_dd(month_eq):
    peak, dd = START_EQUITY, 0.0
    for k in sorted(month_eq):
        peak = max(peak, month_eq[k])
        dd = max(dd, 1 - month_eq[k] / peak)
    return dd


def _row(label, tr):
    n = len(tr)
    implied = sum(x["p"] for x in tr) / n
    won = sum(x["won"] for x in tr) / n
    gross = sum((1 / x["p"] - 1) if x["won"] else -1.0 for x in tr) / n
    net = sum(x["ret"] for x in tr) / n
    hold = sum(x["hold"] for x in tr) / n
    # upper bound on a month: 100% deployed, recycled the moment each bet
    # resolves, never idle — no real book gets close to this
    ceiling = (1 + net) ** (30 / max(hold, 0.25)) - 1 if net > -1 else -1
    print(f"{label:>18} {n:>6} {implied:>8.3f} {won:>7.3f} {won - implied:>+7.3f} "
          f"{gross:>+8.2%} {net:>+8.2%} {hold:>6.1f} {ceiling:>+9.1%}")
    return net


def main():
    print(f"edge_lab6 — Polymarket favourite harvest [{MODE}], {START}..{END}, "
          f"vol >= ${MIN_VOL:,.0f}, spread {SPREAD * 100:.1f}c + real taker fees\n")
    markets = fetch_markets()
    n_all = len(markets)
    markets = [m for m in markets if not _INTRADAY.search(m["q"])]
    if MODE == "sample" and len(markets) > MAX_SAMPLE:
        markets = random.Random(6).sample(markets, MAX_SAMPLE)
    print(f"{n_all} resolved two-outcome markets listed; "
          f"{len(markets)} kept (intraday crypto dropped, random subsample)")
    markets = attach_histories(markets)
    print(f"{len(markets)} with price history\n")
    idx = index(markets)
    times = _decision_times()
    head = (f"{'':>18} {'n':>6} {'implied':>8} {'won':>7} {'edge':>7} "
            f"{'gross':>8} {'net':>8} {'hold d':>6} {'mo cap':>9}")

    print("CALIBRATION — buy the favourite priced p, <= 7 days before scheduled end.")
    print("edge = win rate minus price. gross/net = return per bet before/after "
          "spread+fees. 'mo cap' = best possible month if every dollar were "
          "always in such bets.")
    print(head)
    for lo, hi in [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90),
                   (0.90, 0.95), (0.95, 0.98), (0.98, 0.995)]:
        tr = per_trade(idx, times, lo=lo, hi=hi, horizon_days=7)
        if tr:
            _row(f"{lo:.2f}-{hi:.3f}", tr)

    print("\nSTRATEGY GRID — favourite band x how close to the scheduled end:")
    print(head)
    best = None
    for lo, hi in [(0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.995),
                   (0.90, 0.995)]:
        for horizon in (1, 3, 7):
            tr = per_trade(idx, times, lo=lo, hi=hi, horizon_days=horizon)
            if len(tr) < 30:
                continue
            net = _row(f"{lo:.2f}-{hi:.3f} <={horizon}d", tr)
            if best is None or net > best[0]:
                best = (net, lo, hi, horizon)

    print("\nBY FINAL VOLUME (0.90-0.995, <=7d) — if favourites fare worse as "
          "volume rises, a volume-ranked sample is biased toward upsets:")
    print(head)
    tr = per_trade(idx, times, lo=0.90, hi=0.995, horizon_days=7)
    for a, b in [(0, 1e4), (1e4, 5e4), (5e4, 3e5), (3e5, 1e12)]:
        x = [t for t in tr if a <= t["m"]["vol"] < b]
        if len(x) >= 30:
            _row(f"${a:,.0f}+", x)

    if MODE == "headline" and best:
        print("\nPORTFOLIO (headline sample) — daily scan, hold to resolution, "
              f"${START_EQUITY:.0f} start")
        _, lo, hi, horizon = best
        for frac in (0.05, 0.20):
            me = portfolio(idx, times, lo=lo, hi=hi,
                           horizon_days=horizon, frac=frac)
            keys, rets = _monthly(me)
            if rets:
                hits = sum(r >= TARGET_MO for r in rets)
                print(f"  {lo:.2f}-{hi:.3f} <={horizon}d {frac:.0%}/bet: "
                      f"${me[keys[-1]]:.2f} final, {hits}/{len(rets)} months "
                      f">=10%, worst month {min(rets):+.1%}, maxDD {_max_dd(me):.0%}")

    print("\nTHE UPSETS — biggest favourites that lost (<=7d, >= 90c):")
    for x in sorted((x for x in tr if not x["won"]), key=lambda x: -x["p"])[:10]:
        print(f"  {x['p']:.3f}  {x['m']['q'][:90]}")


if __name__ == "__main__":
    main()
