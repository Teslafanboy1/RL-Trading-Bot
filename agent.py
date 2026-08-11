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

# Per-call subprocess ceilings for `claude -p`. These are DEADLINES, not budgets:
# a call that hits one is killed mid-analysis and its work is lost, so each is set
# well above what the call actually takes (2–5 min observed) and sized to the
# window the phase runs in.
#   EXEC     — the market-hours turn. Deliberately the tightest: it runs inside a
#              POLL_MINUTES cycle, so a wedged call must not outlive its cycle.
#   RESEARCH — pre-market research + midweek. Opus with web search over 60+
#              candidates. Bounded by the 08:55 start and the 09:30 bell (35 min),
#              so 30 min is the most it can have without spilling into the open.
#   LEARNING — postmortems, victories, skill_5 rewrites. These run in the 19:35
#              maintenance window with ~5 hours of headroom and are the calls the
#              operator most wants to survive; at the old shared 10-minute default
#              a deep Opus + web-search postmortem could be cut off mid-analysis.
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "600"))
RESEARCH_TIMEOUT = int(os.environ.get("RESEARCH_TIMEOUT", "1800"))
LEARNING_TIMEOUT = int(os.environ.get("LEARNING_TIMEOUT", "1800"))

SOURCES = ["news", "social", "fundamental", "macro", "rss"]
WATCHLIST = [s.strip().upper() for s in os.environ.get("WATCHLIST", "SPY").split(",") if s.strip()]

# robinhood-cli MCP tools (authorized via the `claude` CLI). Read tools are always
# allowed; order-placing tools only when EXECUTION_MODE=live.
# The tool-name prefix depends on how this machine's `claude` CLI registered the
# Robinhood MCP connection: a named global server (`claude mcp add robinhood-cli ...`,
# used on the dev Mac) yields `mcp__robinhood-cli__*`; the account-level claude.ai
# connector flow (what the Oracle VM ended up using) auto-names it from the
# connector's display name instead (e.g. `mcp__claude_ai_Trading__*`). A mismatch
# here doesn't error — it just means every MCP tool call the model attempts gets
# silently permission-denied under headless --permission-mode default (no TTY to
# approve an unlisted tool), which is exactly what happened on the VM from
# 2026-08-02 onward. Override via RH_MCP_SERVER to match whatever `claude mcp list`
# actually shows connected on this machine.
_RH = "mcp__" + os.environ.get("RH_MCP_SERVER", "robinhood-cli") + "__"
RH_READ = [_RH + t for t in ("get_accounts", "get_portfolio", "get_equity_positions",
           "get_equity_orders", "get_equity_quotes", "get_equity_tradability",
           "search", "get_watchlists", "get_watchlist_items", "review_equity_order")]
RH_WRITE = [_RH + "place_equity_order", _RH + "cancel_equity_order"]
# Option READ tools for the Phase B shadow pass (read-only — never place_option_order).
RH_OPTION_READ = [_RH + t for t in ("get_option_chains", "get_option_quotes",
                  "get_option_instruments", "get_option_positions")]
# Research/midweek-only extras (never granted to the execution turn, which has no
# use for them): the "Dynamic MCP scan" + momentum/blackout math skill_1/skill_3
# describe in CLAUDE.md — daily-movers scans, 1w/1m/6m historicals, fundamentals
# for source scoring, and the earnings calendar for the blackout-window check.
# All read-only. Missing until 2026-08-04, research silently fell back to
# WebSearch + the pre-supplied watchlist ribbon only (still correct, just narrow).
RH_RESEARCH_EXTRA = [_RH + t for t in (
    "get_equity_historicals", "get_equity_technical_indicators", "get_equity_fundamentals",
    "get_popular_watchlists", "create_scan", "run_scan", "get_scans",
    "get_scanner_filter_specs", "get_earnings_calendar", "get_earnings_results")]


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
        if "ntfy" in url:
            # ntfy-native publish: plain-text body, title/priority in headers, so
            # the phone notification is readable instead of a raw JSON blob.
            req = urllib.request.Request(
                url, data=body.encode("utf-8"), method="POST",
                headers={"Title": subject.encode("ascii", "ignore").decode(),
                         "Priority": "high",
                         "Content-Type": "text/plain"})
        else:
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

try:
    import rotation_engine  # RX-3 deterministic brain (paper mode until promoted)
except Exception:
    rotation_engine = None

try:
    import risk_guard  # account-level kill-switch + heartbeat (approved 2026-07-06)
except Exception:
    risk_guard = None

try:
    import usage_governor  # Claude 5h-session-window governor (tiers + cooldown)
except Exception:
    usage_governor = None

# Priority tiers for every `claude -p` call. Kept as module constants (rather
# than reaching into usage_governor at each call site) so the agent still runs
# with the governor absent — see run_model().
TIER_PROTECTIVE = 0   # force_sell + the broker read backing a forced exit
TIER_EXECUTION = 1    # market-hours execution turn, routine broker read
TIER_RESEARCH = 2     # pre-market research, midweek review
TIER_LEARNING = 3     # postmortems, victories, skill_5 strategy rewrites
TIER_SHADOW = 4       # paper-options quotes — zero real money at stake

DEFAULT_STOP_LOSS_PCT = 0.10  # hard 10% drawdown limit


# ------------------------------------------------- dashboard control plane
# The TradeCommand dashboard (dashboard/) writes plain files under control/;
# the bot reads them each cycle. All helpers resolve ROOT at call time (tests
# patch agent.ROOT) and never raise — a missing/garbled control file must
# degrade to "no directive", not crash the loop.

def _control_paused():
    """control/PAUSE exists → operator paused the bot from the dashboard:
    no model turns, no new entries; protective exits + bookkeeping still run."""
    return os.path.exists(os.path.join(ROOT, "control", "PAUSE"))


def _do_not_trade():
    """Operator blocklist (control/do_not_trade.json): the bot must never BUY
    or add to these symbols. Selling/closing them stays allowed."""
    try:
        with open(os.path.join(ROOT, "control", "do_not_trade.json")) as f:
            return {str(s).upper() for s in (json.load(f).get("symbols") or []) if s}
    except Exception:
        return set()


def _stop_overrides():
    """Per-symbol stop overrides (control/stop_overrides.json):
    {SYM: {stop_price?, stop_pct?, trail_pct?}}. Consumed by the stop/trailing
    alert checks and mirrored into logs/stops.json for the watchdog."""
    try:
        with open(os.path.join(ROOT, "control", "stop_overrides.json")) as f:
            d = json.load(f)
        return {k.upper(): v for k, v in d.items()
                if isinstance(v, dict) and not k.startswith("_")}
    except Exception:
        return {}


def _manual_lock_active(symbol, ttl=600):
    """True while the dashboard holds a fresh in-flight order lock for symbol
    (control/locks/SYM.manual.lock). force_sell defers that symbol for one
    cycle instead of racing the operator's own order; stale locks are ignored."""
    p = os.path.join(ROOT, "control", "locks", f"{symbol.upper()}.manual.lock")
    try:
        return (time.time() - os.path.getmtime(p)) < ttl
    except OSError:
        return False


def _recent_manual_sells(hours=48):
    """{SYMBOL: ts} of successful manual dashboard sells in the last N hours
    (from logs/manual_actions.jsonl). Used to tag broker-detected closes as
    exit_reason='manual' so they don't fire a bogus postmortem."""
    out = {}
    try:
        with open(os.path.join(ROOT, "logs", "manual_actions.jsonl")) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("action") != "order_place":
                    continue
                params = rec.get("params") or {}
                if str(params.get("side", "")).lower() != "sell":
                    continue
                if (rec.get("result") or {}).get("ok") is False:
                    continue
                h = _hours_since(rec.get("ts"))
                sym = str(params.get("symbol", "")).upper()
                if sym and h is not None and h <= hours:
                    out[sym] = rec.get("ts")
    except Exception:
        pass
    return out


