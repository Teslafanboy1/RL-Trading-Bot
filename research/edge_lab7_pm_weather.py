"""edge_lab7_pm_weather.py — Polymarket daily-temperature markets vs. NWP forecasts.

edge_lab6 found Polymarket well calibrated: mechanical "buy the favourite"
loses after spread + taker fees. This harness tests an INFORMATION edge
instead. Polymarket lists ~50 daily "Highest temperature in <city> on <date>?"
events (one bucket market per whole degree C, or per 2-degree F range in US
cities; mutually exclusive, exactly one wins; resolved from the airport's
METAR record on Wunderground). Free numerical weather forecasts (GFS, ECMWF
IFS, DWD ICON via Open-Meteo) are skilful 1-2 days ahead. If the crowd
misprices buckets relative to a calibrated forecast distribution, buying the
buckets where forecast probability > price + costs should make money.

Method (all public, read-only data; cached in research/_cache/polymarket_weather/):
  1. Events: gamma /events?tag_slug=weather&closed=true, one query per end
     date (paged), titles "Highest temperature in X on <Month D>?". The whole
     population is listed; a SEEDED RANDOM subsample (PMW_MAX_EVENTS) gets
     price histories. Only an event-volume floor of PMW_MIN_VOL is applied —
     never a volume ranking (edge_lab6: final volume is caused by upsets).
  2. Parse: city/date from the title, buckets from groupItemTitle ("29°C",
     "26°C or below", "36°C or higher", "80-81°F"), the resolution station's
     ICAO code from the description's Wunderground / weather.gov link, its
     lat/lon from aviationweather.gov. Events whose buckets don't tile the
     line, that don't have exactly one winner, or that resolve to 0.1°C
     (Hong Kong Observatory — bucket semantics ambiguous) are dropped.
  3. Market price at decision time D = local end of target day minus 48h,
     24h, 16h (i.e. 00:00 local two days before / 00:00 local on the day /
     08:00 local on the day): last CLOB hourly price at or before D, stale if
     older than 6h. Every bucket of the event must be priced.
  4. Forecast AS ISSUED before D (no look-ahead): Open-Meteo's Previous Runs
     API. temperature_2m_previous_dayN at valid hour T comes from the model
     run initialised at or before T-24N h (verified against the Single Runs
     API: day1 at 00-05Z == the run from 00Z the day before). For each local
     hour T of the target day we use the SMALLEST N with
     T - 24N h + PMW_PUB_DELAY_H <= D, i.e. only runs that were initialised
     AND published (default 9h publication delay, conservative for ECMWF
     open data) before the decision. Daily max = max over local hours.
     Models: gfs_global, ecmwf_ifs025, icon_global, and their average.
  5. Forecast -> bucket probabilities: true max = fmax + mu + e,
     e ~ N(0, sigma); bucket "29°C" = P(28.5 <= X < 29.5) (rounding to whole
     degrees; °F buckets converted to °C bounds). mu, sigma fitted by
     interval-censored maximum likelihood on the winning bucket, per lead
     time, globally + per station shrunk toward global (PMW_SHRINK
     pseudo-events). Fitted on TRAIN only (first half of events by date);
     the forecast model (gfs/ecmwf/icon/avg) is also chosen on TRAIN.
  6. Strategy: edge = q_model - (price + PMW_SPREAD + fee), fee per share =
     rate*c*(1-c) from each market's feeSchedule. Buy YES where edge >=
     threshold, hold to resolution. Also the mirror (buy NO where the model
     says the bucket is overpriced). Per-bet stats, per-EVENT accounting
     (buckets of one event are one correlated bet), Brier/log-loss model vs
     market, a 5%-per-bet bankroll sim, and a by-month model-vs-market check.
     Sanity check done: shifting the forecast one day either way blows the
     fitted sigma from 1.5 to 3.4-3.6°C, so day/timezone alignment is right.

Run:
    python3 research/edge_lab7_pm_weather.py            # ~25 min cold, <1 min cached
    PMW_MAX_EVENTS=1000 python3 research/edge_lab7_pm_weather.py   # quicker
Knobs: PMW_START, PMW_END, PMW_MAX_EVENTS, PMW_MIN_VOL, PMW_SPREAD,
       PMW_PUB_DELAY_H, PMW_SHRINK, PMW_THREADS, PMW_SEED.

VERDICT (2026-09-26): NO EDGE. THE CROWD IS A BETTER FORECASTER THAN FREE NWP.
  Population: 10,438 resolved events / 110,592 bucket markets, 2025-09-20 to
  2026-09-22 (2 cities until Dec 2025, ~50 from Apr 2026); 9,887 parsed (5.3%
  dropped: 345 under the $500 floor, 186 Hong Kong 0.1°C, 20 other). Error
  model calibrated on all 4,933 TRAIN-date events; seeded 25% subsample
  (2,500 events, 26,472 tokens) priced -> ~2,200-2,300 usable events per lead
  across 53 cities. TRAIN < 2026-06-17 <= TEST.
  * The market beats the forecast OUT OF SAMPLE at every lead. At D 00:00L:
    log-loss of the winning bucket 1.32 (market) vs 1.63 (best model: 3-model
    average + online station bias). Brier 0.059 vs 0.068. D-2 and 08:00L
    look the same. The market beat the model in every month from Oct 2025 on
    (the one exception is Sep 2025, 8 events), so there was no early,
    inefficient window.
  * The forecast adds no information beyond the price. A log-pool blend
    (price^w * model^(1-w)) fit on TRAIN puts w = 0.80-0.90 on the price, and
    on TEST it scores worse than the price alone (LL 1.328 vs 1.321).
  * The strategy loses everywhere with real samples. "Buy YES where
    model - cost >= 5c" at D 00:00L: TEST 1,833 bets, 1,018 events, won 5.6%
    at avg cost 8.4c (model said 19%), -46%/bet, -37%/event (t = -4.1).
    It lost on TRAIN too (-22%/bet, t = -2.0). All 36 model cells (3 leads x
    YES/NO/both x 4 thresholds) lose per bet on TEST. Per event, 35 of 36
    lose: YES -17% to -47%, NO -5% to -15%, both -9% to -24%, most with
    t < -3. The exception is D-2 YES 0.15, at +7.7%/event with t = 0.3.
    The 5%-per-bet bankroll went $374 -> $0.22 in the first two TEST weeks.
    The model is honestly calibrated but much less sharp. Where it disagrees
    with the crowd, the crowd is right.
  * The one positive TEST cell is noise. Blend, NO side, thr 0.05, 08:00L:
    73 bets / 67 events, +14%/event, t = 0.8, bankroll $374 -> $506 in 3.5
    months. Its blend weight was fit in-sample on TRAIN, and it was the best of
    ~50 cells. Do not trade it.
  * Why: the traders already use these forecasts plus better inputs: ensembles,
    MOS, high-resolution regional models (HRRR/UKV/AROME) and live METAR
    obs. To win, a bot would need a forecast sharper than that crowd, and then
    still clear 1c spread + the 5% x p(1-p) taker fee. Not reachable with
    free global models.
  * Not tested: maker (limit-order) execution, which avoids the taker fee
    and earns the rebate. That is market making, a different business with
    adverse-selection risk. Also untested: same-day trading once the
    morning obs are in, since the model here never sees observations.
"""

