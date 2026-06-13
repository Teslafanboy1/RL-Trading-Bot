"""
signals.py — the real signal layer (closes the "estimated EMA" gap).

Computes the 4-line ribbon from REAL closes and classifies the BUY / SELL /
NEUTRAL state, so the agent acts on a COMPUTED signal instead of eyeballing it.
Pure standard library — no pip dependencies, no database.

Lines — MUST match the TradingView chart the strategy is read from, which is
"TEMA 13 21 55" + "EMA 8" (NOT a plain-EMA ribbon):
  blue = EMA(8)   green = TEMA(13)   yellow = TEMA(21)   red = TEMA(55)
TEMA tracks price with far less lag than a plain EMA, so the red(55) line rolls
over and crosses on top of the ribbon days earlier in a run-up-then-fade. A
plain EMA(55) here reads BUY long after the chart reads SELL.

Signal (matches strategy.json):
  BUY  = red is the LOWEST line  (uptrend confirmed)
  SELL = red is the HIGHEST line (downtrend confirmed)
  Action fires on the TRANSITION into a state, not every bar it holds.
  agent.py additionally treats SELL *state* on a HELD position as a sell
  trigger, so a cross missed while the bot wasn't looking still exits.

Bar interval is set by SIGNAL_INTERVAL (default "1h" = the 1-month chart; use
"30m"/"15m" for the 1-week chart, "1d" for the daily chart). The 8/13/21/55
lengths are in BARS at that interval.

Data source — pluggable, tried in this order by fetch_closes():
  1. data/<SYMBOL>.csv  — local CSV with a 'Close' (or 'close') column, at the
     interval you want. Drop in a TradingView/broker export to match your chart
     exactly. Required when trading a sandbox account whose prices differ from the
     real market.
  2. Remote — Yahoo Finance free chart API for all intervals ("1h", "1d", etc.).
     Real-market data: correct for a REAL account, will NOT match a sandbox.

To use a different source, edit fetch_closes() — nothing else changes.

CLI:  python3 signals.py SPY MU CAT      # print computed signals
"""

import os
import csv
import ssl
import json
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))


def _ssl_context():
    """Verified TLS context. Some Python installs (notably python.org macOS
    builds) ship without a usable system CA bundle — ssl's default cafile is
    None — so every HTTPS fetch fails CERTIFICATE_VERIFY_FAILED and the signal
    layer goes dark. Prefer certifi's bundle when present; fall back to the
    system default otherwise. This keeps verification ON (unlike
    ALLOW_INSECURE_FETCH, which disables it)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


LENGTHS = {"blue": 8, "green": 13, "yellow": 21, "red": 55}
MIN_BARS = 60        # below this, refuse to emit a signal
WARMUP_OK = 165      # at/above this the 55-line is well-seeded (TEMA's triple
                     # EMA cascade carries seed bias ~3x longer than a plain EMA)

# Bar interval the EMA is computed on — MUST match the chart you read signals off.
#   1-month chart -> "1h" (default)   1-week chart -> "30m" or "15m"   daily -> "1d"
# The 8/13/21/55 lengths are in BARS, so on "1h" they are 8/13/21/55 HOURS.
_RANGE_BY_INTERVAL = {"1d": "1y", "1h": "3mo", "30m": "1mo",
                      "15m": "1mo", "5m": "5d", "1m": "5d"}
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "1h")
RANGE = os.environ.get("SIGNAL_RANGE") or _RANGE_BY_INTERVAL.get(INTERVAL, "3mo")


# ----------------------------------------------------------------- indicators
def ema(values, length):
    """Standard EMA, same length as input (seeded on the first value)."""
    if not values:
        return []
    k = 2 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def tema(values, length):
    """Triple EMA: 3*e1 - 3*e2 + e3 where e2=EMA(e1), e3=EMA(e2). Same length
    as input. Matches TradingView's TEMA indicator (much lower lag than EMA)."""
    e1 = ema(values, length)
    e2 = ema(e1, length)
    e3 = ema(e2, length)
    return [3 * a - 3 * b + c for a, b, c in zip(e1, e2, e3)]


# blue(8) is a plain EMA; the three slow lines are TEMA — same as the chart.
SMOOTHER = {"blue": ema, "green": tema, "yellow": tema, "red": tema}


def compute_lines(closes):
    """Latest value of each of the 4 ribbon lines."""
    return {name: SMOOTHER[name](closes, length)[-1]
            for name, length in LENGTHS.items()}


def classify_signal(lines):
    """red lowest = BUY, red highest = SELL, otherwise NEUTRAL (knotted)."""
    red = lines["red"]
    others = [lines["blue"], lines["green"], lines["yellow"]]
    if red < min(others):
        return "BUY"
    if red > max(others):
        return "SELL"
    return "NEUTRAL"