def write_cycle_status(state, task=None, detail=""):
    """logs/cycle_status.json — lets the dashboard warn 'bot cycle running now'
    on its order-confirm screen. Best-effort, never raises."""
    try:
        d = os.path.join(ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cycle_status.json"), "w") as f:
            json.dump({"state": state, "task": task, "detail": detail,
                       "ts": now_iso()}, f, indent=2)
    except Exception:
        pass


def append_equity_point(total, cash=None):
    """Append the broker-confirmed account total to logs/equity_curve.jsonl —
    the dashboard's equity-curve series. Deduped to one point per minute;
    never raises."""
    try:
        if not total:
            return
        path = os.path.join(ROOT, "logs", "equity_curve.jsonl")
        stamp = now_iso()
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 400))
                last = f.read().decode("utf-8", "replace").strip().splitlines()[-1]
            if json.loads(last).get("ts", "")[:16] == stamp[:16]:
                return
        except Exception:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"ts": stamp, "total": round(float(total), 2), "source": "agent"}
        if cash is not None:
            rec["cash"] = round(float(cash), 2)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


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
              read_only=False, allow_write=False, extra_tools=None,
              tier=TIER_EXECUTION):
    """One agent turn via the `claude` CLI. Returns (text, usage_dict).

    usage_dict keys: input_tokens, output_tokens, cost_usd.
    Tools are restricted by --allowedTools: read-only by default; order-placing
    tools only when EXECUTION_MODE=live. The big prompt goes via stdin.

    read_only=True forces RH read tools only (no order placement) regardless of
    EXECUTION_MODE — used for the independent broker-state verification read.
    allow_write=True arms order placement (subject to EXECUTION_MODE=live) for a
    dedicated forced-sell call even outside the main execution turn.

    `tier` is the usage-governor priority (TIER_PROTECTIVE … TIER_SHADOW). Every
    call funnels through here, which is what makes the governor's view of the
    5-hour session window complete: it books each call, refuses low-priority
    work once the window is spent, and — critically — suppresses everything but
    protective sells while a 429 cooldown is active. Before this gate, a session
    limit at 09:45 meant 26 more identical failing calls before the close
    (2026-07-06). A refusal is returned in the same `(error: …)` shape as a CLI
    failure, so every existing caller's error handling covers it unchanged."""
    if usage_governor is not None:
        ok, why = usage_governor.allow(tier)
        if not ok:
            return f"(error: usage-governor deferred this {usage_governor.TIER_NAMES.get(tier, tier)} call — {why})", _EMPTY_USAGE
    tools = []
    if mcp:
        tools += RH_READ
        if EXECUTION_MODE == "live" and (allow_write or not read_only):
            tools += RH_WRITE
    if web:
        tools += ["WebSearch", "WebFetch"]
    if extra_tools:
        tools += list(extra_tools)
    if tools:
        # When several claude.ai connectors share this account (Gmail/Calendar/
        # Drive/Trading — the VM's actual setup), the CLI defers most MCP tools
        # behind ToolSearch to keep the base tool list small. A granted-but-deferred
        # tool is invisible until searched for, and a headless model with no human
        # to approve anything was observed just stalling ("Would you like me to
        # proceed?") instead of resolving that itself — every MCP read silently
        # no-op'd this way from 2026-08-02 onward. Spelling out that it must
        # self-serve via ToolSearch and never wait for approval fixes it.
        system = (
            "You are running fully autonomously and headlessly — there is no "
            "human available to approve or confirm anything. Every tool you were "
            "granted is already pre-authorized: if one is not immediately visible "
            "in your tool list, call ToolSearch yourself (e.g. `select:<tool_name>`) "
            "to load it, then call it directly. Never pause to ask for "
            "confirmation or permission — that question would go unanswered.\n\n"
            + system
        )
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
        # A timeout still burned real session usage — book it.
        _governor_record(tier, None)
        return "(error: claude -p timed out)", _EMPTY_USAGE
    if res.returncode != 0:
        # With --output-format json the CLI reports failures (usage cap, auth,
        # model errors) on STDOUT; stderr is often empty. Capture both so the
        # run record shows WHY it failed instead of a blank "rc=1:".
        detail = (res.stderr or "").strip() or (res.stdout or "").strip()
        err = f"(claude -p error rc={res.returncode}: {detail[:500]})"
        # A 429 payload carries the exact reset time ("resets 10:50pm
        # (Asia/Calcutta)") — the only ground truth we ever get about the real
        # window boundary, including usage the OPERATOR burned in their own
        # Claude sessions. Hand it to the governor so the rest of the session
        # stops re-firing into a wall.
        if usage_governor is not None and usage_governor.is_limit_error(err):
            until = usage_governor.note_limit(err)
            if until:
                print(f"  [USAGE] session limit hit — pausing non-protective "
                      f"model calls until {until:%H:%M} ET.")
        else:
            _governor_record(tier, None)
        return err, _EMPTY_USAGE
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
    _governor_record(tier, usage)
    # A successful call proves the limit lifted — drop any stale cooldown so the
    # protective probe that got through immediately re-opens the other tiers.
    if usage_governor is not None and usage_governor.cooldown_until():
        usage_governor.clear_cooldown()
    return text, usage


def _governor_record(tier, usage):
    """Book a completed call against the current 5h window. Never raises."""
    if usage_governor is None:
        return
    try:
        usage_governor.record(tier, usage)
    except Exception:
        pass


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


def watchlist_confirm_symbols():
    """Symbols from the latest weekend_picks_*.md '## WATCHLIST CONFIRM' block —
    names research flagged as worth tracking but didn't rank as a '### #N' pick
    (e.g. momentum leaders it couldn't independently confirm a ribbon for). Without
    this, those names got no EMA computed for the rest of the day despite the
    research file explicitly saying they'd be "confirmed at execution" (2026-08-04:
    MU/NVDA/AVGO/ANET flagged this way, never actually checked again). Hard
    contract, same pattern as momentum_options_watch(): one comma-separated line
    after the exact heading. Returns [] if the file or block is absent, or the
    line is the literal '(none)' placeholder."""
    try:
        text = load_latest_research_file("weekend_picks_") or ""
    except Exception:
        return []
    if not text:
        return []
    m = re.search(r"##\s*WATCHLIST CONFIRM\s*\n-\s*(.+)", text)
    if not m:
        return []
    line = m.group(1).strip()
    if not line or line.lower().startswith("(none"):
        return []
    return [s.strip().upper() for s in line.split(",") if s.strip()]