import bisect
import calendar
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo                # Python 3.9+
except ImportError:                              # pragma: no cover
    ZoneInfo = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import _ssl_context             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "research", "_cache", "polymarket_weather")
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com/prices-history"
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
STATIONS = "https://aviationweather.gov/api/data/stationinfo"

START = os.environ.get("PMW_START", "2025-09-20")
END = os.environ.get("PMW_END", "2026-09-22")
MAX_EVENTS = int(os.environ.get("PMW_MAX_EVENTS", "2500"))
MIN_VOL = float(os.environ.get("PMW_MIN_VOL", "500"))
SPREAD = float(os.environ.get("PMW_SPREAD", "0.01"))
PUB_DELAY_H = float(os.environ.get("PMW_PUB_DELAY_H", "9"))
SHRINK = float(os.environ.get("PMW_SHRINK", "10"))
THREADS = int(os.environ.get("PMW_THREADS", "10"))
SEED = int(os.environ.get("PMW_SEED", "7"))
STALE_S = 6 * 3600
LEADS = (48, 24, 16)                # hours before the local end of the target day
LEAD_NAME = {48: "D-2 00:00L", 24: "D 00:00L", 16: "D 08:00L"}
MODELS = ("gfs_global", "ecmwf_ifs025", "icon_global")
N_MAX = 4                           # previous_day1..N_MAX fetched
THRESHOLDS = (0.03, 0.05, 0.10, 0.15)
START_EQUITY = 374.0
BET_FRAC = 0.05
EVENT_CAP = 0.15
H = 3600
SQRT2 = math.sqrt(2.0)
HKO = {"lat": 22.3019, "lon": 114.1742, "name": "Hong Kong Observatory"}

_SSL = _ssl_context()


# --------------------------------------------------------------------------- io

def _get_json(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "edge-lab7"})
            with urllib.request.urlopen(req, timeout=60, context=_SSL) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 422) or i == tries - 1:
                raise
            time.sleep((5 if e.code == 429 else 1.5) * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _ts(s):
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


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ----------------------------------------------------------------- 1. events

TITLE_RE = re.compile(r"^Highest temperature in (.+?) on ([A-Z][a-z]+) (\d{1,2})\??\s*$")
MONTHS = {n: i for i, n in enumerate(calendar.month_name) if n}


def _compact_event(e):
    ms = []
    for m in e.get("markets") or []:
        fee = m.get("feeSchedule") or {}
        ms.append({
            "id": m.get("id"),
            "group": m.get("groupItemTitle") or "",
            "q": m.get("question") or "",
            "outcomes": m.get("outcomes"),
            "prices": m.get("outcomePrices"),
            "tokens": m.get("clobTokenIds"),
            "fee_rate": float(fee.get("rate") or 0) if m.get("feesEnabled") else 0.0,
            "fee_exp": float(fee.get("exponent") or 1),
            "closed": m.get("closedTime"),
            "created": m.get("createdAt"),
        })
    desc = ((e.get("markets") or [{}])[0].get("description")) or e.get("description") or ""
    return {"id": str(e.get("id")), "title": e.get("title") or "",
            "end": e.get("endDate"), "closed": e.get("closedTime"),
            "vol": float(e.get("volume") or 0), "desc": desc, "markets": ms}


def _events_day(day):
    path = os.path.join(CACHE_DIR, "events", day + ".json")
    got = _load(path)
    if got is not None:
        return got
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "tag_slug": "weather", "closed": "true", "limit": 100, "offset": offset,
            "end_date_min": day + "T00:00:00Z", "end_date_max": day + "T23:59:59Z"})
        page = _get_json(f"{GAMMA_EVENTS}?{q}")
        if not page:
            break
        offset += len(page)
        out.extend(_compact_event(e) for e in page
                   if (e.get("title") or "").startswith("Highest temperature"))
        if len(page) < 100:
            break
    if day < (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).date().isoformat():
        _save(path, out)
    return out


