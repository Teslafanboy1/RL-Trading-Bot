"""risk_guard.py — account-level kill-switch + heartbeat (approved 2026-07-06).

Three jobs, all deliberately tiny and dependency-free:

1. HEARTBEAT — `beat()` touches logs/heartbeat every cycle. The independent
   watchdog (watchdog.sh, launchd) alerts the operator when the heartbeat goes
   stale during market hours — the "bot died with the market open" hole that
   let MU fall 17% through a 10% stop (2026-06-27..07-02).

2. MONTHLY HALT — `check_halt(equity)` tracks the month's peak equity in
   logs/risk_state.json. If equity draws down >= HALT_DD_PCT (25%) from that
   peak, it writes the HALT file and returns ("halt", ...). While the HALT
   file exists the trading loop must refuse to trade (agent.py gates on
   `halted()`); removing the file is a deliberate manual operator act.

3. DEPOSIT REBASE — `detect_deposit(broker_total, tracked_total)`: when the
   broker's real account value differs from the tracked value by more than
   both $25 and 5%, the difference is treated as an external deposit/withdrawal
   and the caller rebases month_start_value by it (so deposits are never
   counted as trading P&L and the sizing denominator never goes stale — the
   $255-vs-$395 drift found in the 2026-07-06 audit).

Never raises: every public function swallows its own errors (a guard failure
must not break the trading loop) and returns conservative defaults.
"""

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")

HALT_DD_PCT = 0.25
HALT_FILE = os.path.join(ROOT, "HALT")
STATE_FILE = os.path.join(ROOT, "logs", "risk_state.json")
HEARTBEAT_FILE = os.path.join(ROOT, "logs", "heartbeat")

DEPOSIT_MIN_USD = 25.0
DEPOSIT_MIN_PCT = 0.05


# ------------------------------------------------------------------ heartbeat
def beat(note=""):
    """Touch the heartbeat file. Cheap; called every cycle including skips."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(f"{datetime.now(ET).isoformat(timespec='seconds')} {note}\n")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ halt state
def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(st, f, indent=2)
    except Exception:
        pass


def halted():
    """True while the HALT file exists — the loop must not trade."""
    return os.path.exists(HALT_FILE)


def check_halt(equity, now=None):
    """Update the month-peak watermark and evaluate the monthly-drawdown halt.

    equity — current REAL account value (broker total, not the trade log).
    Returns (status, detail) where status is one of:
      "ok"      — within budget
      "halt"    — threshold just breached; HALT file written NOW (caller must
                  flatten if live and alert the operator)
      "halted"  — HALT file already present (no state change)
    A None/zero equity returns ("ok", "no_equity") — never halt on missing data
    (a failed read is unknown, not a drawdown — same doctrine as the broker
    reconciliation layer)."""
    try:
        if halted():
            return "halted", "HALT file present"
        if not equity or equity <= 0:
            return "ok", "no_equity"
        now = now or datetime.now(ET)
        month = now.strftime("%Y-%m")
        st = _load_state()
        if st.get("month") != month:
            st = {"month": month, "peak": float(equity)}
        peak = max(float(st.get("peak") or 0), float(equity))
        st["peak"] = peak
        st["last_equity"] = float(equity)
        st["updated"] = now.isoformat(timespec="seconds")
        dd = (peak - float(equity)) / peak if peak > 0 else 0.0
        st["drawdown"] = round(dd, 4)
        _save_state(st)
        if dd >= HALT_DD_PCT:
            with open(HALT_FILE, "w") as f:
                f.write(
                    f"HALTED {now.isoformat(timespec='seconds')}\n"
                    f"month peak equity {peak:.2f} -> current {float(equity):.2f} "
                    f"(drawdown {dd*100:.1f}% >= {HALT_DD_PCT*100:.0f}%)\n"
                    "The bot will not trade while this file exists.\n"
                    "Review what happened, then DELETE this file to resume.\n")
            return "halt", f"drawdown {dd*100:.1f}% from month peak {peak:.2f}"
        return "ok", f"dd {dd*100:.1f}% of month peak {peak:.2f}"
    except Exception as e:
        return "ok", f"guard_error:{e}"       # guard failure must not stop trading


# ------------------------------------------------------------------ deposits
def detect_deposit(broker_total, tracked_total):
    """Difference between the broker's real total and the tracked value that
    is large enough to be an external deposit/withdrawal rather than P&L noise.
    Returns the signed delta to add to month_start_value, or 0.0."""
    try:
        b = float(broker_total or 0)
        t = float(tracked_total or 0)
        if b <= 0 or t <= 0:
            return 0.0
        delta = b - t
        if abs(delta) >= DEPOSIT_MIN_USD and abs(delta) / t >= DEPOSIT_MIN_PCT:
            return round(delta, 2)
        return 0.0
    except Exception:
        return 0.0