def watchlist_symbols(log):
    """SPY + open positions + WATCHLIST env + latest weekend picks (ranked +
    WATCHLIST CONFIRM), de-duped."""
    syms = (["SPY"] + [p["symbol"] for p in log.get("open_positions", [])]
            + WATCHLIST + weekend_pick_symbols() + watchlist_confirm_symbols())
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
    tighter stop than the base 10%. An operator stop override from the
    dashboard (control/stop_overrides.json: stop_pct or absolute stop_price)
    replaces the computed threshold for that symbol."""
    if not signals:
        return []
    strategy = load_strategy()
    overrides = _stop_overrides()
    alerts = []
    for pos in log.get("open_positions", []):
        symbol = pos["symbol"]
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            continue
        ov = overrides.get(symbol, {})
        pct = effective_stop_pct(symbol, strategy)
        try:
            if ov.get("stop_pct"):
                pct = float(ov["stop_pct"])
        except (TypeError, ValueError):
            pass
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
        thr = None
        try:
            if ov.get("stop_price"):
                thr = float(ov["stop_price"])
        except (TypeError, ValueError):
            thr = None
        triggered = (last <= thr) if thr else (loss_pct <= -pct)
        if triggered:
            alerts.append({
                "symbol": symbol,
                "entry_price": entry,
                "threshold_price": round(thr if thr else entry * (1 - pct), 4),
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
    falls back to signals.signal_for() per symbol, exactly like the hard-stop check.
    A dashboard trail_pct override (control/stop_overrides.json) replaces the
    global fraction for that symbol — and can enable trailing on one name even
    when the global default is off."""
    if not signals:
        return []
    trail_default = trailing_stop_pct(load_strategy())
    overrides = _stop_overrides()
    if trail_default <= 0 and not any(v.get("trail_pct") for v in overrides.values()):
        return []
    prices = prices or {}
    alerts = []
    for pos in log.get("open_positions", []):
        symbol = pos["symbol"]
        trail = trail_default
        try:
            if overrides.get(symbol, {}).get("trail_pct"):
                trail = float(overrides[symbol]["trail_pct"])
        except (TypeError, ValueError):
            pass
        if trail <= 0:
            continue
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
    # Operator do-not-trade list: a blocked symbol's BUY opportunity must not
    # wake the model (there is nothing it may do with it). Sell-side wakes on
    # held names are unaffected — closing a blocked symbol stays allowed.
    dnt = _do_not_trade()
    for sym, s in raw_sigs.items():
        if not s.get("ok"):
            # unknown signal — let the model decide
            return False, f"unknown_signal:{sym}"
        t = s.get("transition", "NO_ACTION")
        if t == "ENTER_LONG" and sym not in dnt:
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

    # Collect weekend picks (ranked + WATCHLIST CONFIRM) that are in BUY zone,
    # not yet held, and not blocked. WATCHLIST CONFIRM names matter here too — a
    # name research couldn't independently rank may already be sitting in BUY
    # state (an established uptrend, not a fresh crossover), which only this
    # periodic gate — not the ENTER_LONG edge check above — would ever catch.
    picks = {s.upper() for s in weekend_pick_symbols()} | {s.upper() for s in watchlist_confirm_symbols()}
    pending_buys = [sym for sym in picks - held - dnt
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
    entry price/confidence/sources/thesis).

    Scaling INTO an already-held name (the let-the-winners-run "scale into
    winners" path) is a real add, not a no-op: we blend the cost basis, sum the
    shares, and keep the higher peak so the deterministic stop-loss / trailing /
    P&L machinery (which all read entry_price, shares, peak_price from the trade
    log) stays in sync with the broker. Treating an add as "already tracked" and
    returning — the old behavior — left the log pinned to the original tranche,
    so a 0.0167-share CAT topped up to 4x size still reported the original cost,
    stop, and P&L. The hard stop after a blend is -10% of the BLENDED avg cost,
    which is the correct reference for an averaged-up position."""
    symbol = action["symbol"]
    add_price = float(action.get("price") or 0)
    add_shares = float(action.get("shares") or 0)
    existing = next((p for p in log["open_positions"] if p["symbol"] == symbol), None)
    if existing is not None:
        old_shares = float(existing.get("shares") or 0)
        old_entry = float(existing.get("entry_price") or 0)
        new_shares = old_shares + add_shares
        if new_shares > 0 and add_shares > 0:
            blended = (old_shares * old_entry + add_shares * add_price) / new_shares
            existing["entry_price"] = round(blended, 4)
            existing["shares"] = new_shares
            existing["dollar_amount"] = round(blended * new_shares, 2)
            # never lower the trailing-stop high-water mark; raise it if we added
            # at a fresh high so a later giveback is measured from the true peak
            existing["peak_price"] = max(float(existing.get("peak_price") or 0),
                                         add_price, blended)
            # latest conviction wins if the add carried a (re-scored) confidence
            if action.get("confidence"):
                existing["confidence_score"] = action["confidence"]
            existing.setdefault("scaled_in", []).append(
                {"date": now_iso(), "price": add_price, "shares": add_shares})
        return
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


def log_trade_outcome(log, open_pos, exit_price, exit_date, stop_loss=False,
                      exit_reason=None, skip_pipeline=False):
    """Close a position: compute P&L, append to trades, update summary, fire the
    post-trade analysis pipeline. Returns the closed trade dict.

    skip_pipeline=True records the close but does NOT fire the postmortem/
    victory + rewrite-queue pipeline — used for exit_reason='manual' (an
    operator dashboard sell is not a bot decision, so it must not teach the
    bot anything or spend an Opus analysis call). Monthly progress still
    updates so the summary stays truthful."""
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
    if exit_reason:
        trade["exit_reason"] = exit_reason

    log["trades"].append(trade)
    s = log["summary"]
    s["total_trades"] += 1
    s["wins" if outcome == "WIN" else "losses"] += 1
    s["total_pnl"] = round(s["total_pnl"] + trade["pnl_dollar"], 4)
    s["win_rate"] = round(s["wins"] / s["total_trades"], 4)
    log["open_positions"] = [p for p in log["open_positions"] if p["id"] != open_pos["id"]]
    save_trade_log(log)

    if skip_pipeline:
        update_monthly_progress(log)
        return trade

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
    text, _usage = run_model(system, user, web=True, tier=TIER_LEARNING,
                             timeout=LEARNING_TIMEOUT)

    # A failed call must NOT consume the queue entry. Without this guard the
    # error string itself ("(error: claude -p timed out)", a governor deferral, a
    # 429) was written to postmortems/postmortem_NNN.md and returned as a
    # perfectly good filename — the trade was then flagged postmortem_filed and
    # the analysis was lost for good. skill_5 has always checked this; the
    # analysis engines never did. Return empty so the caller re-queues instead.
    if text.startswith("(claude -p error") or text.startswith("(error:"):
        print(f"  [{kind}] {trade.get('symbol')} analysis call failed: "
              f"{text[:120]} — leaving it queued to retry.")
        return {}, None

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


ANALYSIS_QUEUE = os.path.join("logs", "analysis_queue.jsonl")


def enqueue_trade_analysis(trade):
    """Defer a close's postmortem/victory analysis to the maintenance window.

    The analysis call is the most expensive thing the bot does — Opus WITH web
    search — and it used to fire inline, the instant a position closed, which is
    by definition during market hours. So the single event most likely to be
    followed by more trading (a stop-loss cascade, a rotation) was also the event
    that dumped the biggest call of the day into the middle of the execution
    window. Queue it instead; `drain_analysis_queue()` runs it after the close in
    a session window of its own. Nothing about the trade record is lost — the
    close itself, P&L, and the summary are all written synchronously as before."""
    try:
        path = os.path.join(ROOT, ANALYSIS_QUEUE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({
                "trade_id": trade.get("id"),
                "symbol": trade.get("symbol"),
                "outcome": trade.get("outcome"),
                "queued_at": now_iso(),
            }) + "\n")
        return True
    except Exception as e:
        print(f"  [analysis-queue] enqueue failed for {trade.get('id')}: {e}")
        return False


def pending_trade_analyses():
    """Queued trade ids awaiting a postmortem/victory, oldest first."""
    path = os.path.join(ROOT, ANALYSIS_QUEUE)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("trade_id") and not e.get("done"):
                    out.append(e)
    except Exception:
        return []
    return out


def _mark_analysis_done(trade_id):
    """Rewrite the queue with `trade_id` marked done (idempotent, never raises)."""
    path = os.path.join(ROOT, ANALYSIS_QUEUE)
    try:
        with open(path) as f:
            lines = f.readlines()
        out = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except json.JSONDecodeError:
                out.append(s)
                continue
            if e.get("trade_id") == trade_id:
                e["done"] = now_iso()
            out.append(json.dumps(e))
        with open(path, "w") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        return True
    except Exception:
        return False


def _analyze_trade(log, trade):
    """Run the analysis engine for one closed trade and fold the result back in.

    This is the body that used to live inline in run_post_trade_pipeline; it is
    now callable either immediately (off-hours close) or from the maintenance
    drain."""
    if trade["outcome"] == "LOSS":
        verdicts, fname = trigger_postmortem(trade)
    else:
        verdicts, fname = trigger_victory_analysis(trade)

    # No file => the model call failed. Leave the trade unflagged so the drain
    # retries it next window instead of recording a postmortem that never ran.
    if not fname:
        return None

    if trade["outcome"] == "LOSS":
        trade["postmortem_filed"] = True
    else:
        trade["victory_filed"] = True
    trade["analysis_file"] = fname

    # persist the analysis flags onto the stored trade
    for t in log["trades"]:
        if t["id"] == trade["id"]:
            t.update({"postmortem_filed": trade.get("postmortem_filed", False),
                      "victory_filed": trade.get("victory_filed", False),
                      "analysis_file": fname})
    save_trade_log(log)
    update_source_weights(trade, verdicts)
    return fname


def drain_analysis_queue(limit=None):
    """Run the deferred postmortems/victories. Returns the number completed.

    Bounded per drain so a day with many closes can't empty a fresh window in
    one go, and fully isolated — an analysis failure must never break the loop.
    An entry whose model call was refused (governor cooldown or budget) is left
    in the queue for the next drain rather than being marked done."""
    cfg = usage_governor.config() if usage_governor else {}
    limit = limit if limit is not None else cfg.get("max_analyses_per_drain", 4)
    pending = pending_trade_analyses()
    if not pending:
        return 0
    log = load_trade_log()
    by_id = {t.get("id"): t for t in log.get("trades", [])}
    done = 0
    for entry in pending[:limit]:
        tid = entry["trade_id"]
        trade = by_id.get(tid)
        if trade is None:
            print(f"  [analysis-queue] {tid} not found in trade log — dropping.")
            _mark_analysis_done(tid)
            continue
        if trade.get("postmortem_filed") or trade.get("victory_filed"):
            _mark_analysis_done(tid)
            continue
        try:
            print(f"  [analysis-queue] running {trade['outcome']} analysis for "
                  f"{tid} ({trade.get('symbol')})...")
            fname = _analyze_trade(log, trade)
            if not fname:
                print(f"  [analysis-queue] {tid} produced no file — will retry "
                      "next drain.")
                break
            _mark_analysis_done(tid)
            done += 1
        except Exception as e:
            print(f"  [analysis-queue] {tid} failed: {e} — will retry next drain.")
            break
    if done:
        update_monthly_progress(log)
    return done


def run_post_trade_pipeline(log, trade, *, defer=None):
    """The full Phase 2 reaction to a close — fires automatically.

    The cheap, local parts (monthly progress, queueing the strategy rewrite) run
    immediately. The expensive analysis call is DEFERRED to the maintenance
    window whenever the market is open, so a close never spends execution-window
    budget on retrospection. Pass defer=False to force it inline (used by the
    maintenance drain itself and by tests)."""
    if defer is None:
        defer = is_market_open()

    update_monthly_progress(log)
    flag_strategy_rewrite(trade)

    if defer:
        enqueue_trade_analysis(trade)
        print(f"  [analysis-queue] {trade['id']} ({trade.get('symbol')}) queued — "
              "analysis runs after the close, not against execution budget.")
        return

    # Off-hours close: analyse inline. If that call fails (timeout, 429, governor
    # deferral) the trade would otherwise never be analysed at all — there is no
    # queue entry to retry from — so fall back to queueing it for the next drain.
    if _analyze_trade(log, trade) is None:
        enqueue_trade_analysis(trade)
        print(f"  [analysis-queue] {trade['id']} ({trade.get('symbol')}) inline "
              "analysis failed — queued for the next maintenance drain.")
        return
    update_monthly_progress(log)


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

    Returns True when an entry was consumed (so a caller draining a backlog knows
    to come back for the next one) and False when the queue is empty, the entry
    was unusable, or the model call failed.

    Called from run_maintenance() after the close — NOT during market hours: it
    is a full-Opus call carrying strategy.json plus all eight skill files, and
    nothing about a rule change is time-critical.
    Processes AT MOST ONE entry per cycle so one bad rewrite can't block the loop.
    Skips gracefully if the file is missing; never raises (the caller also wraps
    this in try/except — a rewrite failure must never crash the trading loop)."""
    queue_path = os.path.join(ROOT, "research", "strategy_rewrite_queue.md")
    if not os.path.exists(queue_path):
        return False

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
        return False  # all processed

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
            return True  # an entry WAS consumed (skipped) — keep draining

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

        text, _usage = run_model(skill5, user, web=False, tier=TIER_LEARNING,
                                 timeout=LEARNING_TIMEOUT)

        if text.startswith("(claude -p error") or text.startswith("(error:"):
            print(f"  [skill_5] model call failed: {text[:100]}")
            return False

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
        return True

    except Exception as e:
        print(f"  [skill_5] ERROR processing rewrite queue: {e} — skipping this entry")
        return False


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
    elif task == "research_and_prep" and (
        re.search(r"^###\s+#\d+\s+—\s+\w+", text, re.M)
        or re.search(r"^##\s+RANKED PICKS:\s*NONE\b", text, re.M)
    ):
        # Persist research that either contains ranked picks OR explicitly declares
        # no qualifying picks (the `## RANKED PICKS: NONE` marker skill_1 emits in a
        # risk-off tape). The no-pick case MUST still be saved: otherwise the day's
        # research is dropped and execution silently inherits the prior day's
        # weekend_picks_*.md — a stale watchlist that can list names which have since
        # flipped to SELL (the 2026-06-19 NVDA/AVGO bug). Saving an empty-picks file
        # makes weekend_pick_symbols() return the *current* (possibly empty) watchlist
        # instead of a days-old one. Genuinely malformed output (no picks AND no
        # marker) still falls through to the unsaved_*.md + WARNING path in run_agent.
        fname = f"weekend_picks_{today}.md"
    else:
        return None
    path = os.path.join(ROOT, "research", fname)
    if os.path.exists(path):
        return None  # never clobber an existing review/picks file
    with open(path, "w") as f:
        f.write(text + "\n")
    return fname


def read_broker_state(tier=TIER_EXECUTION):
    """Independent, read-only broker snapshot via a dedicated `claude -p` call —
    the AUTHORITATIVE ground truth for close detection. The main execution turn's
    self-reported footer cannot be trusted: on 2026-06-12 the model reported CLOV
    and AI sold (cash + a positions list excluding them) while the broker showed
    both still held at full size and ZERO orders placed. Trusting that footer
    phantom-closed both positions and fired two bogus postmortems.

    Returns (positions, sell_symbols_today, account_total):
      positions          — list of {symbol, shares, avg_price, last_price}, or
                           None if the read itself failed (caller must then NOT
                           treat anything as closed — a failed read is unknown,
                           not "flat").
      sell_symbols_today — set of symbols with a SELL order placed today in any
                           state (used to avoid double-selling: don't force-sell a
                           name the main turn already has a working order for).
      account_total      — the broker's real total account value (get_portfolio),
                           or None. Feeds the monthly-drawdown kill-switch and the
                           deposit-aware equity rebase (the $255-vs-$395 sizing
                           drift found in the 2026-07-06 audit)."""
    system = ("You are a read-only Robinhood query tool. Use only the MCP read "
              "tools. Do not place, modify, or cancel any order.")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    user = (
        f"For Robinhood account {ACCOUNT_NUMBER}:\n"
        "1. Call get_equity_positions and list every currently held position.\n"
        f"2. Call get_equity_orders (created_at_gte={today}) and note which symbols "
        "have a SELL-side order placed today (any state).\n"
        "3. Call get_portfolio and read total_value (the account's total value).\n"
        "Output ONLY one fenced ```json block, no prose:\n"
        '{"positions": [{"symbol": "X", "shares": <float>, "avg_price": <float>, '
        '"last_price": <float|null>}], "sell_orders_today": ["SYM", ...], '
        '"account_total": <float|null>}\n'
        "shares = quantity held (use 0 only if truly flat). If no positions, use []."
    )
    text, _ = run_model(system, user, mcp=True, read_only=True, model=CHECK_MODEL,
                        timeout=240, tier=tier)
    block = extract_last_json_block(text)
    if not (block and isinstance(block, dict) and isinstance(block.get("positions"), list)):
        return None, set(), None
    positions = [p for p in block["positions"]
                 if p.get("symbol") and float(p.get("shares") or 0) > 0]
    sells = {s.upper() for s in (block.get("sell_orders_today") or []) if isinstance(s, str)}
    try:
        total = float(block.get("account_total") or 0) or None
    except (TypeError, ValueError):
        total = None
    return positions, sells, total


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
                        timeout=240, tier=TIER_PROTECTIVE)
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
                        extra_tools=RH_OPTION_READ, model=CHECK_MODEL, timeout=240,
                        tier=TIER_SHADOW)
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
                        extra_tools=RH_OPTION_READ, model=CHECK_MODEL, timeout=180,
                        tier=TIER_SHADOW)
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
    try:
        process_rx3_paper(log)
    except Exception as e:
        print(f"  [rx3] paper pass failed: {e}")
    try:
        process_rx4_paper(log)
    except Exception as e:
        print(f"  [rx4] paper pass failed: {e}")


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
    manual_sells = _recent_manual_sells()
    for op in detect_closed_positions(log["open_positions"], broker_positions):
        sym = op["symbol"]
        info = exit_info.get(sym, {})
        a = sell_actions.get(sym, {})
        reason = info.get("reason") or str(a.get("reason", "")).lower()
        # A close with no bot-side reason that matches a journaled dashboard
        # sell is the OPERATOR's trade: record it tagged 'manual' and skip the
        # learning pipeline (not a bot decision — nothing to postmortem).
        if not reason and sym in manual_sells:
            reason = "manual"
        is_stop_loss = reason == "stop_loss"
        price = (info.get("price") or a.get("price")
                 or last_by_sym.get(sym) or prev_last.get(sym) or op["entry_price"])
        log_trade_outcome(log, op, price, now, stop_loss=is_stop_loss,
                          exit_reason=reason or None,
                          skip_pipeline=(reason == "manual"))

    # 3. snapshot the authoritative broker positions for next cycle's diff.
    log["_state"]["last_positions"] = broker_positions
    save_trade_log(log)


