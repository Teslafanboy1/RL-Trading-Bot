"""readers.py — read/parse every bot state file. Pure functions, missing-safe.

Every reader returns a plain dict/list and NEVER raises: the dashboard must
render whatever subset of state exists (e.g. shadow/rx3_paper.json before the
first paper day, or a repo with no postmortems yet). File formats follow the
contracts documented in CLAUDE.md (### #N — SYMBOL pick headings, the
`## MOMENTUM OPTIONS WATCH` block, change_events.jsonl schema, etc.).
"""

import os
import re
import json
import glob
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- primitives


def _path(*parts):
    return os.path.join(ROOT, *parts)


def load_json(relpath, default):
    try:
        with open(_path(relpath)) as f:
            return json.load(f)
    except Exception:
        return default


def load_text(relpath, default=""):
    try:
        with open(_path(relpath)) as f:
            return f.read()
    except Exception:
        return default


def file_age_seconds(relpath):
    """Seconds since mtime, or None if the file doesn't exist."""
    try:
        return max(0, datetime.now(timezone.utc).timestamp() - os.path.getmtime(_path(relpath)))
    except Exception:
        return None


def _parse_ts(ts):
    """ISO timestamp -> epoch seconds (naive treated as local), else None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- core state
def trade_log():
    return load_json("trade_log.json", {"summary": {}, "open_positions": [], "trades": []})


def strategy():
    return load_json("strategy/strategy.json", {})


def strategy_version_history():
    """version_history entries from strategy.json, newest first, shape-tolerant."""
    hist = strategy().get("version_history") or []
    out = []
    for h in hist if isinstance(hist, list) else []:
        if isinstance(h, dict):
            out.append({
                "version": h.get("version") or h.get("v"),
                "date": h.get("date") or h.get("timestamp") or h.get("updated"),
                "summary": h.get("changes") or h.get("summary") or h.get("reason") or "",
                "trade_ids": h.get("trade_ids") or [],
                "severity": h.get("severity"),
            })
        else:
            out.append({"version": None, "date": None, "summary": str(h),
                        "trade_ids": [], "severity": None})
    out.reverse()
    return out


def strategy_history_files():
    files = sorted(glob.glob(_path("strategy", "history", "strategy_v*.json")),
                   key=lambda p: (len(p), p))
    out = []
    for p in files:
        m = re.search(r"strategy_v(\d+)\.json$", p)
        out.append({"file": os.path.basename(p),
                    "version": int(m.group(1)) if m else None,
                    "mtime": os.path.getmtime(p)})
    return out


def change_events():
    """logs/change_events.jsonl newest first."""
    out = []
    for line in load_text("logs/change_events.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out


def skill_versions():
    """Per-skill snapshot counts from skills/history/."""
    counts = {}
    for p in glob.glob(_path("skills", "history", "*_v*.md")):
        m = re.match(r"(.+)_v(\d+)\.md$", os.path.basename(p))
        if not m:
            continue
        name, v = m.group(1), int(m.group(2))
        cur = counts.setdefault(name, {"versions": 0, "latest": 0})
        cur["versions"] += 1
        cur["latest"] = max(cur["latest"], v)
    return counts


# ---------------------------------------------------------------- research md
_PICK_RE = re.compile(
    r"###\s+#(\d+)\s+—\s+([A-Z][A-Z0-9.]*)\s*(?:\(([^)]*)\))?"
    r"(?:.*?Confidence:\s*(\d+)\s*/\s*100)?", re.IGNORECASE)


def research_files():
    """All research outputs, newest first: weekend picks + midweek reviews."""
    out = []
    for p in glob.glob(_path("research", "weekend_picks_*.md")):
        out.append({"file": os.path.basename(p), "kind": "picks",
                    "date": os.path.basename(p)[14:-3], "mtime": os.path.getmtime(p)})
    for p in glob.glob(_path("research", "midweek_review_*.md")):
        out.append({"file": os.path.basename(p), "kind": "midweek",
                    "date": os.path.basename(p)[15:-3], "mtime": os.path.getmtime(p)})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _classify_bullet(line):
    low = line.lower()
    for key, tags in (("invalidates", ("invalidat",)),
                      ("ema", ("ema state", "ema:")),
                      ("momentum", ("momentum",)),
                      ("targets", ("entry target", "entry:", "exit target")),
                      ("allocation", ("allocation",)),
                      ("sources", ("sources",))):
        if any(t in low for t in tags):
            return key
    return None


def parse_picks_file(fname):
    """Parse a weekend_picks_*.md into structured cards. Returns {} if absent."""
    text = load_text(os.path.join("research", fname))
    if not text:
        return {}
    lines = text.splitlines()
    picks, current = [], None
    section = ""
    header, reassessment, watchlist, momentum_watch, deployment = [], [], [], [], []
    for line in lines:
        s = line.strip()
        m = _PICK_RE.match(s) if s.startswith("###") else None
        if m:
            current = {"rank": int(m.group(1)), "symbol": m.group(2).upper(),
                       "name": (m.group(3) or "").strip() or None,
                       "confidence": int(m.group(4)) if m.group(4) else None,
                       "fields": {}, "bullets": []}
            picks.append(current)
            continue
        if s.startswith("## "):
            section = s[3:].strip().lower()
            current = None
            continue
        if current is not None and s.startswith(("-", "*")):
            bullet = s.lstrip("-* ").strip()
            key = _classify_bullet(bullet)
            if key and key not in current["fields"]:
                current["fields"][key] = re.sub(r"\*\*", "", bullet.split(":", 1)[-1]).strip()
            current["bullets"].append(bullet)
            continue
        if not s:
            continue
        clean = s
        if "momentum options watch" in section:
            if s.startswith(("-", "*")):
                momentum_watch.append(clean.lstrip("-* ").strip())
        elif "watchlist" in section:
            if s.startswith(("-", "*")):
                watchlist.append(clean.lstrip("-* ").strip())
        elif "deployment" in section:
            if s.startswith(("-", "*")):
                deployment.append(clean.lstrip("-* ").strip())
        elif "reassessment" in section or "open position" in section:
            if s.startswith(("-", "*")):
                reassessment.append(clean.lstrip("-* ").strip())
        elif "header" in section or (not section and not s.startswith("#")):
            if s.startswith(("-", "*")) or not s.startswith("#"):
                if len(header) < 12:
                    header.append(clean.lstrip("-* ").strip())
    return {"file": fname, "picks": picks, "header": header,
            "reassessment": reassessment, "watchlist": watchlist,
            "momentum_watch": momentum_watch, "deployment": deployment,
            "raw": text}


def parse_momentum_watch(fname=None):
    """{SYMBOL: {confidence, catalyst}} from the MOMENTUM OPTIONS WATCH block."""
    if fname is None:
        files = [f for f in research_files() if f["kind"] == "picks"]
        if not files:
            return {}
        fname = files[0]["file"]
    parsed = parse_picks_file(fname)
    out = {}
    for line in parsed.get("momentum_watch", []):
        m = re.match(r"([A-Z][A-Z0-9.]*)\s*\|\s*conf\s*(\d+)\s*\|\s*catalyst:\s*(.+)",
                     line, re.IGNORECASE)
        if m:
            out[m.group(1).upper()] = {"confidence": int(m.group(2)),
                                       "catalyst": m.group(3).strip()}
    return out


def parse_midweek_file(fname):
    """Split a midweek review into ## sections (generic; format varies by run)."""
    text = load_text(os.path.join("research", fname))
    if not text:
        return {}
    sections, cur_name, cur = [], "intro", []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur:
                sections.append({"title": cur_name, "body": "\n".join(cur).strip()})
            cur_name, cur = line[3:].strip(), []
        else:
            cur.append(line)
    if cur:
        sections.append({"title": cur_name, "body": "\n".join(cur).strip()})
    return {"file": fname, "sections": sections, "raw": text}


