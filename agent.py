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
         "change": reason, "trade_ids": trade_ids})


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
              read_only=False, allow_write=False):
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


def check_stop_loss_alerts(log):
    """Compare open positions against the hard stop-loss threshold using the
    latest close from signals.py. Returns a list of triggered positions so
    the agent prompt can order immediate sells before any other logic runs."""
    if not signals:
        return []
    strategy = load_strategy()
    pct = strategy.get("risk_management", {}).get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    alerts = []
    for pos in log.get("open_positions", []):
        symbol = pos["symbol"]
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            continue
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
        if t == "EXIT" and sym in held:
            return False, f"exit:{sym}"
        # State-based safety net: the EXIT edge exists only on the bar where the
        # cross happens. If the cross occurred while the bot wasn't looking (down,
        # or before an indicator change), transition reads NO_ACTION forever and
        # the edge-based wake above never fires. A held position sitting in SELL
        # state must wake the model regardless; re-fires every cycle until the
        # position is actually sold (same deliberate nagging as stop-loss alerts).
        if s.get("state") == "SELL" and sym in held:
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
        print(f"  BEHIND ON MONTHLY GOAL ({monthly_return:.1f}% vs target pace) — "
              "raise min confidence next scan.")
    return on_track


def flag_strategy_rewrite(trade):
    """Queue a note so skill_5 reviews this outcome on its next cycle."""
    line = (f"- {now_iso()} | {trade['id']} "
            f"{trade['symbol']} {trade['outcome']} {trade['pnl_pct']}% | "
            f"analysis: {trade.get('analysis_file')} | skill_5 review\n")
    path = os.path.join(ROOT, "research", "strategy_rewrite_queue.md")
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
def version_skill_file(skill_name, new_content):
    """Archive the current skill file into skills/history/ before overwriting it
    with new_content. Mirrors snapshot_strategy() for skill files so a bad skill_5
    rewrite is always reversible.

    Naming: skills/history/{skill_name}_v{NNN}.md (zero-padded 3 digits); the
    version number is (count of existing history files for this skill) + 1.
    Returns True on success, False on any error (never raises — a versioning
    failure must not crash the rewrite loop)."""
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

    print(f"  [skill_5] processing rewrite queue entry: {target_line[:80]}")

    try:
        # Build full context for skill_5
        skill5 = load_file("skills/skill_5_strategy_rewriter.md")
        strategy = load_file("strategy/strategy.json")
        postmortems = load_postmortems()
        trade_log = json.dumps(load_trade_log(), indent=2)

        # Load all current skill file contents for skill_5 to rewrite
        skill_contents = {}
        for sk in ["skill_0_orchestrator", "skill_1_research", "skill_2_execution",
                   "skill_3_midweek", "skill_4_postmortem", "skill_4b_victory",
                   "skill_5_strategy_rewriter", "skill_6_pattern_detector"]:
            skill_contents[sk] = load_file(f"skills/{sk}.md")

        user = f"""A trade closed and requires strategy review. Queue entry: {target_line}

Current strategy.json:
{strategy}

All postmortems and victory analyses:
{postmortems}

Full trade log:
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
            print(f"  [skill_5] strategy.json updated v{old_v} -> v{new_strategy.get('version', old_v+1)}")

        # Parse and apply skill file updates
        skill_updates = re.findall(
            r"## SKILL FILE UPDATE: (\S+)\n(.*?)\n## END SKILL FILE UPDATE",
            text, re.DOTALL
        )
        for skill_name, new_content in skill_updates:
            skill_path = os.path.join(ROOT, "skills", f"{skill_name}.md")
            if os.path.exists(skill_path):
                version_skill_file(skill_name, new_content.strip())
                print(f"  [skill_5] skill updated: {skill_name}")
            else:
                print(f"  [skill_5] WARNING: unknown skill name in update block: {skill_name}")

        # Save raw output for audit trail
        stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
        with open(os.path.join(ROOT, "research", f"skill5_run_{stamp}.md"), "w") as f:
            f.write(f"# skill_5 run {stamp}\nQueue entry: {target_line}\n\n{text}\n")

        # Mark entry as DONE
        lines[target_idx] = lines[target_idx].rstrip() + f" [DONE {now_iso()}]\n"
        with open(queue_path, "w") as f:
            f.writelines(lines)

        print(f"  [skill_5] queue entry marked [DONE]")

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
    return (
        "⚠ SELL SIGNAL ACTIVE ON HELD POSITIONS — core ribbon rule: red(55) on "
        "top = downtrend = SELL:\n" + "\n".join(rows) + "\n"
        "For EACH symbol above: sell ALL shares at market via the Robinhood MCP "
        "THIS cycle. In actions_taken set type='sell' and reason='ema_exit'. "
        "A transition of NO_ACTION does NOT cancel this — the cross already "
        "happened on an earlier bar. Only stop-loss alerts take precedence.\n\n"
    )


def run_agent():
    log = load_trade_log()
    strategy_text = load_file("strategy/strategy.json")
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

    # Hard forced-exit set (from pre-turn signals): stop-loss alerts + held names
    # sitting in ribbon SELL state. stop_loss takes precedence over ema_exit.
    # Computed up here so the error path below can warn about pending exits even
    # when the model call itself fails (e.g. Claude session limit).
    held_syms = {p["symbol"] for p in log.get("open_positions", [])}
    must_sell = {a["symbol"]: "stop_loss" for a in stop_loss_alerts}
    for sym, s in raw_sigs.items():
        if sym in held_syms and s.get("ok") and s.get("state") == "SELL":
            must_sell.setdefault(sym, "ema_exit")

    # SMART SKIP: if every signal is NEUTRAL/HOLD and no stop-loss → no model call needed.
    # Research and midweek phases always run (they do web research, not just signal checks).
    if task == "market_hours_check":
        skip, skip_reason = should_skip_model_call(raw_sigs, log)
        if skip:
            stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M")
            print(f"[{stamp}] task={task} SKIPPED ({skip_reason}) — 0 tokens used.")
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
- Ribbon signal (TEMA 13/21/55 + EMA 8 — matches the operator's chart):
  red(55) lowest = BUY, red(55) highest = SELL. ENTER_LONG transition triggers
  buys; for a HELD position the SELL state itself triggers the sell — never
  wait for an EXIT transition, the cross may have passed on an earlier bar.
- Honor blackout windows + min_confidence_to_trade. Real scoreboard = beat SPY;
  100% monthly is the stretch ceiling, not a reason to oversize risk."""

    user = (
        f"Task: {task}\nTime (ET): {datetime.now(ET):%Y-%m-%d %H:%M} ({datetime.now(ET):%A})\n\n"
        + _format_stop_loss_block(stop_loss_alerts)
        + _format_ema_sell_block(raw_sigs, log)
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

    # write the human-readable run record
    with open(os.path.join(ROOT, "research", f"agent_run_{stamp}.md"), "w") as f:
        f.write(f"# Agent run {stamp} — task={task}\n\n{text}\n")

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
    broker_positions, sell_orders_today = read_broker_state()
    exit_info = {}
    if broker_positions is None:
        print(f"[{stamp}] WARNING: could not read authoritative broker state — "
              "skipping close detection this cycle (no phantom closes). Any "
              "alerts re-fire next cycle.")
    else:
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
        print(f"  w — wait until {research_time:%H:%M} ET ({opens:%A}) for pre-market research, then trade")
        choice = input("\nYour choice (r/w): ").strip().lower()

        if choice == "r":
            print("Running research now...\n")
            run_agent()
            return
        else:
            # Sleep until 30 min before open, run research, then sleep the final 30 min
            wait_secs = max(0, (research_time - datetime.now(ET)).total_seconds())
            if wait_secs > 0:
                print(f"\nWaiting until {research_time:%A %Y-%m-%d at %H:%M} ET for pre-market research. (Ctrl-C to cancel)")
                time.sleep(wait_secs)
            print("Running pre-market research...\n")
            run_agent()
            wait_secs = max(0, (opens - datetime.now(ET)).total_seconds())
            if wait_secs > 0:
                print(f"Research done. Waiting until {opens:%H:%M} ET for market open. (Ctrl-C to cancel)")
                time.sleep(wait_secs)
            print("Market is now open. Starting trading loop...\n")

    schedule.every(POLL_MINUTES).minutes.do(run_agent)
    run_agent()
    while True:
        if not is_market_open():
            print("Market just closed. Running end-of-day research once and exiting.")
            run_agent()
            return
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
