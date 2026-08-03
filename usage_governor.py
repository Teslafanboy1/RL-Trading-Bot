"""usage_governor.py — Claude subscription 5-hour-window governor.

The problem this solves (observed live, 2026-07-06): the bot hit
`api_error_status: 429 — "You've hit your session limit · resets 10:50pm"` and
then kept firing a full `claude -p` call every 15 minutes for the rest of the
session, doing nothing but burning subprocesses. Worse, the calls that got
starved were the discretionary ones the operator cares most about (pre-market
research, the midweek review, postmortems) because they happen to be the
biggest and the least defended.

Claude subscription usage is metered in **rolling 5-hour session windows**: a
window opens on the first request after the previous one expired and closes
exactly 5 hours later. That is a schedulable resource, and this module treats
it as one. Three jobs:

1. WINDOW TRACKING — every `claude -p` call is recorded, so the module always
   knows when the current window opened and when it expires. `window_end()` is
   what lets the scheduler place heavy work in a window of its own instead of
   guessing from wall-clock arithmetic.

2. TIERED ADMISSION — every call declares a priority tier. As a window fills,
   the ceilings bite from the bottom up: paper/shadow work stops first, then
   learning (postmortems, strategy rewrites), then research, and protective
   exits are never blocked at all. This is the "won't collide" guarantee: by
   construction, discretionary work cannot eat the budget that a stop-loss
   sell needs.

3. COOLDOWN — on a 429 the reset time is parsed straight out of the CLI's
   error payload and every non-protective call is suppressed until then, so a
   limited session costs one failed call instead of twenty-six.

Never raises: every public function swallows its own errors and fails OPEN
(returns "allowed") on an internal fault. A governor bug must never be the
reason a stop-loss doesn't fire.
"""

import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
STATE_FILE = os.path.join(ROOT, "logs", "usage_state.json")

# ---------------------------------------------------------------- tiers
# Lower number = higher priority = harder to block.
TIER_PROTECTIVE = 0   # force_sell, the broker read backing a forced exit
TIER_EXECUTION = 1    # market-hours execution turn, routine broker read
TIER_RESEARCH = 2     # pre-market research, midweek review
TIER_LEARNING = 3     # postmortems, victories, skill_5 strategy rewrites
TIER_SHADOW = 4       # paper-options quotes (zero real money at stake)

TIER_NAMES = {
    TIER_PROTECTIVE: "protective",
    TIER_EXECUTION: "execution",
    TIER_RESEARCH: "research",
    TIER_LEARNING: "learning",
    TIER_SHADOW: "shadow",
}

WINDOW_HOURS = 5.0

# Fraction of the window budget each tier is allowed to consume. A tier is
# refused once EITHER the call count or the token count is past its ceiling,
# so a few enormous calls throttle the same way as many small ones.
DEFAULT_TIER_CEILING = {
    TIER_PROTECTIVE: 1.00,   # never blocked by budget (only by an active 429)
    TIER_EXECUTION:  1.00,
    TIER_RESEARCH:   0.75,
    TIER_LEARNING:   0.55,
    TIER_SHADOW:     0.40,
}

# Soft caps. These are deliberately NOT an attempt to model the plan's real
# quota (which the CLI does not expose) — they are a spend shape. What matters
# is the RATIO between tiers: whatever the true limit turns out to be, shadow
# work stops at 40% of the way through the window and execution keeps going.
DEFAULT_MAX_CALLS = 120
DEFAULT_MAX_TOKENS = 900_000