def rewrite_queue():
    """Pending + done entries from research/strategy_rewrite_queue.md."""
    out = []
    for line in load_text("research/strategy_rewrite_queue.md").splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        done = "[DONE" in s
        m = re.match(r"-\s*([\d:T+\-.]+)\s*\|\s*(T\d{4})\s+(\w+)\s+(WIN|LOSS)\s+([\-\d.]+%?)"
                     r"\s*\|\s*analysis:\s*(\S+)", s)
        entry = {"raw": s.lstrip("- "), "done": done}
        if m:
            entry.update({"queued_at": m.group(1), "trade_id": m.group(2),
                          "symbol": m.group(3), "outcome": m.group(4),
                          "pnl": m.group(5), "analysis": m.group(6).rstrip("|").strip()})
        out.append(entry)
    return out


# ---------------------------------------------------------------- postmortems
def postmortem_files():
    out = []
    for p in sorted(glob.glob(_path("postmortems", "*.md"))):
        base = os.path.basename(p)
        kind = "victory" if base.startswith("victory") else "postmortem"
        text = load_text(os.path.join("postmortems", base))
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines()
                      if ln.startswith("#")), base)
        tids = sorted(set(re.findall(r"\bT\d{4}\b", text)))
        syms = sorted(set(re.findall(r"\b([A-Z]{2,5})\b(?=[^a-z]*(?:trade|position|entry|exit))",
                                     text)))[:3]
        out.append({"file": base, "kind": kind, "title": title[:140],
                    "trade_ids": tids, "symbols_guess": syms,
                    "mtime": os.path.getmtime(p)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def postmortem_text(fname):
    if "/" in fname or ".." in fname or not fname.endswith(".md"):
        return ""
    return load_text(os.path.join("postmortems", fname))


def _last_json_block(text):
    for b in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)):
        try:
            return json.loads(b)
        except Exception:
            continue
    return None


