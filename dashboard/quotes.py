"""quotes.py — free market data for the dashboard (Yahoo chart API, stdlib).

Reuses the bot's own signal layer (signals.py) for EMA ribbon states so the
dashboard shows EXACTLY what the bot trades on, and adds a light near-real-time
quote fetch + daily-close series for charts. Everything is cached with TTLs so
a 20-second frontend poll costs nothing extra.
"""

import os
import sys
import json
import time
import threading
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import signals  # noqa: E402  (bot's signal layer — plain EMA 8/13/21/55)

ET = ZoneInfo("America/New_York")

QUOTE_TTL = 20          # seconds — intraday quote cache
RIBBON_TTL = 300        # ribbon states move on bar closes; 5 min is plenty
DAILY_TTL = 3600        # daily-close series for perf charts
NEWS_TTL = 300

_lock = threading.Lock()
_cache = {}             # key -> (expires_epoch, value)


def _get_cached(key):
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _set_cached(key, value, ttl):
    with _lock:
        _cache[key] = (time.time() + ttl, value)


def market_open_now():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= 16 * 60


def _fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tradecommand/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=signals._ssl_context()) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def quote(symbol):
    """Near-real-time quote via Yahoo v8 chart meta: price, prev close, day change.
    Returns dict or None. Cached QUOTE_TTL."""
    symbol = symbol.upper()
    key = f"q:{symbol}"
    hit = _get_cached(key)
    if hit is not None:
        return hit
    try:
        # range=5d (not 1d): pre-market/overnight, today's session has only a
        # handful of real ticks so far — a 1d spark degenerates to 2-3 flat
        # points. 5d keeps the tail of the prior session(s) in the window so
        # the chart always has real shape; still sliced to the most recent 70
        # points below, so once today's session is underway this converges
        # back to "mostly today" same as before.
        d = _fetch_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                        f"{symbol}?range=5d&interval=5m&includePrePost=true")
        res = d["chart"]["result"][0]
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        # previousClose is always yesterday's close regardless of range;
        # chartPreviousClose is relative to the start of whatever `range` was
        # requested — since range=5d above, that field silently became "the
        # close from 5 days ago" and inflated the day-change % accordingly
        # (caught 2026-08-11: a fabricated +2.82%/+$10.05 "today"). previousClose
        # must come first now that range isn't always 1d.
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
        if price is None and closes:
            price = closes[-1]
        out = None
        if price:
            out = {"symbol": symbol, "price": round(float(price), 4),
                   "prev_close": round(float(prev), 4) if prev else None,
                   "change_pct": round((float(price) / float(prev) - 1) * 100, 2) if prev else None,
                   "spark": [round(float(c), 4) for c in closes[-70:]],
                   "asof": datetime.now(ET).isoformat(timespec="seconds")}
        _set_cached(key, out, QUOTE_TTL)
        return out
    except Exception:
        _set_cached(key, None, QUOTE_TTL)
        return None


def quotes(symbols):
    """{symbol: quote|None} for a list of symbols (deduped, sequential — the
    server polls this in a background thread so latency doesn't hit requests)."""
    out = {}
    for s in dict.fromkeys(x.upper() for x in symbols if x):
        out[s] = quote(s)
    return out


def ribbon(symbol):
    """Bot-identical EMA ribbon signal (signals.signal_for), cached RIBBON_TTL."""
    symbol = symbol.upper()
    key = f"r:{symbol}"
    hit = _get_cached(key)
    if hit is not None:
        return hit
    try:
        sig = signals.signal_for(symbol)
    except Exception as e:
        sig = {"symbol": symbol, "ok": False, "state": "ERROR", "note": str(e)[:80]}
    _set_cached(key, sig, RIBBON_TTL)
    return sig


def ribbons(symbols):
    return {s.upper(): ribbon(s) for s in dict.fromkeys(x.upper() for x in symbols if x)}


def daily_series(symbol, rng="6mo"):
    """Daily closes [{ts, close}] for charts/benchmarks. Cached DAILY_TTL."""
    return bar_series(symbol, "1d", rng)


def bar_series(symbol, interval="1d", rng="6mo"):
    """Timestamped closes [{ts, close}] at any Yahoo interval — the Analyzer
    chart's data (1d for the long view, 1h/30m to match the bot's ribbon
    chart). Intraday caches shorter than daily."""
    symbol = symbol.upper()
    key = f"bars:{symbol}:{interval}:{rng}"
    hit = _get_cached(key)
    if hit is not None:
        return hit
    out = []
    try:
        d = _fetch_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                        f"{symbol}?range={rng}&interval={interval}"
                        "&includePrePost=false")
        res = d["chart"]["result"][0]
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0].get("close") or []
        daily = interval == "1d"
        for t, c in zip(ts, closes):
            if c is not None:
                stamp = datetime.fromtimestamp(t, ET)
                out.append({"ts": stamp.strftime("%Y-%m-%d") if daily
                                  else stamp.isoformat(timespec="minutes"),
                            "close": round(float(c), 4)})
    except Exception:
        pass
    _set_cached(key, out, DAILY_TTL if interval == "1d" else RIBBON_TTL)
    return out


INDICES = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("DIA", "Dow"),
           ("IWM", "Russell 2k"), ("^VIX", "VIX"), ("^TNX", "10Y yield")]


def indices():
    out = []
    for sym, label in INDICES:
        q = quote(sym)
        if q:
            q = dict(q)
            q["label"] = label
            out.append(q)
        else:
            out.append({"symbol": sym, "label": label, "price": None})
    return out


# ---------------------------------------------------------------- news (RSS)
_FEEDS = [
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
]


def _parse_rss(xml_text, source):
    import xml.etree.ElementTree as ETree
    items = []
    try:
        root = ETree.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title:
                items.append({"source": source, "title": title[:220],
                              "link": link, "published": pub})
    except Exception:
        pass
    return items


def general_news(limit=30):
    key = "news:general"
    hit = _get_cached(key)
    if hit is not None:
        return hit
    items = []
    for source, url in _FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tradecommand/1.0"})
            with urllib.request.urlopen(req, timeout=12, context=signals._ssl_context()) as r:
                items += _parse_rss(r.read().decode("utf-8", "replace"), source)[:12]
        except Exception:
            continue
    items = items[:limit]
    _set_cached(key, items, NEWS_TTL)
    return items


def symbol_news(symbol, limit=10):
    symbol = symbol.upper()
    key = f"news:{symbol}"
    hit = _get_cached(key)
    if hit is not None:
        return hit
    items = []
    try:
        url = ("https://feeds.finance.yahoo.com/rss/2.0/headline?"
               f"s={symbol}&region=US&lang=en-US")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tradecommand/1.0"})
        with urllib.request.urlopen(req, timeout=12, context=signals._ssl_context()) as r:
            items = _parse_rss(r.read().decode("utf-8", "replace"), f"Yahoo ({symbol})")[:limit]
    except Exception:
        items = []
    _set_cached(key, items, NEWS_TTL)
    return items