# ---------------------------------------------------------------- config
def _config(strategy=None):
    """Read the `usage` block from strategy.json, with env overrides. Falls back
    to module defaults for anything missing or malformed."""
    cfg = {}
    try:
        if strategy is None:
            with open(os.path.join(ROOT, "strategy", "strategy.json")) as f:
                strategy = json.load(f)
        cfg = dict((strategy or {}).get("usage") or {})
    except Exception:
        cfg = {}

    def _num(key, env, default, cast=float):
        raw = os.environ.get(env, cfg.get(key, default))
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return default

    ceilings = dict(DEFAULT_TIER_CEILING)
    try:
        for k, v in (cfg.get("tier_ceiling_pct") or {}).items():
            ceilings[int(k)] = float(v)
    except (TypeError, ValueError):
        pass

    return {
        "enabled": str(os.environ.get(
            "USAGE_GOVERNOR", cfg.get("enabled", True))).lower() not in ("0", "false", "no"),
        "window_hours": _num("window_hours", "USAGE_WINDOW_HOURS", WINDOW_HOURS),
        "max_calls": _num("max_calls_per_window", "USAGE_MAX_CALLS", DEFAULT_MAX_CALLS, int),
        "max_tokens": _num("max_tokens_per_window", "USAGE_MAX_TOKENS", DEFAULT_MAX_TOKENS, int),
        "tier_ceiling": ceilings,
        # scheduling knobs (read by agent.main(); kept here so the whole usage
        # policy lives in one place)
        "anchor_minutes_before_open": _num(
            "anchor_minutes_before_open", "USAGE_ANCHOR_MIN", 305, int),
        "research_minutes_before_open": _num(
            "research_minutes_before_open", "USAGE_RESEARCH_MIN", 35, int),
        "maintenance_hour_et": _num("maintenance_hour_et", "USAGE_MAINT_HOUR", 19, int),
        "maintenance_minute_et": _num("maintenance_minute_et", "USAGE_MAINT_MIN", 35, int),
        "max_analyses_per_drain": _num(
            "max_analyses_per_drain", "USAGE_MAX_ANALYSES", 4, int),
        "max_rewrites_per_drain": _num(
            "max_rewrites_per_drain", "USAGE_MAX_REWRITES", 3, int),
        "weekend_only_rewrites": bool(cfg.get("weekend_only_rewrites", False)),
    }


def config(strategy=None):
    """Public config accessor (used by agent.py's scheduler)."""
    try:
        return _config(strategy)
    except Exception:
        return {
            "enabled": True, "window_hours": WINDOW_HOURS,
            "max_calls": DEFAULT_MAX_CALLS, "max_tokens": DEFAULT_MAX_TOKENS,
            "tier_ceiling": dict(DEFAULT_TIER_CEILING),
            "anchor_minutes_before_open": 305, "research_minutes_before_open": 35,
            "maintenance_hour_et": 19, "maintenance_minute_et": 35,
            "max_analyses_per_drain": 4, "max_rewrites_per_drain": 3,
            "weekend_only_rewrites": False,
        }


# ---------------------------------------------------------------- state io
def _state_path():
    """Where the window state lives.

    `USAGE_STATE_FILE` overrides it so a test harness (or a second bot instance)
    can never write a real cooldown into the running bot's state — a stray
    429-shaped fixture would otherwise mute live non-protective calls for hours,
    which is exactly how the 2026-08-03 false-halt leak worked on risk_guard."""
    return os.environ.get("USAGE_STATE_FILE") or STATE_FILE


def _load():
    try:
        with open(_state_path()) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save(st):
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=ET) if d.tzinfo is None else d


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _blank_window(now, cfg):
    return {
        "window_start": _iso(now),
        "window_end": _iso(now + timedelta(hours=cfg["window_hours"])),
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "by_tier": {},
    }


def _roll(st, now, cfg):
    """Expire the current window if it is over. Returns the (possibly new) state.

    An expired window is archived to `history` (last 24 kept) purely for
    operator forensics — nothing reads it back."""
    end = _parse_dt(st.get("window_end"))
    if st.get("window_start") and end and now >= end:
        hist = st.get("history") or []
        hist.append({k: st.get(k) for k in
                     ("window_start", "window_end", "calls",
                      "input_tokens", "output_tokens", "by_tier")})
        st["history"] = hist[-24:]
        for k in ("window_start", "window_end", "calls", "input_tokens",
                  "output_tokens", "by_tier"):
            st.pop(k, None)
    return st


# ---------------------------------------------------------------- 429 parsing
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE)