def source_verdicts():
    """Per-analysis source right/wrong verdicts, parsed from each postmortem's
    machine-readable verdicts JSON block. -> [{file, trade_id, sources:{name:bool}}]"""
    out = []
    for meta in postmortem_files():
        text = postmortem_text(meta["file"])
        block = _last_json_block(text)
        if not isinstance(block, dict):
            continue
        v = block.get("verdicts") if isinstance(block.get("verdicts"), dict) else block
        srcs = v.get("sources") if isinstance(v, dict) else None
        if isinstance(srcs, dict):
            out.append({"file": meta["file"], "kind": meta["kind"],
                        "trade_ids": meta["trade_ids"], "sources": srcs})
    return out


def source_leaderboard():
    """Accuracy leaderboard straight from strategy.json's tracked counts."""
    s = strategy()
    perf = (s.get("research") or {}).get("source_performance") or {}
    weights = (s.get("research") or {}).get("source_weights") or {}
    rows = []
    for name, p in perf.items():
        wins, losses = int(p.get("wins") or 0), int(p.get("losses") or 0)
        rows.append({"source": name, "wins": wins, "losses": losses,
                     "n": wins + losses,
                     "accuracy": p.get("accuracy"),
                     "weight": weights.get(name)})
    rows.sort(key=lambda r: (-(r["accuracy"] or 0), -r["n"]))
    return rows


def news_citations(limit=120):
    """News/sources the bot cited, mined from research files + postmortems.
    Tagged by symbol where derivable. Newest files first."""
    items = []
    for meta in research_files()[:6]:
        parsed = parse_picks_file(meta["file"]) if meta["kind"] == "picks" else {}
        for pick in parsed.get("picks", []):
            src = pick["fields"].get("sources")
            thesis_bits = [b for b in pick["bullets"]
                           if not _classify_bullet(b)][:2]
            items.append({
                "when": meta["date"], "file": meta["file"],
                "symbol": pick["symbol"], "confidence": pick["confidence"],
                "sources": src or "", "note": " ".join(thesis_bits)[:300],
                "invalidates": pick["fields"].get("invalidates", ""),
            })
    for v in source_verdicts()[:30]:
        right = [k for k, ok in v["sources"].items() if ok]
        wrong = [k for k, ok in v["sources"].items() if not ok]
        items.append({
            "when": None, "file": v["file"],
            "symbol": (v["trade_ids"] or [""])[0],
            "confidence": None,
            "sources": ("right: " + ", ".join(right) if right else "")
                       + ("  wrong: " + ", ".join(wrong) if wrong else ""),
            "note": f"{v['kind']} source verdicts", "invalidates": "",
        })
    return items[:limit]


# ---------------------------------------------------------------- bot health
def heartbeat():
    txt = load_text("logs/heartbeat").strip()
    age = file_age_seconds("logs/heartbeat")
    return {"raw": txt or None, "age_seconds": age}


def halt_state():
    p = _path("HALT")
    if not os.path.exists(p):
        return {"halted": False}
    return {"halted": True, "detail": load_text("HALT")[:600],
            "age_seconds": file_age_seconds("HALT")}


def pause_state():
    p = _path("control", "PAUSE")
    if not os.path.exists(p):
        return {"paused": False}
    try:
        with open(p) as f:
            body = f.read()[:300]
    except Exception:
        body = ""
    return {"paused": True, "detail": body, "age_seconds": file_age_seconds("control/PAUSE")}


def risk_state():
    return load_json("logs/risk_state.json", {})


def stops():
    return load_json("logs/stops.json", {})


def cycle_status():
    return load_json("logs/cycle_status.json", {})


def stop_overrides():
    return load_json("control/stop_overrides.json", {})


def do_not_trade():
    d = load_json("control/do_not_trade.json", {})
    return sorted({str(s).upper() for s in d.get("symbols", []) if s})


def watchdog_recent(limit=40):
    lines = load_text("logs/watchdog.log").splitlines()
    return lines[-limit:][::-1]


