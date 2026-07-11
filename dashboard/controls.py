"""controls.py — the command side of the command center.

* Bot override switches: PAUSE/resume, manual HALT / clear-halt, per-symbol
  do-not-trade list, per-symbol stop-loss / trailing-stop overrides. All are
  plain files under control/ (or the existing HALT convention) that agent.py
  reads each cycle — the dashboard never reaches into the bot's process.
* Per-symbol manual locks: taken while a dashboard order is in flight so the
  bot's deterministic force_sell skips that symbol for one cycle instead of
  double-selling (and vice versa the UI warns while the bot cycle runs).
* Manual-action journal: logs/manual_actions.jsonl — every operator action,
  separate from the bot's own decision logs ("did I do that or did the bot").
* PIN arming: salted PBKDF2 hash in dashboard/.auth.json; a verified PIN arms
  a short-lived token that every money/control endpoint requires.
"""

import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

CONTROL_DIR = os.path.join(ROOT, "control")
LOCK_DIR = os.path.join(CONTROL_DIR, "locks")
PAUSE_FILE = os.path.join(CONTROL_DIR, "PAUSE")
DNT_FILE = os.path.join(CONTROL_DIR, "do_not_trade.json")
OVERRIDES_FILE = os.path.join(CONTROL_DIR, "stop_overrides.json")
HALT_FILE = os.path.join(ROOT, "HALT")
JOURNAL = os.path.join(ROOT, "logs", "manual_actions.jsonl")
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".auth.json")

ARM_TTL_SECONDS = int(os.environ.get("DASHBOARD_ARM_TTL", "300"))
MAX_PIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 900
MANUAL_LOCK_TTL = 600          # a lock older than this is stale and ignored


def _now_iso():
    return datetime.now(ET).isoformat(timespec="seconds")


def _ensure_dirs():
    os.makedirs(CONTROL_DIR, exist_ok=True)
    os.makedirs(LOCK_DIR, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)


# ---------------------------------------------------------------- journal
def journal(action, params=None, result=None, ip=None, by="dashboard"):
    """Append one manual-action record. Never raises."""
    try:
        _ensure_dirs()
        rec = {"ts": _now_iso(), "action": action, "params": params or {},
               "result": result if result is not None else {}, "ip": ip, "by": by}
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    except Exception:
        return None


# ---------------------------------------------------------------- switches
def pause(reason="", ip=None):
    _ensure_dirs()
    with open(PAUSE_FILE, "w") as f:
        f.write(f"PAUSED {_now_iso()} via dashboard\n{reason}\n"
                "While this file exists agent.py runs protective exits and "
                "bookkeeping only — no model turns, no new entries.\n")
    journal("bot_pause", {"reason": reason}, {"ok": True}, ip)
    return {"ok": True, "paused": True}


def resume(ip=None):
    try:
        os.remove(PAUSE_FILE)
        ok = True
    except FileNotFoundError:
        ok = True
    except OSError as e:
        journal("bot_resume", {}, {"ok": False, "error": str(e)}, ip)
        return {"ok": False, "error": str(e)}
    journal("bot_resume", {}, {"ok": True}, ip)
    return {"ok": True, "paused": False}


def manual_halt(reason="", ip=None):
    """Write the same HALT file risk_guard uses. NOTE: a manual HALT does NOT
    flatten the book (unlike the drawdown kill-switch, which force-sells as it
    trips) — it stops the bot from trading at all, INCLUDING its stop-loss
    enforcement. The watchdog remains the only stop defense while halted."""
    with open(HALT_FILE, "w") as f:
        f.write(f"HALTED {_now_iso()} — MANUAL operator halt via dashboard.\n"
                f"Reason: {reason or '(none given)'}\n"
                "The bot will not trade while this file exists.\n"
                "Delete this file (dashboard: Clear halt) to resume.\n")
    journal("bot_halt", {"reason": reason}, {"ok": True}, ip)
    return {"ok": True, "halted": True}