def parse_reset(text, *, now=None):
    """Pull the reset moment out of a CLI limit error.

    The payload looks like:
        "You've hit your session limit · resets 10:50pm (Asia/Calcutta)"

    Note the timezone is the MACHINE's, not ET (this server runs on IST), so the
    parenthesised zone is honoured when present. Returns a tz-aware datetime, or
    None if nothing parseable is there.

    The result is clamped to (now, now + window]: a session reset is by
    definition at most one window away, so a parse that lands further out is a
    day-boundary artefact and gets pulled back."""
    if not text:
        return None
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour, minute, ampm, tzname = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        hour = int(hour)
        minute = int(minute or 0)
    except (TypeError, ValueError):
        return None
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    tz = None
    if tzname:
        try:
            tz = ZoneInfo(tzname.strip())
        except Exception:
            tz = None
    if tz is None:
        # No/unknown zone: the CLI prints the machine's local time.
        tz = datetime.now().astimezone().tzinfo

    now = now or datetime.now(ET)
    local_now = now.astimezone(tz)
    cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= local_now:
        cand += timedelta(days=1)
    if cand - local_now > timedelta(hours=WINDOW_HOURS):
        # e.g. "resets 10:50pm" read at 11:05pm — the reset already passed
        # today rather than being ~23h away.
        cand -= timedelta(days=1)
    if cand <= local_now:
        return None
    return cand.astimezone(ET)


def is_limit_error(text):
    """True when a run_model() return string is a subscription-limit refusal."""
    if not text:
        return False
    t = text.lower()
    return ("session limit" in t or "usage limit" in t
            or '"api_error_status":429' in t.replace(" ", "")
            or "429" in t and "limit" in t)


# ---------------------------------------------------------------- public api
def note_limit(text, *, now=None):
    """Record a 429. Sets the cooldown to the parsed reset time (or one window
    out when unparseable) and re-anchors the window end to match — a 429 is the
    only ground truth we ever get about where the real boundary sits, including
    usage the operator burned in their own Claude sessions.

    When the reset time is NOT parseable we deliberately do not blackout a full
    window — we'd risk sitting out an entire afternoon that had already freed up.
    Instead the cooldown backs off geometrically across consecutive unparseable
    limits (30m → 1h → 2h → 4h, capped at one window), which still collapses the
    every-15-minutes hammering while recovering fast if the limit lifted early.
    A parseable reset always wins and resets the backoff.

    Returns the cooldown datetime, or None if nothing was recorded."""
    UNPARSED_BASE_MIN = 30
    try:
        cfg = _config()
        now = now or datetime.now(ET)
        st = _load()
        reset = parse_reset(text, now=now)
        if reset is None:
            n = int(st.get("unparsed_limits") or 0) + 1
            st["unparsed_limits"] = n
            mins = min(UNPARSED_BASE_MIN * (2 ** (n - 1)), cfg["window_hours"] * 60)
            reset = now + timedelta(minutes=mins)
        else:
            st["unparsed_limits"] = 0
        st["cooldown_until"] = _iso(reset)
        st["cooldown_reason"] = (text or "")[:300]
        st["cooldown_set_at"] = _iso(now)
        st["limit_hits"] = int(st.get("limit_hits") or 0) + 1
        # The real window ends when the limit resets, whatever we thought.
        st["window_end"] = _iso(reset)
        if not st.get("window_start"):
            st["window_start"] = _iso(now)
        _save(st)
        return reset
    except Exception:
        return None


def cooldown_until(*, now=None):
    """Active cooldown expiry, or None when not cooling down."""
    try:
        now = now or datetime.now(ET)
        until = _parse_dt(_load().get("cooldown_until"))
        return until if until and now < until else None
    except Exception:
        return None


def clear_cooldown():
    try:
        st = _load()
        st.pop("cooldown_until", None)
        st.pop("cooldown_reason", None)
        _save(st)
        return True
    except Exception:
        return False


