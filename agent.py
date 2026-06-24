"""
agent.py — autonomous self-improving trading agent (Phase 1 + Phase 2).

PHASE 1: every 5 min during market hours, load the orchestrator + active skill +
strategy + trade log + postmortems, and let Claude read/trade via the Robinhood
Agentic MCP. Weekends = research.

PHASE 2 (the learning loop): after every trade CLOSES, agent.py automatically:
  1. logs the full outcome to trade_log.json,
  2. fires skill_4 (loss) or skill_4b (win) to write a postmortem/victory,
  3. recalibrates source weights in strategy.json (counts every trade; weights
     only shift after >= min_trades_before_weight_shift trades),
  4. updates monthly progress toward the 100% goal.

How agent.py "sees" a close: the model executes orders inside the MCP connector,
so each run ends with a machine-readable JSON footer (cash + positions +
actions_taken). Python parses it to detect opens/closes deterministically — the
same logic as the spec's positions_before/after diff, adapted to who executes.

Deps: schedule, plus the `claude` CLI (Claude Code) which supplies BOTH the model
and the authorized robinhood-cli connection — no API key, no connector token.
Flat files only, no database.
"""

import os
import re
import json
import time
import shutil
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import subprocess
import schedule

# ---------------------------------------------------------------- config
MODEL = os.environ.get("MODEL", "claude-opus-4-8")              # research / postmortem
CHECK_MODEL = os.environ.get("CHECK_MODEL", "claude-haiku-4-5-20251001")  # routine market checks
ROOT = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")     # market clock — correct regardless of machine TZ
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
ACCOUNT_NUMBER = os.environ.get("RH_ACCOUNT", "696283985")  # Agentic cash acct
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "15"))
# Force a thesis/news check at least every N hours even when EMA signals are flat.
# Catches news-driven thesis breaks before the lagging 55-bar EMA can reflect them.
NEWS_CHECK_HOURS = int(os.environ.get("NEWS_CHECK_HOURS", "4"))
# advisory = read-only (cannot place orders); live = may place/cancel orders
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "advisory").strip().lower()

SOURCES = ["news", "social", "fundamental", "macro", "rss"]
WATCHLIST = [s.strip().upper() for s in os.environ.get("WATCHLIST", "SPY").split(",") if s.strip()]

# robinhood-cli MCP tools (authorized via the `claude` CLI). Read tools are always
# allowed; order-placing tools only when EXECUTION_MODE=live.
_RH = "mcp__robinhood-cli__"
RH_READ = [_RH + t for t in ("get_accounts", "get_portfolio", "get_equity_positions",
           "get_equity_orders", "get_equity_quotes", "get_equity_tradability",
           "search", "get_watchlists", "get_watchlist_items", "review_equity_order")]
RH_WRITE = [_RH + "place_equity_order", _RH + "cancel_equity_order"]
# Option READ tools for the Phase B shadow pass (read-only — never place_option_order).
RH_OPTION_READ = [_RH + t for t in ("get_option_chains", "get_option_quotes",
                  "get_option_instruments", "get_option_positions")]


def notify_operator(subject, body):
    """Best-effort OUT-OF-BAND alert for the two conditions the operator must see
    even when not tailing the log: a hard forced exit (stop-loss / ribbon SELL)
    that did NOT complete, and claude -p being unavailable (session/usage limit)
    while a forced exit is pending.

    Two hard constraints:
      * must NOT depend on the claude CLI — that is exactly what is down in the
        session-limit case, so the alert is a direct stdlib HTTP POST, not a
        model call;
      * must NEVER raise — an alerting failure can't be allowed to break the
        trading loop, so every delivery path is wrapped and swallowed.

    Always prints to stdout (today's behavior). Additionally POSTs to
    ALERT_WEBHOOK_URL when set: JSON {"title","message","text"} — the "text"
    field is Slack/Discord-native, "message"/"title" suit ntfy and generic
    webhooks. Unconfigured => stdout only, no-op (no new required setup)."""
    print(f"[ALERT] {subject} :: {body}")
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        payload = json.dumps({
            "title": subject,
            "message": body,
            "text": f"*{subject}*\n{body}",  # Slack/Discord-compatible field
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        # Last resort: the stdout line above already fired; just note the miss.
        print(f"  [ALERT] webhook delivery to ALERT_WEBHOOK_URL failed: {e}")


try:
    import signals  # real EMA signal layer (optional; agent degrades if absent)
except Exception:
    signals = None

try:
    import options_shadow  # Phase B paper-options engine (optional; agent degrades if absent)
except Exception:
    options_shadow = None

try:
    import momentum_screen  # Phase B+ multi-timeframe momentum screener (optional)
except Exception:
    momentum_screen = None

DEFAULT_STOP_LOSS_PCT = 0.10  # hard 10% drawdown limit


# ---------------------------------------------------------------- time helpers
def now_iso():
    """ET wall-clock with UTC offset — the single timestamp format for all log
    writes. (The trade log previously mixed naive machine-local, ET and UTC
    stamps from different writers, which made elapsed-time math unreliable.)"""
    return datetime.now(ET).isoformat(timespec="seconds")


def _hours_since(ts):
    """Hours elapsed since an ISO timestamp, or None if missing/unparseable.
    Naive timestamps (written by older versions / manual edits) are assumed to
    be machine-local time. Never raises — a bad stamp must degrade to a model
    call, not crash the loop or silently force one every cycle."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now(ET) - dt).total_seconds() / 3600


# ---------------------------------------------------------------- file IO
def load_file(path, default=""):
    full = os.path.join(ROOT, path)
    if os.path.exists(full):
        with open(full) as f:
            return f.read()
    return default


def load_json(path, default):
    raw = load_file(path)
    try:
        return json.loads(raw) if raw else default
    except json.JSONDecodeError:
        return default


def save_json(path, obj):
    with open(os.path.join(ROOT, path), "w") as f:
        json.dump(obj, f, indent=2)


def load_trade_log():
    return load_json("trade_log.json", {
        "summary": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "month_start_value": 91, "current_value": 91,
                    "monthly_return_pct": 0, "monthly_goal": "100%", "on_track": True},
        "open_positions": [], "trades": [], "_state": {"last_positions": [], "next_id": 1},
    })


def save_trade_log(log):
    save_json("trade_log.json", log)


def load_strategy():
    return load_json("strategy/strategy.json", {})


def save_strategy(strategy):
    save_json("strategy/strategy.json", strategy)


def record_change_event(kind, target, version, *, trade_ids=None, severity=None,
                        summary=""):
    """Append ONE structured line to logs/change_events.jsonl — the timestamped
    audit trail of every strategy.json / skill-file version bump.

    This is the INPUT Phase 4's fast-path regression detector reads: it correlates
    trade losses against version bumps in a 48h window, and needs *whether a
    strategy.json OR skill-file bump happened, and when*. Before this, strategy
    bumps lived only as prose in version_history and skill bumps had no structured
    record at all (just file snapshots) — the detector would have had nothing
    machine-readable to query, and would launch with zero history. Recording it now
    means the month of history exists before Phase 4 is built. Phase 5's detection-
    latency metric also anchors on these timestamps.

    NOTE: this only CAPTURES the events. The detector that consumes them, the
    flagged-regression "major-changes log" it writes, and wiring this history into
    the postmortem prompt are all Phase 4 build work — deliberately not done here.

    Append-only, ONE file (not one-per-event), never raises (a logging failure must
    not break the trading or learning loop).

    kind     — "strategy" | "skill"
    target   — "strategy.json" or the skill name (e.g. "skill_2_execution")
    version  — the NEW version number after the bump
    severity — "ROUTINE" | "MAJOR" | None (from skill_5's SEVERITY tag when known)
    """
    try:
        d = os.path.join(ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        evt = {"timestamp": now_iso(), "kind": kind, "target": target,
               "version": version, "trade_ids": trade_ids or [],
               "severity": severity, "summary": (summary or "")[:300]}
        with open(os.path.join(d, "change_events.jsonl"), "a") as f:
            f.write(json.dumps(evt) + "\n")
    except Exception as e:
        print(f"  [change-log] failed to record {kind} change event: {e}")


def append_audit_log(name, title, body):
    """Append a human-readable audit entry to a single monthly rolling log
    (logs/{name}_YYYY-MM.md) instead of writing one .md file per event.

    The bot never reads these back (verified: only weekend_picks / midweek_review /
    the rewrite queue are re-read) — they are a pure human audit trail, and the
    old one-file-per-cycle scheme produced 135+ agent_run_*.md files that just
    piled up in research/. One file per month keeps the full audit while keeping
    the folder clean. Never raises."""
    try:
        d = os.path.join(ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        month = datetime.now(ET).strftime("%Y-%m")
        path = os.path.join(d, f"{name}_{month}.md")
        with open(path, "a") as f:
            f.write(f"\n\n{'='*78}\n## {title}\n{'='*78}\n\n{body}\n")
    except Exception as e:
        print(f"  [audit-log] failed to append {name} entry: {e}")


def snapshot_strategy(strategy, reason, trade_ids):
    """Version-bump + copy the prior strategy.json into strategy/history/."""
    old_v = strategy.get("version", 1)
    src = os.path.join(ROOT, "strategy", "strategy.json")
    dst = os.path.join(ROOT, "strategy", "history", f"strategy_v{old_v}.json")
    if os.path.exists(src):
        shutil.copy(src, dst)
    strategy["version"] = old_v + 1
    strategy["last_updated"] = date.today().isoformat()
    strategy.setdefault("version_history", []).append(
        {"version": strategy["version"], "date": date.today().isoformat(),
         # Full ISO timestamp (not just the date) so Phase 4's fast-path regression
         # detector can resolve the "3+ losses within 48h of a version bump" window.
         "timestamp": now_iso(),
         "change": reason, "trade_ids": trade_ids})
    # Structured event for Phase 4's detector. snapshot_strategy() is only used for
    # auto-applied minor changes (e.g. source-weight rebalance), so severity=ROUTINE.
    record_change_event("strategy", "strategy.json", strategy["version"],
                        trade_ids=trade_ids, severity="ROUTINE", summary=reason)


def strategy_for_prompt():
    """strategy.json as a string for READ-ONLY prompt consumers (the execution,
    research, and midweek cycles) with the heavy, audit-only `version_history`
    array dropped.

    Why: version_history is ~half of strategy.json (8.7KB of 18.7KB at v13) and is
    pure historical audit — the model never needs it to make a trading or research
    decision, yet the raw file rode along in EVERY cycle's system prompt, growing
    without bound (the skill_5 re-fire churn appended a ~500-word essay per NO-OP
    review). This trims that weight from every cycle while changing NOTHING the
    model acts on.

    The full file stays on disk: skill_5 (which must REWRITE strategy.json and
    therefore has to see every field) loads the complete file, NOT this view, and
    Phase 4's regression detector will read the complete version_history from disk
    too. This only changes what read-only consumers are shown, never what is
    stored. Falls back to the raw file text if the JSON can't be parsed."""
    s = load_strategy()
    if not s:
        return load_file("strategy/strategy.json")
    s = dict(s)
    vh = s.pop("version_history", [])
    if vh:
        latest = vh[-1]
        s["version_history_note"] = (
            f"[{len(vh)} version_history entries elided from this prompt to save "
            f"tokens; full audit history is on disk in strategy/strategy.json. "
            f"Latest: v{latest.get('version')} on {latest.get('date')}.]")
    return json.dumps(s, indent=2)


def load_postmortems():
    d = os.path.join(ROOT, "postmortems")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    return "\n\n---\n\n".join(load_file(f"postmortems/{f}") for f in files)


def load_latest_research_file(prefix):
    """Return the most recent research/PREFIX*.md file, or '' if none exists."""
    d = os.path.join(ROOT, "research")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d)
                   if f.startswith(prefix) and f.endswith(".md"))
    return load_file(f"research/{files[-1]}") if files else ""


# ---------------------------------------------------------------- claude helpers
_EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def run_model(system, user, *, mcp=False, web=False, timeout=600, model=None,
              read_only=False, allow_write=False, extra_tools=None):
    """One agent turn via the `claude` CLI. Returns (text, usage_dict).

    usage_dict keys: input_tokens, output_tokens, cost_usd.
    Tools are restricted by --allowedTools: read-only by default; order-placing
    tools only when EXECUTION_MODE=live. The big prompt goes via stdin.

    read_only=True forces RH read tools only (no order placement) regardless of
    EXECUTION_MODE — used for the independent broker-state verification read.
    allow_write=True arms order placement (subject to EXECUTION_MODE=live) for a
    dedicated forced-sell call even outside the main execution turn."""
    tools = []
    if mcp:
        tools += RH_READ
        if EXECUTION_MODE == "live" and (allow_write or not read_only):
            tools += RH_WRITE
    if web:
        tools += ["WebSearch", "WebFetch"]
    if extra_tools:
        tools += list(extra_tools)
    # Skill/Task/Agent are explicitly disallowed: user-level skills (e.g. the
    # trading-agent-* skills) leak into the -p context, and the model has tried
    # to "launch" one instead of doing the work inline — the invocation is
    # denied, and the turn ends mid-flow without the JSON state footer.
    cmd = [CLAUDE_BIN, "-p", "--model", model or MODEL, "--output-format", "json",
           "--permission-mode", "default",
           "--disallowedTools", "Skill", "Task", "Agent"]
    if tools:
        cmd += ["--allowedTools", *tools]
    prompt = f"{system}\n\n=== TASK ===\n{user}"
    try:
        res = subprocess.run(cmd, input=prompt, capture_output=True,
                             text=True, timeout=timeout)
    except FileNotFoundError:
        return "(error: `claude` CLI not found — set CLAUDE_BIN or install Claude Code)", _EMPTY_USAGE
    except subprocess.TimeoutExpired:
        return "(error: claude -p timed out)", _EMPTY_USAGE
    if res.returncode != 0:
        # With --output-format json the CLI reports failures (usage cap, auth,
        # model errors) on STDOUT; stderr is often empty. Capture both so the
        # run record shows WHY it failed instead of a blank "rc=1:".
        detail = (res.stderr or "").strip() or (res.stdout or "").strip()
        return f"(claude -p error rc={res.returncode}: {detail[:500]})", _EMPTY_USAGE
    raw = (res.stdout or "").strip()
    try:
        parsed = json.loads(raw)
        text = parsed.get("result", raw)
        u = parsed.get("usage", {})
        usage = {
            "input_tokens":  u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cost_usd":      parsed.get("total_cost_usd", 0.0),
        }
    except (json.JSONDecodeError, AttributeError):
        text, usage = raw, _EMPTY_USAGE
    return text, usage


def extract_last_json_block(text):
    """Return the last parseable {...} (preferring fenced ```json blocks)."""
    for b in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)):
        try:
            return json.loads(b)
        except json.JSONDecodeError:
            continue
    for b in reversed(re.findall(r"(\{.*?\})", text, re.DOTALL)):
        try:
            return json.loads(b)
        except json.JSONDecodeError:
            continue
    return None


