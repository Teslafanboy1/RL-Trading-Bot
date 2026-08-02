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

# A single equity reading this far outside the last known-good value is more
# likely a bad/hallucinated broker read than a real move (see 2026-08-02
# false halt: peak 544 -> 322 with no corresponding trade, deposit, or
# equity_curve entry anywhere — the account was flat cash at ~$273 the whole
# time). Ratios: > IMPLAUSIBLE_HIGH or < IMPLAUSIBLE_LOW vs last_equity.
IMPLAUSIBLE_HIGH_RATIO = 2.0
IMPLAUSIBLE_LOW_RATIO = 0.5
CORROBORATION_TOLERANCE = 0.10


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
      "ok"      — within budget (includes a held "suspect_reading" — see below)
      "halt"    — threshold just breached; HALT file written NOW (caller must
                  flatten if live and alert the operator)
      "halted"  — HALT file already present (no state change)
    A None/zero equity returns ("ok", "no_equity") — never halt on missing data
    (a failed read is unknown, not a drawdown — same doctrine as the broker
    reconciliation layer).

    PLAUSIBILITY GUARD (2026-08-03, added after a false halt): a reading more
    than IMPLAUSIBLE_HIGH_RATIO above or IMPLAUSIBLE_LOW_RATIO below the last
    accepted equity is held as "pending" for one cycle instead of being
    trusted immediately — it does NOT move the peak, last_equity, or drawdown,
    and cannot trip a halt. It is only accepted once a SECOND reading arrives
    within CORROBORATION_TOLERANCE of the pending value, confirming it's real
    (a genuine crash or a large undeclared deposit/withdrawal) rather than a
    one-off bad/hallucinated broker read. A legitimate detected deposit is
    unaffected: agent.sync_account_equity / process_manual_cash_flows call
    rebase_peak() BEFORE this runs each cycle, which pre-aligns last_equity to
    the new total so it never looks implausible in the first place. This
    trades up to one extra poll cycle of halt latency on a genuine crash for
    never again freezing the bot on a phantom reading (2026-08-02: peak
    jumped to 544 and a later 322 tripped a 40.8% halt while the real account
    sat flat at ~$273 the whole time — no trade, deposit, or equity_curve
    entry anywhere corroborates either number)."""
    try:
        if halted():
            return "halted", "HALT file present"
        if not equity or equity <= 0:
            return "ok", "no_equity"
        equity = float(equity)
        now = now or datetime.now(ET)
        month = now.strftime("%Y-%m")
        st = _load_state()
        if st.get("month") != month:
            st = {"month": month, "peak": equity, "last_equity": equity,
                  "drawdown": 0.0, "updated": now.isoformat(timespec="seconds")}
            _save_state(st)
            return "ok", f"dd 0.0% of month peak {equity:.2f} (month reset)"

        last_known = st.get("last_equity")
        if last_known and float(last_known) > 0:
            ratio = equity / float(last_known)
            if ratio > IMPLAUSIBLE_HIGH_RATIO or ratio < IMPLAUSIBLE_LOW_RATIO:
                pending = st.get("pending_reading") or {}
                corroborated = (
                    pending.get("last_known") == last_known
                    and abs(pending.get("equity", 0) - equity) / max(equity, 1)
                        <= CORROBORATION_TOLERANCE)
                if not corroborated:
                    st["pending_reading"] = {"equity": equity, "last_known": last_known,
                                             "first_seen": now.isoformat(timespec="seconds")}
                    _save_state(st)
                    return "ok", (f"suspect_reading {equity:.2f} vs last-known "
                                  f"{float(last_known):.2f} ({ratio:.2f}x) — held for "
                                  "corroboration, NOT applied to peak/drawdown")
        st.pop("pending_reading", None)
        peak = max(float(st.get("peak") or 0), equity)
        st["peak"] = peak
        st["last_equity"] = equity
        st["updated"] = now.isoformat(timespec="seconds")
        dd = (peak - equity) / peak if peak > 0 else 0.0
        st["drawdown"] = round(dd, 4)
        _save_state(st)
        if dd >= HALT_DD_PCT:
            with open(HALT_FILE, "w") as f:
                f.write(
                    f"HALTED {now.isoformat(timespec='seconds')}\n"
                    f"month peak equity {peak:.2f} -> current {equity:.2f} "
                    f"(drawdown {dd*100:.1f}% >= {HALT_DD_PCT*100:.0f}%)\n"
                    "The bot will not trade while this file exists.\n"
                    "Review what happened, then DELETE this file to resume.\n")
            return "halt", f"drawdown {dd*100:.1f}% from month peak {peak:.2f}"
        return "ok", f"dd {dd*100:.1f}% of month peak {peak:.2f}"
    except Exception as e:
        return "ok", f"guard_error:{e}"       # guard failure must not stop trading


def rebase_peak(delta, now=None):
    """Shift the tracked month-peak equity (and last_equity) by an external
    deposit/withdrawal delta so a withdrawal is never mistaken for a trading
    drawdown by check_halt (and a deposit never masks a real one). Mirrors the
    month_start_value rebase in agent.sync_account_equity — same delta, same
    call site, applied to the OTHER thing that tracks raw equity. Caused the
    2026-07-22 false-positive halt: a ~$100-120 withdrawal read as a 30.5%
    drawdown against the month peak because only month_start_value was
    deposit-aware, not risk_guard's peak.

    No-op if there's no state for the current month yet (check_halt will seed
    it fresh next call, already net of the withdrawal) or if HALT is already
    tripped (don't move the goalposts under an active halt under review)."""
    try:
        if not delta or halted():
            return
        now = now or datetime.now(ET)
        month = now.strftime("%Y-%m")
        st = _load_state()
        if st.get("month") != month or "peak" not in st:
            return
        st["peak"] = round(float(st["peak"]) + float(delta), 4)
        if "last_equity" in st:
            st["last_equity"] = round(float(st["last_equity"]) + float(delta), 4)
        peak = float(st["peak"])
        last_equity = float(st.get("last_equity") or peak)
        dd = (peak - last_equity) / peak if peak > 0 else 0.0
        st["drawdown"] = round(max(dd, 0.0), 4)
        st["updated"] = now.isoformat(timespec="seconds")
        _save_state(st)
    except Exception:
        pass       # guard failure must not stop trading


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