def sync_account_equity(log, broker_total):
    """Deposit-aware equity sync off the REAL broker total (2026-07-06 audit fix).

    If the broker total differs from the tracked current_value by enough to be
    an external deposit/withdrawal (risk_guard.detect_deposit thresholds), the
    delta is folded into month_start_value AND current_value so (a) deposits are
    never counted as trading gains and (b) the risk-sizing denominator tracks
    the real account instead of a stale manual rebase. Mirrors the operator's
    manual 2026-06-24 rebase, automatically. The same delta also rebases
    risk_guard's kill-switch peak (risk_guard.rebase_peak) — before this, a
    withdrawal only adjusted month_start_value, so the kill-switch's separate
    peak tracker still saw the withdrawal as a straight equity drop and could
    trip a false-positive halt (2026-07-22: a ~$100-120 withdrawal read as a
    30.5% drawdown). Never raises."""
    try:
        if not (risk_guard and broker_total):
            return
        s = log.get("summary", {})
        delta = risk_guard.detect_deposit(broker_total, s.get("current_value"))
        if not delta:
            return
        s["month_start_value"] = round(float(s.get("month_start_value") or 0) + delta, 4)
        s["current_value"] = round(float(s.get("current_value") or 0) + delta, 4)
        strategy = load_strategy()
        pt = strategy.get("progress_tracking")
        if pt:
            pt["month_start_value"] = s["month_start_value"]
            pt["current_value"] = s["current_value"]
            save_strategy(strategy)
        save_trade_log(log)
        risk_guard.rebase_peak(delta)
        print(f"  [equity-sync] external deposit/withdrawal detected: {delta:+.2f} "
              f"(broker total {broker_total:.2f}) — month_start_value and "
              "kill-switch peak rebased, not counted as trading P&L.")
    except Exception as e:
        print(f"  [equity-sync] skipped (non-fatal): {e}")


def process_manual_cash_flows(log):
    """Apply operator-declared deposits/withdrawals (control/cash_flows.json,
    written by the dashboard's PIN-armed /api/cash_flow endpoint) to
    month_start_value / current_value, the same bookkeeping sync_account_equity
    does automatically off a broker-total diff — except keyed to an EXACT
    dollar amount the operator typed in, not an inferred diff.

    Why this exists (2026-08-03): sync_account_equity's detect_deposit only
    fires when a broker-total diff clears $25/5% AND a broker read actually
    happens (off-hours cycles skip the broker read entirely, and a single bad
    self-reported broker total can misattribute the diff — see the 2026-08-02
    false-halt incident). A manual declaration needs no broker call, applies
    the exact amount the operator confirms, and runs every non-halted cycle
    regardless of market hours, closing both gaps.

    Each control/cash_flows.json entry is {amount, note, ts, applied}; only
    un-applied entries are processed, then marked applied in place so a
    replay never double-counts. Never raises."""
    try:
        path = os.path.join(ROOT, "control", "cash_flows.json")
        try:
            with open(path) as f:
                flows = json.load(f)
        except Exception:
            return
        if not isinstance(flows, list):
            return
        changed = False
        for entry in flows:
            if not isinstance(entry, dict) or entry.get("applied"):
                continue
            try:
                delta = round(float(entry["amount"]), 2)
            except (KeyError, TypeError, ValueError):
                continue
            if not delta:
                entry["applied"] = True
                entry["applied_ts"] = now_iso()
                changed = True
                continue
            s = log.setdefault("summary", {})
            s["month_start_value"] = round(float(s.get("month_start_value") or 0) + delta, 4)
            s["current_value"] = round(float(s.get("current_value") or 0) + delta, 4)
            strategy = load_strategy()
            pt = strategy.get("progress_tracking")
            if pt:
                pt["month_start_value"] = s["month_start_value"]
                pt["current_value"] = s["current_value"]
                save_strategy(strategy)
            if risk_guard:
                risk_guard.rebase_peak(delta)
            entry["applied"] = True
            entry["applied_ts"] = now_iso()
            changed = True
            print(f"  [cash-flow] applied operator-declared {delta:+.2f} "
                  f"({entry.get('note') or 'no note'}) — month_start_value and "
                  "current_value rebased, not counted as trading P&L.")
        if changed:
            save_trade_log(log)
            update_monthly_progress(log)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(flows, f, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"  [cash-flow] skipped (non-fatal): {e}")


