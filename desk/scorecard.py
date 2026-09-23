"""The Brain's scorecard: every call is written down BEFORE its outcome is
known, then graded against real prices. This is the learning loop's ground
truth — an engine's capital grows or shrinks on these numbers, not on how
convincing its reasoning sounded (operator plan, 2026-09-23).

Storage: append-only JSONL (one call per line, graded in place by rewrite).
Pure helpers + two small file functions; never raises into the caller.
"""
import json
import os
from datetime import date, datetime, timedelta

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "shadow", "desk_scorecard.jsonl")


def make_call(engine, symbol, direction, entry, *, stop_pct=0.10, target_pct=0.20,
              horizon_days=20, confidence=None, thesis="", today=None):
    """A pre-registered call. direction 'long' = profits if price rises.
    (An inverse ETF is a LONG position on the ETF; its thesis is bearish.)"""
    today = today or date.today()
    entry = float(entry)
    sign = 1 if direction == "long" else -1
    return {
        "id": f"{engine}-{symbol}-{today.isoformat()}",
        "engine": engine, "symbol": symbol, "direction": direction,
        "opened": today.isoformat(), "entry": round(entry, 4),
        "stop": round(entry * (1 - sign * stop_pct), 4),
        "target": round(entry * (1 + sign * target_pct), 4),
        "expires": (today + timedelta(days=int(horizon_days * 1.45))).isoformat(),
        "confidence": confidence, "thesis": thesis,
        "status": "open", "result": None, "ret": None, "closed": None,
    }


def grade(call, price, today=None):
    """Resolve an open call against the latest price. Returns the call (mutated
    copy). Stop is checked before target — on a gap through both, assume the
    worse outcome."""
    c = dict(call)
    if c.get("status") != "open" or not price:
        return c
    today = today or date.today()
    sign = 1 if c["direction"] == "long" else -1
    ret = sign * (float(price) / c["entry"] - 1)
    hit_stop = (price <= c["stop"]) if sign > 0 else (price >= c["stop"])
    hit_tgt = (price >= c["target"]) if sign > 0 else (price <= c["target"])
    expired = today.isoformat() >= c["expires"]
    if hit_stop or hit_tgt or expired:
        c.update(status="closed", ret=round(ret, 4), closed=today.isoformat(),
                 result="stop" if hit_stop else "target" if hit_tgt else "expired")
    return c


def summary(calls):
    """Per-engine: n closed, win rate, avg return, expectancy, calibration
    (Brier score, only over calls that carried a confidence)."""
    out = {}
    for c in calls:
        e = out.setdefault(c["engine"], {"open": 0, "closed": 0, "wins": 0,
                                         "sum_ret": 0.0, "brier_n": 0, "brier": 0.0})
        if c.get("status") != "closed":
            e["open"] += 1
            continue
        e["closed"] += 1
        win = (c.get("ret") or 0) > 0
        e["wins"] += win
        e["sum_ret"] += c.get("ret") or 0
        if c.get("confidence") is not None:
            p = float(c["confidence"]) / (100.0 if c["confidence"] > 1 else 1.0)
            e["brier"] += (p - (1.0 if win else 0.0)) ** 2
            e["brier_n"] += 1
    for e in out.values():
        n = e["closed"]
        e["win_rate"] = round(e["wins"] / n, 3) if n else None
        e["avg_ret"] = round(e["sum_ret"] / n, 4) if n else None
        e["brier"] = round(e["brier"] / e["brier_n"], 4) if e["brier_n"] else None
        del e["sum_ret"]
    return out


def load(path=DEFAULT_PATH):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []


def save(calls, path=DEFAULT_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            for c in calls:
                f.write(json.dumps(c) + "\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False
