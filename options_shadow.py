"""options_shadow.py — Phase B shadow (paper) options mode.

The account ($65) is far below the options activation threshold, so NO real option
orders are placed (strategy.json options.enabled = false is a hard gate). Instead,
when the equity strategy produces a clean directional BUY signal, this records the
OPTIONS expression it would have taken — a v1 ATM long call, 30–45 DTE — using REAL
bid/ask quotes (so the bid/ask spread and IV crush, the two things that make
small-account options −EV, are captured), then closes it on the same exit discipline
as the equity book. Over time that builds a credible, spread-inclusive track record
to prove or disprove the options expression BEFORE a dollar is at risk.

Why P&L is reported as % return ON PREMIUM: it's independent of contract count and
account size, so the track record is meaningful now and still applies once the
account is large enough to size real contracts.

This module is PURE deterministic logic (selection/validation/sizing/exit/P&L +
the JSON log). The real-quote fetch is injected by agent.py (an isolated read-only
MCP call), so everything here is unit-testable with stubbed quotes. It is never the
trading path: read-only, isolated, and must not raise into the loop.
"""

import json
import os
from datetime import date


# ---------------------------------------------------------------- config
def shadow_cfg(strategy):
    return (strategy or {}).get("options", {}) or {}


def shadow_enabled(strategy):
    """Shadow runs whenever shadow_mode is on — independent of `enabled` (which
    gates REAL orders). So below the activation threshold we still paper-trade."""
    return bool(shadow_cfg(strategy).get("shadow_mode"))


# ---------------------------------------------------------------- dates
def dte(expiry_iso, today=None):
    """Calendar days to expiry. Returns None if expiry can't be parsed."""
    today = today or date.today()
    try:
        exp = date.fromisoformat(str(expiry_iso)[:10])
    except (ValueError, TypeError):
        return None
    return (exp - today).days


# ---------------------------------------------------------------- selection / validation
def validate_contract(contract, underlying_price, cfg, today=None):
    """Validate a (model-proposed) contract against the v1 selection rules.
    contract = {type, strike, expiry, bid, ask}. Returns (ok: bool, reason: str)."""
    sel = cfg.get("selection", {})
    try:
        ctype = str(contract.get("type", "")).lower()
        strike = float(contract.get("strike"))
        bid = float(contract.get("bid"))
        ask = float(contract.get("ask"))
        up = float(underlying_price)
    except (TypeError, ValueError):
        return False, "unparseable_contract"

    if ctype not in ("call", "put"):
        return False, f"bad_type:{ctype}"
    if up <= 0:
        return False, "bad_underlying_price"
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, "bad_quote"

    d = dte(contract.get("expiry"), today)
    if d is None:
        return False, "bad_expiry"
    if not (sel.get("target_dte_min", 30) <= d <= sel.get("target_dte_max", 45)):
        return False, f"dte_out_of_window:{d}"

    mid = (bid + ask) / 2.0
    if mid > 0 and (ask - bid) / mid > sel.get("max_bid_ask_pct_of_mid", 0.10):
        return False, f"illiquid_spread:{round((ask-bid)/mid, 3)}"

    # ATM band: nearest listed strike should be within ~5% of spot
    if abs(strike - up) / up > 0.05:
        return False, f"not_atm:{round(abs(strike-up)/up, 3)}"

    return True, "ok"


def entry_premium(contract):
    """You BUY at the ask — the realistic entry cost (captures the spread)."""
    return round(float(contract["ask"]), 4)


def risk_assessment(premium, account_value, cfg):
    """Per-contract max loss vs the per-trade risk budget. For a long option the max
    loss IS the premium. On a tiny account a single ATM contract dwarfs the 2% budget —
    that overshoot is recorded (oversized=True), which is itself the evidence that
    options aren't sizable here yet."""
    risk = cfg.get("risk", {})
    max_loss = round(premium * 100, 2)  # one contract = 100 shares
    budget = round(float(account_value or 0) * risk.get("max_loss_per_trade_pct", 0.02), 2)
    pct = round(max_loss / account_value, 4) if account_value else None
    return {"max_loss_usd": max_loss, "budget_usd": budget,
            "oversized": bool(budget and max_loss > budget), "pct_of_account": pct}