def write_stop_snapshot(log, strategy):
    """logs/stops.json — the per-position stop levels for the INDEPENDENT
    watchdog (watchdog.sh), which prices them off Yahoo and alerts the operator
    if a stop is breached while the bot may be dead. This file is the only
    stop-defense that survives the process dying (fractional positions cannot
    carry broker-side GTC stop orders on Robinhood). Never raises."""
    try:
        snap = {}
        trail = trailing_stop_pct(strategy)
        overrides = _stop_overrides()
        for p in log.get("open_positions", []):
            entry = float(p.get("entry_price") or 0)
            if entry <= 0:
                continue
            ov = overrides.get(p["symbol"], {})
            stop = entry * (1 - effective_stop_pct(p["symbol"], strategy))
            t = trail
            try:
                # dashboard overrides — keep the watchdog's view identical to
                # what check_stop_loss_alerts / check_trailing_stop_alerts fire on
                if ov.get("stop_pct"):
                    stop = entry * (1 - float(ov["stop_pct"]))
                if ov.get("stop_price"):
                    stop = float(ov["stop_price"])
                if ov.get("trail_pct"):
                    t = float(ov["trail_pct"])
            except (TypeError, ValueError):
                pass
            peak = float(p.get("peak_price") or entry)
            snap[p["symbol"]] = {
                "stop": round(stop, 4),
                "trail_stop": round(peak * (1 - t), 4) if t > 0 else None,
                "shares": p.get("shares"),
            }
        d = os.path.join(ROOT, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stops.json"), "w") as f:
            json.dump({"updated": now_iso(), "stops": snap}, f, indent=2)
    except Exception:
        pass


# ---------------- RX-3 paper track (approved 2026-07-06; promotion gate #1) ----
RX3_STATE_PATH = os.path.join(ROOT, "shadow", "rx3_paper.json")
RX4_STATE_PATH = os.path.join(ROOT, "shadow", "rx4_paper.json")


def _rx3_universe(strategy):
    r = (strategy.get("rotation") or {})
    return r.get("universe") or [
        "NVDA", "AMD", "AVGO", "MU", "AMAT", "SMCI", "MRVL", "TSM",
        "PLTR", "COIN", "MSTR", "HOOD", "SOFI", "SNOW", "CRWD", "NET", "DDOG",
        "META", "TSLA", "SHOP", "RBLX",
        "TQQQ", "SOXL", "FNGU", "TECL", "LABU", "FAS", "TNA",
    ]


def _rx3_fetch_closes(symbols):
    """Daily closes (~2y) per symbol via the same Yahoo layer signals uses.
    Zero model tokens. Missing symbols are simply absent from the result."""
    out = {}
    for s in symbols:
        try:
            c = signals._fetch_yahoo(s, "1d", "2y")
        except Exception:
            c = None
        if c and len(c) > 30:
            out[s] = c
    return out


def process_rx3_paper(log):
    """Daily RX-3 paper pass — the approved strategy running with ZERO real
    dollars while it builds the promotion record (2 weeks paper -> half size ->
    full; see research/redesign_proposal_2026-07-06.md).

    Once per market day: fetch daily closes (Yahoo, no model tokens), make the
    day's decision from closes THROUGH YESTERDAY (the engine's lag discipline —
    the last Yahoo daily bar during market hours is today's partial, so it is
    dropped for decisions and used only to MARK the paper book), rebalance the
    paper portfolio to the engine's target book at latest prices with 5bps/side,
    and append the equity curve to shadow/rx3_paper.json. Isolated + never
    raises into the loop (same contract as the options shadow passes)."""
    if not (rotation_engine and signals):
        return
    if not is_market_open():
        return
    strategy = load_strategy()
    rcfg = strategy.get("rotation") or {}
    if rcfg.get("paper_enabled") is False:
        return
    st = log.setdefault("_state", {})
    h = _hours_since(st.get("last_rx3_ts"))
    if h is not None and h < 20:
        return
    st["last_rx3_ts"] = now_iso()
    save_trade_log(log)

    universe = _rx3_universe(strategy)
    comp_syms = [s for pair in rotation_engine.RISKX_COMPONENTS for s in pair if s]
    def_syms = list(rotation_engine.DEFENSIVE_ASSETS)
    closes = _rx3_fetch_closes(sorted(set(universe + comp_syms + def_syms)))
    if len([s for s in universe if s in closes]) < 5:
        print("  [rx3] data fetch too thin — skipping today's paper pass")
        return

    hist = lambda s: closes[s][:-1]          # decisions: through yesterday only
    marks = {s: closes[s][-1] for s in closes}

    paper = load_json("shadow/rx3_paper.json", None) or {
        "_note": "RX-3 paper track record (approved 2026-07-06). No real money. "
                 "Promotion gate: >=10 trading days, decisions match engine, "
                 "behavior consistent with backtest envelope.",
        "start_date": now_iso(), "start_equity": 100.0,
        "cash": 100.0, "positions": {}, "leaders": [],
        "sleeve_rets": [], "history": [],
    }

    # 1) raw sleeve return from yesterday's leaders (vol-throttle input)
    prev_leaders = paper.get("leaders") or []
    rets = []
    for sym in prev_leaders:
        c = closes.get(sym)
        if c and len(c) >= 2 and c[-2] > 0:
            rets.append(c[-1] / c[-2] - 1)
    if rets:
        paper["sleeve_rets"] = (paper.get("sleeve_rets", [])
                                + [sum(rets) / len(rets)])[-60:]

    # 2) the day's target book (pure engine call)
    tb = rotation_engine.target_book(
        {s: hist(s) for s in universe if s in closes},
        {s: hist(s) for s in comp_syms if s in closes},
        {s: hist(s) for s in def_syms if s in closes},
        paper.get("sleeve_rets", []),
        held=prev_leaders,
        sector_of=lambda s: sector_for(s, strategy),
        leverage_factor=lambda s: leverage_factor(s, strategy))
    targets = {**tb["weights"], **tb["defensive"]}

    # 3) mark + rebalance the paper book at latest prices, 5bps/side
    positions = paper.get("positions", {})
    cash = float(paper.get("cash") or 0)
    equity = cash + sum(float(p.get("shares") or 0) * marks.get(s, float(p.get("last_px") or 0))
                        for s, p in positions.items())
    if equity <= 0:
        equity = paper.get("start_equity", 100.0)
    for sym in sorted(set(list(positions) + list(targets))):
        px = marks.get(sym)
        if not px or px <= 0:
            continue
        cur_val = float(positions.get(sym, {}).get("shares") or 0) * px
        tgt_val = targets.get(sym, 0.0) * equity
        delta = tgt_val - cur_val
        if abs(delta) < 0.005 * equity:      # rebalance band: skip dust trades
            if sym in positions:
                positions[sym]["last_px"] = px
            continue
        cash -= delta + abs(delta) * 0.0005  # 5bps per side
        if tgt_val <= 0:
            positions.pop(sym, None)
        else:
            positions[sym] = {"shares": round(tgt_val / px, 6), "last_px": px}
    equity = cash + sum(float(p["shares"]) * marks.get(s, float(p.get("last_px") or 0))
                        for s, p in positions.items())

    paper.update({"cash": round(cash, 4), "positions": positions,
                  "leaders": tb["leaders"], "equity": round(equity, 4),
                  "last_run": now_iso()})
    paper.setdefault("history", []).append({
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "equity": round(equity, 4), "leaders": tb["leaders"],
        "defensive": list(tb["defensive"]), "riskx": tb["riskx"],
        "vol_scale": tb["vol_scale"], "cash_w": tb["cash"]})
    os.makedirs(os.path.dirname(RX3_STATE_PATH), exist_ok=True)
    save_json("shadow/rx3_paper.json", paper)
    ret_pct = (equity / paper.get("start_equity", 100.0) - 1) * 100
    print(f"  [rx3] paper: equity {equity:.2f} ({ret_pct:+.2f}% since start) | "
          f"leaders={tb['leaders']} defensive={list(tb['defensive'])} "
          f"riskx={tb['riskx']} vol_scale={tb['vol_scale']} cash={tb['cash']}")
    append_audit_log("rx3", f"RX-3 paper {datetime.now(ET):%Y-%m-%d}",
                     json.dumps(paper["history"][-1], indent=2))


def process_rx4_paper(log):
    """RX-4 (operator-directed 2026-08-04): the exact same rotation_engine.py
    brain as RX-3, run with full_deploy=True (always 100% invested, no RISKX
    defensive carve-out, no vol-throttle — 'turn up the volume, use all the
    money'). top_n is a live config knob (strategy.json -> rotation_rx4.top_n,
    default here matches RX-3's 2): briefly widened to 6 the same day after
    operator pushback ('use my whole portfolio' was not a request to put it
    all on 2 names), backtested both ways, then reverted to 2 after top_n=6
    gave back most of the extra return for a smoother ride — see strategy.json
    rotation_rx4._note for the numbers. Paper-only, ZERO real dollars,
    NOT on any promotion ladder to live. Isolated + never raises into the
    loop, same contract as process_rx3_paper / the options shadow passes."""
    if not (rotation_engine and signals):
        return
    if not is_market_open():
        return
    strategy = load_strategy()
    rcfg = strategy.get("rotation_rx4") or {}
    if rcfg.get("paper_enabled") is False:
        return
    st = log.setdefault("_state", {})
    h = _hours_since(st.get("last_rx4_ts"))
    if h is not None and h < 20:
        return
    st["last_rx4_ts"] = now_iso()
    save_trade_log(log)

    universe = rcfg.get("universe") or _rx3_universe(strategy)
    comp_syms = [s for pair in rotation_engine.RISKX_COMPONENTS for s in pair if s]
    def_syms = list(rotation_engine.DEFENSIVE_ASSETS)
    closes = _rx3_fetch_closes(sorted(set(universe + comp_syms + def_syms)))
    if len([s for s in universe if s in closes]) < 5:
        print("  [rx4] data fetch too thin — skipping today's paper pass")
        return

    hist = lambda s: closes[s][:-1]          # decisions: through yesterday only
    marks = {s: closes[s][-1] for s in closes}

    paper = load_json("shadow/rx4_paper.json", None) or {
        "_note": "RX-4 paper track record (operator-directed 2026-08-04, "
                 "HYPOTHETICAL 'turn up the volume' test of RX-3's engine). "
                 "No real money, not on the RX-3 promotion ladder.",
        "start_date": now_iso(), "start_equity": 100.0,
        "cash": 100.0, "positions": {}, "leaders": [],
        "sleeve_rets": [], "history": [],
    }

    prev_leaders = paper.get("leaders") or []
    rets = []
    for sym in prev_leaders:
        c = closes.get(sym)
        if c and len(c) >= 2 and c[-2] > 0:
            rets.append(c[-1] / c[-2] - 1)
    if rets:
        paper["sleeve_rets"] = (paper.get("sleeve_rets", [])
                                + [sum(rets) / len(rets)])[-60:]

    tb = rotation_engine.target_book(
        {s: hist(s) for s in universe if s in closes},
        {s: hist(s) for s in comp_syms if s in closes},
        {s: hist(s) for s in def_syms if s in closes},
        paper.get("sleeve_rets", []),
        held=prev_leaders,
        sector_of=lambda s: sector_for(s, strategy),
        leverage_factor=lambda s: leverage_factor(s, strategy),
        full_deploy=True, top_n=rcfg.get("top_n", 2))
    targets = {**tb["weights"], **tb["defensive"]}

    positions = paper.get("positions", {})
    cash = float(paper.get("cash") or 0)
    equity = cash + sum(float(p.get("shares") or 0) * marks.get(s, float(p.get("last_px") or 0))
                        for s, p in positions.items())
    if equity <= 0:
        equity = paper.get("start_equity", 100.0)
    for sym in sorted(set(list(positions) + list(targets))):
        px = marks.get(sym)
        if not px or px <= 0:
            continue
        cur_val = float(positions.get(sym, {}).get("shares") or 0) * px
        tgt_val = targets.get(sym, 0.0) * equity
        delta = tgt_val - cur_val
        if abs(delta) < 0.005 * equity:      # rebalance band: skip dust trades
            if sym in positions:
                positions[sym]["last_px"] = px
            continue
        cash -= delta + abs(delta) * 0.0005  # 5bps per side
        if tgt_val <= 0:
            positions.pop(sym, None)
        else:
            positions[sym] = {"shares": round(tgt_val / px, 6), "last_px": px}
    equity = cash + sum(float(p["shares"]) * marks.get(s, float(p.get("last_px") or 0))
                        for s, p in positions.items())

    paper.update({"cash": round(cash, 4), "positions": positions,
                  "leaders": tb["leaders"], "equity": round(equity, 4),
                  "last_run": now_iso()})
    paper.setdefault("history", []).append({
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "equity": round(equity, 4), "leaders": tb["leaders"],
        "defensive": list(tb["defensive"]), "riskx": tb["riskx"],
        "vol_scale": tb["vol_scale"], "cash_w": tb["cash"]})
    os.makedirs(os.path.dirname(RX4_STATE_PATH), exist_ok=True)
    save_json("shadow/rx4_paper.json", paper)
    ret_pct = (equity / paper.get("start_equity", 100.0) - 1) * 100
    print(f"  [rx4] paper: equity {equity:.2f} ({ret_pct:+.2f}% since start) | "
          f"leaders={tb['leaders']} cash={tb['cash']}")
    append_audit_log("rx4", f"RX-4 paper {datetime.now(ET):%Y-%m-%d}",
                     json.dumps(paper["history"][-1], indent=2))


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
    # Heartbeat first — the independent watchdog alerts the operator when this
    # goes stale during market hours (the "bot died with the market open" hole).
    if risk_guard:
        risk_guard.beat("cycle")
        if risk_guard.halted():
            stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
            print(f"[{stamp}] HALTED — HALT file present (monthly-drawdown "
                  "kill-switch). NO trading until the operator reviews and "
                  "deletes the HALT file.")
            write_cycle_status("halted")
            return
    # Operator-declared deposits/withdrawals (dashboard PIN-armed action): needs no
    # broker call, so it runs every cycle before anything else touches progress
    # tracking. See process_manual_cash_flows for why this exists alongside the
    # automatic broker-diff heuristic in sync_account_equity.
    process_manual_cash_flows(log)
    # Lean view (version_history elided) for the read-only cycle prompt; the full
    # file stays on disk for skill_5 and Phase 4. See strategy_for_prompt().
    strategy_text = strategy_for_prompt()
    strategy = load_strategy()  # dict form for the deterministic risk/sizing block
    skill, task = active_skill()
    write_cycle_status("running", task)  # dashboard: warn on manual-order collisions
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
    # Refresh the watchdog's independent stop snapshot (survives process death).
    write_stop_snapshot(log, strategy)

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

    # OPERATOR PAUSE (control/PAUSE, written by the dashboard): no model turn,
    # no new entries — but the protective rails stay hot: heartbeat (above),
    # peaks/stop snapshot (above), deterministic forced exits, kill-switch
    # check, and broker bookkeeping (so a manual dashboard sell still
    # reconciles into the trade log while paused).
    if _control_paused():
        stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
        exit_info = {}
        if is_market_open():
            broker_positions, sell_orders_today, broker_total = read_broker_state(
                TIER_PROTECTIVE if must_sell else TIER_EXECUTION)
            if broker_total:
                sync_account_equity(log, broker_total)
                append_equity_point(broker_total)
                if risk_guard:
                    g_status, g_detail = risk_guard.check_halt(broker_total)
                    if g_status == "halt":
                        print(f"[{stamp}] KILL-SWITCH (while paused): {g_detail} — "
                              "flattening book and halting.")
                        notify_operator(
                            "Trading bot: MONTHLY KILL-SWITCH TRIPPED — flattening",
                            f"{g_detail}. Tripped during an operator pause; all "
                            "positions are being force-sold and the bot will not "
                            "trade until you review and delete the HALT file.")
                        for p in (broker_positions or []):
                            must_sell.setdefault(p["symbol"], "halt")
            if broker_positions is not None:
                held_at_broker = {p["symbol"]: p for p in broker_positions}
                sold_any = False
                for sym, reason in must_sell.items():
                    if sym not in held_at_broker or sym in sell_orders_today:
                        continue
                    if _manual_lock_active(sym):
                        print(f"  [FORCED-SELL] {sym} deferred — manual dashboard "
                              "order in flight (paused cycle)")
                        continue
                    placed, fill = force_sell(sym, held_at_broker[sym].get("shares"),
                                              reason)
                    exit_info[sym] = {"reason": reason, "price": fill}
                    print(f"  [FORCED-SELL] {sym} placed={placed} fill={fill} "
                          "(paused cycle)")
                    sold_any = True
                # Only re-read when a sell was actually PLACED. `must_sell` being
                # non-empty is not enough: a stop-loss alert re-fires every cycle
                # until the position is gone, so a name with a working sell order
                # (or one already flat) triggered a full extra broker call every
                # 15 minutes for nothing.
                if sold_any:
                    reread, _, _ = read_broker_state(TIER_PROTECTIVE)
                    if reread is not None:
                        broker_positions = reread
                process_cycle_state(log, [], broker_positions, exit_info)
            elif must_sell:
                print(f"[{stamp}] WARNING: paused + broker read failed with forced "
                      f"exits pending for {sorted(must_sell)} — will retry next cycle.")
        print(f"[{stamp}] task={task} PAUSED by operator — protective exits + "
              "bookkeeping only, no model call.")
        write_cycle_status("idle", task, "paused")
        return

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
            write_cycle_status("idle", task, f"skipped:{skip_reason}")
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

    # Operator do-not-trade list (dashboard): hard no-buy rule, injected as the
    # first block so it outranks any pick/signal enthusiasm below.
    _dnt = _do_not_trade()
    _dnt_block = ("" if not _dnt else
                  "OPERATOR DO-NOT-TRADE LIST (hard rule from the dashboard): "
                  f"{', '.join(sorted(_dnt))} — NEVER buy or add to these symbols "
                  "this cycle, regardless of signals or picks. Selling/closing "
                  "them is still allowed.\n\n")

    user = (
        f"Task: {task}\nTime (ET): {datetime.now(ET):%Y-%m-%d %H:%M} ({datetime.now(ET):%A})\n\n"
        + _dnt_block
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
    # research_and_prep / midweek_validation: use Opus for deeper reasoning + web search.
    call_model = CHECK_MODEL if task == "market_hours_check" else MODEL
    call_tier = TIER_EXECUTION if task == "market_hours_check" else TIER_RESEARCH
    # Research/midweek are Opus + web search over a large candidate set; the
    # execution turn stays on the tight cycle-bounded ceiling.
    call_timeout = EXEC_TIMEOUT if task == "market_hours_check" else RESEARCH_TIMEOUT
    research_extras = RH_RESEARCH_EXTRA if task != "market_hours_check" else None
    text, usage = run_model(system, user, mcp=True, web=(task != "market_hours_check"),
                            model=call_model, tier=call_tier, timeout=call_timeout,
                            extra_tools=research_extras)

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
    model_failed = text.startswith("(claude -p error") or text.startswith("(error:")
    # A governor deferral is OUR OWN decision, not Claude being down: the
    # protective tier is still permitted (it gets a probe even mid-cooldown). So
    # when the chatty execution turn is deferred but a stop-loss is pending, we
    # must NOT bail out — we skip the turn and go straight to the deterministic
    # broker-read + force_sell path, which is the part that actually matters.
    governor_deferred = text.startswith("(error: usage-governor")
    if model_failed and governor_deferred and must_sell and is_market_open():
        print(f"[{stamp}] execution turn deferred by the usage governor, but a "
              f"forced exit is pending for {sorted(must_sell)} — proceeding "
              f"straight to the deterministic protective path. {text[:120]}")
        # Empty text ⇒ no footer, no phase output; reconciliation below runs off
        # the broker read alone, which is exactly what the protective path needs.
        text, model_failed = "", False
    if model_failed:
        session_limited = ("session limit" in text.lower()
                           or "429" in text or "usage limit" in text.lower())
        if governor_deferred:
            why = "usage governor DEFERRED this call (budget/cooldown)"
        elif session_limited:
            why = "Claude SESSION/USAGE LIMIT hit"
        else:
            why = "model call FAILED"
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
        write_cycle_status("idle", task, "model unavailable")
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
        broker_positions, sell_orders_today, broker_total = None, set(), None
        print(f"[{stamp}] off-hours run — skipping broker reconciliation "
              "(market closed; no opens/closes possible).")
    else:
        # When a forced exit is pending this read IS the protective path (it
        # decides what force_sell fires on), so it rides the tier that a spent
        # usage window can never block.
        broker_positions, sell_orders_today, broker_total = read_broker_state(
            TIER_PROTECTIVE if must_sell else TIER_EXECUTION)
    # Account-level guard off the REAL broker total: deposit-aware equity sync +
    # the monthly-drawdown kill-switch. A fresh halt turns every held name into
    # a forced exit through the same deterministic force_sell path below.
    if broker_total:
        sync_account_equity(log, broker_total)
        append_equity_point(broker_total)  # dashboard equity-curve series
        if risk_guard:
            g_status, g_detail = risk_guard.check_halt(broker_total)
            if g_status == "halt":
                print(f"[{stamp}] KILL-SWITCH: {g_detail} — flattening book and halting.")
                notify_operator(
                    "Trading bot: MONTHLY KILL-SWITCH TRIPPED — flattening",
                    f"{g_detail}. All positions are being force-sold and the bot "
                    "will not trade until you review and delete the HALT file.")
                for p in (broker_positions or []):
                    must_sell.setdefault(p["symbol"], "halt")
    if broker_positions is None and market_open:
        print(f"[{stamp}] WARNING: could not read authoritative broker state — "
              "skipping close detection this cycle (no phantom closes). Any "
              "alerts re-fire next cycle.")
    elif broker_positions is not None:
        # Deterministic forced exits: a hard must-sell still held at the broker,
        # with no working sell order this cycle, gets its own dedicated sell call.
        if must_sell and is_market_open():
            held_at_broker = {p["symbol"]: p for p in broker_positions}
            sold_any = False
            for sym, reason in must_sell.items():
                if sym not in held_at_broker:
                    continue  # already gone (the main turn actually sold it)
                if sym in sell_orders_today:
                    continue  # a sell order already exists — don't double-sell
                if _manual_lock_active(sym):
                    # the operator's own dashboard order for this symbol is in
                    # flight RIGHT NOW — don't race it; retry next cycle
                    print(f"  [FORCED-SELL] {sym} deferred — manual dashboard "
                          "order in flight.")
                    continue
                shares = held_at_broker[sym].get("shares")
                print(f"  [FORCED-SELL] {sym} still held after model turn "
                      f"(reason={reason}) — placing dedicated market sell.")
                placed, fill = force_sell(sym, shares, reason)
                exit_info[sym] = {"reason": reason, "price": fill}
                print(f"  [FORCED-SELL] {sym} placed={placed} fill={fill}")
                sold_any = True
            # Re-read so close detection sees the post-forced-sell truth — but
            # ONLY when a sell was actually placed. `must_sell` stays populated
            # every cycle until the position is gone, so re-reading on the mere
            # presence of an alert burned an extra broker call every 15 minutes
            # while a sell order was already working.
            if sold_any:
                reread, _, _ = read_broker_state(TIER_PROTECTIVE)
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
    #
    # Now gated to OUTSIDE market hours. skill_5 is a full-Opus call carrying the
    # complete strategy.json plus all eight skill files — the second-biggest
    # payload the bot sends — and it was firing at the end of every cycle,
    # including 15-minute execution cycles. Nothing about a strategy rewrite is
    # time-critical: the queue is drained by run_maintenance() after the close.
    if not is_market_open():
        try:
            process_strategy_rewrite_queue()
        except Exception as e:
            print(f"  [skill_5] rewrite queue processing failed: {e}")

    # Phase B + B+: shadow (paper) options passes — isolated, read-only, never trade.
    # Same bundle the smart-skip path runs, so paper trading behaves identically
    # whether or not the equity model was called this cycle.
    _run_shadow_passes(raw_sigs, log)

    write_cycle_status("idle", task)
    print(f"[{stamp}] task={task} done.")


# ================================================================ usage schedule
# The bot runs 24/7 on a server, but Claude subscription usage is metered in
# rolling 5-hour session windows that open on the FIRST call after the previous
# one expired. That start time is therefore something the bot chooses, and the
# daily schedule below places each phase in a window of its own so the phases
# never compete:
#
#   window A  04:25–09:25 ET   preflight anchor (tiny) + pre-market research
#   window B  09:30–14:30 ET   trading, first half   (opened by the 9:30 cycle)
#   window C  14:30–19:30 ET   trading, second half + the 16:00 close
#   window D  19:35–00:35 ET   maintenance: postmortems, victories, skill_5
#
# The anchor call at 04:25 exists purely to START window A far enough ahead that
# it EXPIRES five minutes before the opening bell — which is what guarantees the
# opening cycle gets a completely fresh window, rather than inheriting whatever
# the overnight research left behind. Research at 08:55 then sits comfortably
# inside window A with ~30 minutes of headroom for a long Opus web-search run.
# Maintenance waits for the live window to actually expire (the governor knows
# the real boundary) instead of trusting the clock, so a quiet afternoon that
# opened window C late still gets its own window for the heavy learning work.


def preflight_anchor_time(opens, cfg):
    """When to fire the session-anchor call for a given market open."""
    return opens - timedelta(minutes=cfg.get("anchor_minutes_before_open", 305))


def premarket_research_time(opens, cfg):
    """When to run pre-market research (inside the anchored window)."""
    return opens - timedelta(minutes=cfg.get("research_minutes_before_open", 35))


def next_maintenance_slot(now, cfg, last_run_date=None):
    """Next wall-clock maintenance start (at most one per calendar day).

    `last_run_date` is the date of the last completed drain, so a restart in the
    evening doesn't re-run tonight's maintenance."""
    slot = now.replace(hour=cfg.get("maintenance_hour_et", 19),
                       minute=cfg.get("maintenance_minute_et", 35),
                       second=0, microsecond=0)
    for _ in range(8):
        if slot > now and not (last_run_date and slot.date() <= last_run_date):
            return slot
        slot += timedelta(days=1)
    return slot


def maintenance_time(now, cfg):
    """When tonight's maintenance drain may start.

    The later of (a) the configured wall-clock hour and (b) one minute after the
    CURRENT usage window expires — so the heavy learning calls always land in a
    fresh window instead of finishing off the one the trading day was using."""
    target = now.replace(hour=cfg.get("maintenance_hour_et", 19),
                         minute=cfg.get("maintenance_minute_et", 35),
                         second=0, microsecond=0)
    if target < now:
        target = now
    if usage_governor is not None:
        end = usage_governor.window_end(now=now)
        if end and end + timedelta(minutes=1) > target:
            target = end + timedelta(minutes=1)
    return target


def run_preflight():
    """Tiny `claude -p` call whose real job is to OPEN the 5-hour session window
    at a time of our choosing — everything else it does is a bonus.

    It doubles as a genuine morning system test: the round trip proves the CLI is
    on PATH, the subscription is not already limited, and the Robinhood MCP
    connection is authorized — all hours before the opening bell, when there is
    still time to fix it, instead of discovering it from a failed 9:30 order."""
    system = ("You are a health-check probe. Answer in one short line. Do not "
              "analyze markets, do not place any order.")
    user = (
        f"Pre-market system check for Robinhood account {ACCOUNT_NUMBER}. "
        "Call get_accounts once to confirm the connection works, then reply with "
        "exactly: OK <account-number> <one-word connection status>. Nothing else."
    )
    text, usage = run_model(system, user, mcp=True, read_only=True,
                            model=CHECK_MODEL, timeout=120, tier=TIER_EXECUTION)
    stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
    healthy = not (text.startswith("(claude -p error") or text.startswith("(error:"))
    if healthy:
        print(f"[{stamp}] PREFLIGHT OK — {text.strip()[:120]}")
    else:
        print(f"[{stamp}] PREFLIGHT FAILED — {text[:200]}")
        notify_operator(
            "Trading bot: pre-market system check FAILED",
            f"The 04:25 ET preflight could not reach Claude/Robinhood: "
            f"{text[:300]}. Today's research and execution are at risk — check "
            "the CLI auth and subscription limit before the open.")
    if usage_governor is not None:
        st = usage_governor.status()
        if st.get("window_end"):
            print(f"  [USAGE] session window anchored — expires "
                  f"{st['window_end']} (target: before the 09:30 open).")
    try:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "preflight.json"), "w") as f:
            json.dump({"ts": now_iso(), "healthy": healthy,
                       "detail": text[:300],
                       "usage": usage_governor.status() if usage_governor else {}},
                      f, indent=2)
    except Exception:
        pass
    return healthy