def clear_halt(ip=None):
    try:
        os.remove(HALT_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        journal("bot_clear_halt", {}, {"ok": False, "error": str(e)}, ip)
        return {"ok": False, "error": str(e)}
    journal("bot_clear_halt", {}, {"ok": True}, ip)
    return {"ok": True, "halted": False}


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    _ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def set_do_not_trade(symbol, blocked, ip=None):
    d = _load_json(DNT_FILE, {"symbols": []})
    syms = {str(s).upper() for s in d.get("symbols", [])}
    symbol = symbol.upper()
    if blocked:
        syms.add(symbol)
    else:
        syms.discard(symbol)
    _save_json(DNT_FILE, {"symbols": sorted(syms), "updated": _now_iso()})
    journal("do_not_trade", {"symbol": symbol, "blocked": blocked}, {"ok": True}, ip)
    return {"ok": True, "symbols": sorted(syms)}


def set_stop_override(symbol, stop_price=None, stop_pct=None, trail_pct=None,
                      clear=False, ip=None):
    """Per-symbol stop override consumed by agent.check_stop_loss_alerts /
    check_trailing_stop_alerts / write_stop_snapshot. clear=True removes it."""
    d = _load_json(OVERRIDES_FILE, {})
    symbol = symbol.upper()
    if clear:
        d.pop(symbol, None)
    else:
        entry = {}
        if stop_price is not None:
            entry["stop_price"] = round(float(stop_price), 4)
        if stop_pct is not None:
            entry["stop_pct"] = round(float(stop_pct), 4)
        if trail_pct is not None:
            entry["trail_pct"] = round(float(trail_pct), 4)
        if not entry:
            return {"ok": False, "error": "nothing to set"}
        entry["set_at"] = _now_iso()
        d[symbol] = entry
    d["_updated"] = _now_iso()
    _save_json(OVERRIDES_FILE, d)
    journal("stop_override",
            {"symbol": symbol, "stop_price": stop_price, "stop_pct": stop_pct,
             "trail_pct": trail_pct, "clear": clear}, {"ok": True}, ip)
    return {"ok": True, "overrides": {k: v for k, v in d.items() if not k.startswith("_")}}


# ---------------------------------------------------------------- locks
def take_manual_lock(symbol):
    _ensure_dirs()
    p = os.path.join(LOCK_DIR, f"{symbol.upper()}.manual.lock")
    with open(p, "w") as f:
        f.write(_now_iso())
    return p


def release_manual_lock(symbol):
    try:
        os.remove(os.path.join(LOCK_DIR, f"{symbol.upper()}.manual.lock"))
    except OSError:
        pass


def manual_lock_active(symbol):
    p = os.path.join(LOCK_DIR, f"{symbol.upper()}.manual.lock")
    try:
        return (time.time() - os.path.getmtime(p)) < MANUAL_LOCK_TTL
    except OSError:
        return False


# ---------------------------------------------------------------- PIN auth
_PBKDF2_ITERS = 240_000


def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, _PBKDF2_ITERS)


def set_pin(pin):
    if not pin or len(str(pin)) < 4:
        raise ValueError("PIN must be at least 4 characters")
    salt = secrets.token_bytes(16)
    rec = {"salt": base64.b64encode(salt).decode(),
           "hash": base64.b64encode(_hash_pin(str(pin), salt)).decode(),
           "iters": _PBKDF2_ITERS, "set_at": _now_iso()}
    with open(AUTH_FILE, "w") as f:
        json.dump(rec, f)
    os.chmod(AUTH_FILE, 0o600)
    return True


def pin_configured():
    return os.path.exists(AUTH_FILE)


class Armory:
    """PIN verification + short-lived arm tokens, with brute-force lockout."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = {}          # token -> {ip, expires}
        self._fails = []           # timestamps of recent failures
        self._locked_until = 0

    def locked_out(self):
        return time.time() < self._locked_until

    def arm(self, pin, ip=None):
        with self._lock:
            if self.locked_out():
                wait = int(self._locked_until - time.time())
                journal("arm_attempt", {}, {"ok": False, "error": "locked_out"}, ip)
                return {"ok": False, "error": f"locked out — retry in {wait}s"}
            rec = _load_json(AUTH_FILE, None)
            if not rec:
                return {"ok": False, "error": "no PIN configured — run "
                        "`python3 dashboard/server.py --set-pin`"}
            salt = base64.b64decode(rec["salt"])
            good = hmac.compare_digest(
                base64.b64decode(rec["hash"]), _hash_pin(str(pin or ""), salt))
            now = time.time()
            if not good:
                self._fails = [t for t in self._fails if now - t < LOCKOUT_SECONDS]
                self._fails.append(now)
                if len(self._fails) >= MAX_PIN_ATTEMPTS:
                    self._locked_until = now + LOCKOUT_SECONDS
                    self._fails = []
                journal("arm_attempt", {}, {"ok": False, "error": "bad_pin"}, ip)
                return {"ok": False, "error": "wrong PIN"}
            self._fails = []
            token = secrets.token_urlsafe(24)
            expires = now + ARM_TTL_SECONDS
            self._tokens = {t: v for t, v in self._tokens.items() if v["expires"] > now}
            self._tokens[token] = {"ip": ip, "expires": expires}
            journal("armed", {}, {"ok": True, "ttl": ARM_TTL_SECONDS}, ip)
            return {"ok": True, "token": token,
                    "expires_at": datetime.fromtimestamp(expires, ET).isoformat(timespec="seconds"),
                    "ttl": ARM_TTL_SECONDS}

    def check(self, token, ip=None):
        with self._lock:
            v = self._tokens.get(token or "")
            if not v:
                return False
            if time.time() > v["expires"]:
                self._tokens.pop(token, None)
                return False
            if v["ip"] and ip and v["ip"] != ip:
                return False
            return True

    def disarm(self, token):
        with self._lock:
            self._tokens.pop(token or "", None)
        return True


armory = Armory()