def manual_actions(limit=200):
    out = []
    for line in load_text("logs/manual_actions.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out[-limit:][::-1]


def agent_log_warnings(limit=30):
    """WARNING / [ALERT] / KILL-SWITCH lines from the newest agent_live_*.log."""
    logs = sorted(glob.glob(_path("agent_live_*.log")))
    if not logs:
        return []
    text = load_text(os.path.basename(logs[-1]))
    hits = [ln.strip() for ln in text.splitlines()
            if ("WARNING" in ln or "[ALERT]" in ln or "KILL-SWITCH" in ln
                or "FORCED-SELL" in ln or "STOP-LOSS" in ln or "TRAILING-STOP" in ln)]
    return [{"file": os.path.basename(logs[-1]), "line": ln} for ln in hits[-limit:][::-1]]


def alerts():
    """Assembled operator-alert feed, most urgent first."""
    out = []
    h = halt_state()
    if h["halted"]:
        out.append({"level": "critical", "kind": "halt",
                    "title": "KILL-SWITCH / HALT active",
                    "detail": h.get("detail", ""), "age_seconds": h.get("age_seconds")})
    hb = heartbeat()
    now_et = datetime.now(ET)
    market_hours = now_et.weekday() < 5 and (9 * 60 + 30) <= (now_et.hour * 60 + now_et.minute) <= 16 * 60
    if hb["age_seconds"] is None:
        out.append({"level": "warn", "kind": "heartbeat",
                    "title": "No heartbeat file — bot has never run here",
                    "detail": "", "age_seconds": None})
    elif market_hours and hb["age_seconds"] > 30 * 60:
        out.append({"level": "critical", "kind": "heartbeat",
                    "title": f"Heartbeat STALE ({hb['age_seconds']/60:.0f} min) with market open",
                    "detail": "Bot may be down; bot-side stops are not being enforced. "
                              "Watchdog (launchd) is the only stop defense right now.",
                    "age_seconds": hb["age_seconds"]})
    elif not market_hours and hb["age_seconds"] > 26 * 3600:
        out.append({"level": "warn", "kind": "heartbeat",
                    "title": f"Heartbeat {hb['age_seconds']/3600:.0f}h old",
                    "detail": "Bot hasn't cycled in over a day.",
                    "age_seconds": hb["age_seconds"]})
    p = pause_state()
    if p["paused"]:
        out.append({"level": "warn", "kind": "pause",
                    "title": "Bot PAUSED by operator",
                    "detail": p.get("detail", ""), "age_seconds": p.get("age_seconds")})
    for w in watchdog_recent(15):
        if "ALERT" in w:
            out.append({"level": "warn", "kind": "watchdog", "title": w[:180],
                        "detail": "", "age_seconds": None})
    for a in agent_log_warnings(10):
        out.append({"level": "info", "kind": "agent_log", "title": a["line"][:180],
                    "detail": a["file"], "age_seconds": None})
    for ma in manual_actions(20):
        if ma.get("result", {}).get("ok") is False:
            out.append({"level": "warn", "kind": "manual_action_failed",
                        "title": f"Manual {ma.get('action')} FAILED",
                        "detail": json.dumps(ma.get("result"))[:200],
                        "age_seconds": None})
    return out


# ---------------------------------------------------------------- shadow/paper
def options_shadow():
    return load_json("shadow/options_shadow_log.json", {"positions": [], "summary": {}})


def momentum_last_scan():
    return load_json("shadow/momentum_last_scan.json", {})


def rx3_paper():
    return load_json("shadow/rx3_paper.json", {})


def go_live_checklist(broker_total=None):
    """Options go-live gates: account size vs activation thresholds + proven edge."""
    s = strategy()
    opt = s.get("options") or {}
    act = opt.get("activation") or {}
    shadow = options_shadow()
    summ = shadow.get("summary") or {}
    closed = int(summ.get("closed") or 0)
    avg_prem = summ.get("avg_pct_on_premium")
    win_rate = summ.get("win_rate")
    total = broker_total
    gates = []
    for label, key in (("Long calls / cheap momentum", "long_calls_min_account_usd"),
                       ("Debit spreads", "debit_spreads_min_account_usd"),
                       ("Premium selling", "premium_selling_min_account_usd")):
        thr = act.get(key)
        if thr is None:
            continue
        gates.append({"gate": f"Account ≥ ${thr:,.0f} — {label}",
                      "threshold": thr, "current": total,
                      "met": bool(total and total >= thr)})
    edge_met = bool(closed >= 10 and (avg_prem or 0) > 0)
    gates.append({"gate": "Proven shadow edge (≥10 closed, avg % on premium > 0)",
                  "threshold": None,
                  "current": {"closed": closed, "avg_pct_on_premium": avg_prem,
                              "win_rate": win_rate},
                  "met": edge_met})
    return {"enabled": bool(opt.get("enabled")),
            "shadow_mode": bool(opt.get("shadow_mode")),
            "gates": gates}


# ---------------------------------------------------------------- performance
def equity_curve():
    """logs/equity_curve.jsonl -> [{ts, total, cash?}] oldest first."""
    out = []
    for line in load_text("logs/equity_curve.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
            if j.get("total"):
                out.append(j)
        except Exception:
            continue
    out.sort(key=lambda x: x.get("ts") or "")
    return out


def append_equity_point(total, cash=None, source="dashboard"):
    """Append an equity snapshot (deduped to one per minute per source)."""
    try:
        now = datetime.now(ET).isoformat(timespec="seconds")
        pts = equity_curve()
        if pts:
            last = pts[-1]
            if (last.get("source") == source and str(last.get("ts", ""))[:16] == now[:16]):
                return False
        os.makedirs(_path("logs"), exist_ok=True)
        with open(_path("logs", "equity_curve.jsonl"), "a") as f:
            rec = {"ts": now, "total": round(float(total), 2), "source": source}
            if cash is not None:
                rec["cash"] = round(float(cash), 2)
            f.write(json.dumps(rec) + "\n")
        return True
    except Exception:
        return False


def realized_pnl_series():
    """Cumulative realized P&L by exit date from closed trades (backfill curve)."""
    log = trade_log()
    trades = sorted((t for t in log.get("trades", []) if t.get("exit_date")),
                    key=lambda t: t["exit_date"])
    cum, out = 0.0, []
    for t in trades:
        cum += float(t.get("pnl_dollar") or 0)
        out.append({"ts": t["exit_date"], "cum_pnl": round(cum, 2),
                    "trade_id": t.get("id"), "symbol": t.get("symbol"),
                    "pnl": t.get("pnl_dollar")})
    return out


def performance_summary():
    log = trade_log()
    s = log.get("summary", {})
    trades = log.get("trades", [])
    by_reason = {}
    for t in trades:
        r = (t.get("exit_reason") or ("stop_loss" if t.get("stop_loss") else "unknown"))
        by_reason[r] = by_reason.get(r, 0) + 1
    return {"summary": s, "n_open": len(log.get("open_positions", [])),
            "exit_reasons": by_reason}


# ---------------------------------------------------------------- unified log
_WD_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:+\-]+)\s+(.*)$")
_AGENT_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})\]")