def run_maintenance(deep=False):
    """After-hours learning drain, in its own session window.

    This is where the deferred expensive work happens: postmortems and victory
    analyses queued during the trading day, then the skill_5 strategy rewrites
    they flagged. Both are bounded per run so one busy day cannot drain a whole
    window, and both are individually isolated.

    `deep=True` (weekends) lifts the bounds — there is no execution to protect,
    so the backlog gets cleared in one pass."""
    stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
    cfg = usage_governor.config() if usage_governor else {}
    print(f"\n[{stamp}] MAINTENANCE ({'deep/weekend' if deep else 'nightly'}) — "
          "deferred learning work.")
    if usage_governor is not None:
        st = usage_governor.status()
        print(f"  [USAGE] window={st.get('window_start')} → {st.get('window_end')} "
              f"calls={st.get('calls')} used={st.get('used_pct')}")
        cd = usage_governor.cooldown_until()
        if cd:
            print(f"  [USAGE] cooling down until {cd:%H:%M} ET — maintenance "
                  "deferred to the next run.")
            return

    a_limit = None if deep else cfg.get("max_analyses_per_drain", 4)
    try:
        n = drain_analysis_queue(limit=a_limit)
        print(f"  [analysis-queue] {n} analysis run(s) completed; "
              f"{len(pending_trade_analyses())} still queued.")
    except Exception as e:
        print(f"  [analysis-queue] drain failed: {e}")

    # Strategy rewrites: one entry per call, bounded per drain. Weekend-only mode
    # is available for operators who would rather batch every rule change into a
    # single weekly review.
    if cfg.get("weekend_only_rewrites") and not deep:
        print("  [skill_5] weekend_only_rewrites=true — rewrites deferred to the "
              "weekend deep maintenance.")
        return
    max_rw = (cfg.get("max_rewrites_per_drain", 3) if not deep else 12)
    for _ in range(max_rw):
        if usage_governor is not None:
            ok, why = usage_governor.allow(TIER_LEARNING)
            if not ok:
                print(f"  [skill_5] stopping rewrite drain — {why}")
                break
        try:
            if not process_strategy_rewrite_queue():
                break  # nothing left to do
        except Exception as e:
            print(f"  [skill_5] rewrite queue processing failed: {e}")
            break
    print(f"[{stamp}] maintenance done.")