def fetch_events():
    d0, d1 = dt.date.fromisoformat(START), dt.date.fromisoformat(END)
    days = [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        per_day = list(ex.map(_events_day, days))
    seen, out = set(), []
    for evs in per_day:
        for e in evs:
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    return out


# ------------------------------------------------------------------ 2. parse

B_RANGE = re.compile(r"^(-?\d+)\s*(?:-|–|to)\s*(-?\d+)\s*°\s*([CF])$")
B_EXACT = re.compile(r"^(-?\d+)\s*°\s*([CF])$")
B_LOW = re.compile(r"^(-?\d+)\s*°\s*([CF])\s+or\s+(?:below|lower|less)$", re.I)
B_HIGH = re.compile(r"^(-?\d+)\s*°\s*([CF])\s+or\s+(?:higher|above|more)$", re.I)


def _bucket(label):
    """-> (lo, hi, unit) in whole reported degrees, None = open end."""
    s = label.strip().replace("º", "°").replace("−", "-").replace("˚", "°")
    s = re.sub(r"\s+", " ", s)
    m = B_RANGE.match(s)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3)
    m = B_EXACT.match(s)
    if m:
        v = int(m.group(1))
        return v, v, m.group(2)
    m = B_LOW.match(s)
    if m:
        return None, int(m.group(1)), m.group(2)
    m = B_HIGH.match(s)
    if m:
        return int(m.group(1)), None, m.group(2)
    return None


def _to_c(x, unit):
    return x if unit == "C" else (x - 32.0) * 5.0 / 9.0


def _station(desc):
    m = re.search(r"wunderground\.com/history/daily/\S*?/([A-Za-z0-9]{4})(?:[./\s]|$)", desc)
    if m:
        return m.group(1).upper()
    m = re.search(r"weather\.gov/wrh/timeseries\?site=([A-Za-z0-9]{4})", desc)
    if m:
        return m.group(1).upper()
    if "Hong Kong Observatory" in desc:
        return "HKO"
    return None


def parse_event(e):
    """-> (parsed, None) or (None, drop_reason)."""
    if e["vol"] < MIN_VOL:
        return None, "volume < floor"
    m = TITLE_RE.match(e["title"].strip())
    if not m:
        return None, "title"
    city, mon, day = m.group(1), m.group(2), int(m.group(3))
    if mon not in MONTHS:
        return None, "title month"
    end_ts = _ts(e["end"])
    if end_ts is None:
        return None, "no endDate"
    end_d = dt.datetime.fromtimestamp(end_ts, dt.timezone.utc).date()
    target = None
    for y in (end_d.year, end_d.year - 1, end_d.year + 1):
        try:
            cand = dt.date(y, MONTHS[mon], day)
        except ValueError:
            continue
        if abs((cand - end_d).days) <= 2:
            target = cand
            break
    if target is None:
        return None, "date mismatch"
    desc = e["desc"]
    if re.search(r"one decimal place|tenth", desc, re.I):
        return None, "0.1-degree resolution (HKO)"
    station = _station(desc)
    if not station or station == "HKO":
        return None, "no station"
    buckets = []
    for mk in e["markets"]:
        b = _bucket(mk["group"])
        if b is None:
            mq = re.search(r" be (?:between )?(.+?) on [A-Z][a-z]+ \d", mk["q"])
            b = _bucket(mq.group(1)) if mq else None
        if b is None:
            return None, "bucket label"
        try:
            outcomes = json.loads(mk["outcomes"] or "[]")
            prices = json.loads(mk["prices"] or "[]")
            tokens = json.loads(mk["tokens"] or "[]")
        except (TypeError, ValueError):
            return None, "market json"
        if len(tokens) != 2 or [o.lower() for o in outcomes] != ["yes", "no"]:
            return None, "not yes/no"
        if prices not in (["1", "0"], ["0", "1"]):
            return None, "unresolved/void bucket"
        lo, hi, unit = b
        buckets.append({"lo": lo, "hi": hi, "unit": unit, "token": tokens[0],
                        "won": prices[0] == "1", "fee": mk["fee_rate"],
                        "fee_exp": mk["fee_exp"], "label": mk["group"],
                        "closed": _ts(mk["closed"])})
    if len(buckets) < 3:
        return None, "too few buckets"
    units = {b["unit"] for b in buckets}
    if len(units) != 1:
        return None, "mixed units"
    unit = units.pop()
    buckets.sort(key=lambda b: (-1e9 if b["lo"] is None else b["lo"]))
    if buckets[0]["lo"] is not None or buckets[-1]["hi"] is not None:
        return None, "buckets not open-ended"
    for a, b in zip(buckets, buckets[1:]):
        if a["hi"] is None or b["lo"] is None or b["lo"] != a["hi"] + 1:
            return None, "buckets not contiguous"
    if sum(b["won"] for b in buckets) != 1:
        return None, "not exactly one winner"
    for b in buckets:                    # continuous °C bounds of the bucket
        b["a"] = None if b["lo"] is None else _to_c(b["lo"] - 0.5, unit)
        b["b"] = None if b["hi"] is None else _to_c(b["hi"] + 0.5, unit)
    closes = [b["closed"] for b in buckets if b["closed"]]
    return {"id": e["id"], "city": city, "station": station, "date": target.isoformat(),
            "unit": unit, "buckets": buckets, "vol": e["vol"],
            "close_ts": max(closes) if closes else _ts(e["closed"])}, None


# --------------------------------------------------------------- stations

def fetch_stations(icaos):
    path = os.path.join(CACHE_DIR, "stations.json")
    have = _load(path) or {}
    need = sorted(i for i in icaos if i not in have)
    for k in range(0, len(need), 40):
        chunk = need[k:k + 40]
        q = urllib.parse.urlencode({"ids": ",".join(chunk), "format": "json"})
        try:
            rows = _get_json(f"{STATIONS}?{q}")
        except Exception as ex:
            print(f"  WARNING station lookup failed for {chunk}: {ex}")
            continue
        for r in rows or []:
            have[r["icaoId"]] = {"lat": r["lat"], "lon": r["lon"],
                                 "elev": r.get("elev"), "name": r.get("site")}
    _save(path, have)
    return have