def window_end(*, now=None):
    """When the current 5h window expires, or None if no window is open.

    The scheduler uses this to start heavy off-hours work in a window of its
    own rather than trusting clock arithmetic."""
    try:
        cfg = _config()
        now = now or datetime.now(ET)
        st = _roll(_load(), now, cfg)
        return _parse_dt(st.get("window_end"))
    except Exception:
        return None


def status(*, now=None):
    """Snapshot for logging / the dashboard: window bounds, usage, cooldown."""
    try:
        cfg = _config()
        now = now or datetime.now(ET)
        st = _roll(_load(), now, cfg)
        end = _parse_dt(st.get("window_end"))
        calls = int(st.get("calls") or 0)
        tokens = int(st.get("input_tokens") or 0) + int(st.get("output_tokens") or 0)
        cd = _parse_dt(st.get("cooldown_until"))
        return {
            "enabled": cfg["enabled"],
            "window_start": st.get("window_start"),
            "window_end": st.get("window_end"),
            "minutes_left": (round((end - now).total_seconds() / 60) if end else None),
            "calls": calls,
            "max_calls": cfg["max_calls"],
            "tokens": tokens,
            "max_tokens": cfg["max_tokens"],
            "used_pct": round(max(calls / cfg["max_calls"] if cfg["max_calls"] else 0,
                                  tokens / cfg["max_tokens"] if cfg["max_tokens"] else 0), 3),
            "by_tier": st.get("by_tier") or {},
            "cooldown_until": st.get("cooldown_until") if (cd and now < cd) else None,
            "limit_hits": int(st.get("limit_hits") or 0),
        }
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def allow(tier, *, now=None):
    """(allowed: bool, reason: str) for a call at `tier`.

    Fails OPEN on any internal error — a governor fault must never be why a
    protective sell doesn't happen."""
    try:
        cfg = _config()
        if not cfg["enabled"]:
            return True, "governor_disabled"
        now = now or datetime.now(ET)
        st = _roll(_load(), now, cfg)

        cd = _parse_dt(st.get("cooldown_until"))
        if cd and now < cd:
            # Protective calls still get ONE attempt through: if the limit has
            # in fact lifted early, a pending stop-loss must not sit blocked.
            if tier <= TIER_PROTECTIVE:
                return True, "protective_probe_during_cooldown"
            mins = round((cd - now).total_seconds() / 60)
            return False, f"cooldown_{mins}m_until_{cd:%H:%M}_ET"

        ceiling = cfg["tier_ceiling"].get(tier, 1.0)
        if ceiling >= 1.0:
            return True, "no_ceiling"
        calls = int(st.get("calls") or 0)
        tokens = int(st.get("input_tokens") or 0) + int(st.get("output_tokens") or 0)
        call_frac = calls / cfg["max_calls"] if cfg["max_calls"] else 0.0
        tok_frac = tokens / cfg["max_tokens"] if cfg["max_tokens"] else 0.0
        used = max(call_frac, tok_frac)
        if used >= ceiling:
            return False, (f"tier_{TIER_NAMES.get(tier, tier)}_over_budget "
                           f"({used:.0%} used, ceiling {ceiling:.0%})")
        return True, f"ok_{used:.0%}_used"
    except Exception:
        return True, "governor_error_fail_open"


def record(tier, usage=None, *, now=None):
    """Book a completed call against the current window, opening one if needed."""
    try:
        cfg = _config()
        now = now or datetime.now(ET)
        st = _roll(_load(), now, cfg)
        if not st.get("window_start"):
            st.update(_blank_window(now, cfg))
        st["calls"] = int(st.get("calls") or 0) + 1
        u = usage or {}
        st["input_tokens"] = int(st.get("input_tokens") or 0) + int(u.get("input_tokens") or 0)
        st["output_tokens"] = int(st.get("output_tokens") or 0) + int(u.get("output_tokens") or 0)
        bt = dict(st.get("by_tier") or {})
        key = str(tier)
        bt[key] = int(bt.get(key) or 0) + 1
        st["by_tier"] = bt
        st["last_call_ts"] = _iso(now)
        _save(st)
        return True
    except Exception:
        return False