def unified_log(limit=250):
    """One chronological ops feed (newest first) across every event stream:
    manual dashboard actions, strategy/skill change events, watchdog lines,
    and agent-log warnings. Each event: {ts, kind, title, detail, severity, ok}."""
    events = []
    for a in manual_actions(200):
        ok = (a.get("result") or {}).get("ok") is not False
        events.append({
            "ts": a.get("ts") or "", "kind": "manual",
            "title": a.get("action") or "action",
            "detail": json.dumps(a.get("params") or {})[:180],
            "severity": None if ok else "MAJOR", "ok": ok})
    for e in change_events()[:120]:
        events.append({
            "ts": e.get("timestamp") or "", "kind": "change",
            "title": f"{e.get('kind', 'change')}: {e.get('target', '')} "
                     f"v{e.get('version', '?')}",
            "detail": (e.get("summary") or "")[:220],
            "severity": e.get("severity"), "ok": True})
    for line in watchdog_recent(80):
        m = _WD_TS_RE.match(line.strip())
        ts, body = (m.group(1), m.group(2)) if m else ("", line.strip())
        events.append({
            "ts": ts, "kind": "watchdog", "title": body[:160],
            "detail": "",
            "severity": "MAJOR" if "ALERT" in body else None,
            "ok": "ALERT" not in body})
    for w in agent_log_warnings(50):
        line = w["line"]
        m = _AGENT_TS_RE.search(line)
        ts = f"{m.group(1)}T{m.group(2)}:{m.group(3)}:00" if m else ""
        sev = "MAJOR" if ("WARNING" in line or "KILL-SWITCH" in line
                          or "[ALERT]" in line) else None
        events.append({"ts": ts, "kind": "agent", "title": line[:180],
                       "detail": w["file"], "severity": sev, "ok": sev is None})
    events.sort(key=lambda e: e["ts"] or "0000", reverse=True)
    return events[:limit]