# --------------------------------------------------------------- forecasts

def fetch_forecast(station, info, d_lo, d_hi):
    """Previous-runs hourly 2m temperature for one station: {tz, t[], m->N->[]}"""
    path = os.path.join(CACHE_DIR, "fc", f"{station}_{d_lo}_{d_hi}.json")
    got = _load(path)
    if got is not None:
        return got
    hourly = ",".join(f"temperature_2m_previous_day{n}" for n in range(1, N_MAX + 1))
    q = urllib.parse.urlencode({
        "latitude": info["lat"], "longitude": info["lon"], "hourly": hourly,
        "models": ",".join(MODELS), "start_date": d_lo, "end_date": d_hi,
        "timezone": "auto", "timeformat": "unixtime"})
    r = _get_json(f"{PREV_RUNS}?{q}")
    h = r["hourly"]
    out = {"tz": r.get("timezone"), "t": h["time"], "m": {}}
    for mdl in MODELS:
        out["m"][mdl] = {str(n): h.get(f"temperature_2m_previous_day{n}_{mdl}")
                         for n in range(1, N_MAX + 1)}
    _save(path, out)
    time.sleep(0.5)                        # stay well inside Open-Meteo's free limits
    return out


def local_day_bounds(date_iso, tz):
    """UTC epoch of local 00:00 on the date and local 00:00 the next day."""
    z = ZoneInfo(tz)
    d = dt.date.fromisoformat(date_iso)
    s = dt.datetime(d.year, d.month, d.day, tzinfo=z)
    n = d + dt.timedelta(days=1)
    e = dt.datetime(n.year, n.month, n.day, tzinfo=z)
    return int(s.timestamp()), int(e.timestamp())


def forecast_max(fc, t0, t1, decision, model):
    """Daily max over local hours [t0, t1) using, per hour, the freshest run
    that was initialised AND published before `decision`."""
    ts = fc["t"]
    i0 = bisect.bisect_left(ts, t0)
    i1 = bisect.bisect_left(ts, t1)
    if i1 - i0 < 20:
        return None
    best = None
    series = fc["m"][model]
    for i in range(i0, i1):
        T = ts[i]
        n = max(1, int(math.ceil((T + PUB_DELAY_H * H - decision) / (24.0 * H))))
        if n > N_MAX:
            return None
        arr = series.get(str(n))
        v = arr[i] if arr else None
        if v is None:
            return None
        best = v if best is None else max(best, v)
    return best


# ------------------------------------------------------------------ prices

def _history(token, end_ts):
    path = os.path.join(CACHE_DIR, "px", f"{token[-24:]}_{int(end_ts)}.json")
    got = _load(path)
    if got is not None:
        return got
    q = urllib.parse.urlencode({"market": token, "startTs": int(end_ts - 60 * H),
                                "endTs": int(end_ts), "fidelity": 60})
    try:
        hist = _get_json(f"{CLOB}?{q}").get("history", [])
    except Exception:
        return None                            # not cached: retried next run
    pts = [[int(p["t"]), float(p["p"])] for p in hist]
    _save(path, pts)
    return pts


def _price_at(px, t):
    if not px or px[0][0] > t:
        return None
    i = bisect.bisect_right([p[0] for p in px], t) - 1
    return px[i][1] if t - px[i][0] <= STALE_S else None


# ------------------------------------------------------------- error model

def _phi(x):
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def bucket_probs(f, buckets, mu, sigma):
    out = []
    for b in buckets:
        pa = 0.0 if b["a"] is None else _phi((b["a"] - f - mu) / sigma)
        pb = 1.0 if b["b"] is None else _phi((b["b"] - f - mu) / sigma)
        out.append(max(pb - pa, 0.0))
    return out


def _nll(recs, mu, sigma):
    s = 0.0
    for f, a, b in recs:
        pa = 0.0 if a is None else _phi((a - f - mu) / sigma)
        pb = 1.0 if b is None else _phi((b - f - mu) / sigma)
        s -= math.log(max(pb - pa, 1e-6))
    return s / len(recs)


def fit(recs):
    """Interval-censored MLE of (mu, sigma): coarse grid then fine grid."""
    best = None
    for i in range(-12, 13):
        for j in range(0, 20):
            mu, sg = 0.5 * i, 0.4 + 0.3 * j
            v = _nll(recs, mu, sg)
            if best is None or v < best[0]:
                best = (v, mu, sg)
    _, m0, s0 = best
    for i in range(-10, 11):
        for j in range(-10, 11):
            mu, sg = m0 + 0.05 * i, s0 + 0.03 * j
            if sg < 0.2:
                continue
            v = _nll(recs, mu, sg)
            if v < best[0]:
                best = (v, mu, sg)
    return best[1], best[2], best[0]


# -------------------------------------------------------------- evaluation

def _cost(p, rate, exp=1.0):
    c = min(max(p + SPREAD, 0.001), 0.999)
    return c + rate * (c * (1.0 - c)) ** exp


def bets_for(recs, thr, side):
    """recs: decision records with q (model) and p (price) per bucket."""
    out = []
    for r in recs:
        for b, q, p in zip(r["buckets"], r["q"], r["p"]):
            if side in ("yes", "both"):
                c = _cost(p, b["fee"], b["fee_exp"])
                if q - c >= thr:
                    out.append({"r": r, "q": q, "c": c, "won": b["won"],
                                "ret": (1.0 / c - 1.0) if b["won"] else -1.0})
            if side in ("no", "both"):
                c = _cost(1.0 - p, b["fee"], b["fee_exp"])
                if (1.0 - q) - c >= thr:
                    won = not b["won"]
                    out.append({"r": r, "q": 1.0 - q, "c": c, "won": won,
                                "ret": (1.0 / c - 1.0) if won else -1.0})
    return out