def detect_transition(prev_state, curr_state):
    """The actionable edge between two bars."""
    if curr_state == "BUY" and prev_state != "BUY":
        return "ENTER_LONG"
    if curr_state == "SELL" and prev_state != "SELL":
        return "EXIT"
    if curr_state == "BUY":
        return "HOLD"
    return "NO_ACTION"


# ----------------------------------------------------------------- data sources
def _read_local_csv(symbol):
    path = os.path.join(ROOT, "data", f"{symbol.upper()}.csv")
    if not os.path.exists(path):
        return None
    closes = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = next((k for k in row if k.lower() == "close"), None)
            if key is None:
                return None
            try:
                closes.append(float(row[key]))
            except (ValueError, TypeError):
                pass
    return closes or None


def _fetch_yahoo(symbol, interval, rng, timeout=20):
    """Closes at any interval from Yahoo's free chart API (stdlib JSON, no key)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval={interval}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 trading-agent/1.0"})
    data = None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        if os.environ.get("ALLOW_INSECURE_FETCH") == "1":
            try:
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                return None
        else:
            return None
    try:
        q = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    closes = [float(c) for c in q if c is not None]
    return closes or None


def fetch_closes(symbol):
    """Local CSV first (chart-aligned, any interval), then Yahoo Finance for all
    intervals. Returns (closes, source)."""
    local = _read_local_csv(symbol)
    if local:
        return local, "local_csv"
    remote = _fetch_yahoo(symbol, INTERVAL, RANGE)
    if remote:
        return remote, f"yahoo({INTERVAL})"
    return [], "none"


# backwards-compatible alias (older callers used fetch_daily_closes)
fetch_daily_closes = fetch_closes


# ----------------------------------------------------------------- public API
def signal_for(symbol):
    """Compute the live EMA signal for one symbol. Never fabricates: if data is
    missing/short it returns ok=False with a clear reason."""
    closes, source = fetch_daily_closes(symbol)
    n = len(closes)
    if n < MIN_BARS:
        return {"symbol": symbol, "ok": False, "state": "INSUFFICIENT_DATA",
                "bars": n, "source": source,
                "note": f"need >= {MIN_BARS} bars at {INTERVAL}, got {n}"}

    lines = compute_lines(closes)
    state = classify_signal(lines)
    prev = classify_signal(compute_lines(closes[:-1]))
    transition = detect_transition(prev, state)
    return {
        "symbol": symbol, "ok": True, "state": state, "transition": transition,
        "lines": {k: round(v, 4) for k, v in lines.items()},
        "last_close": round(closes[-1], 4), "bars": n, "source": source,
        "warmup_ok": n >= WARMUP_OK,
        "note": "" if n >= WARMUP_OK else f"only {n} bars — 55-line not fully seeded",
    }


def signals_block(symbols):
    """One-line-per-symbol summary for injection into the agent prompt."""
    rows = []
    for sym in symbols:
        s = signal_for(sym)
        if not s["ok"]:
            rows.append(f"- {sym}: {s['state']} ({s['note']}; source={s['source']})")
        else:
            rows.append(
                f"- {sym}: {s['state']} / {s['transition']} | last={s['last_close']} "
                f"| red(55)={s['lines']['red']} blue(8)={s['lines']['blue']} "
                f"green(13)={s['lines']['green']} yellow(21)={s['lines']['yellow']} "
                f"| {s['bars']} bars, source={s['source']}"
                + ("" if s["warmup_ok"] else f" [{s['note']}]"))
    return "\n".join(rows)


def signals_with_raw(symbols):
    """Compute signals once; return (raw_dict, formatted_block) to avoid double-fetching."""
    rows = []
    raw = {}
    for sym in symbols:
        s = signal_for(sym)
        raw[sym] = s
        if not s["ok"]:
            rows.append(f"- {sym}: {s['state']} ({s['note']}; source={s['source']})")
        else:
            rows.append(
                f"- {sym}: {s['state']} / {s['transition']} | last={s['last_close']} "
                f"| red(55)={s['lines']['red']} blue(8)={s['lines']['blue']} "
                f"green(13)={s['lines']['green']} yellow(21)={s['lines']['yellow']} "
                f"| {s['bars']} bars, source={s['source']}"
                + ("" if s["warmup_ok"] else f" [{s['note']}]"))
    return raw, "\n".join(rows)


if __name__ == "__main__":
    import sys
    syms = [a.upper() for a in sys.argv[1:]] or ["SPY"]
    print(signals_block(syms))
