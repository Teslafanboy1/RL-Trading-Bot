"""watchdog.py — INDEPENDENT dead-man + stop-breach monitor (approved 2026-07-06).

Runs OUTSIDE the trading agent (launchd, every 5 minutes) so it survives the
agent dying — the exact hole that let MU fall 17% through a 10% stop while the
bot was down 2026-06-27..07-02 with nobody notified.

Checks (market hours only, Mon-Fri 9:25-16:05 ET):
  1. HEARTBEAT — logs/heartbeat older than STALE_MIN minutes => the bot is dead
     or wedged with the market open. Alert.
  2. STOP BREACH — for each position in logs/stops.json (written by the agent
     every cycle), fetch the latest price from Yahoo and alert if it is at/below
     the hard stop or trailing stop. Prices go stale only if BOTH the agent and
     this watchdog die — two independent processes.
  3. HALT — the risk_guard kill-switch file exists. Alert (once).

Alerts go to ALERT_WEBHOOK_URL (env), or the contents of .alert_webhook_url in
the repo root (so the launchd job needs no env setup). ntfy URLs get native
title/priority headers. Every alert type is rate-limited via
logs/watchdog_state.json so a standing condition pings hourly, not every 5 min.

Stdlib only. Never raises — a watchdog crash must not spam or wedge launchd.
"""

import os
import sys
import json
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")

STALE_MIN = 30
REALERT_MIN = 60
STATE = os.path.join(ROOT, "logs", "watchdog_state.json")


def log(msg):
    line = f"{datetime.now(ET).isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        with open(os.path.join(ROOT, "logs", "watchdog.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def webhook_url():
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if url:
        return url.strip()
    try:
        with open(os.path.join(ROOT, ".alert_webhook_url")) as f:
            return f.read().strip() or None
    except Exception:
        return None


def alert(key, subject, body):
    """Rate-limited alert: each `key` fires at most once per REALERT_MIN."""
    try:
        st = {}
        if os.path.exists(STATE):
            with open(STATE) as f:
                st = json.load(f)
        last = st.get(key)
        now = datetime.now(ET)
        if last:
            try:
                prev = datetime.fromisoformat(last)
                if (now - prev).total_seconds() < REALERT_MIN * 60:
                    return
            except ValueError:
                pass
        st[key] = now.isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f, indent=2)
    except Exception:
        pass
    log(f"ALERT {subject} :: {body}")
    url = webhook_url()
    if not url:
        return
    try:
        if "ntfy" in url:
            req = urllib.request.Request(
                url, data=body.encode("utf-8"), method="POST",
                headers={"Title": subject.encode("ascii", "ignore").decode(),
                         "Priority": "urgent", "Content-Type": "text/plain"})
        else:
            req = urllib.request.Request(
                url, method="POST",
                data=json.dumps({"title": subject, "message": body,
                                 "text": f"*{subject}*\n{body}"}).encode(),
                headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"webhook delivery failed: {e}")


def market_hours():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 25) <= hm <= (16 * 60 + 5)


def latest_price(symbol):
    try:
        import signals
        closes = signals._fetch_yahoo(symbol, "1d", "5d")
        return closes[-1] if closes else None
    except Exception:
        return None


def main():
    if not market_hours():
        return
    # 1. dead-man: heartbeat freshness
    hb = os.path.join(ROOT, "logs", "heartbeat")
    if not os.path.exists(hb):
        alert("heartbeat", "Trading bot: NO HEARTBEAT FILE",
              "The market is open and the bot has never written a heartbeat. "
              "It is probably not running — start it (bash run.sh).")
    else:
        age_min = (datetime.now(ET).timestamp() - os.path.getmtime(hb)) / 60
        if age_min > STALE_MIN:
            alert("heartbeat", "Trading bot: HEARTBEAT STALE — bot may be DOWN",
                  f"Last heartbeat {age_min:.0f} min ago with the market open. "
                  "Positions are unprotected (stops live in the bot). Check the "
                  "process (ps aux | grep agent.py) and restart it.")
    # 2. independent stop-breach check
    try:
        with open(os.path.join(ROOT, "logs", "stops.json")) as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    for sym, s in (snap.get("stops") or {}).items():
        px = latest_price(sym)
        if not px:
            continue
        stop = s.get("stop")
        tstop = s.get("trail_stop")
        worst = max(x for x in [stop, tstop] if x) if (stop or tstop) else None
        if worst and px <= worst:
            alert(f"stop_{sym}",
                  f"Trading bot: {sym} AT/THROUGH STOP ({px:.2f} <= {worst:.2f})",
                  f"{sym} last {px:.2f} vs stop {stop} / trail {tstop}. If the "
                  "bot is alive it should be selling now; if this repeats and no "
                  "sell appears in Robinhood, SELL MANUALLY.")
    # 3. kill-switch engaged
    if os.path.exists(os.path.join(ROOT, "HALT")):
        alert("halt", "Trading bot: HALTED by monthly kill-switch",
              "The HALT file exists — trading is stopped until you review and "
              "delete it. See the file contents for the trigger details.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"watchdog error: {e}")