def _mean_sd(xs):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return m, math.sqrt(v)


def summarize(bets):
    if not bets:
        return None
    n = len(bets)
    per_ev = defaultdict(list)
    for x in bets:
        per_ev[x["r"]["id"]].append(x["ret"])
    ev_rets = [sum(v) / len(v) for v in per_ev.values()]   # $1 per bet
    em, esd = _mean_sd(ev_rets)
    return {
        "n": n, "win": sum(x["won"] for x in bets) / n,
        "cost": sum(x["c"] for x in bets) / n, "q": sum(x["q"] for x in bets) / n,
        "ret": sum(x["ret"] for x in bets) / n,
        "n_ev": len(ev_rets), "ev_ret": em,
        "ev_t": em / (esd / math.sqrt(len(ev_rets))) if esd > 0 and len(ev_rets) > 1 else 0.0,
        "ev_pos": sum(1 for x in ev_rets if x > 0) / len(ev_rets),
    }


def _fmt(label, s):
    if s is None:
        return f"  {label:<26} (no bets)"
    return (f"  {label:<26} {s['n']:>5} {s['win']:>6.3f} {s['cost']:>6.3f} {s['q']:>6.3f} "
            f"{s['ret']:>+8.2%} | {s['n_ev']:>5} {s['ev_ret']:>+8.2%} {s['ev_t']:>+6.2f} "
            f"{s['ev_pos']:>6.1%}")


HEAD = (f"  {'':<26} {'bets':>5} {'win':>6} {'cost':>6} {'q_mdl':>6} {'net/bet':>8} | "
        f"{'events':>5} {'net/ev':>8} {'t(ev)':>6} {'ev>0':>6}")


def brier(recs):
    bm = bk = lm = lk = 0.0
    nb = 0
    for r in recs:
        tot = sum(r["p"]) or 1.0
        for b, q, p in zip(r["buckets"], r["q"], r["p"]):
            y = 1.0 if b["won"] else 0.0
            bm += (q - y) ** 2
            bk += (p - y) ** 2
            nb += 1
            if b["won"]:
                lm -= math.log(max(q, 1e-3))
                lk -= math.log(max(p / tot, 1e-3))
    n = len(recs)
    return bm / nb, bk / nb, lm / n, lk / n


def calibration(recs, key):
    bins = [0, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.01]
    agg = defaultdict(lambda: [0, 0.0, 0])
    for r in recs:
        for b, v in zip(r["buckets"], r[key]):
            i = bisect.bisect_right(bins, v) - 1
            a = agg[i]
            a[0] += 1
            a[1] += v
            a[2] += b["won"]
    return [(bins[i], bins[i + 1], a[0], a[1] / a[0], a[2] / a[0])
            for i, a in sorted(agg.items())]


def bankroll(recs, thr, side, qkey="q"):
    """Chronological: 5% of equity per bet, <=15% per event, settle at close."""
    bets = _bets(recs, thr, side, qkey)
    by_t = defaultdict(list)
    for x in bets:
        by_t[x["r"]["D"]].append(x)
    cash, book, month_eq = START_EQUITY, [], {}
    evs = sorted(by_t)

    def settle(t):
        nonlocal cash, book
        keep = []
        for pos in book:
            if pos["close"] <= t:
                cash += pos["shares"] if pos["won"] else 0.0
            else:
                keep.append(pos)
        book = keep

    def equity():
        return cash + sum(p["stake"] for p in book)

    for t in evs:
        settle(t)
        eq = equity()
        by_ev = defaultdict(list)
        for x in by_t[t]:
            by_ev[x["r"]["id"]].append(x)
        for eid, xs in by_ev.items():
            stake = min(BET_FRAC * eq, EVENT_CAP * eq / len(xs))
            for x in xs:
                s = min(stake, cash)
                if s < 0.5:
                    continue
                cash -= s
                book.append({"stake": s, "shares": s / x["c"], "won": x["won"],
                             "close": x["r"]["close_ts"] or (t + 2 * 86400)})
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        month_eq[(d.year, d.month)] = equity()
    settle(float("inf"))
    if month_eq:
        month_eq[max(month_eq)] = equity()
    keys = sorted(month_eq)
    rets, prev = [], START_EQUITY
    for k in keys:
        rets.append((k, month_eq[k] / prev - 1.0 if prev > 1e-9 else 0.0))
        prev = month_eq[k]
    peak, dd, v = START_EQUITY, 0.0, START_EQUITY
    for k in keys:
        v = month_eq[k]
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak)
    return (month_eq[keys[-1]] if keys else START_EQUITY), rets, dd, len(bets)


# ------------------------------------------------------------ calibration

def _winner(e):
    return [b for b in e["buckets"] if b["won"]][0]


def attach_forecasts(events, fcs):
    """fmax per lead/model for every parsed event — needs no prices, so the
    error model can be calibrated on the whole population, not the sample."""
    for e in events:
        e["fl"] = {}
        fc = fcs.get(e["station"])
        e["t0"] = e["t1"] = None
        if not fc or not fc.get("tz") or ZoneInfo is None:
            continue
        e["t0"], e["t1"] = local_day_bounds(e["date"], fc["tz"])
        for L in LEADS:
            D = e["t1"] - L * H
            fm = {m: forecast_max(fc, e["t0"], e["t1"], D, m) for m in MODELS}
            if any(v is None for v in fm.values()):
                continue
            fm["avg"] = sum(fm[m] for m in MODELS) / len(MODELS)
            e["fl"][L] = fm