def strip_json_blocks(text):
    return re.sub(r"```json\s*\{.*?\}\s*```", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------- phase routing
def is_market_open():
    """True only 9:30-16:00 US/Eastern on a weekday — DST-correct and independent
    of the machine's local timezone (e.g. Central)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def active_skill():
    now = datetime.now(ET)
    wd = now.weekday()  # 0=Mon … 6=Sun
    if is_market_open():
        # Wednesday at noon: fire midweek review once per day
        if wd == 2 and now.hour >= 12:
            today = now.strftime("%Y-%m-%d")
            if not os.path.exists(os.path.join(ROOT, "research", f"midweek_review_{today}.md")):
                return load_file("skills/skill_3_midweek.md"), "midweek_validation"
        return load_file("skills/skill_2_execution.md"), "market_hours_check"
    # Off-hours Wednesday fallback (e.g. script started after close) — skip if already done today
    if wd == 2:
        today = now.strftime("%Y-%m-%d")
        if not os.path.exists(os.path.join(ROOT, "research", f"midweek_review_{today}.md")):
            return load_file("skills/skill_3_midweek.md"), "midweek_validation"
    return load_file("skills/skill_1_research.md"), "research_and_prep"


def weekend_pick_symbols():
    """Symbols from the latest weekend_picks_*.md (the pending buy candidates)."""
    d = os.path.join(ROOT, "research")
    if not os.path.isdir(d):
        return []
    picks = sorted(f for f in os.listdir(d) if f.startswith("weekend_picks_"))
    if not picks:
        return []
    text = load_file(f"research/{picks[-1]}")
    return re.findall(r"###\s+#\d+\s+—\s+(\w+)\b", text)


def weekend_pick_confidences():
    """{SYMBOL: confidence} parsed from the latest weekend_picks_*.md headings
    '### #N — SYMBOL (...) | Confidence: XX/100'. Lets the deterministic risk
    block size each pending pick without re-deriving its confidence."""
    d = os.path.join(ROOT, "research")
    if not os.path.isdir(d):
        return {}
    picks = sorted(f for f in os.listdir(d) if f.startswith("weekend_picks_"))
    if not picks:
        return {}
    text = load_file(f"research/{picks[-1]}")
    out = {}
    for m in re.finditer(
            r"###\s+#\d+\s+—\s+(\w+)\b.*?[Cc]onfidence[:\s]*?(\d{1,3})\s*/\s*100", text):
        out[m.group(1).upper()] = int(m.group(2))
    return out


def watchlist_symbols(log):
    """SPY + open positions + WATCHLIST env + latest weekend picks, de-duped."""
    syms = (["SPY"] + [p["symbol"] for p in log.get("open_positions", [])]
            + WATCHLIST + weekend_pick_symbols())
    seen, out = set(), []
    for s in syms:
        if s.upper() not in seen:
            seen.add(s.upper()); out.append(s.upper())
    return out


def computed_signals(symbols):
    """Real EMA signals for the prompt — never an estimate."""
    if not signals:
        return ("(signals.py unavailable — EMA signals NOT computed this cycle; "
                "do not trade on an unverified signal.)")
    try:
        return signals.signals_block(symbols)
    except Exception as e:
        return f"(signal computation error: {e} — treat EMA signals as unknown this cycle.)"


# ---------------------------------------------------------------- risk model (Phase A)
# Deterministic, quant-firm risk sizing. The risk-critical math (how big, how
# correlated, how tight the stop) lives in Python — NOT in a prompt — so the model
# can't oversize the way it did on SOXL (3x, full band -> -11.84%) and AMD (parabolic
# chase -> -4.99%); those two = 73% of all loss dollars. All read strategy/strategy.json
# (position_sizing + factor_exposure_limits, added v18) and degrade to safe no-ops if
# those blocks are absent (older strategy files / minimal test fixtures).
def _ps(strategy):
    return strategy.get("position_sizing", {}) or {}


def leverage_factor(symbol, strategy):
    """Daily-reset leverage multiple for a symbol (1.0 if not a leveraged ETF)."""
    lf = _ps(strategy).get("leverage_factors", {})
    try:
        return float(lf.get((symbol or "").upper(), 1) or 1)
    except (TypeError, ValueError):
        return 1.0


def effective_stop_pct(symbol, strategy):
    """Stop-loss distance for a symbol: the base stop, tightened for leveraged
    ETFs to base/leverage (e.g. ~3.3% on a 3x). TIGHTER only, never looser — a
    pure safety improvement that gates the >=2x daily-reset whipsaw (LR002)."""
    rm = strategy.get("risk_management", {})
    base = rm.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    if not rm.get("leverage_adjusted_stop"):
        return base
    lf = leverage_factor(symbol, strategy)
    return round(base / lf, 4) if lf > 1 else base


def _band_ceiling(confidence, strategy):
    """Per-confidence max position fraction (a CEILING, not the target)."""
    bc = _ps(strategy).get("band_ceilings", {})
    if confidence is None:
        return 0.0
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if c >= 90:
        return float(bc.get("90_to_100", 0.30))
    if c >= 75:
        return float(bc.get("75_to_89", 0.20))
    if c >= 60:
        return float(bc.get("60_to_74", 0.15))
    return float(bc.get("below_60", 0.0))


def position_size_pct(confidence, symbol, strategy):
    """Risk-based target size as a fraction of TOTAL equity.

        size = min(band_ceiling, risk_per_trade / base_stop) / leverage_factor

    capped at max_single_position; 0.0 below the 60 confidence floor. The
    confidence band is a ceiling; the 2%-risk / 10%-stop budget is the binding
    global cap (so the old 30% top band is now effectively 20% for a normal name);
    leveraged names are divided down so a 3x ETF gets ~1/3 the dollars. Returns
    the band ceiling unchanged if position_sizing is absent (old strategy)."""
    ps = _ps(strategy)
    band = _band_ceiling(confidence, strategy)
    if band <= 0:
        return 0.0
    if not ps:
        return band
    risk = ps.get("risk_per_trade_pct", 0.02)
    base_stop = strategy.get("risk_management", {}).get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    risk_cap = (risk / base_stop) if base_stop else band
    lf = leverage_factor(symbol, strategy)
    raw = min(band, risk_cap) / (lf if lf > 0 else 1)
    return round(min(raw, ps.get("max_single_position", 0.30)), 4)


def sector_for(symbol, strategy):
    """Sector bucket for a symbol from factor_exposure_limits, or 'other'."""
    buckets = strategy.get("factor_exposure_limits", {}).get("sector_buckets", {})
    u = (symbol or "").upper()
    for name, syms in buckets.items():
        if u in syms:
            return name
    return "other"


def sector_exposure(positions, strategy, total_equity=None):
    """Current per-sector weight, leveraged ETFs counted at their leverage (a 3x
    semis ETF eats 3x its dollar weight of the semis cap). positions =
    [{symbol, market_value}]. Returns {sector: {pct, names, count}} and
    '_total_equity'. The guard against the 'whole book is one bet' failure
    (SOXL+AMD+AMAT were all semis).

    total_equity is the denominator — the FULL account value INCLUDING cash, so a
    sector cap means '% of the whole account'. If None, falls back to the sum of
    position market values (i.e. % of invested capital), which is only meaningful
    when fully invested — pass the account total whenever it's known, otherwise two
    positions always sum to ~100% and every sector falsely reads over-cap."""
    fl = strategy.get("factor_exposure_limits", {})
    count_lev = fl.get("leveraged_etf_counts_at_leverage", True)
    vals = []
    for p in positions:
        sym = p.get("symbol")
        mv = float(p.get("market_value") or 0)
        if sym and mv > 0:
            vals.append((sym, mv))
    invested = sum(mv for _, mv in vals)
    denom = total_equity if (total_equity and total_equity > 0) else invested
    out = {}
    for sym, mv in vals:
        sec = sector_for(sym, strategy)
        eff = mv * (leverage_factor(sym, strategy) if count_lev else 1)
        d = out.setdefault(sec, {"_w": 0.0, "names": [], "count": 0})
        d["_w"] += eff
        d["names"].append(sym)
        d["count"] += 1
    for d in out.values():
        d["pct"] = round(d.pop("_w") / denom, 4) if denom else 0.0
    out["_total_equity"] = round(denom, 2)
    return out


def leveraged_sleeve_exposure(positions, strategy, total_equity=None):
    """NOTIONAL fraction of the account held in leveraged (>1x daily-reset) ETFs —
    the 'how much of the book can a leveraged blowup hit' number. Measured at
    NOTIONAL dollars (NOT leverage-adjusted) on purpose: a 25% cap then means a 60%
    sleeve drawdown costs ~15% of the account, which is the gap-risk math the cap
    exists to bound. positions = [{symbol, market_value}]. Returns {pct, notional, names}."""
    vals = [(p.get("symbol"), float(p.get("market_value") or 0)) for p in positions]
    invested = sum(mv for _, mv in vals if mv > 0)
    denom = total_equity if (total_equity and total_equity > 0) else invested
    lev = [(sym, mv) for sym, mv in vals
           if mv > 0 and leverage_factor(sym, strategy) > 1]
    notional = sum(mv for _, mv in lev)
    return {"pct": round(notional / denom, 4) if denom else 0.0,
            "notional": round(notional, 2), "names": [sym for sym, _ in lev]}


def check_stop_loss_alerts(log):
    """Compare open positions against the hard stop-loss threshold using the
    latest close from signals.py. Returns a list of triggered positions so
    the agent prompt can order immediate sells before any other logic runs.

    The threshold is per-symbol (effective_stop_pct): leveraged ETFs get a
    tighter stop than the base 10%."""
    if not signals:
        return []
    strategy = load_strategy()
    alerts = []
    for pos in log.get("open_positions", []):
        symbol = pos["symbol"]
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            continue
        pct = effective_stop_pct(symbol, strategy)
        try:
            sig = signals.signal_for(symbol)
        except Exception:
            continue
        if not sig.get("ok"):
            continue
        last = float(sig.get("last_close") or 0)
        if last <= 0:
            continue
        loss_pct = (last - entry) / entry
        if loss_pct <= -pct:
            alerts.append({
                "symbol": symbol,
                "entry_price": entry,
                "threshold_price": round(entry * (1 - pct), 4),
                "last_price": last,
                "loss_pct": round(loss_pct * 100, 2),
                "shares": pos.get("shares", 0),
                "position_id": pos.get("id"),
            })
    return alerts


def trailing_stop_pct(strategy):
    """Trailing-stop giveback fraction (off the high-water mark) from
    risk_management.trailing_stop_pct. 0 / absent / invalid => trailing disabled,
    so every check below is a no-op on strategies that predate this field."""
    try:
        v = float((strategy.get("risk_management", {}) or {}).get("trailing_stop_pct") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def update_position_peaks(log, prices):
    """Bump each open position's `peak_price` high-water mark from the latest price.

    prices: {symbol: last_close}. Seeds peak at entry_price the first time a
    position is seen. Returns True if any peak moved, so the caller can persist —
    this must run EVERY cycle (including skipped ones) or the trailing stop trails
    a stale high. Never raises; a missing/garbage price just leaves the peak put."""
    changed = False
    for pos in log.get("open_positions", []):
        base = pos.get("peak_price")
        if base is None:
            base = pos.get("entry_price") or 0
        try:
            base = float(base)
        except (TypeError, ValueError):
            base = 0.0
        last = prices.get(pos.get("symbol"))
        try:
            last = float(last) if last is not None else None
        except (TypeError, ValueError):
            last = None
        new = max(base, last) if last and last > 0 else base
        if new != pos.get("peak_price"):
            pos["peak_price"] = new
            changed = True
    return changed


def check_trailing_stop_alerts(log, prices=None):
    """Trailing-stop exits: held positions that have given back >= trailing_stop_pct
    from their `peak_price` high-water mark. Mirrors check_stop_loss_alerts' shape so
    the alert flows through the same deterministic force_sell() path.

    Inert (returns []) when trailing_stop_pct is absent/<=0. `prices` is an optional
    {symbol: last_close} map to avoid re-fetching (the cycle already has signals);
    falls back to signals.signal_for() per symbol, exactly like the hard-stop check."""
    if not signals:
        return []
    trail = trailing_stop_pct(load_strategy())
    if trail <= 0:
        return []
    prices = prices or {}
    alerts = []
    for pos in log.get("open_positions", []):
        symbol = pos["symbol"]
        try:
            peak = float(pos.get("peak_price") or pos.get("entry_price") or 0)
        except (TypeError, ValueError):
            continue
        if peak <= 0:
            continue
        last = prices.get(symbol)
        if last is None:
            try:
                sig = signals.signal_for(symbol)
            except Exception:
                continue
            if not sig.get("ok"):
                continue
            last = sig.get("last_close")
        try:
            last = float(last or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        giveback = (peak - last) / peak
        if giveback >= trail:
            alerts.append({
                "symbol": symbol,
                "peak_price": round(peak, 4),
                "last_price": last,
                "threshold_price": round(peak * (1 - trail), 4),
                "giveback_pct": round(giveback * 100, 2),
                "shares": pos.get("shares", 0),
                "position_id": pos.get("id"),
            })
    return alerts


def should_skip_model_call(raw_sigs, log):
    """Return (skip: bool, reason: str).

    Skip only when there is provably nothing for the model to do: every signal
    is NEUTRAL/HOLD, no stop-loss has triggered, and no held position is in
    SELL state (EXIT edge or not). This eliminates ~90% of cycles on flat days.

    Called ONLY for market_hours_check — research and midweek always run.
    """
    if not signals:
        return False, "signals_unavailable"

    stop_alerts = check_stop_loss_alerts(log)
    if stop_alerts:
        return False, "stop_loss_alert"

    # Trailing-stop exits ride the same "never skip a forced exit" rail as the hard
    # stop. Reuse this cycle's signal closes so no extra Yahoo fetch is incurred.
    trail_prices = {s: v.get("last_close") for s, v in raw_sigs.items() if v.get("ok")}
    if check_trailing_stop_alerts(log, prices=trail_prices):
        return False, "trailing_stop_alert"

    # When exit_on_ribbon_sell is false (the let-winners-run config) a ribbon SELL on
    # a held name is advisory, not a forced exit, so it must NOT wake the model every
    # cycle — the trailing/hard stops above and the periodic forced_news_check still
    # cover safety and thesis. Default true preserves the prior wake behavior.
    exit_on_ribbon = (load_strategy().get("risk_management", {}) or {}).get(
        "exit_on_ribbon_sell", True)

    held = {p["symbol"] for p in log.get("open_positions", [])}
    # ENTER_LONG dedup: on an intraday chart the live partial bar can flicker a
    # crossover in and out for hours, re-reporting ENTER_LONG on every cycle.
    # Once the model has been shown a symbol's crossover (stamped in
    # _state.enter_long_seen at model-call time), the same symbol can't wake it
    # again until NEWS_CHECK_HOURS pass — the BUY-state pending_buy gate below
    # still covers the ongoing opportunity.
    el_seen = log.get("_state", {}).get("enter_long_seen", {})
    for sym, s in raw_sigs.items():
        if not s.get("ok"):
            # unknown signal — let the model decide
            return False, f"unknown_signal:{sym}"
        t = s.get("transition", "NO_ACTION")
        if t == "ENTER_LONG":
            h = _hours_since(el_seen.get(sym))
            if h is None or h >= NEWS_CHECK_HOURS:
                return False, f"enter_long:{sym}"
        if t == "EXIT" and sym in held and exit_on_ribbon:
            return False, f"exit:{sym}"
        # State-based safety net: the EXIT edge exists only on the bar where the
        # cross happens. If the cross occurred while the bot wasn't looking (down,
        # or before an indicator change), transition reads NO_ACTION forever and
        # the edge-based wake above never fires. A held position sitting in SELL
        # state must wake the model regardless; re-fires every cycle until the
        # position is actually sold (same deliberate nagging as stop-loss alerts).
        if s.get("state") == "SELL" and sym in held and exit_on_ribbon:
            return False, f"ema_sell_held:{sym}"

    # Collect weekend picks that are in BUY zone and not yet held.
    picks = {s.upper() for s in weekend_pick_symbols()}
    pending_buys = [sym for sym in picks - held
                    if raw_sigs.get(sym, {}).get("ok")
                    and raw_sigs.get(sym, {}).get("state") == "BUY"]

    # Unified time-gate: wake the model periodically if open positions exist
    # (thesis/news integrity check) OR if pending buys exist (buy opportunity).
    # Without this gate, pending_buy fired every 15-min cycle for the entire time
    # a weekend pick stayed in BUY state (potentially days), burning tokens on
    # cycles where the model had no cash to deploy anyway.
    needs_periodic_check = bool(log.get("open_positions") or pending_buys)
    if needs_periodic_check:
        # Use last_execution_call_ts (stamped only on market_hours_check) so a
        # research or midweek run doesn't poison this gate.  E.g. pre-market
        # research at 9:23 should not delay the 9:30 execution by 4 hours.
        # No fallback to last_model_call_ts: on a log that has never stamped
        # last_execution_call_ts, a pre-market research run would have just
        # bumped last_model_call_ts and the fallback would re-suppress the
        # 9:30 open — the exact bug this field exists to fix. A missing stamp
        # degrades to one extra model call, which is the safe direction.
        exec_ts = log.get("_state", {}).get("last_execution_call_ts")
        elapsed_h = _hours_since(exec_ts)
        if elapsed_h is None or elapsed_h >= NEWS_CHECK_HOURS:
            reason = "forced_news_check" if log.get("open_positions") else f"pending_buy:{pending_buys[0]}"
            return False, reason

    return True, "all_neutral_no_action"


# ================================================================ PHASE 2
def generate_trade_id(log):
    n = log["_state"].get("next_id", 1)
    log["_state"]["next_id"] = n + 1
    return f"T{n:04d}"


def research_metadata_for(symbol):
    """Fallback entry metadata from the latest weekend_picks_*.md if the buy
    action didn't carry confidence/sources/thesis."""
    d = os.path.join(ROOT, "research")
    picks = sorted(f for f in os.listdir(d) if f.startswith("weekend_picks_")) if os.path.isdir(d) else []
    if not picks:
        return {}
    text = load_file(f"research/{picks[-1]}")
    m = re.search(rf"{re.escape(symbol)}\b.*?[Cc]onfidence[^\d]*(\d+)", text, re.DOTALL)
    return {"confidence": int(m.group(1))} if m else {}


def record_open_position(log, action):
    """Record entry metadata when the model BUYS (needed so a future close has
    entry price/confidence/sources/thesis)."""
    symbol = action["symbol"]
    if any(p["symbol"] == symbol for p in log["open_positions"]):
        return  # already tracked
    meta = research_metadata_for(symbol)
    log["open_positions"].append({
        "id": generate_trade_id(log),
        "symbol": symbol,
        "entry_price": float(action.get("price") or 0),
        "peak_price": float(action.get("price") or 0),  # trailing-stop high-water mark, seeded at entry
        "entry_date": now_iso(),
        "shares": float(action.get("shares") or 0),
        "dollar_amount": round(float(action.get("price") or 0) * float(action.get("shares") or 0), 2),
        "confidence_score": action.get("confidence") or meta.get("confidence"),
        "sources_used": action.get("sources") or [],
        "thesis": action.get("thesis", ""),
    })


def adopt_untracked_positions(log, positions):
    """Adopt any account-held position the agent isn't already tracking — e.g.
    entered manually or before the agent took over the account. Without this,
    such positions are invisible to the stop-loss monitor (check_stop_loss_alerts
    iterates open_positions) and to the post-trade learning loop (a close only
    fires a postmortem/victory for a tracked open). Entry price uses the broker's
    average cost; entry metadata is backfilled from weekend_picks when available.
    Returns the list of adopted symbols."""
    tracked = {p["symbol"] for p in log["open_positions"]}
    adopted = []
    for p in positions:
        symbol = p.get("symbol")
        shares = float(p.get("shares") or 0)
        if not symbol or symbol in tracked or shares <= 0:
            continue
        entry = float(p.get("avg_price") or 0)
        meta = research_metadata_for(symbol)
        log["open_positions"].append({
            "id": generate_trade_id(log),
            "symbol": symbol,
            "entry_price": entry,
            "peak_price": entry,  # trailing-stop high-water mark; climbs from adoption cost
            "entry_date": now_iso(),
            "shares": shares,
            "dollar_amount": round(entry * shares, 2),
            "confidence_score": meta.get("confidence"),
            "sources_used": [],
            "thesis": "",
            "adopted": True,  # not opened by the agent; entry_date is adoption time, not true entry
        })
        adopted.append(symbol)
        tracked.add(symbol)
    return adopted


def detect_closed_positions(open_positions, current_positions):
    """Spec's positions diff: open positions whose symbol is gone (or zeroed)."""
    held = {p["symbol"]: float(p.get("shares") or 0) for p in current_positions}
    return [op for op in open_positions if held.get(op["symbol"], 0) <= 0]


def log_trade_outcome(log, open_pos, exit_price, exit_date, stop_loss=False):
    """Close a position: compute P&L, append to trades, update summary, fire the
    post-trade analysis pipeline. Returns the closed trade dict."""
    entry = float(open_pos["entry_price"])
    shares = float(open_pos["shares"])
    exit_price = float(exit_price)
    pnl = exit_price - entry
    pnl_pct = ((exit_price - entry) / entry * 100) if entry else 0.0
    outcome = "WIN" if pnl > 0 else "LOSS"

    trade = {
        "id": open_pos["id"], "symbol": open_pos["symbol"],
        "entry_price": entry, "exit_price": exit_price,
        "entry_date": open_pos["entry_date"], "exit_date": exit_date,
        "shares": shares, "dollar_amount": open_pos.get("dollar_amount"),
        "pnl_dollar": round(pnl * shares, 4), "pnl_pct": round(pnl_pct, 2),
        "outcome": outcome, "stop_loss": stop_loss,
        "confidence_score": open_pos.get("confidence_score"),
        "sources_used": open_pos.get("sources_used", []), "thesis": open_pos.get("thesis", ""),
        "postmortem_filed": False, "victory_filed": False, "analysis_file": None,
    }

    log["trades"].append(trade)
    s = log["summary"]
    s["total_trades"] += 1
    s["wins" if outcome == "WIN" else "losses"] += 1
    s["total_pnl"] = round(s["total_pnl"] + trade["pnl_dollar"], 4)
    s["win_rate"] = round(s["wins"] / s["total_trades"], 4)
    log["open_positions"] = [p for p in log["open_positions"] if p["id"] != open_pos["id"]]
    save_trade_log(log)

    run_post_trade_pipeline(log, trade)
    return trade


# ---------------- analysis engines (skill_4 / skill_4b) ----------------
def _next_index(prefix):
    d = os.path.join(ROOT, "postmortems")
    os.makedirs(d, exist_ok=True)
    return len([f for f in os.listdir(d) if f.startswith(prefix)]) + 1


def _run_analysis(trade, skill_path, prefix, kind):
    """Fire skill_4 / skill_4b: research the hold period (web search), write the
    structured markdown to /postmortems/, and return the verdicts JSON."""
    system = load_file(skill_path)
    depth = ("HIGH confidence — go DEEPEST" if (trade.get("confidence_score") or 0) >= 90
             else "moderate confidence — standard depth")
    sl_note = (" This was a STOP-LOSS forced exit (hard 10% drawdown limit hit)."
               " Focus on: what caused the drawdown, whether the entry thesis was"
               " already invalidated before stop-loss triggered, and whether an"
               " earlier EMA SELL signal was missed." if trade.get("stop_loss") else "")
    user = (
        f"A trade just closed as a {trade['outcome']}.{sl_note} Analysis depth: {depth}.\n\n"
        f"Trade:\n{json.dumps(trade, indent=2)}\n\n"
        "Research what happened between entry_date and exit_date via web search "
        "(news + Reddit). Then produce the EXACT markdown format from your skill, "
        "followed by the machine-readable JSON verdicts block. Today: "
        f"{date.today().isoformat()}."
    )
    text, _usage = run_model(system, user, web=True)
    verdicts = extract_last_json_block(text) or {}

    idx = _next_index(prefix)
    fname = f"{prefix}{idx:03d}.md"
    with open(os.path.join(ROOT, "postmortems", fname), "w") as f:
        f.write(strip_json_blocks(text) + "\n")
    print(f"  [{kind}] {trade['symbol']} {trade['outcome']} -> postmortems/{fname}")
    return verdicts, fname


def trigger_postmortem(trade):
    return _run_analysis(trade, "skills/skill_4_postmortem.md", "postmortem_", "postmortem")


def trigger_victory_analysis(trade):
    return _run_analysis(trade, "skills/skill_4b_victory.md", "victory_", "victory")


# ---------------- confidence calibration ----------------
def update_source_weights(trade, verdicts):
    """Counts update EVERY trade; weights only rebalance after >= min trades."""
    strategy = load_strategy()
    sp = strategy.get("research", {}).get("source_performance", {})
    verdict_map = (verdicts or {}).get("sources", {})
    for source in SOURCES:
        if source not in sp:
            continue
        v = verdict_map.get(source, "na")
        if v == "accurate":
            sp[source]["wins"] += 1
        elif v == "inaccurate":
            sp[source]["losses"] += 1
        total = sp[source]["wins"] + sp[source]["losses"]
        sp[source]["accuracy"] = round(sp[source]["wins"] / total, 4) if total else 0

    total_trades = load_trade_log()["summary"]["total_trades"]
    min_trades = strategy.get("research", {}).get("min_trades_before_weight_shift", 5)
    if total_trades >= min_trades:
        rebalance_source_weights(strategy)
        snapshot_strategy(strategy, f"source-weight rebalance after trade {trade['id']}", [trade["id"]])
    save_strategy(strategy)


def rebalance_source_weights(strategy):
    """Shift weight toward accuracy — gently (<=0.05/source per update), renormalized."""
    weights = strategy["research"]["source_weights"]
    sp = strategy["research"]["source_performance"]
    acc = {s: sp[s]["accuracy"] for s in weights
           if s in sp and (sp[s]["wins"] + sp[s]["losses"]) > 0}
    total_acc = sum(acc.values())
    if total_acc <= 0:
        return
    for s in list(weights):
        target = (acc[s] / total_acc) if s in acc else weights[s]
        blended = 0.8 * weights[s] + 0.2 * target
        delta = max(-0.05, min(0.05, blended - weights[s]))
        weights[s] = max(0.0, min(0.6, round(weights[s] + delta, 4)))
    tot = sum(weights.values()) or 1
    for s in weights:
        weights[s] = round(weights[s] / tot, 4)
    # Enforce the 0.6 cap after normalization; normalization can push a pre-capped
    # source above 0.6 when other sources were reduced (total < 1 before normalizing).
    # Collect excess and redistribute to uncapped sources to keep sum = 1.0.
    excess = sum(max(0.0, weights[s] - 0.6) for s in weights)
    for s in weights:
        weights[s] = min(0.6, weights[s])
    if excess > 0:
        uncapped = {s: w for s, w in weights.items() if w < 0.6 - 1e-6}
        base = sum(uncapped.values()) or 1
        for s in uncapped:
            weights[s] = round(weights[s] + excess * (uncapped[s] / base), 4)


# ---------------- progress tracker ----------------
def get_current_week_of_month():
    return min(4, ((date.today().day - 1) // 7) + 1)


def required_weekly_return(current, target, weeks_remaining):
    if weeks_remaining <= 0 or current <= 0:
        return 0.0
    return round(((target / current) ** (1 / weeks_remaining) - 1) * 100, 2)


def update_monthly_progress(log):
    strategy = load_strategy()
    pt = strategy.setdefault("progress_tracking", {})
    start = pt.get("month_start_value", 91)
    current = round(start + log["summary"]["total_pnl"], 4)
    monthly_return = ((current - start) / start * 100) if start else 0.0

    week = get_current_week_of_month()
    req = required_weekly_return(current, start * 2, 4 - week)
    on_track = monthly_return >= (week / 4) * 100

    pt["current_value"] = current
    pt["current_return"] = f"{monthly_return:.1f}%"
    pt["on_track"] = on_track
    pt["required_weekly_return"] = req
    save_strategy(strategy)

    s = log["summary"]
    s["current_value"] = current
    s["monthly_return_pct"] = round(monthly_return, 2)
    s["on_track"] = on_track
    save_trade_log(log)

    if not on_track:
        print(f"  Behind goal pace ({monthly_return:.1f}%). Per goal_framing this is "
              "INFORMATIONAL — do NOT raise per-trade risk to chase the 100% ceiling.")
    return on_track


def flag_strategy_rewrite(trade):
    """Queue a note so skill_5 reviews this outcome on its next cycle.
    Each trade ID is queued at most once — dedup prevents phantom-close
    or pipeline-replay from adding duplicate entries."""
    path = os.path.join(ROOT, "research", "strategy_rewrite_queue.md")
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
        if f"| {trade['id']} " in existing:
            return  # already queued (pending or done)
    line = (f"- {now_iso()} | {trade['id']} "
            f"{trade['symbol']} {trade['outcome']} {trade['pnl_pct']}% | "
            f"analysis: {trade.get('analysis_file')} | skill_5 review\n")
    header = "" if os.path.exists(path) else "# Strategy rewrite queue (skill_5 reads this)\n\n"
    with open(path, "a") as f:
        f.write(header + line)


def run_post_trade_pipeline(log, trade):
    """The full Phase 2 reaction to a close — fires automatically."""
    if trade["outcome"] == "LOSS":
        verdicts, fname = trigger_postmortem(trade)
        trade["postmortem_filed"] = True
    else:
        verdicts, fname = trigger_victory_analysis(trade)
        trade["victory_filed"] = True
    trade["analysis_file"] = fname

    # persist the analysis flags onto the stored trade
    for t in log["trades"]:
        if t["id"] == trade["id"]:
            t.update({"postmortem_filed": trade["postmortem_filed"],
                      "victory_filed": trade["victory_filed"],
                      "analysis_file": fname})
    save_trade_log(log)

    update_source_weights(trade, verdicts)
    update_monthly_progress(log)
    flag_strategy_rewrite(trade)


# ================================================================ PHASE 3
# Close the learning loop: actually PROCESS the strategy_rewrite_queue (skill_5),
# and version skill-file edits so a bad rewrite can be rolled back.
def version_skill_file(skill_name, new_content, *, reason="", trade_ids=None,
                       severity=None):
    """Archive the current skill file into skills/history/ before overwriting it
    with new_content. Mirrors snapshot_strategy() for skill files so a bad skill_5
    rewrite is always reversible.

    Naming: skills/history/{skill_name}_v{NNN}.md (zero-padded 3 digits); the
    version number is (count of existing history files for this skill) + 1.
    Returns True on success, False on any error (never raises — a versioning
    failure must not crash the rewrite loop).

    reason/trade_ids/severity are recorded to logs/change_events.jsonl so Phase 4's
    regression detector can see WHEN/WHY/from-which-trade a skill changed — until
    now a skill bump left only a content snapshot with no structured record (the
    blind spot the TEMA incident slipped through). Optional + keyword-only so
    existing callers keep working."""
    try:
        skill_path = os.path.join(ROOT, "skills", f"{skill_name}.md")
        history_dir = os.path.join(ROOT, "skills", "history")
        os.makedirs(history_dir, exist_ok=True)

        # Count existing versions to determine next version number
        existing = [f for f in os.listdir(history_dir)
                    if f.startswith(f"{skill_name}_v") and f.endswith(".md")]
        version = len(existing) + 1

        # Archive current version
        if os.path.exists(skill_path):
            dst = os.path.join(history_dir, f"{skill_name}_v{version:03d}.md")
            shutil.copy(skill_path, dst)

        # Write new version
        with open(skill_path, "w") as f:
            f.write(new_content)

        print(f"  [version] {skill_name} -> v{version:03d} (history saved)")
        # Structured event for Phase 4 (skill-file bumps were previously invisible
        # to any detector). A skill_2 (execution) change is MAJOR by definition;
        # otherwise fall back to the tag skill_5 supplied.
        sev = severity or ("MAJOR" if skill_name == "skill_2_execution" else None)
        record_change_event("skill", skill_name, version, trade_ids=trade_ids,
                            severity=sev, summary=reason)
        return True
    except Exception as e:
        print(f"  [version] ERROR versioning {skill_name}: {e}")
        return False


def rollback_skill(skill_name, version):
    """Restore a skill file from skills/history/. Manual operator tool — never
    called automatically. Usage: rollback_skill("skill_1_research", 2) restores
    skills/history/skill_1_research_v002.md. Returns True on success, False if the
    version isn't found. Never raises."""
    try:
        history_dir = os.path.join(ROOT, "skills", "history")
        src = os.path.join(history_dir, f"{skill_name}_v{version:03d}.md")
        dst = os.path.join(ROOT, "skills", f"{skill_name}.md")

        if not os.path.exists(src):
            print(f"  [rollback] version not found: {src}")
            return False

        shutil.copy(src, dst)
        print(f"  [rollback] {skill_name} restored to v{version:03d}")
        return True
    except Exception as e:
        print(f"  [rollback] ERROR: {e}")
        return False


def _compact_trade_log_for_rewrite(log, focus_trade_id=None):
    """Compact trade-log view for the skill_5 prompt: the rolling summary + open
    positions + a one-line-per-trade closed history, plus the FULL record of the
    trade under review. Replaces dumping the entire trade_log.json, which grows
    without bound — skill_5 needs the cross-trade pattern (symbol/outcome/pnl/
    confidence/stop_loss per close), not every field of every closed trade."""
    closed = log.get("trades", [])
    rows = [{"id": t.get("id"), "symbol": t.get("symbol"),
             "outcome": t.get("outcome"), "pnl_pct": t.get("pnl_pct"),
             "confidence_score": t.get("confidence_score"),
             "stop_loss": t.get("stop_loss", False),
             "entry_date": t.get("entry_date"), "exit_date": t.get("exit_date")}
            for t in closed]
    view = {"summary": log.get("summary", {}),
            "open_positions": log.get("open_positions", []),
            "closed_trades_compact": rows}
    focus = next((t for t in closed if t.get("id") == focus_trade_id), None)
    if focus:
        view["focus_trade_full"] = focus
    return json.dumps(view, indent=2)


def _postmortems_for_rewrite(analysis_file):
    """The referenced analysis in full + a filename index of the others, instead of
    concatenating every postmortem/victory (which grew on every close). skill_5
    reviews the ONE referenced outcome; cross-trade pattern detection is served by
    closed_trades_compact and strategy.confidence_accuracy, so the others only need
    to be named, not pasted in full. (skill_5 is headless with no file tool, so the
    index is a pointer for the audit trail, not something it can open mid-run.)"""
    d = os.path.join(ROOT, "postmortems")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    out = []
    if analysis_file and os.path.exists(os.path.join(d, analysis_file)):
        out.append(f"=== REFERENCED ANALYSIS ({analysis_file}) — full text ===\n"
                   + load_file(f"postmortems/{analysis_file}"))
    idx = [f"  - {f}" for f in files if f != analysis_file]
    if idx:
        out.append("=== OTHER ANALYSES ON FILE (filenames only; their outcomes are "
                   "in closed_trades_compact and strategy.confidence_accuracy) ===\n"
                   + "\n".join(idx))
    return "\n\n".join(out)


def process_strategy_rewrite_queue():
    """Read research/strategy_rewrite_queue.md and process the first entry not yet
    marked [DONE]: run skill_5 (headless, no file-write tool), parse its output, and
    apply the strategy.json + skill-file updates from Python. Marks the entry [DONE].

    Called at the end of every run_agent() cycle, after process_cycle_state().
    Processes AT MOST ONE entry per cycle so one bad rewrite can't block the loop.
    Skips gracefully if the file is missing; never raises (the caller also wraps
    this in try/except — a rewrite failure must never crash the trading loop)."""
    queue_path = os.path.join(ROOT, "research", "strategy_rewrite_queue.md")
    if not os.path.exists(queue_path):
        return

    with open(queue_path) as f:
        lines = f.readlines()

    # Find first unprocessed entry
    target_idx = None
    target_line = None
    for i, line in enumerate(lines):
        if line.startswith("- ") and "[DONE" not in line:
            target_idx = i
            target_line = line.strip()
            break

    if target_idx is None:
        return  # all processed

    # Auto-skip entries whose referenced analysis file no longer exists —
    # no point burning an Opus call on a postmortem that was quarantined or
    # never written (e.g. phantom-close artifacts).
    analysis_match = re.search(r"analysis: (\S+\.md)", target_line)
    if analysis_match:
        analysis_file = analysis_match.group(1)
        analysis_path = os.path.join(ROOT, "postmortems", analysis_file)
        if not os.path.exists(analysis_path):
            print(f"  [skill_5] skipping entry (analysis file missing: {analysis_file}): {target_line[:60]}")
            lines[target_idx] = lines[target_idx].rstrip() + f" [DONE {now_iso()} SKIPPED-missing-analysis]\n"
            with open(queue_path, "w") as f:
                f.writelines(lines)
            return

    print(f"  [skill_5] processing rewrite queue entry: {target_line[:80]}")

    # The trade under review — used to slim the prompt to the relevant context.
    analysis_file = analysis_match.group(1) if analysis_match else None
    tid_match = re.search(r"\|\s*(T\d+)\b", target_line)
    focus_trade_id = tid_match.group(1) if tid_match else None

    try:
        # strategy.json is sent IN FULL — skill_5 must REWRITE it and therefore has
        # to see every field (incl. version_history) to echo it back complete. The
        # skill files are sent in full too, so skill_5 keeps the ability to rewrite
        # any of them. Postmortems and the trade log are slimmed: the referenced
        # analysis in full + an index of the rest, and a compact trade-log view +
        # the focus trade in full. Both used to be dumped whole and grew on every
        # close, while most of that bulk was irrelevant to reviewing one outcome.
        skill5 = load_file("skills/skill_5_strategy_rewriter.md")
        strategy = load_file("strategy/strategy.json")
        postmortems = _postmortems_for_rewrite(analysis_file)
        trade_log = _compact_trade_log_for_rewrite(load_trade_log(), focus_trade_id)

        # Load all current skill file contents for skill_5 to rewrite
        skill_contents = {}
        for sk in ["skill_0_orchestrator", "skill_1_research", "skill_2_execution",
                   "skill_3_midweek", "skill_4_postmortem", "skill_4b_victory",
                   "skill_5_strategy_rewriter", "skill_6_pattern_detector"]:
            skill_contents[sk] = load_file(f"skills/{sk}.md")

        user = f"""A trade closed and requires strategy review. Queue entry: {target_line}

Current strategy.json (COMPLETE — preserve every field when you rewrite it):
{strategy}

Postmortem / victory analyses (referenced one in full; others indexed):
{postmortems}

Trade log (compact: summary + open positions + closed-trade history; focus trade in full):
{trade_log}

Current skill file contents:
{json.dumps(skill_contents, indent=2)}

Your job:
1. Read the postmortem/victory referenced in the queue entry.
2. Check if 3+ similar outcomes justify any core rule changes.
3. Apply minor changes (source weights, target tweaks) immediately.
4. Flag major changes with reasoning and trade IDs — do not auto-apply.
5. If any skill file needs updating, output the COMPLETE updated file
   using this EXACT format (agent.py parses this):

## SKILL FILE UPDATE: skill_name_without_extension
[complete file content here]
## END SKILL FILE UPDATE

6. Output the COMPLETE updated strategy.json as a fenced json block.
7. Be conservative — one or two trades is noise, not signal.
"""

        text, _usage = run_model(skill5, user, web=False)

        if text.startswith("(claude -p error") or text.startswith("(error:"):
            print(f"  [skill_5] model call failed: {text[:100]}")
            return

        # Severity tag skill_5 emits (forward-compat for Phase 5; recorded into the
        # change-event log so Phase 4 can see ROUTINE vs MAJOR per bump).
        sev_match = re.search(r"SEVERITY:\s*(ROUTINE|MAJOR)", text, re.IGNORECASE)
        severity = sev_match.group(1).upper() if sev_match else None
        review_trades = [focus_trade_id] if focus_trade_id else []

        # Parse and apply strategy.json updates
        new_strategy = extract_last_json_block(text)
        if new_strategy and isinstance(new_strategy, dict) and "version" in new_strategy:
            current = load_strategy()
            old_v = current.get("version", 1)
            # snapshot before overwriting
            src = os.path.join(ROOT, "strategy", "strategy.json")
            dst = os.path.join(ROOT, "strategy", "history", f"strategy_v{old_v}.json")
            if os.path.exists(src):
                shutil.copy(src, dst)
            save_json("strategy/strategy.json", new_strategy)
            new_v = new_strategy.get("version", old_v + 1)
            print(f"  [skill_5] strategy.json updated v{old_v} -> v{new_v}")
            # This path bypasses snapshot_strategy(), so record the change event here.
            record_change_event("strategy", "strategy.json", new_v,
                                trade_ids=review_trades, severity=severity,
                                summary=f"skill_5 review of {target_line[:120]}")

        # Parse and apply skill file updates
        skill_updates = re.findall(
            r"## SKILL FILE UPDATE: (\S+)\n(.*?)\n## END SKILL FILE UPDATE",
            text, re.DOTALL
        )
        for skill_name, new_content in skill_updates:
            skill_path = os.path.join(ROOT, "skills", f"{skill_name}.md")
            if os.path.exists(skill_path):
                version_skill_file(skill_name, new_content.strip(),
                                   reason=f"skill_5 review of {target_line[:120]}",
                                   trade_ids=review_trades, severity=severity)
                print(f"  [skill_5] skill updated: {skill_name}")
            else:
                print(f"  [skill_5] WARNING: unknown skill name in update block: {skill_name}")

        # Audit trail -> single monthly rolling log (was one skill5_run_*.md per run)
        stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
        append_audit_log("skill5", f"skill_5 run {stamp} (severity={severity})",
                         f"Queue entry: {target_line}\n\n{text}")

        # Mark entry as DONE
        lines[target_idx] = lines[target_idx].rstrip() + f" [DONE {now_iso()}]\n"
        with open(queue_path, "w") as f:
            f.writelines(lines)

        print("  [skill_5] queue entry marked [DONE]")

    except Exception as e:
        print(f"  [skill_5] ERROR processing rewrite queue: {e} — skipping this entry")


# ================================================================ MAIN RUN
FOOTER_INSTRUCTION = (
    "\n\nAFTER you finish, END YOUR RESPONSE WITH ONE fenced ```json block "
    "reporting machine-readable state (no prose after it), schema:\n"
    '{"cash": <settled cash float>, '
    '"positions": [{"symbol": "X", "shares": <float>, "avg_price": <float>, "last_price": <float>}], '
    '"actions_taken": [{"type": "buy"|"sell", "symbol": "X", "shares": <float>, "price": <float>, '
    '"confidence": <int|null>, "sources": ["news","social",...], "thesis": "<short>"}]}\n'
    'If you placed no orders, use "actions_taken": []. Report only what is real.\n'
    "Do ALL work yourself inline with the tools provided — NEVER invoke a Skill, "
    "Task, or subagent tool (they are unavailable here and the attempt will stall "
    "your run before the JSON footer is produced)."
)


def persist_phase_output(task, text):
    """Persist research/midweek model output to the file the phase routing
    expects. The model runs headless (claude -p) with NO file-write tool, so it
    cannot write these files itself — without this, active_skill() sees the
    midweek review as missing and re-fires midweek_validation every cycle for
    the rest of the day. Returns the filename written, or None."""
    if text.startswith("(claude -p error") or text.startswith("(error:"):
        return None  # failed run — leave the file absent so the phase retries
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if task == "midweek_validation":
        fname = f"midweek_review_{today}.md"
    elif task == "research_and_prep" and re.search(r"^###\s+#\d+\s+—\s+\w+", text, re.M):
        # only persist research output that actually contains ranked picks
        fname = f"weekend_picks_{today}.md"
    else:
        return None
    path = os.path.join(ROOT, "research", fname)
    if os.path.exists(path):
        return None  # never clobber an existing review/picks file
    with open(path, "w") as f:
        f.write(text + "\n")
    return fname


def read_broker_state():
    """Independent, read-only broker snapshot via a dedicated `claude -p` call —
    the AUTHORITATIVE ground truth for close detection. The main execution turn's
    self-reported footer cannot be trusted: on 2026-06-12 the model reported CLOV
    and AI sold (cash + a positions list excluding them) while the broker showed
    both still held at full size and ZERO orders placed. Trusting that footer
    phantom-closed both positions and fired two bogus postmortems.

    Returns (positions, sell_symbols_today):
      positions          — list of {symbol, shares, avg_price, last_price}, or
                           None if the read itself failed (caller must then NOT
                           treat anything as closed — a failed read is unknown,
                           not "flat").
      sell_symbols_today — set of symbols with a SELL order placed today in any
                           state (used to avoid double-selling: don't force-sell a
                           name the main turn already has a working order for)."""
    system = ("You are a read-only Robinhood query tool. Use only the MCP read "
              "tools. Do not place, modify, or cancel any order.")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    user = (
        f"For Robinhood account {ACCOUNT_NUMBER}:\n"
        "1. Call get_equity_positions and list every currently held position.\n"
        f"2. Call get_equity_orders (created_at_gte={today}) and note which symbols "
        "have a SELL-side order placed today (any state).\n"
        "Output ONLY one fenced ```json block, no prose:\n"
        '{"positions": [{"symbol": "X", "shares": <float>, "avg_price": <float>, '
        '"last_price": <float|null>}], "sell_orders_today": ["SYM", ...]}\n'
        "shares = quantity held (use 0 only if truly flat). If no positions, use []."
    )
    text, _ = run_model(system, user, mcp=True, read_only=True, model=CHECK_MODEL,
                        timeout=240)
    block = extract_last_json_block(text)
    if not (block and isinstance(block, dict) and isinstance(block.get("positions"), list)):
        return None, set()
    positions = [p for p in block["positions"]
                 if p.get("symbol") and float(p.get("shares") or 0) > 0]
    sells = {s.upper() for s in (block.get("sell_orders_today") or []) if isinstance(s, str)}
    return positions, sells


def force_sell(symbol, shares, reason):
    """Deterministically place a market SELL for ALL shares of one symbol via a
    tight, single-purpose `claude -p` call, then self-confirm via get_equity_orders.
    Returns (placed: bool, fill_price: float|None).

    This is the execution path for HARD forced exits (stop-loss, ribbon SELL): the
    single most critical action must not depend on the big, chatty execution turn,
    which has been observed to narrate a sell in its footer without ever calling
    place_equity_order. A one-instruction prompt on the research model (Opus) is
    far more likely to actually fire the order, and we verify it afterwards."""
    if EXECUTION_MODE != "live":
        return False, None
    system = ("You place exactly one Robinhood order and then confirm it. No "
              "analysis, no hedging, no second-guessing.")
    user = (
        f"Place a MARKET SELL order for ALL {shares} shares of {symbol} in Robinhood "
        f"account {ACCOUNT_NUMBER} using place_equity_order RIGHT NOW. This is a "
        f"pre-authorized forced exit (reason={reason}) — do not analyze, do not skip. "
        "After placing it, call get_equity_orders to confirm the order exists. "
        "Output ONLY one fenced ```json block, no prose:\n"
        '{"placed": true|false, "symbol": "' + symbol + '", "order_id": "<id or null>", '
        '"fill_price": <float|null>}\n'
        "Set placed=true ONLY if place_equity_order returned an order id."
    )
    text, _ = run_model(system, user, mcp=True, allow_write=True, model=MODEL,
                        timeout=240)
    block = extract_last_json_block(text) or {}
    fp = block.get("fill_price")
    try:
        fp = float(fp) if fp is not None else None
    except (TypeError, ValueError):
        fp = None
    return bool(block.get("placed")), fp


# ---------------- Phase B: shadow (paper) options ----------------
def select_shadow_contract(underlying, underlying_price, cfg):
    """Read-only MCP call: find the ~ATM call in the configured DTE window for the
    underlying and return its current quote. Returns a contract dict or None. Uses
    read_only + option READ tools only — it can NEVER place an option order."""
    sel = cfg.get("selection", {})
    today = datetime.now(ET).strftime("%Y-%m-%d")
    system = ("You are a READ-ONLY options data tool. Use only the MCP option/equity "
              "read tools. Never place, modify, or cancel any order.")
    user = (
        f"For underlying {underlying} (spot ~{underlying_price}) in account {ACCOUNT_NUMBER}:\n"
        f"Find the CALL closest to at-the-money (strike nearest spot) expiring "
        f"{sel.get('target_dte_min', 30)}-{sel.get('target_dte_max', 45)} calendar days "
        f"from today ({today}). Use get_option_chains / get_option_quotes.\n"
        "Output ONLY one fenced ```json block, no prose:\n"
        '{"type":"call","strike":<float>,"expiry":"YYYY-MM-DD","bid":<float>,"ask":<float>,'
        '"underlying_price":<float>}\n'
        'If no such liquid contract exists, output {"type":null}.'
    )
    text, _ = run_model(system, user, mcp=True, read_only=True,
                        extra_tools=RH_OPTION_READ, model=CHECK_MODEL, timeout=240)
    block = extract_last_json_block(text)
    if not (block and isinstance(block, dict) and block.get("type")):
        return None
    return block


def read_shadow_quote(shadow):
    """Read-only MCP call: current bid for an exact option contract (the price you'd
    sell at). Returns the bid float or None if the read fails."""
    system = ("You are a READ-ONLY options data tool. Use only MCP option read tools. "
              "Never place, modify, or cancel any order.")
    user = (
        f"In account {ACCOUNT_NUMBER}, get the CURRENT bid for this option:\n"
        f"underlying={shadow['underlying']} type={shadow['type']} strike={shadow['strike']} "
        f"expiry={shadow['expiry']}. Use get_option_quotes.\n"
        'Output ONLY one fenced ```json block: {"bid":<float>,"ask":<float>}'
    )
    text, _ = run_model(system, user, mcp=True, read_only=True,
                        extra_tools=RH_OPTION_READ, model=CHECK_MODEL, timeout=180)
    block = extract_last_json_block(text) or {}
    try:
        return float(block["bid"])
    except (KeyError, TypeError, ValueError):
        return None


def process_options_shadow(raw_sigs, log):
    """Phase B shadow (paper) options pass — fully isolated, READ-ONLY, never trades.
    Runs at the end of an ACTIVE market-hours cycle: marks/closes open shadows on the
    same exit discipline as the equity book, and opens new shadows on qualified BUY
    signals — all using REAL option quotes so the spread/IV-crush cost is captured.

    NOTE: only runs on cycles where the model was called (a smart-skipped flat cycle
    returns before this), so exit marks can lag on quiet days — acceptable for paper
    trading. The caller wraps this in try/except; it also guards internally and never
    places an order (select/quote use read_only)."""
    if not options_shadow or not options_shadow.shadow_enabled(load_strategy()):
        return
    if not is_market_open():
        return  # real option quotes need a live market
    strategy = load_strategy()
    cfg = options_shadow.shadow_cfg(strategy)
    shadow_dir = os.path.join(ROOT, "shadow")
    os.makedirs(shadow_dir, exist_ok=True)
    path = os.path.join(shadow_dir, "options_shadow_log.json")
    slog = options_shadow.load_shadow_log(path)
    now = now_iso()
    account_value = float(log.get("summary", {}).get("current_value") or 0)

    # CLOSE/mark pass: re-quote each open shadow and apply the exit discipline.
    for sh in list(options_shadow.open_shadows(slog)):
        bid = read_shadow_quote(sh)
        if bid is None:
            continue  # quote read failed — leave open, retry a later active cycle
        state = (raw_sigs.get(sh["underlying"], {}) or {}).get("state")
        close, reason = options_shadow.should_close(sh, state, bid, cfg)
        if close:
            options_shadow.close_shadow_record(slog, sh["id"], bid, reason, now_iso=now)
            print(f"  [shadow] closed {sh['id']} {sh['underlying']} {sh['type']} "
                  f"({reason}) exit_bid={bid}")

    # OPEN pass (capped): qualified BUY signals not already shadowed.
    confs = weekend_pick_confidences()
    held_conf = {p["symbol"]: p.get("confidence_score") for p in log.get("open_positions", [])}
    max_open = cfg.get("max_open_shadows", 5)
    opened = 0
    for sym, s in raw_sigs.items():
        if opened >= cfg.get("max_opens_per_cycle", 1):
            break
        if len(options_shadow.open_shadows(slog)) >= max_open:
            break
        if not s.get("ok"):
            continue
        if not (s.get("state") == "BUY" or s.get("transition") == "ENTER_LONG"):
            continue
        if options_shadow.has_open_shadow(slog, sym):
            continue
        conf = held_conf.get(sym)
        if conf is None:
            conf = confs.get(sym)
        if conf is None or conf < 60:
            continue  # shadow only vetted setups
        up = s.get("last_close")
        if not up:
            continue
        contract = select_shadow_contract(sym, up, cfg)
        if not contract:
            continue
        ok, reason = options_shadow.validate_contract(contract, up, cfg)
        if not ok:
            print(f"  [shadow] skip {sym}: contract failed gate ({reason})")
            continue
        rec = options_shadow.open_shadow_record(
            slog, underlying=sym, contract=contract, underlying_price=up,
            account_value=account_value, cfg=cfg, confidence=conf,
            thesis=f"shadow of {sym} BUY signal", now_iso=now)
        opened += 1
        print(f"  [shadow] opened {rec['id']} {sym} {rec['type']} strike={rec['strike']} "
              f"exp={rec['expiry']} entry_ask={rec['entry_premium']} "
              f"oversized={rec['oversized_for_account']}")

    options_shadow.save_shadow_log(path, slog)
    summ = options_shadow.shadow_summary(slog)
    if summ["open"] or summ["closed"]:
        append_audit_log("shadow", f"options shadow {datetime.now(ET):%Y-%m-%d_%H%M}",
                         json.dumps(summ, indent=2))


# ---------------- Phase B+: momentum→shadow (the operator's real edge) ----------------
def fetch_daily_closes(symbol):
    """Daily closes (~1y) for the momentum screen. Reuses signals._fetch_yahoo at
    1d/1y so the screener sees the same data source as the EMA layer. Returns a list
    of closes (oldest→newest) or [] on any failure — never raises into the loop."""
    if not signals:
        return []
    try:
        return signals._fetch_yahoo(symbol, "1d", "1y") or []
    except Exception:
        return []


def momentum_options_watch():
    """Catalyst source for the shadow — REUSED from the morning research, so the
    paper-options pass spends NO extra model tokens (the operator's explicit
    constraint: don't double token usage for paper trading). The single daily
    research run (skill_1, Opus) already searches news/filings and validates WHY
    each name is moving; it emits a dedicated, machine-readable block in its picks
    file:

        ## MOMENTUM OPTIONS WATCH
        - SYMBOL | conf XX | catalyst: <one line on why it is moving>

    kept SEPARATE from the equity `### #N — SYMBOL` picks so cheap momentum names
    fed to the options sleeve never leak into the real equity book. This parses that
    block from the latest weekend_picks_*.md → {SYMBOL: {confidence, catalyst}}.
    Returns {} if the file or block is absent (then the shadow simply opens nothing
    this cycle — it never falls back to a paid catalyst call). Never raises."""
    try:
        text = load_latest_research_file("weekend_picks_") or ""
    except Exception:
        return {}
    if not text:
        return {}
    # Isolate the MOMENTUM OPTIONS WATCH section (up to the next ## heading / EOF).
    m = re.search(r"##\s*MOMENTUM OPTIONS WATCH\s*(.*?)(?:\n##\s|\Z)", text,
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        # - SYMBOL | conf XX | catalyst: ...
        lm = re.match(r"\s*[-*]\s*([A-Za-z][A-Za-z.\-]{0,9})\b.*?conf\w*\s*[:=]?\s*"
                      r"(\d{1,3}).*?catalyst\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if not lm:
            continue
        sym = lm.group(1).upper()
        conf = max(0, min(100, int(lm.group(2))))
        out[sym] = {"confidence": conf, "catalyst": lm.group(3).strip()[:300]}
    return out


def save_momentum_scan(evals):
    """Persist the latest screen for operator inspection + audit. Overwrites a single
    snapshot file (the bot never reads it back) and appends a one-line summary to the
    monthly audit log. Never raises."""
    try:
        shadow_dir = os.path.join(ROOT, "shadow")
        os.makedirs(shadow_dir, exist_ok=True)
        snap = {"scanned_at": now_iso(),
                "qualified": [e["symbol"] for e in evals if e.get("qualified")],
                "evals": evals}
        with open(os.path.join(shadow_dir, "momentum_last_scan.json"), "w") as f:
            json.dump(snap, f, indent=2)
        top = ", ".join(f"{e['symbol']}({e['score']})" for e in evals[:8])
        append_audit_log("momentum", f"momentum scan {datetime.now(ET):%Y-%m-%d_%H%M}",
                         f"qualified={snap['qualified']}\ntop_by_score: {top}")
    except Exception as e:
        print(f"  [momentum] scan snapshot failed: {e}")


def process_momentum_shadow(log):
    """Phase B+ momentum→shadow pass — the operator's real edge as a SECOND shadow
    signal source (NOT the EMA ribbon), built to spend ZERO extra model tokens.

    The catalyst ("there must be a REASON it is up") is REUSED from the morning
    research run (`momentum_options_watch()` parses skill_1's MOMENTUM OPTIONS WATCH
    block) instead of a separate paid web-search call. This pass then layers the
    FREE deterministic multi-timeframe momentum screen (Yahoo daily) + affordability
    gates on top of those already-vetted names, and paper-opens an ATM call on each
    survivor via the SAME options_shadow.py engine as the EMA path (tagged
    signal_source=momentum_research). The ONLY model cost is the read-only Haiku
    option-quote lookup per survivor (unavoidable for a real-quote paper fill) — the
    same kind of read the EMA shadow path already makes. Closes/marks are handled by
    the shared process_options_shadow pass.

    Fully isolated, READ-ONLY, never trades (real orders stay gated by
    options.enabled=false). Screen runs at most once per scan_interval_hours (default
    24h) — it keys off the morning research, which only changes daily. Caller wraps
    this in try/except; it also guards internally and never places an order."""
    strategy = load_strategy()
    if not (options_shadow and momentum_screen and options_shadow.shadow_enabled(strategy)):
        return
    ocfg = options_shadow.shadow_cfg(strategy)
    mcfg = ocfg.get("momentum", {}) or {}
    if not mcfg.get("enabled"):
        return
    if not is_market_open():
        return  # opening a shadow needs live option quotes

    # Once-per-day gate (the research it reads only changes daily).
    st = log.setdefault("_state", {})
    since = _hours_since(st.get("last_momentum_scan_ts"))
    if since is not None and since < mcfg.get("scan_interval_hours", 24):
        return

    # Catalyst names come FREE from the morning research — no paid catalyst call.
    watch = momentum_options_watch()
    if not watch:
        print("  [momentum] no MOMENTUM OPTIONS WATCH names in latest research — "
              "nothing to shadow (no paid catalyst fallback)")
        # Still stamp so we don't re-parse every cycle; research is daily anyway.
        st["last_momentum_scan_ts"] = now_iso()
        save_trade_log(log)
        return
    st["last_momentum_scan_ts"] = now_iso()
    save_trade_log(log)  # persist the stamp (cycle won't save again after this pass)

    min_conf = mcfg.get("min_catalyst_confidence", 60)
    # 1) FREE multi-timeframe momentum + affordability screen over the watch names.
    candidates = {}
    for sym, info in watch.items():
        if info.get("confidence", 0) < min_conf:
            continue
        closes = fetch_daily_closes(sym)
        if closes:
            candidates[sym] = {"closes": closes, "last_price": closes[-1]}
    evals = momentum_screen.screen(candidates, mcfg)
    quals = momentum_screen.qualified(evals)
    save_momentum_scan(evals)
    if not quals:
        print(f"  [momentum] {len(watch)} research watch names, none pass the "
              "momentum/affordability screen this scan")
        return
    print(f"  [momentum] {len(quals)} qualify (research-vetted + momentum): "
          f"{', '.join(e['symbol'] for e in quals[:8])}")

    # 2) Paper-open survivors. Catalyst confidence/text reused from the research.
    shadow_dir = os.path.join(ROOT, "shadow")
    os.makedirs(shadow_dir, exist_ok=True)
    path = os.path.join(shadow_dir, "options_shadow_log.json")
    slog = options_shadow.load_shadow_log(path)
    max_open = ocfg.get("max_open_shadows", 5)
    max_cost = mcfg.get("max_contract_cost_usd")
    account_value = float(log.get("summary", {}).get("current_value") or 0)
    now = now_iso()
    opened = 0
    for ev in quals[: mcfg.get("top_n_open", 3)]:
        sym = ev["symbol"]
        if len(options_shadow.open_shadows(slog)) >= max_open:
            break
        if options_shadow.has_open_shadow(slog, sym):
            continue
        info = watch.get(sym, {})
        contract = select_shadow_contract(sym, ev["last_price"], ocfg)
        if not contract:
            print(f"  [momentum] skip {sym}: no liquid ATM contract")
            continue
        ok, reason = options_shadow.validate_contract(contract, ev["last_price"], ocfg)
        if not ok:
            print(f"  [momentum] skip {sym}: contract failed gate ({reason})")
            continue
        cost = options_shadow.entry_premium(contract) * 100
        if max_cost and cost > max_cost:
            print(f"  [momentum] skip {sym}: contract ${cost:.0f} > "
                  f"affordability cap ${max_cost:.0f} (unaffordable)")
            continue
        rec = options_shadow.open_shadow_record(
            slog, underlying=sym, contract=contract, underlying_price=ev["last_price"],
            account_value=account_value, cfg=ocfg, confidence=info.get("confidence"),
            thesis=f"research catalyst: {info.get('catalyst', '')}", now_iso=now)
        rec["signal_source"] = "momentum_research"
        rec["momentum_score"] = ev["score"]
        rec["momentum_returns"] = ev["returns"]
        rec["catalyst"] = info.get("catalyst", "")
        rec["research_confidence"] = info.get("confidence")
        opened += 1
        print(f"  [momentum] opened {rec['id']} {sym} call strike={rec['strike']} "
              f"exp={rec['expiry']} entry_ask={rec['entry_premium']} "
              f"cost=${cost:.0f} conf={info.get('confidence')} "
              f"score={ev['score']} oversized={rec['oversized_for_account']}")

    if opened:
        options_shadow.save_shadow_log(path, slog)


def _run_shadow_passes(raw_sigs, log):
    """Phase B + B+ paper-options passes, bundled so they run on EVERY market-hours
    cycle — including smart-skipped flat ones. This matters for the momentum edge:
    cheap momentum names break out exactly when the EMA watchlist (and the index) is
    flat and the equity model is skipped, so gating the scan behind the equity model
    call would miss the very setups the operator is after. Both passes are isolated,
    read-only, never trade, internally gated (momentum to a 24h scan interval), and
    each is wrapped so a shadow failure can never crash the trading loop. Cheap when
    the book is empty: with no open shadows and no qualifying signals neither pass
    makes a model call."""
    try:
        process_options_shadow(raw_sigs, log)
    except Exception as e:
        print(f"  [shadow] options shadow pass failed: {e}")
    try:
        process_momentum_shadow(log)
    except Exception as e:
        print(f"  [momentum] momentum shadow pass failed: {e}")


def process_cycle_state(log, actions, broker_positions, exit_info=None):
    """Phase 2 bookkeeping driven by the AUTHORITATIVE broker snapshot, not the
    model's self-report.

    actions          — actions_taken[] from the execution-turn footer (used only
                       to recover exit prices / reasons for confirmed closes and
                       to know which buys to record metadata for).
    broker_positions — the independent broker read (see read_broker_state). A
                       position is closed ONLY when the broker confirms it is gone.
    exit_info        — optional {symbol: {"reason": str, "price": float|None}} for
                       deterministic forced sells placed this cycle.
    """
    actions = actions or []
    exit_info = exit_info or {}
    now = now_iso()
    broker_syms = {p["symbol"] for p in broker_positions if float(p.get("shares") or 0) > 0}

    # 1. record opens from buy actions — but ONLY if the broker confirms the
    #    position actually exists now (a fabricated buy must not be recorded).
    for a in actions:
        if a.get("type") == "buy" and a.get("symbol") in broker_syms:
            record_open_position(log, a)

    # 1b. adopt account-held positions the agent isn't tracking (manual or
    # pre-existing) so the stop-loss monitor and learning loop cover them too.
    adopted = adopt_untracked_positions(log, broker_positions)
    if adopted:
        print(f"  adopted untracked positions (now stop-loss monitored): {', '.join(adopted)}")
    save_trade_log(log)

    # 2. closes: a tracked open whose symbol is GONE from the broker snapshot.
    #    Exit price/reason come from the forced-sell record first, then the
    #    footer's matching sell action, then last price, then entry.
    sell_actions = {a["symbol"]: a for a in actions
                    if a.get("type") == "sell" and a.get("symbol")}
    last_by_sym = {p.get("symbol"): p.get("last_price") for p in broker_positions}
    prev_last = {p.get("symbol"): p.get("last_price")
                 for p in log["_state"].get("last_positions", [])}
    for op in detect_closed_positions(log["open_positions"], broker_positions):
        sym = op["symbol"]
        info = exit_info.get(sym, {})
        a = sell_actions.get(sym, {})
        reason = info.get("reason") or str(a.get("reason", "")).lower()
        is_stop_loss = reason == "stop_loss"
        price = (info.get("price") or a.get("price")
                 or last_by_sym.get(sym) or prev_last.get(sym) or op["entry_price"])
        log_trade_outcome(log, op, price, now, stop_loss=is_stop_loss)

    # 3. snapshot the authoritative broker positions for next cycle's diff.
    log["_state"]["last_positions"] = broker_positions
    save_trade_log(log)


def _format_stop_loss_block(alerts):
    """Build the STOP-LOSS ALERTS section injected at the top of every user prompt."""
    if not alerts:
        return ""
    lines = [
        "⚠ STOP-LOSS TRIGGERED — SELL IMMEDIATELY (overrides EMA signal, blackout windows, and all other rules):",
    ]
    for a in alerts:
        lines.append(
            f"  {a['symbol']}: entry={a['entry_price']}  last={a['last_price']}  "
            f"loss={a['loss_pct']}%  (threshold {a['threshold_price']})  shares={a['shares']}"
        )
    lines.append(
        "For EACH symbol above: sell ALL shares at market price RIGHT NOW via the "
        "Robinhood MCP. In actions_taken set type='sell' and reason='stop_loss'. "
        "Do this before evaluating any other buy or hold decision."
    )
    return "\n".join(lines) + "\n\n"


def _format_ema_sell_block(raw_sigs, log):
    """SELL-ribbon alerts for HELD positions, injected right after stop-loss
    alerts. Keyed off SELL *state*, not the EXIT transition — the cross may
    have happened on a bar the bot never evaluated, in which case transition
    reads NO_ACTION even though the ribbon is in a confirmed downtrend."""
    held = {p["symbol"]: p for p in log.get("open_positions", [])}
    rows = []
    for sym, s in raw_sigs.items():
        if sym in held and s.get("ok") and s.get("state") == "SELL":
            ln = s.get("lines", {})
            rows.append(
                f"  {sym}: red(55)={ln.get('red')} is ON TOP of "
                f"blue(8)={ln.get('blue')} green(13)={ln.get('green')} "
                f"yellow(21)={ln.get('yellow')} | last={s.get('last_close')} "
                f"entry={held[sym].get('entry_price')}"
            )
    if not rows:
        return ""
    exit_on_ribbon = (load_strategy().get("risk_management", {}) or {}).get(
        "exit_on_ribbon_sell", True)
    if not exit_on_ribbon:
        # Let-winners-run config: the ribbon flip is ADVISORY. The engine's
        # deterministic trailing/hard stops own the mechanical exit; do NOT
        # reflexively dump on the SELL state (that cut winners early — measured).
        return (
            "ⓘ RIBBON IN SELL STATE on held positions (ADVISORY — the trailing stop "
            "and hard stop govern the mechanical exit; the engine force-sells on a "
            "trailing-stop breach):\n" + "\n".join(rows) + "\n"
            "Do NOT sell merely because the ribbon flipped — let the trailing stop "
            "work. Sell THIS cycle ONLY if the entry THESIS is broken (set type='sell' "
            "and reason='thesis_break'); otherwise HOLD.\n\n"
        )
    return (
        "⚠ SELL SIGNAL ACTIVE ON HELD POSITIONS — core ribbon rule: red(55) on "
        "top = downtrend = SELL:\n" + "\n".join(rows) + "\n"
        "For EACH symbol above: sell ALL shares at market via the Robinhood MCP "
        "THIS cycle. In actions_taken set type='sell' and reason='ema_exit'. "
        "A transition of NO_ACTION does NOT cancel this — the cross already "
        "happened on an earlier bar. Only stop-loss alerts take precedence.\n\n"
    )


def _format_risk_block(raw_sigs, log, strategy):
    """Never-raise wrapper around _risk_block_impl — this string is concatenated
    straight into the prompt, so a formatting failure must degrade to '' rather than
    crash the trading cycle (same defensive contract as the rest of the loop)."""
    try:
        return _risk_block_impl(raw_sigs, log, strategy)
    except Exception as e:
        print(f"  [risk-block] skipped (non-fatal): {e}")
        return ""


def _risk_block_impl(raw_sigs, log, strategy):
    """Deterministic RISK MODEL block injected into the prompt (Phase A): per-
    candidate risk-based max size, leverage flags, and current sector exposure vs
    the factor caps. The model must size AT OR BELOW these — the risk-critical math
    is computed here, not left to the model. Returns '' if position_sizing is absent
    (older strategy / minimal fixture) so nothing changes for those."""
    ps = strategy.get("position_sizing")
    if not ps:
        return ""
    rm = strategy.get("risk_management", {})
    base_stop = rm.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    risk = ps.get("risk_per_trade_pct", 0.02)

    held = log.get("open_positions", [])
    held_syms = {p["symbol"] for p in held}
    mv_positions = []
    for p in held:
        sym = p["symbol"]
        last = (raw_sigs.get(sym, {}) or {}).get("last_close")
        mv = (float(p.get("shares") or 0) * float(last)) if last else float(p.get("dollar_amount") or 0)
        mv_positions.append({"symbol": sym, "market_value": mv})
    # Denominator = full account value (positions + cash), so a sector cap means
    # '% of the whole account'. current_value tracks the account total; fall back to
    # invested if it isn't larger (e.g. minimal fixtures).
    invested = sum(p["market_value"] for p in mv_positions)
    total_acct = float(log.get("summary", {}).get("current_value") or 0)
    total_equity = total_acct if total_acct > invested else invested
    exp = sector_exposure(mv_positions, strategy, total_equity=total_equity)

    fl = strategy.get("factor_exposure_limits", {})
    cap = fl.get("max_sector_pct", 0.40)
    maxn = fl.get("max_positions_per_sector", 2)

    out = ["RISK MODEL (deterministic — Phase A; size AT OR BELOW these, NEVER above):"]
    out.append(
        f"  Sizing is RISK-BASED: risk {risk*100:.0f}% of equity / {base_stop*100:.0f}% stop "
        f"=> max {min(0.30, risk/base_stop)*100:.0f}% of equity per NON-leveraged name "
        f"(the confidence band is a ceiling, not the target). Leveraged ETFs are divided by "
        f"their leverage AND get a tighter stop.")

    confs = weekend_pick_confidences()
    rows = []
    for sym, s in raw_sigs.items():
        if not s.get("ok"):
            continue
        if not (s.get("state") == "BUY" or s.get("transition") == "ENTER_LONG"):
            continue
        conf = next((p.get("confidence_score") for p in held if p["symbol"] == sym), None)
        if conf is None:
            conf = confs.get(sym)
        if conf is None:
            continue  # unknown confidence -> model scores + sizes via the formula above
        pct = position_size_pct(conf, sym, strategy)
        lf = leverage_factor(sym, strategy)
        tag = (f"  [{lf:g}x leveraged -> 1/{lf:g} size, stop {effective_stop_pct(sym, strategy)*100:.1f}%]"
               if lf > 1 else "")
        held_tag = " (HELD)" if sym in held_syms else ""
        rows.append(f"    {sym}{held_tag}: conf {conf} -> size <= {pct*100:.1f}% of equity{tag}")
    if rows:
        out.append("  Max size per BUY/ENTER_LONG candidate with a known confidence:")
        out += rows

    out.append(
        f"  SECTOR CAP = {cap*100:.0f}% of equity per sector (leveraged ETFs counted at "
        f"leverage), max {maxn} names/sector. Current exposure:")
    sectors = [k for k in exp if k != "_total_equity"]
    if sectors:
        for sec in sorted(sectors):
            d = exp[sec]
            breach = d["pct"] >= cap or d["count"] >= maxn
            flag = "  <-- AT/OVER CAP: do NOT add to this sector" if breach else ""
            out.append(f"    {sec}: {d['pct']*100:.0f}% ({', '.join(d['names'])}){flag}")
    else:
        out.append("    (no open positions)")
    # Leveraged-sleeve cap (#3) — the gap-risk rail for the leverage sleeve. Only
    # surfaced when configured; absent => no change for older strategies/fixtures.
    sleeve_cap = fl.get("leveraged_sleeve_max_pct")
    if sleeve_cap is not None:
        sl = leveraged_sleeve_exposure(mv_positions, strategy, total_equity=total_equity)
        names = ", ".join(sl["names"]) or "none"
        breach = sl["pct"] >= sleeve_cap
        flag = "  <-- AT/OVER CAP: do NOT add leveraged exposure" if breach else ""
        out.append(
            f"  LEVERAGED SLEEVE CAP = {sleeve_cap*100:.0f}% of the account in >1x ETFs "
            f"(NOTIONAL — the overnight-gap limit). Current: {sl['pct']*100:.0f}% ({names}){flag}.")
        out.append(
            "  A 3x ETF can gap 60%+ overnight and NO stop (trailing or hard) beats a gap — "
            "this notional cap is the real defense. Never let total leveraged notional exceed it.")

    # Concentration (#2) — fewer, higher-conviction names. Only when configured.
    max_pos = ps.get("max_concurrent_positions")
    if max_pos:
        out.append(
            f"  CONCENTRATION: hold at most {max_pos} names ({len(held)} open now). Deploy "
            f"TOP-DOWN — fill the highest-confidence idea to its band ceiling before adding a "
            f"lower-ranked one; do NOT dilute into marginal 60-74 names while a 90+ name still "
            f"has capacity. If at the cap, only rotate (sell the weakest to fund a stronger).")

    out.append(
        "  Before ANY buy: confirm the new position keeps its sector <= the cap and "
        "<= max names/sector; if it would breach, size down or skip.")
    return "\n".join(out) + "\n\n"


def run_agent():
    log = load_trade_log()
    # Lean view (version_history elided) for the read-only cycle prompt; the full
    # file stays on disk for skill_5 and Phase 4. See strategy_for_prompt().
    strategy_text = strategy_for_prompt()
    strategy = load_strategy()  # dict form for the deterministic risk/sizing block
    skill, task = active_skill()
    syms = watchlist_symbols(log)

    # Compute signals once; reuse raw dict for skip check and formatted block for prompt.
    if signals:
        try:
            raw_sigs, sig_block = signals.signals_with_raw(syms)
        except Exception as e:
            raw_sigs, sig_block = {}, f"(signal error: {e})"
    else:
        raw_sigs, sig_block = {}, "(signals.py unavailable — EMA signals NOT computed this cycle)"

    stop_loss_alerts = check_stop_loss_alerts(log)
    if stop_loss_alerts:
        for a in stop_loss_alerts:
            print(f"  [STOP-LOSS] {a['symbol']} down {a['loss_pct']}% — flagging for immediate sell")

    # Update each held position's high-water mark from this cycle's closes, then
    # persist — the trailing stop trails a STALE high if peaks aren't bumped on every
    # cycle that price made a new high (incl. ones that later skip). Cheap local write.
    trail_prices = {s: v.get("last_close") for s, v in raw_sigs.items() if v.get("ok")}
    if update_position_peaks(log, trail_prices):
        save_trade_log(log)

    # Hard forced-exit set (from pre-turn signals): stop-loss + trailing-stop breaches,
    # plus — only when exit_on_ribbon_sell is true — held names sitting in ribbon SELL
    # state. stop_loss takes precedence. Computed up here so the error path below can
    # warn about pending exits even when the model call itself fails (e.g. session limit).
    held_syms = {p["symbol"] for p in log.get("open_positions", [])}
    must_sell = {a["symbol"]: "stop_loss" for a in stop_loss_alerts}
    for a in check_trailing_stop_alerts(log, prices=trail_prices):
        must_sell.setdefault(a["symbol"], "trailing_stop")
        print(f"  [TRAILING-STOP] {a['symbol']} -{a['giveback_pct']}% off peak "
              f"{a['peak_price']} — flagging for immediate sell")
    if (strategy.get("risk_management", {}) or {}).get("exit_on_ribbon_sell", True):
        for sym, s in raw_sigs.items():
            if sym in held_syms and s.get("ok") and s.get("state") == "SELL":
                must_sell.setdefault(sym, "ema_exit")

    # SMART SKIP: if every signal is NEUTRAL/HOLD and no stop-loss → no model call needed.
    # Research and midweek phases always run (they do web research, not just signal checks).
    if task == "market_hours_check":
        skip, skip_reason = should_skip_model_call(raw_sigs, log)
        if skip:
            stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
            print(f"[{stamp}] task={task} SKIPPED ({skip_reason}) — 0 equity tokens used.")
            # Still run the paper-options passes: the momentum edge fires on exactly
            # these flat-index cycles, and they no-op cheaply when nothing qualifies.
            _run_shadow_passes(raw_sigs, log)
            return

    # For market-hours execution checks, strip data the model doesn't need:
    # — postmortems (only relevant for research/strategy phases)
    # — closed trades (model only needs open positions + summary for execution)
    if task == "market_hours_check":
        postmortems_section = ""
        log_for_prompt = {"summary": log["summary"], "open_positions": log["open_positions"]}
    else:
        postmortems_section = f"\nALL POSTMORTEMS AND VICTORY ANALYSES:\n{load_postmortems()}"
        log_for_prompt = log

    # Research context: execution and midweek must see the weekend picks so they
    # know which stocks were selected and at what targets/confidence. Execution also
    # gets the latest midweek review (which may flag positions to cut or redeploy).
    # Research phase skips this — it produces fresh picks, not reads old ones.
    research_context = ""
    if task in ("market_hours_check", "midweek_validation"):
        picks = load_latest_research_file("weekend_picks_")
        if picks:
            research_context += f"\nLATEST WEEKEND RESEARCH (your candidate list + entry/exit targets for this week):\n{picks}\n"
    if task == "market_hours_check":
        midweek = load_latest_research_file("midweek_review_")
        if midweek:
            research_context += f"\nLATEST MIDWEEK REVIEW (hold/cut/redeploy verdicts; act on any flagged cuts):\n{midweek}\n"

    _rm = strategy.get("risk_management", {}) or {}
    _stop_pct = _rm.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    _trail_pct = trailing_stop_pct(strategy)
    if _trail_pct > 0 and not _rm.get("exit_on_ribbon_sell", True):
        _exit_rule = (
            f"HELD positions are exited by the engine's deterministic stops — the hard "
            f"{_stop_pct*100:.0f}% stop and the {_trail_pct*100:.0f}% trailing stop off the "
            f"post-entry peak — NOT by the ribbon flip (let winners run). A ribbon SELL on a "
            f"held name is ADVISORY: sell early only if the THESIS is broken.")
    else:
        _exit_rule = (
            "for a HELD position the SELL state itself triggers the sell — never wait for "
            "an EXIT transition, the cross may have passed on an earlier bar.")
    system = f"""{load_file('skills/skill_0_orchestrator.md')}

CURRENT SKILL ACTIVE:
{skill}

CURRENT STRATEGY (strategy/strategy.json):
{strategy_text}

TRADE LOG (trade_log.json):
{json.dumps(log_for_prompt, indent=2)}
{research_context}{postmortems_section}
EXECUTION RULES:
- MODE = {EXECUTION_MODE.upper()}. In advisory mode you have READ-ONLY tools and
  CANNOT place orders — output proposals only. In live mode you may place/cancel
  orders via robinhood-cli.
- Trade ONLY in Robinhood account {ACCOUNT_NUMBER} (the Agentic cash account).
  Never use the default margin account.
- T+1 settlement: read SETTLED cash before any buy; never deploy unsettled funds.
- Ribbon signal (plain EMA 8/13/21/55 — matches the operator's chart):
  red(55) lowest = BUY, red(55) highest = SELL. ENTER_LONG transition triggers
  buys; {_exit_rule}
- Honor blackout windows + min_confidence_to_trade. Real scoreboard = beat SPY;
  100% monthly is the stretch ceiling, not a reason to oversize risk."""

    user = (
        f"Task: {task}\nTime (ET): {datetime.now(ET):%Y-%m-%d %H:%M} ({datetime.now(ET):%A})\n\n"
        + _format_stop_loss_block(stop_loss_alerts)
        + _format_ema_sell_block(raw_sigs, log)
        + _format_risk_block(raw_sigs, log, strategy)
        + "COMPUTED EMA SIGNALS (authoritative — computed from real closes at the "
        "configured bar interval; use these as the BUY/SELL gate, do NOT eyeball. "
        "'INSUFFICIENT_DATA' = unknown, do not trade that name):\n"
        f"{sig_block}\n\n"
        "1. Read settled cash + positions from the Robinhood MCP (account "
        f"{ACCOUNT_NUMBER}).\n"
        "2. If any STOP-LOSS or SELL SIGNAL alerts appear above, execute those "
        "sells FIRST before any other action.\n"
        "3. Run the active skill. If a valid EMA signal + confidence + settled "
        "cash align, execute the buy/sell via the MCP.\n"
        "4. When you SELL (close a position), report it in actions_taken so the "
        "learning loop fires.\n"
        "5. Note progress vs. the goal and the settlement schedule."
        + FOOTER_INSTRUCTION
    )

    # Stamp + persist before the model runs so the gate survives any downstream
    # exception (process_cycle_state, post-trade pipeline, etc.).
    now_ts = now_iso()
    log["_state"]["last_model_call_ts"] = now_ts
    if task == "market_hours_check":
        log["_state"]["last_execution_call_ts"] = now_ts
    # Remember which ENTER_LONG crossovers the model is being shown this call, so
    # a partial-bar flicker can't re-wake it every cycle (see should_skip_model_call).
    # Stale stamps (>= NEWS_CHECK_HOURS) are pruned; a later re-cross counts as new.
    el_seen = {}
    for sym, ts in log["_state"].get("enter_long_seen", {}).items():
        h = _hours_since(ts)
        if h is not None and h < NEWS_CHECK_HOURS:
            el_seen[sym] = ts
    for sym, s in raw_sigs.items():
        if s.get("ok") and s.get("transition") == "ENTER_LONG":
            el_seen.setdefault(sym, now_ts)
    log["_state"]["enter_long_seen"] = el_seen
    save_trade_log(log)

    # market_hours_check: use Haiku (fast, cheap) — signals are pre-computed, rules are explicit.
    # research_and_prep / midweek_validation: use Opus for deeper reasoning.
    call_model = CHECK_MODEL if task == "market_hours_check" else MODEL
    text, usage = run_model(system, user, mcp=True, model=call_model)

    stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
    print(f"  tokens: in={usage['input_tokens']} out={usage['output_tokens']} cost=${usage['cost_usd']:.4f}")

    # Human-readable run record -> single monthly rolling log (was one
    # research/agent_run_*.md per cycle; 135+ piled up and nothing ever read them).
    append_audit_log("runs", f"Agent run {stamp} — task={task}", text)

    # If the primary model call FAILED (rc≠0 → error string, no footer), claude -p
    # is unavailable this cycle — the broker read and force_sell would fail too, so
    # don't attempt them (no phantom closes), but surface WHY loudly. The session
    # limit (HTTP 429) is the common case and silently no-ops every cycle until it
    # resets — exactly when a pending forced exit can't be placed.
    if text.startswith("(claude -p error") or text.startswith("(error:"):
        session_limited = ("session limit" in text.lower()
                           or "429" in text or "usage limit" in text.lower())
        why = "Claude SESSION/USAGE LIMIT hit" if session_limited else "model call FAILED"
        print(f"[{stamp}] WARNING: {why} — this cycle did NOTHING. {text[:180]}")
        if must_sell:
            print(f"[{stamp}] WARNING: forced exit PENDING for {sorted(must_sell)} "
                  "but Claude is unavailable — positions NOT sold. MANUAL SELL may "
                  "be required until the limit resets / connection recovers.")
            notify_operator(
                f"Trading bot: forced exit BLOCKED — {why}",
                f"{sorted(must_sell)} need to be sold (stop-loss / ribbon SELL) but "
                f"claude -p is unavailable, so NOTHING was sold this cycle. Manual "
                f"sell may be required until it recovers. Detail: {text[:180]}")
        save_trade_log(log)  # preserve the pre-call stamps
        print(f"[{stamp}] task={task} done (model unavailable).")
        return

    # persist midweek/research output to the file the phase routing checks for
    # (the headless model cannot write files itself)
    saved = persist_phase_output(task, text)
    if saved:
        print(f"  phase output saved -> research/{saved}")
    elif task in ("research_and_prep", "midweek_validation") and not (
        text.startswith("(claude -p error") or text.startswith("(error:")
    ):
        # The run completed but produced nothing the phase routing can consume
        # (research: no `### #N — SYMBOL` picks; midweek: file already existed).
        # Fail loudly so a silently-unsaved plan never lets execution run on a
        # stale picks file, and keep the raw output as a fallback so the plan is
        # never lost. (An already-existing file is the normal no-op case below.)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        expected = (f"weekend_picks_{today}.md" if task == "research_and_prep"
                    else f"midweek_review_{today}.md")
        if not os.path.exists(os.path.join(ROOT, "research", expected)):
            fallback = f"unsaved_{task}_{stamp}.md"
            with open(os.path.join(ROOT, "research", fallback), "w") as f:
                f.write(text + "\n")
            print(f"  WARNING: {task} run produced no parseable output for "
                  f"research/{expected} — execution will use the stale picks "
                  f"file. Raw output preserved at research/{fallback}.")

    # ---- AUTHORITATIVE broker reconciliation ----------------------------------
    # NEVER trust the execution turn's self-reported footer for closes. The model
    # has been observed to narrate sells (footer actions_taken + a positions list
    # excluding the name) without ever calling place_equity_order. We re-read the
    # broker independently and treat THAT as ground truth, and we deterministically
    # re-fire any hard forced exit the model failed to place.
    footer = extract_last_json_block(text)
    footer_actions = footer.get("actions_taken", []) if isinstance(footer, dict) else []

    # must_sell was computed pre-turn (stop-loss + held names in ribbon SELL state).
    # Off-hours runs (weekend/pre-market research, off-hours midweek fallback) can't
    # open or close anything — orders don't fill while the market is closed — so the
    # authoritative broker read (an extra Haiku MCP call) is pure waste there. Skip
    # it when the market is closed; the next market-open cycle reconciles.
    exit_info = {}
    market_open = is_market_open()
    if not market_open:
        broker_positions, sell_orders_today = None, set()
        print(f"[{stamp}] off-hours run — skipping broker reconciliation "
              "(market closed; no opens/closes possible).")
    else:
        broker_positions, sell_orders_today = read_broker_state()
    if broker_positions is None and market_open:
        print(f"[{stamp}] WARNING: could not read authoritative broker state — "
              "skipping close detection this cycle (no phantom closes). Any "
              "alerts re-fire next cycle.")
    elif broker_positions is not None:
        # Deterministic forced exits: a hard must-sell still held at the broker,
        # with no working sell order this cycle, gets its own dedicated sell call.
        if must_sell and is_market_open():
            held_at_broker = {p["symbol"]: p for p in broker_positions}
            for sym, reason in must_sell.items():
                if sym not in held_at_broker:
                    continue  # already gone (the main turn actually sold it)
                if sym in sell_orders_today:
                    continue  # a sell order already exists — don't double-sell
                shares = held_at_broker[sym].get("shares")
                print(f"  [FORCED-SELL] {sym} still held after model turn "
                      f"(reason={reason}) — placing dedicated market sell.")
                placed, fill = force_sell(sym, shares, reason)
                exit_info[sym] = {"reason": reason, "price": fill}
                print(f"  [FORCED-SELL] {sym} placed={placed} fill={fill}")
            # re-read so close detection sees the post-forced-sell truth
            reread, _ = read_broker_state()
            if reread is not None:
                broker_positions = reread

        process_cycle_state(log, footer_actions, broker_positions, exit_info)

    # Warn if any hard forced exit is STILL held at the broker after everything.
    if must_sell and broker_positions is not None:
        still = {p["symbol"] for p in broker_positions} & set(must_sell)
        if still:
            print(f"[{stamp}] WARNING: forced exit NOT completed for "
                  f"{sorted(still)} — still held at broker. Will retry next "
                  "cycle; MANUAL SELL may be required.")
            notify_operator(
                "Trading bot: forced exit NOT completed",
                f"{sorted(still)} still held at the broker after BOTH the model "
                f"turn and the dedicated force-sell. The bot will retry next cycle, "
                f"but a MANUAL SELL may be required now.")

    # Phase 3: process any pending strategy rewrites (skill_5). Runs AFTER all
    # broker reconciliation so a rewrite never interferes with close detection or
    # forced exits, and is fully isolated — a rewrite failure must not crash the
    # trading loop.
    try:
        process_strategy_rewrite_queue()
    except Exception as e:
        print(f"  [skill_5] rewrite queue processing failed: {e}")

    # Phase B + B+: shadow (paper) options passes — isolated, read-only, never trade.
    # Same bundle the smart-skip path runs, so paper trading behaves identically
    # whether or not the equity model was called this cycle.
    _run_shadow_passes(raw_sigs, log)

    print(f"[{stamp}] task={task} done.")


def next_market_open():
    """Return the next 9:30 AM ET on a weekday as a timezone-aware datetime."""
    candidate = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    if datetime.now(ET) >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def main():
    print(f"Trading agent (Phase 1+2) via `claude` CLI. "
          f"check_model={CHECK_MODEL} research_model={MODEL} "
          f"account={ACCOUNT_NUMBER} mode={EXECUTION_MODE} "
          f"interval={os.environ.get('SIGNAL_INTERVAL', '1h')}. Ctrl-C to stop.")

    # Phase 3: create skills/history/ and baseline v001 snapshots if not done yet,
    # so the very first skill_5 rewrite has a known-good baseline to roll back to.
    history_dir = os.path.join(ROOT, "skills", "history")
    os.makedirs(history_dir, exist_ok=True)
    for sk in ["skill_0_orchestrator", "skill_1_research", "skill_2_execution",
               "skill_3_midweek", "skill_4_postmortem", "skill_4b_victory",
               "skill_5_strategy_rewriter", "skill_6_pattern_detector"]:
        v001_path = os.path.join(history_dir, f"{sk}_v001.md")
        skill_path = os.path.join(ROOT, "skills", f"{sk}.md")
        if not os.path.exists(v001_path) and os.path.exists(skill_path):
            shutil.copy(skill_path, v001_path)
            print(f"  [baseline] {sk}_v001.md created")

    if not is_market_open():
        now = datetime.now(ET)
        opens = next_market_open()
        research_time = opens - timedelta(minutes=10)
        hours_away = (opens - now).total_seconds() / 3600
        print(f"\nMarket is currently closed. ({now:%A %Y-%m-%d %H:%M} ET)")
        print(f"Next market open: {opens:%A %Y-%m-%d at %H:%M} ET ({hours_away:.1f} hours away)\n")

        print("What would you like to do?")
        print("  r — run research now and exit")
        print(f"  w — wait until {research_time:%H:%M} ET ({opens:%A}) for pre-market research, then trade all week")
        choice = input("\nYour choice (r/w): ").strip().lower()

        if choice == "r":
            print("Running research now...\n")
            run_agent()
            return
        # w (or anything else): fall through — the daily loop below handles it

    # Daily loop: research at 9:20 → trade → close → repeat. Runs until Ctrl-C.
    while True:
        # If market is closed (startup w-path or just closed), sleep until pre-market research.
        if not is_market_open():
            opens = next_market_open()
            research_time = opens - timedelta(minutes=10)
            now = datetime.now(ET)
            wait_secs = max(0, (research_time - now).total_seconds())
            if wait_secs > 0:
                print(
                    f"\nWaiting until {research_time:%A %Y-%m-%d at %H:%M} ET "
                    f"for pre-market research. (Ctrl-C to stop)"
                )
                time.sleep(wait_secs)
            print("Running pre-market research...\n")
            run_agent()
            opens = next_market_open()
            wait_secs = max(0, (opens - datetime.now(ET)).total_seconds())
            if wait_secs > 0:
                print(
                    f"Research done. Waiting until {opens:%H:%M} ET "
                    f"for market open. (Ctrl-C to cancel)"
                )
                time.sleep(wait_secs)
            print("Market is now open. Starting trading loop...\n")

        # Intraday trading loop — clear any stale schedule jobs from the prior day.
        schedule.clear()
        schedule.every(POLL_MINUTES).minutes.do(run_agent)
        run_agent()
        while True:
            if not is_market_open():
                opens = next_market_open()
                print(
                    f"Market just closed. Will run pre-market research at "
                    f"{(opens - timedelta(minutes=10)):%H:%M} ET on "
                    f"{opens:%A %Y-%m-%d}. (Ctrl-C to stop)"
                )
                break
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    main()