# ---------------------------------------------------------------- exit
def should_close(shadow, underlying_state, current_bid, cfg, today=None):
    """Exit discipline, mirroring the equity book. Returns (close: bool, reason: str).
    current_bid = the option's current BID (you sell at the bid)."""
    ex = cfg.get("shadow_exit", {})
    if ex.get("underlying_sell_state", True) and str(underlying_state).upper() == "SELL":
        return True, "underlying_sell"
    try:
        entry = float(shadow["entry_premium"])
        bid = float(current_bid)
    except (TypeError, ValueError, KeyError):
        bid = None
        entry = None
    if bid is not None and entry:
        stop = ex.get("premium_stop_pct", 0.50)
        if bid <= entry * (1 - stop):
            return True, "premium_stop"
    d = dte(shadow.get("expiry"), today)
    if d is not None and d < ex.get("min_dte_close", 7):
        return True, "dte_expiry"
    return False, ""


def close_pnl(shadow, exit_bid):
    """Realized hypothetical P&L for a closed shadow. % on premium is the headline
    metric (account-size independent); per-contract $ is also recorded."""
    entry = float(shadow["entry_premium"])
    exit_bid = round(float(exit_bid), 4)
    return {
        "exit_premium": exit_bid,
        "pnl_per_contract_usd": round((exit_bid - entry) * 100, 2),
        "pnl_pct_on_premium": round((exit_bid / entry - 1) * 100, 2) if entry else 0.0,
    }


# ---------------------------------------------------------------- log
def make_shadow_id(log):
    n = log.get("_next_id", 1)
    log["_next_id"] = n + 1
    return f"S{n:04d}"


def open_shadow_record(log, *, underlying, contract, underlying_price, account_value,
                       cfg, confidence=None, thesis="", now_iso=""):
    """Append a new open shadow position built from a validated contract."""
    prem = entry_premium(contract)
    ra = risk_assessment(prem, account_value, cfg)
    rec = {
        "id": make_shadow_id(log),
        "underlying": underlying,
        "type": str(contract.get("type", "")).lower(),
        "strike": float(contract["strike"]),
        "expiry": str(contract.get("expiry"))[:10],
        "structure": cfg.get("selection", {}).get("structure_v1", "long_call"),
        "entry_date": now_iso,
        "entry_underlying": round(float(underlying_price), 4),
        "entry_premium": prem,
        "contracts": 1,  # unit trade; P&L reported as % on premium
        "confidence": confidence,
        "thesis": thesis,
        "oversized_for_account": ra["oversized"],
        "max_loss_usd": ra["max_loss_usd"],
        "pct_of_account": ra["pct_of_account"],
        "status": "open",
    }
    log.setdefault("positions", []).append(rec)
    return rec


def close_shadow_record(log, shadow_id, exit_bid, reason, now_iso=""):
    """Mark an open shadow closed with realized P&L."""
    for p in log.get("positions", []):
        if p.get("id") == shadow_id and p.get("status") == "open":
            p.update(close_pnl(p, exit_bid))
            p["status"] = "closed"
            p["exit_date"] = now_iso
            p["exit_reason"] = reason
            return p
    return None


def open_shadows(log):
    return [p for p in log.get("positions", []) if p.get("status") == "open"]


def has_open_shadow(log, underlying):
    return any(p.get("underlying") == underlying and p.get("status") == "open"
               for p in log.get("positions", []))


def shadow_summary(log):
    closed = [p for p in log.get("positions", []) if p.get("status") == "closed"]
    wins = [p for p in closed if (p.get("pnl_per_contract_usd") or 0) > 0]
    pnl = round(sum(p.get("pnl_per_contract_usd") or 0 for p in closed), 2)
    avg_pct = round(sum(p.get("pnl_pct_on_premium") or 0 for p in closed) / len(closed), 2) if closed else 0.0
    return {
        "open": len(open_shadows(log)),
        "closed": len(closed),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "total_pnl_per_contract_usd": pnl,
        "avg_pct_on_premium": avg_pct,
    }


def load_shadow_log(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"positions": [], "_next_id": 1,
            "_note": "Phase B shadow (paper) options track record. No real money. "
                     "P&L is per 1 contract; % on premium is the size-independent metric."}


def save_shadow_log(path, log):
    log["summary"] = shadow_summary(log)
    with open(path, "w") as f:
        json.dump(log, f, indent=2)