def next_market_open():
    """Return the next 9:30 AM ET on a weekday as a timezone-aware datetime."""
    candidate = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    if datetime.now(ET) >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _sleep_until(target, announce=None):
    """Sleep until `target` (tz-aware ET), polling the REAL clock every <=60s so a
    system suspend (laptop lid closed) can't overshoot the wake time. A single long
    time.sleep() PAUSES while macOS is asleep and then wakes hours late — the exact
    bug that left the bot stuck on 'Waiting until ... for pre-market research'
    straight through an open market. This re-checks datetime.now(ET) on a short
    cadence and returns as soon as now >= target (Ctrl-C still interrupts). A late
    wake then recovers gracefully: a past target returns immediately and the loop
    proceeds (runs the missed research, then trades)."""
    if announce:
        print(announce)
    while True:
        now = datetime.now(ET)
        if now >= target:
            return
        time.sleep(min(60.0, (target - now).total_seconds()))


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

    cfg = usage_governor.config() if usage_governor else {
        "anchor_minutes_before_open": 305, "research_minutes_before_open": 35,
        "maintenance_hour_et": 19, "maintenance_minute_et": 35}

    if usage_governor is not None:
        st = usage_governor.status()
        print(f"  usage governor: enabled={st.get('enabled')} "
              f"window_end={st.get('window_end')} used={st.get('used_pct')} "
              f"cooldown={st.get('cooldown_until')}")

    if not is_market_open():
        now = datetime.now(ET)
        opens = next_market_open()
        anchor_t = preflight_anchor_time(opens, cfg)
        research_t = premarket_research_time(opens, cfg)
        hours_away = (opens - now).total_seconds() / 3600
        print(f"\nMarket is currently closed. ({now:%A %Y-%m-%d %H:%M} ET)")
        print(f"Next market open: {opens:%A %Y-%m-%d at %H:%M} ET ({hours_away:.1f} hours away)")
        print("\nDaily usage schedule (each phase gets its own 5h Claude window):")
        print(f"  {anchor_t:%H:%M} ET  preflight system check — anchors the window")
        print(f"  {research_t:%H:%M} ET  pre-market research (inside that window)")
        print(f"  {opens:%H:%M} ET  market open — trading starts on a FRESH window")
        print(f"  {cfg.get('maintenance_hour_et', 19):02d}:"
              f"{cfg.get('maintenance_minute_et', 35):02d} ET  maintenance — "
              "postmortems + strategy rewrites\n")

        print("What would you like to do?")
        print("  r — run research now and exit")
        print(f"  m — run the maintenance drain now and exit (postmortems + rewrites)")
        print(f"  w — enter the 24/7 schedule above (default)")
        choice = input("\nYour choice (r/m/w): ").strip().lower()

        if choice == "r":
            print("Running research now...\n")
            run_agent()
            return
        if choice == "m":
            run_maintenance(deep=True)
            return
        # w (or anything else): fall through — the daily loop below handles it

    # Daily loop: maintenance → anchor → research → trade → repeat. Until Ctrl-C.
    last_maintenance_date = None
    while True:
        # ---------------- off-hours: run each scheduled phase in turn ----------
        while not is_market_open():
            now = datetime.now(ET)
            opens = next_market_open()
            anchor_t = preflight_anchor_time(opens, cfg)
            research_t = premarket_research_time(opens, cfg)

            events = []
            maint_t = next_maintenance_slot(now, cfg, last_maintenance_date)
            # Only if it comfortably clears the next morning's anchor — the
            # anchor is the one appointment that must not slip, because the
            # whole point of it is to expire before the opening bell.
            if maint_t < anchor_t - timedelta(minutes=30):
                events.append((maint_t, "maintenance"))
            if now < anchor_t:
                events.append((anchor_t, "anchor"))
            if now < research_t:
                events.append((research_t, "research"))

            if not events:
                _sleep_until(opens, f"\nWaiting until {opens:%H:%M} ET for the "
                                    "market open. (Ctrl-C to stop)")
                break

            when, what = min(events, key=lambda e: e[0])
            _sleep_until(when, f"\nNext: {what} at {when:%A %Y-%m-%d %H:%M} ET. "
                               "(Ctrl-C to stop)")

            if what == "maintenance":
                # Let the trading day's window fully expire first, so the heavy
                # learning calls open a window of their own.
                ready = maintenance_time(datetime.now(ET), cfg)
                if ready > datetime.now(ET):
                    _sleep_until(ready, f"  holding until the current usage window "
                                        f"expires at {ready:%H:%M} ET...")
                run_maintenance(deep=datetime.now(ET).weekday() >= 5)
                last_maintenance_date = datetime.now(ET).date()
            elif what == "anchor":
                print("\nRunning pre-market system check (anchors today's usage "
                      "window)...\n")
                run_preflight()
            elif what == "research":
                print("\nRunning pre-market research...\n")
                run_agent()

        if not is_market_open():
            continue  # woke early / market still closed — re-evaluate

        print("Market is now open. Starting trading loop...\n")
        # Intraday trading loop — clear any stale schedule jobs from the prior day.
        schedule.clear()
        schedule.every(POLL_MINUTES).minutes.do(run_agent)
        run_agent()
        while True:
            if not is_market_open():
                opens = next_market_open()
                print(
                    f"Market just closed. Next: maintenance at "
                    f"{cfg.get('maintenance_hour_et', 19):02d}:"
                    f"{cfg.get('maintenance_minute_et', 35):02d} ET, then the "
                    f"pre-market system check at "
                    f"{preflight_anchor_time(opens, cfg):%H:%M} ET on "
                    f"{opens:%A %Y-%m-%d}. (Ctrl-C to stop)"
                )
                break
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    main()