def calibrate(train, loc0, min_n=8):
    """Global (mu, sigma) + per-station (mu, sigma) shrunk toward global
    by SHRINK pseudo-events. loc0(e) -> raw forecast location in °C."""
    rows = [(loc0(e), _winner(e)["a"], _winner(e)["b"]) for e in train]
    mu_g, sg_g, nll = fit(rows)
    by_st = defaultdict(list)
    for e, r in zip(train, rows):
        by_st[e["station"]].append(r)
    st = {}
    for s, rs in by_st.items():
        if len(rs) < min_n:
            continue
        mu_c, sg_c, _ = fit(rs)
        n = len(rs)
        st[s] = ((n * mu_c + SHRINK * mu_g) / (n + SHRINK),
                 (n * sg_c + SHRINK * sg_g) / (n + SHRINK))

    def params(e):
        mu, sg = st.get(e["station"], (mu_g, sg_g))
        return loc0(e) + mu, sg
    return params, {"mu": mu_g, "sigma": sg_g, "nll": nll, "n_st": len(st)}


def online_bias_fn(pop, L, mdl, K=30, k0=5.0):
    """Rolling per-station bias: mean(winning-bucket midpoint - forecast) over
    that station's last K events RESOLVED BEFORE the decision time, shrunk
    toward 0 by k0 pseudo-events. Open-ended winners are skipped."""
    by_st = defaultdict(list)
    for e in pop:
        if L not in e["fl"] or not e["close_ts"]:
            continue
        w = _winner(e)
        if w["a"] is None or w["b"] is None:
            continue
        by_st[e["station"]].append((e["close_ts"], 0.5 * (w["a"] + w["b"]) - e["fl"][L][mdl]))
    for v in by_st.values():
        v.sort()
    keys = {s: [x[0] for x in v] for s, v in by_st.items()}

    def bias(e):
        v = by_st.get(e["station"])
        if not v:
            return 0.0
        D = e["t1"] - L * H
        i = bisect.bisect_left(keys[e["station"]], D)          # close_ts < D only
        xs = [r for _, r in v[max(0, i - K):i]]
        return sum(xs) / (len(xs) + k0)
    return bias


def mean_nll(events, params):
    s = 0.0
    for e in events:
        loc, sg = params(e)
        w = _winner(e)
        pa = 0.0 if w["a"] is None else _phi((w["a"] - loc) / sg)
        pb = 1.0 if w["b"] is None else _phi((w["b"] - loc) / sg)
        s -= math.log(max(pb - pa, 1e-6))
    return s / max(len(events), 1)


def blend(q, p, w):
    """Logarithmic pool: prob ∝ price^w * model^(1-w)."""
    xs = [max(pi, 1e-3) ** w * max(qi, 1e-4) ** (1 - w) for qi, pi in zip(q, p)]
    t = sum(xs)
    return [x / t for x in xs]


def fit_blend(recs):
    best = None
    for i in range(0, 21):
        w = i / 20.0
        ll = 0.0
        for r in recs:
            qb = blend(r["q"], r["p"], w)
            ll -= sum(math.log(max(v, 1e-6)) for b, v in zip(r["buckets"], qb) if b["won"])
        ll /= len(recs)
        if best is None or ll < best[1]:
            best = (w, ll)
    return best


# -------------------------------------------------------------------- main

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    t_start = time.time()
    print(f"edge_lab7 — Polymarket daily max-temperature buckets vs NWP forecasts, "
          f"{START}..{END}\n  spread {SPREAD * 100:.1f}c + real taker fees, "
          f"forecast publication delay {PUB_DELAY_H:.0f}h, event vol >= ${MIN_VOL:,.0f}\n")

    raw = fetch_events()
    n_mk = sum(len(e["markets"]) for e in raw)
    print(f"[1] {len(raw)} resolved 'Highest temperature' events listed, {n_mk} bucket "
          f"markets ({time.time() - t_start:.0f}s)")
    parsed, drops = [], defaultdict(int)
    for e in raw:
        pe, why = parse_event(e)
        if pe:
            parsed.append(pe)
        else:
            drops[why] += 1
    print(f"    parsed {len(parsed)} events; dropped {sum(drops.values())} "
          f"({sum(drops.values()) / max(len(raw), 1):.1%}): "
          + ", ".join(f"{k}={v}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])))

    stations = fetch_stations({e["station"] for e in parsed})
    miss = sorted({e["station"] for e in parsed if e["station"] not in stations})
    if miss:
        print(f"    WARNING no coordinates for {miss} — dropped")
    parsed = [e for e in parsed if e["station"] in stations]

    by_st = defaultdict(list)
    for e in parsed:
        by_st[e["station"]].append(e["date"])
    print(f"[2] forecasts (Open-Meteo previous runs, {', '.join(MODELS)}) for "
          f"{len(by_st)} stations ...")
    fcs = {}
    for st, ds in sorted(by_st.items()):
        lo = (dt.date.fromisoformat(min(ds)) - dt.timedelta(days=2)).isoformat()
        hi = (dt.date.fromisoformat(max(ds)) + dt.timedelta(days=2)).isoformat()
        try:
            fcs[st] = fetch_forecast(st, stations[st], lo, hi)
        except Exception as ex:
            print(f"    WARNING forecast fetch failed for {st}: {ex}")
    attach_forecasts(parsed, fcs)
    parsed = [e for e in parsed if e["t1"]]
    print(f"    {len(fcs)} stations; {sum(1 for e in parsed if 24 in e['fl'])} events with a "
          f"D 00:00L forecast ({time.time() - t_start:.0f}s)")

    rnd = random.Random(SEED)
    sample = parsed if len(parsed) <= MAX_EVENTS else rnd.sample(parsed, MAX_EVENTS)
    sample.sort(key=lambda e: e["date"])
    print(f"[3] price histories: seeded random subsample of {len(sample)} events "
          f"({len(sample) / max(len(parsed), 1):.0%} of parsed), "
          f"{sum(len(e['buckets']) for e in sample)} bucket tokens, {THREADS} threads ...")
    jobs = [(b["token"], e["t1"]) for e in sample for b in e["buckets"]]
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        hists = list(ex.map(lambda j: _history(*j), jobs))
    k = 0
    for e in sample:
        for b in e["buckets"]:
            b["px"] = hists[k]
            k += 1
    print(f"    done ({sum(1 for h in hists if h is None)} failed fetches) "
          f"({time.time() - t_start:.0f}s)")

    recs = {L: [] for L in LEADS}
    skip = defaultdict(int)
    for e in sample:
        for L in LEADS:
            D = e["t1"] - L * H
            ps = [_price_at(b["px"], D) for b in e["buckets"]]
            if any(p is None for p in ps):
                skip[(L, "bucket unpriced/not listed yet")] += 1
                continue
            if e["close_ts"] and e["close_ts"] <= D:
                skip[(L, "closed")] += 1
                continue
            if L not in e["fl"]:
                skip[(L, "forecast missing")] += 1
                continue
            recs[L].append({"e": e, "id": e["id"], "station": e["station"],
                            "city": e["city"], "date": e["date"], "D": D,
                            "close_ts": e["close_ts"], "buckets": e["buckets"],
                            "p": ps, "psum": sum(ps)})
    for L in LEADS:
        ps = sorted(r["psum"] for r in recs[L])
        med = ps[len(ps) // 2] if ps else float("nan")
        print(f"    lead {LEAD_NAME[L]} (E-{L}h): {len(recs[L])} priced events (median "
              f"bucket-price sum {med:.3f}); skipped "
              + (", ".join(f"{w}={n}" for (l2, w), n in skip.items() if l2 == L) or "none"))

    dates = sorted(r["date"] for r in recs[24])
    split = dates[len(dates) // 2]
    cities = defaultdict(int)
    for r in recs[24]:
        cities[r["city"]] += 1
    print(f"\n    TRAIN = target dates < {split}, TEST = >= {split} (median of priced sample)")
    print(f"    {len(cities)} cities in the priced sample; most events: "
          + ", ".join(f"{c} {n}" for c, n in sorted(cities.items(), key=lambda kv: -kv[1])[:8]))

    # ---- error model: calibrated on the WHOLE parsed population of TRAIN dates
    print("\n[4] ERROR MODEL — interval-censored MLE on the winning bucket, fit on ALL "
          "parsed TRAIN-date events (no prices needed);\n    NLL = -log P(winning bucket), "
          "lower is better. 'static' = train (mu, sigma) global + per station (shrunk);\n"
          "    'online' = + rolling per-station bias from events resolved before the decision.")
    print(f"  {'lead':<11} {'model':<13} {'calib':<7} {'mu':>6} {'sigma':>6} "
          f"{'NLL train':>10} {'NLL test':>9} {'n train':>8}")
    chosen = {}
    for L in LEADS:
        pop = [e for e in parsed if L in e["fl"]]
        tr = [e for e in pop if e["date"] < split]
        te = [e for e in pop if e["date"] >= split]
        best = None
        for mdl in MODELS + ("avg",):
            params, info = calibrate(tr, lambda e, m=mdl, l=L: e["fl"][l][m])
            nte = mean_nll(te, params)
            print(f"  {LEAD_NAME[L]:<11} {mdl:<13} {'static':<7} {info['mu']:>+6.2f} "
                  f"{info['sigma']:>6.2f} {mean_nll(tr, params):>10.3f} {nte:>9.3f} {len(tr):>8}")
            key = mean_nll(tr, params)
            if best is None or key < best[0]:
                best = (key, mdl, params, "static", info)
        mdl = best[1]
        bias = online_bias_fn(pop, L, mdl)
        params_o, info_o = calibrate(tr, lambda e, m=mdl, l=L: e["fl"][l][m] + bias(e))
        ntr_o, nte_o = mean_nll(tr, params_o), mean_nll(te, params_o)
        print(f"  {LEAD_NAME[L]:<11} {mdl:<13} {'online':<7} {info_o['mu']:>+6.2f} "
              f"{info_o['sigma']:>6.2f} {ntr_o:>10.3f} {nte_o:>9.3f} {len(tr):>8}")
        if ntr_o < best[0]:
            best = (ntr_o, mdl, params_o, "online", info_o)
        chosen[L] = {"model": mdl, "params": best[2], "calib": best[3]}
        print(f"  -> {LEAD_NAME[L]}: {mdl} / {best[3]} (best TRAIN NLL)")
        for r in recs[L]:
            loc, sg = best[2](r["e"])
            r["q"] = bucket_probs(loc, r["buckets"], 0.0, sg)

    # ---- does the forecast add information beyond the price?
    print("\n[5] SCORING on the priced sample — Brier per bucket, LL = -log P(winner); "
          "'blend' = log-pool price^w * model^(1-w), w fit on TRAIN.\n"
          "    w = 1.0 means the forecast adds NOTHING beyond the market price.")
    print(f"  {'lead':<11} {'set':<6} {'events':>6} {'Brier mdl':>10} {'Brier mkt':>10} "
          f"{'Brier bl':>9} {'LL mdl':>7} {'LL mkt':>7} {'LL bl':>7}  w")
    for L in LEADS:
        tr = [r for r in recs[L] if r["date"] < split]
        te = [r for r in recs[L] if r["date"] >= split]
        w, _ = fit_blend(tr)
        chosen[L]["w"] = w
        for r in recs[L]:
            r["qb"] = blend(r["q"], r["p"], w)
        for name, rs in (("train", tr), ("test", te)):
            bm, bk, lm, lk = brier(rs)
            bb = sum((qb - (1.0 if b["won"] else 0.0)) ** 2 for r in rs
                     for b, qb in zip(r["buckets"], r["qb"])) / sum(len(r["buckets"]) for r in rs)
            lb = sum(-math.log(max(v, 1e-3)) for r in rs
                     for b, v in zip(r["buckets"], r["qb"]) if b["won"]) / len(rs)
            print(f"  {LEAD_NAME[L]:<11} {name:<6} {len(rs):>6} {bm:>10.4f} {bk:>10.4f} "
                  f"{bb:>9.4f} {lm:>7.3f} {lk:>7.3f} {lb:>7.3f}  {w:.2f}")

    print("\n  CALIBRATION on TEST, lead D 00:00L — prob bin: n, mean prob, win rate")
    te = [r for r in recs[24] if r["date"] >= split]
    cm, ck = calibration(te, "q"), calibration(te, "p")
    for a in cm:
        print(f"    model  [{a[0]:.2f},{a[1]:.2f}) n={a[2]:>5} p={a[3]:.3f} won={a[4]:.3f}")
    for b in ck:
        print(f"    market [{b[0]:.2f},{b[1]:.2f}) n={b[2]:>5} p={b[3]:.3f} won={b[4]:.3f}")

    # ---- strategy
    print("\n[6] STRATEGY — buy where prob - (price + spread + fee) >= threshold, hold to "
          "resolution, $1 per bet.\n    net/ev = mean over events of P&L/stake of that "
          "event's bets (buckets of one event are ONE correlated bet); t(ev) = its t-stat.")
    cand = []
    for L in LEADS:
        tr = [r for r in recs[L] if r["date"] < split]
        te = [r for r in recs[L] if r["date"] >= split]
        print(f"\n  === lead {LEAD_NAME[L]} (E-{L}h), {chosen[L]['model']} / "
              f"{chosen[L]['calib']}, blend w={chosen[L]['w']:.2f} ===")
        print(HEAD)
        for qkey in ("q", "qb"):
            if qkey == "qb" and chosen[L]["w"] >= 1.0:
                print("  (blend skipped: w = 1, the forecast adds nothing)")
                continue
            for side in ("yes", "no", "both"):
                for thr in THRESHOLDS:
                    s_tr = summarize(_bets(tr, thr, side, qkey))
                    s_te = summarize(_bets(te, thr, side, qkey))
                    tag = "model" if qkey == "q" else "blend"
                    print(_fmt(f"TRAIN {tag} {side:<4} {thr:.2f}", s_tr))
                    print(_fmt(f"TEST  {tag} {side:<4} {thr:.2f}", s_te))
                    if s_tr and s_tr["n_ev"] >= 100:
                        cand.append((s_tr["ev_ret"], L, qkey, side, thr))

    # ---- bankroll with the config chosen on TRAIN
    print(f"\n[7] BANKROLL — ${START_EQUITY:.0f} start, {BET_FRAC:.0%} of equity per bet, "
          f"<= {EVENT_CAP:.0%} per event, settled at market close; config = best "
          f"per-event return on TRAIN")
    cand.sort(reverse=True)
    pre = (float("nan"), 24, "q", "yes", 0.05)          # the hypothesis as stated
    for score, L, qkey, side, thr in [pre] + cand[:2]:
        tag = "model" if qkey == "q" else "blend"
        why = ("pre-registered: the hypothesis as stated" if score != score
               else f"best TRAIN net/ev {score:+.2%} among cells with >= 100 train events")
        print(f"  config: lead {LEAD_NAME[L]}, {tag}, side {side}, thr {thr:.2f} ({why})")
        for name, rs in (("TRAIN", [r for r in recs[L] if r["date"] < split]),
                         ("TEST", [r for r in recs[L] if r["date"] >= split])):
            final, rets, dd, nb = bankroll(rs, thr, side, qkey)
            print(f"    {name}: {nb} bets, ${START_EQUITY:.0f} -> ${final:.2f}, "
                  f"maxDD(month-end) {dd:.0%}; monthly: "
                  + ", ".join(f"{y}-{m:02d} {r:+.1%}" for (y, m), r in rets))
        te = [r for r in recs[L] if r["date"] >= split]
        byc = defaultdict(list)
        for x in _bets(te, thr, side, qkey):
            byc[x["r"]["city"]].append(x)
        top = sorted(byc.items(), key=lambda kv: -len(kv[1]))[:6]
        print("    TEST by city: " + "; ".join(
            f"{c} {len(xs)} bets {summarize(xs)['ret']:+.0%}" for c, xs in top))
    # ---- was the market ever beatable? (early, thin markets vs mature ones)
    print("\n[8] BY MONTH, lead D 00:00L — is the market's lead over the model stable? "
          "(TRAIN months are in-sample for the model's calibration)")
    print(f"  {'month':<8} {'set':<5} {'events':>6} {'cities':>6} {'LL mdl':>7} {'LL mkt':>7} "
          f"{'mdl both .10 net/ev':>20} {'n ev':>5}")
    bym = defaultdict(list)
    for r in recs[24]:
        bym[r["date"][:7]].append(r)
    for mo in sorted(bym):
        rs = bym[mo]
        _, _, lm, lk = brier(rs)
        s = summarize(_bets(rs, 0.10, "both", "q"))
        print(f"  {mo:<8} {'train' if mo < split[:7] else ('mixed' if mo == split[:7] else 'test'):<5} "
              f"{len(rs):>6} {len({r['city'] for r in rs}):>6} {lm:>7.3f} {lk:>7.3f} "
              f"{(s['ev_ret'] if s else 0):>+20.1%} {(s['n_ev'] if s else 0):>5}")
    print(f"\n({time.time() - t_start:.0f}s total)")


def _bets(recs, thr, side, qkey):
    if qkey == "q":
        return bets_for(recs, thr, side)
    alias = [dict(r, q=r[qkey]) for r in recs]
    return bets_for(alias, thr, side)


if __name__ == "__main__":
    main()
