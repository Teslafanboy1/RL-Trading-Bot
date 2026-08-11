"""server.py — TradeCommand HTTP server (stdlib only).

    python3 dashboard/server.py                 # serve on 127.0.0.1:8787
    python3 dashboard/server.py --set-pin       # set/replace the trade PIN
    python3 dashboard/server.py --port 8899

Binds LOOPBACK ONLY by default: remote access is expected to come through
`tailscale serve` (HTTPS on your private tailnet), so nothing is ever exposed
to the LAN or internet. Reads are open to whoever can reach the port (your
tailnet devices); every money/control action additionally requires a PIN-armed
token (see controls.Armory).

GET  /api/overview /api/thinking /api/performance /api/trades /api/strategy
     /api/risk /api/news /api/market /api/shadow /api/actions /api/alerts
     /api/postmortem?file= /api/research_file?file= /api/meta
     /api/symbol?sym=&interval= /api/screen?symbols=&fresh= /api/logs
     /api/library /api/signals /api/orders?days= /api/rx3 /api/health
     /api/learning /api/calendar
POST /api/arm /api/disarm /api/broker/refresh /api/broker/retry_direct
     /api/order/preview /api/order/place /api/order/cancel
     /api/bot/pause /api/bot/resume /api/bot/halt /api/bot/clear_halt
     /api/dnt /api/stop_override /api/watchlist
"""

import os
import sys
import json
import time
import getpass
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import readers, quotes, controls, broker as broker_mod  # noqa: E402
import signals  # noqa: E402 — EMA math for the Analyzer chart overlay
import momentum_screen  # noqa: E402 — pure multi-timeframe screen (Screener tab)

ET = ZoneInfo("America/New_York")
ROOT = readers.ROOT
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

HOST = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))

_previews = {}                 # preview_id -> {params, review, created, ip}
_previews_lock = threading.Lock()
_refresh_gate = {"last": 0.0}
_started = {"epoch": time.time(),
            "iso": datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")}


# ================================================================ aggregation
def _watchlist():
    d = readers.load_json("dashboard/watchlist.json", None)
    if d is None:
        d = {"symbols": ["SPY", "QQQ", "NVDA", "MU", "AMAT", "LABU"]}
    return [s.upper() for s in d.get("symbols", [])][:40]


def _symbols_of_interest(log=None, brk=None):
    log = log or readers.trade_log()
    syms = [p["symbol"] for p in log.get("open_positions", [])]
    if brk and brk.get("positions"):
        syms += [p["symbol"] for p in brk["positions"]]
    files = [f for f in readers.research_files() if f["kind"] == "picks"]
    if files:
        syms += [p["symbol"] for p in readers.parse_picks_file(files[0]["file"]).get("picks", [])]
    syms += _watchlist()
    return list(dict.fromkeys(syms))


def _stop_levels_for(symbol, pos, strategy, overrides):
    """Effective stop / trail levels for a position, override-aware (mirrors
    agent.write_stop_snapshot semantics so the UI shows what will fire)."""
    entry = float(pos.get("entry_price") or 0)
    peak = float(pos.get("peak_price") or entry or 0)
    rm = strategy.get("risk_management", {}) or {}
    base_stop = float(rm.get("stop_loss_pct", 0.10) or 0.10)
    trail = float(rm.get("trailing_stop_pct") or 0)
    ov = overrides.get(symbol.upper(), {}) if isinstance(overrides, dict) else {}
    stop_price = None
    if ov.get("stop_price"):
        stop_price = float(ov["stop_price"])
    elif entry > 0:
        pct = float(ov.get("stop_pct") or base_stop)
        stop_price = entry * (1 - pct)
    t_pct = float(ov.get("trail_pct") or trail or 0)
    trail_price = peak * (1 - t_pct) if (t_pct > 0 and peak > 0) else None
    return {"stop_price": round(stop_price, 4) if stop_price else None,
            "trail_price": round(trail_price, 4) if trail_price else None,
            "override": {k: v for k, v in ov.items() if k != "set_at"} or None}


def _cycle_running(max_age=1200):
    """True only if cycle_status says running AND the stamp is fresh — a bot
    crash mid-cycle must not leave the collision warning stuck on forever."""
    cyc = readers.cycle_status()
    if cyc.get("state") != "running":
        return False
    ts = readers._parse_ts(cyc.get("ts"))
    return bool(ts and (time.time() - ts) < max_age)


def _bot_status():
    hb = readers.heartbeat()
    halt = readers.halt_state()
    pause = readers.pause_state()
    cyc = dict(readers.cycle_status(), running_fresh=_cycle_running())
    market = quotes.market_open_now()
    if halt["halted"]:
        state = "halted"
    elif pause["paused"]:
        state = "paused"
    elif hb["age_seconds"] is None:
        state = "unknown"
    elif market and hb["age_seconds"] > 30 * 60:
        state = "stale"
    elif not market and hb["age_seconds"] > 26 * 3600:
        state = "stale"
    else:
        state = "running"
    return {"state": state, "market_open": market, "heartbeat": hb,
            "halt": halt, "pause": pause, "cycle": cyc,
            "risk_state": readers.risk_state()}


def overview():
    log = readers.trade_log()
    brk = broker.cached()
    pf = brk.get("portfolio") or {}
    strategy = readers.strategy()
    overrides = readers.stop_overrides()
    positions = []
    open_pos = log.get("open_positions", [])
    broker_by_sym = {p["symbol"]: p for p in (brk.get("positions") or [])}
    day_pnl = 0.0
    live_equity = 0.0
    for pos in open_pos:
        sym = pos["symbol"]
        q = quotes.quote(sym)
        rib = quotes.ribbon(sym)
        shares = float(broker_by_sym.get(sym, {}).get("shares") or pos.get("shares") or 0)
        entry = float(pos.get("entry_price") or 0)
        price = (q or {}).get("price")
        stops = _stop_levels_for(sym, pos, strategy, overrides)
        unreal = (price - entry) * shares if (price and entry) else None
        day = ((price - (q or {}).get("prev_close")) * shares
               if (price and (q or {}).get("prev_close")) else None)
        if day:
            day_pnl += day
        if price:
            live_equity += price * shares
        positions.append({
            "id": pos.get("id"), "symbol": sym, "shares": shares,
            "entry_price": entry, "peak_price": pos.get("peak_price"),
            "entry_date": pos.get("entry_date"),
            "confidence": pos.get("confidence_score"),
            "thesis": pos.get("thesis") or "",
            "adopted": bool(pos.get("adopted")),
            "price": price, "prev_close": (q or {}).get("prev_close"),
            "spark": (q or {}).get("spark", []),
            "day_pnl": round(day, 2) if day is not None else None,
            "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
            "unrealized_pct": round((price / entry - 1) * 100, 2) if (price and entry) else None,
            "ema_state": rib.get("state"), "ema_transition": rib.get("transition"),
            "ema_ok": rib.get("ok", False),
            "stop": stops,
            "dnt": sym in readers.do_not_trade(),
            "locked": controls.manual_lock_active(sym),
        })
    cash = pf.get("cash")
    total_broker = pf.get("total")
    # Re-marking the broker's last snapshot against live Yahoo quotes is only
    # trustworthy while the market's open — pre/post-market those quotes are
    # thin and can drift from the broker's own mark by real money (caught
    # 2026-08-11: live_total read $1.50 high against a broker-confirmed total
    # that matched the actual Robinhood app to the penny). The real kill-switch
    # (risk_guard.check_halt) never reads this value — it always uses agent.py's
    # own read_broker_state() — so this only affects this display card.
    live_total = ((live_equity + cash) if (cash is not None and live_equity
                  and quotes.market_open_now()) else None)
    rs = readers.risk_state()
    peak = float(rs.get("peak") or 0)
    ref_total = live_total or total_broker
    dd = (peak - ref_total) / peak if (peak and ref_total) else None
    trades_today = [t for t in log.get("trades", [])
                    if str(t.get("exit_date", ""))[:10] == datetime.now(ET).strftime("%Y-%m-%d")]
    realized_today = round(sum(float(t.get("pnl_dollar") or 0) for t in trades_today), 2)
    return {
        "account": {"total": total_broker, "live_total": live_total,
                    "equity": pf.get("equity"), "cash": cash,
                    "buying_power": pf.get("buying_power"),
                    "asof": pf.get("asof"), "source": pf.get("source"),
                    "updated": brk.get("updated"), "error": brk.get("error"),
                    "cash_pct": round(cash / total_broker * 100, 1)
                                if (cash is not None and total_broker) else None},
        "broker": brk.get("broker"),
        "day_pnl_positions": round(day_pnl, 2),
        "realized_today": realized_today,
        "positions": positions,
        "orders_today": brk.get("orders") or [],
        "bot": _bot_status(),
        "killswitch": {"peak": peak or None, "current": ref_total,
                       "drawdown": round(dd, 4) if dd is not None else None,
                       "threshold": 0.25,
                       "halt_at_value": round(peak * 0.75, 2) if peak else None},
        "summary": log.get("summary", {}),
        "alerts_count": len([a for a in readers.alerts() if a["level"] != "info"]),
        "indices": quotes.indices()[:4],
        "dnt": readers.do_not_trade(),
        "server_time": datetime.now(ET).isoformat(timespec="seconds"),
    }


def thinking():
    files = readers.research_files()
    picks_meta = next((f for f in files if f["kind"] == "picks"), None)
    midweek_meta = next((f for f in files if f["kind"] == "midweek"), None)
    picks = readers.parse_picks_file(picks_meta["file"]) if picks_meta else {}
    held = {p["symbol"] for p in readers.trade_log().get("open_positions", [])}
    for p in picks.get("picks", []):
        rib = quotes.ribbon(p["symbol"])
        q = quotes.quote(p["symbol"])
        p["ema_state"] = rib.get("state")
        p["price"] = (q or {}).get("price")
        p["held"] = p["symbol"] in held
    return {"picks": picks,
            "picks_file": picks_meta,
            "midweek": readers.parse_midweek_file(midweek_meta["file"]) if midweek_meta else {},
            "midweek_file": midweek_meta,
            "momentum_watch": readers.parse_momentum_watch(),
            "rewrite_queue": [e for e in readers.rewrite_queue()],
            "files": files[:14]}


def performance():
    curve = readers.equity_curve()
    realized = readers.realized_pnl_series()
    spy = quotes.daily_series("SPY", "6mo")
    rx3 = readers.rx3_paper()
    rx3_curve = []
    if isinstance(rx3, dict):
        for pt in rx3.get("equity_curve", rx3.get("curve", []) or []):
            if isinstance(pt, dict) and (pt.get("equity") or pt.get("value")):
                rx3_curve.append({"ts": pt.get("date") or pt.get("ts"),
                                  "value": pt.get("equity") or pt.get("value")})
    # drawdown series off the recorded equity curve
    dd, peak = [], 0.0
    for pt in curve:
        peak = max(peak, pt["total"])
        dd.append({"ts": pt["ts"], "dd": round((pt["total"] - peak) / peak * 100, 2) if peak else 0})
    log = readers.trade_log()
    trades = log.get("trades", [])
    monthly = (readers.strategy().get("progress_tracking") or {})
    return {"equity_curve": curve, "drawdown": dd, "realized": realized,
            "benchmark_spy": spy, "rx3_curve": rx3_curve,
            "rx3_meta": {k: v for k, v in rx3.items() if not isinstance(v, (list, dict))}
                        if isinstance(rx3, dict) else {},
            "summary": log.get("summary", {}),
            "per_trade": [{"id": t.get("id"), "symbol": t.get("symbol"),
                           "pnl": t.get("pnl_dollar"), "pnl_pct": t.get("pnl_pct"),
                           "exit_date": t.get("exit_date")} for t in trades],
            "progress": monthly,
            "exit_reasons": readers.performance_summary()["exit_reasons"]}


def trades_view():
    log = readers.trade_log()
    pms = {m["file"]: m for m in readers.postmortem_files()}
    rows = []
    for t in log.get("trades", []):
        rows.append(dict(t, has_analysis=bool(t.get("analysis_file") in pms)))
    rows.sort(key=lambda t: str(t.get("exit_date") or ""), reverse=True)
    return {"trades": rows, "postmortems": readers.postmortem_files(),
            "open_positions": log.get("open_positions", [])}


def strategy_view():
    s = readers.strategy()
    view_keys = ["version", "goal", "last_updated", "account", "ema_strategy",
                 "research", "capital_allocation", "blackout_windows",
                 "progress_tracking", "risk_management", "position_sizing",
                 "factor_exposure_limits", "concentration", "options", "rotation",
                 "learned_rules", "observations_pending_confirmation"]
    cfg = {k: s.get(k) for k in view_keys if k in s}
    return {"config": cfg,
            "version": s.get("version"),
            "version_history": readers.strategy_version_history(),
            "change_events": readers.change_events(),
            "skill_versions": readers.skill_versions(),
            "history_files": readers.strategy_history_files()}


def risk_view():
    log = readers.trade_log()
    strategy = readers.strategy()
    overrides = readers.stop_overrides()
    stops_file = readers.stops()
    rows = []
    for pos in log.get("open_positions", []):
        sym = pos["symbol"]
        q = quotes.quote(sym)
        price = (q or {}).get("price")
        levels = _stop_levels_for(sym, pos, strategy, overrides)
        d_stop = ((price - levels["stop_price"]) / price * 100
                  if (price and levels["stop_price"]) else None)
        d_trail = ((price - levels["trail_price"]) / price * 100
                   if (price and levels["trail_price"]) else None)
        rows.append({"symbol": sym, "shares": pos.get("shares"),
                     "entry": pos.get("entry_price"), "peak": pos.get("peak_price"),
                     "price": price,
                     "stop_price": levels["stop_price"],
                     "trail_price": levels["trail_price"],
                     "dist_stop_pct": round(d_stop, 2) if d_stop is not None else None,
                     "dist_trail_pct": round(d_trail, 2) if d_trail is not None else None,
                     "override": levels["override"],
                     "watchdog_stop": (stops_file.get("stops") or {}).get(sym)})
    ov = overview()
    return {"positions": rows,
            "killswitch": ov["killswitch"],
            "bot": ov["bot"],
            "risk_management": strategy.get("risk_management", {}),
            "overrides": {k: v for k, v in overrides.items() if not k.startswith("_")},
            "dnt": readers.do_not_trade(),
            "stops_file": stops_file,
            "watchdog_log": readers.watchdog_recent(30),
            "cash_flows": readers.cash_flows(20)}


def news_view():
    held = [p["symbol"] for p in readers.trade_log().get("open_positions", [])]
    sym_news = {s: quotes.symbol_news(s, 6) for s in held[:8]}
    return {"citations": readers.news_citations(),
            "leaderboard": readers.source_leaderboard(),
            "verdicts": readers.source_verdicts(),
            "general": quotes.general_news(),
            "symbol_news": sym_news}


_earnings_cache = {"at": 0, "data": None}


def market_view():
    wl = _watchlist()
    held = [p["symbol"] for p in readers.trade_log().get("open_positions", [])]
    rows = []
    for s in dict.fromkeys(held + wl):
        q = quotes.quote(s)
        rib = quotes.ribbon(s)
        rows.append({"symbol": s, "held": s in held,
                     "price": (q or {}).get("price"),
                     "change_pct": (q or {}).get("change_pct"),
                     "spark": (q or {}).get("spark", []),
                     "ema_state": rib.get("state"),
                     "ema_transition": rib.get("transition")})
    return {"indices": quotes.indices(), "watchlist": rows,
            "watchlist_symbols": wl, "earnings": _earnings(),
            "held": held, "general_news": quotes.general_news(20)}


def _earnings(days=14):
    """Earnings calendar via the broker, 6h-cached. Free on direct-mcp only —
    the claude-cli path costs a model call, so it stays on-demand elsewhere."""
    if _earnings_cache["data"] is not None and time.time() - _earnings_cache["at"] < 6 * 3600:
        return _earnings_cache["data"]
    if broker.mode()["mode"] == "direct-mcp":
        try:
            earnings = broker.earnings(days=days)
            _earnings_cache.update(at=time.time(), data=earnings)
            return earnings
        except Exception as e:
            return {"error": str(e)[:200]}
    return {"unavailable": "connect direct broker (rh_login) or press "
                           "refresh on the Market tab to fetch via claude -p"}


def shadow_view():
    brk = broker.cached()
    total = (brk.get("portfolio") or {}).get("total")
    shadow = readers.options_shadow()
    momentum = readers.momentum_last_scan()
    # enrich open shadow positions with the underlying's live quote
    for p in shadow.get("positions", []):
        if p.get("status") == "open":
            q = quotes.quote(p.get("underlying", ""))
            p["underlying_price_now"] = (q or {}).get("price")
    rx3 = readers.rx3_paper()
    return {"options_shadow": shadow,
            "momentum_scan": momentum,
            "momentum_watch": readers.parse_momentum_watch(),
            "rx3": rx3 if isinstance(rx3, dict) else {},
            "go_live": readers.go_live_checklist(total),
            "options_config": (readers.strategy().get("options") or {})}


def symbol_view(handler):
    """Analyzer: full chart bars + EMA-ribbon overlay + signal + stats + news
    for ANY symbol, at 1d (long view) or the bot's intraday intervals."""
    sym = (_q(handler, "sym") or "SPY").upper().strip()
    interval = _q(handler, "interval") or "1d"
    if interval not in ("1d", "1h", "30m"):
        interval = "1d"
    rng = {"1d": "1y", "1h": "1mo", "30m": "1mo"}[interval]
    bars = quotes.bar_series(sym, interval, rng)
    closes = [b["close"] for b in bars]
    ema_series = {}
    if len(closes) >= 60:
        for name, length in signals.LENGTHS.items():
            ema_series[name] = [round(v, 4) for v in signals.ema(closes, length)]
    daily = closes if interval == "1d" else [b["close"]
                                             for b in quotes.daily_series(sym, "1y")]
    returns, score = {}, None
    if daily:
        try:
            returns = momentum_screen.multi_timeframe_returns(daily)
            score = momentum_screen.momentum_score(returns)
        except Exception:
            pass
    hi = max(daily) if daily else None
    lo = min(daily) if daily else None
    strategy = readers.strategy()
    log = readers.trade_log()
    pos = next((p for p in log.get("open_positions", [])
                if p.get("symbol") == sym), None)
    stops = (_stop_levels_for(sym, pos, strategy, readers.stop_overrides())
             if pos else None)
    past = [t for t in log.get("trades", []) if t.get("symbol") == sym]
    return {
        "symbol": sym, "interval": interval,
        "bars": bars, "ema_series": ema_series,
        "quote": quotes.quote(sym), "ribbon": quotes.ribbon(sym),
        "position": pos, "stops": stops,
        "held": bool(pos), "dnt": sym in readers.do_not_trade(),
        "stats": {"high_52w": hi, "low_52w": lo,
                  "off_high_pct": (round((daily[-1] / hi - 1) * 100, 2)
                                   if (hi and daily) else None),
                  "returns": {k: (round(v * 100, 2) if v is not None else None)
                              for k, v in returns.items()},
                  "momentum_score": score},
        "news": quotes.symbol_news(sym, 8),
        "past_trades": [{"id": t.get("id"), "outcome": t.get("outcome"),
                         "pnl_pct": t.get("pnl_pct"),
                         "exit_reason": t.get("exit_reason"),
                         "exit_date": t.get("exit_date")} for t in past][-6:],
    }


_screen_cache = {"at": 0.0, "key": "", "rows": None}


def screen_view(handler):
    """Screener: the bot's own multi-timeframe momentum screen (momentum_screen.py)
    run over held + picks + watchlist + the configured momentum universe.
    Free (Yahoo daily closes, cached), ranked by score. ?symbols=A,B overrides."""
    syms_param = _q(handler, "symbols")
    strategy = readers.strategy()
    log = readers.trade_log()
    held = [p["symbol"] for p in log.get("open_positions", [])]
    picks = []
    files = [f for f in readers.research_files() if f["kind"] == "picks"]
    if files:
        picks = [p["symbol"]
                 for p in readers.parse_picks_file(files[0]["file"]).get("picks", [])]
    mom_cfg = ((strategy.get("options") or {}).get("momentum") or {})
    if syms_param:
        syms = [s.strip().upper() for s in syms_param.split(",") if s.strip()][:40]
    else:
        universe = [str(u).upper() for u in (mom_cfg.get("universe") or [])]
        base = held + picks + _watchlist() + universe
        syms = list(dict.fromkeys(s for s in base if s and not s.startswith("^")))[:28]
    key = ",".join(syms)
    now = time.time()
    fresh = _q(handler, "fresh") == "1"
    if (not fresh and _screen_cache["rows"] is not None
            and _screen_cache["key"] == key
            and now - _screen_cache["at"] < 600):
        rows = _screen_cache["rows"]
    else:
        cand = {}
        for s in syms:
            closes = [b["close"] for b in quotes.daily_series(s, "1y")]
            if closes:
                cand[s] = {"closes": closes, "last_price": closes[-1]}
        rows = []
        for e in momentum_screen.screen(cand, mom_cfg):
            closes = cand.get(e["symbol"], {}).get("closes") or []
            chg = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None
            rows.append(dict(
                e,
                returns={k: (round(v * 100, 2) if v is not None else None)
                         for k, v in (e.get("returns") or {}).items()},
                change_pct=round(chg, 2) if chg is not None else None,
                held=e["symbol"] in held,
                pick=e["symbol"] in picks))
        _screen_cache.update(at=now, key=key, rows=rows)
    return {"rows": rows, "count": len(rows),
            "anchor": mom_cfg.get("anchor_timeframe", "1m"),
            "min_score": mom_cfg.get("min_score", 55),
            "generated_at": datetime.now(ET).isoformat(timespec="seconds")}


def signals_view():
    """Ribbon lab: the bot's exact EMA signal (state, transition, all four
    lines) for every symbol of interest, plus a knot-margin metric showing how
    far the red(55) sits from the fast pack — i.e. how close a flip is."""
    log = readers.trade_log()
    held = {p["symbol"] for p in log.get("open_positions", [])}
    rows = []
    for s in _symbols_of_interest(log=log)[:30]:
        rib = quotes.ribbon(s)
        q = quotes.quote(s)
        row = {"symbol": s, "held": s in held,
               "price": (q or {}).get("price"),
               "change_pct": (q or {}).get("change_pct"),
               "spark": (q or {}).get("spark", []),
               "state": rib.get("state"), "transition": rib.get("transition"),
               "ok": rib.get("ok", False), "lines": rib.get("lines"),
               "last_close": rib.get("last_close"), "bars": rib.get("bars"),
               "source": rib.get("source"), "warmup_ok": rib.get("warmup_ok"),
               "note": rib.get("note") or None,
               "dnt": s in readers.do_not_trade()}
        lines = rib.get("lines") or {}
        red = lines.get("red")
        pack = [lines[k] for k in ("blue", "green", "yellow") if lines.get(k)]
        if red and pack:
            # + margin = red below the whole pack (BUY zone), − = above (SELL)
            row["red_margin_pct"] = round((min(pack) - red) / red * 100, 3)
        rows.append(row)
    order = {"BUY": 0, "SELL": 1, "NEUTRAL": 2}
    rows.sort(key=lambda r: (0 if r["held"] else 1,
                             order.get(r.get("state"), 3), r["symbol"]))
    return {"rows": rows, "interval": signals.INTERVAL,
            "lengths": signals.LENGTHS, "warmup_bars": signals.WARMUP_OK,
            "generated_at": datetime.now(ET).isoformat(timespec="seconds")}


def orders_view(handler):
    """Order center: today's broker orders (cached snapshot) + optional deeper
    history (?days=N, live fetch — free on direct-mcp only) + the dashboard's
    own order journal entries."""
    try:
        days = max(1, min(60, int(_q(handler, "days") or 1)))
    except ValueError:
        days = 1
    brk = broker.cached()
    out = {"today": brk.get("orders") or [], "updated": brk.get("updated"),
           "broker": broker.mode(), "days": days, "history": None,
           "history_error": None}
    if days > 1:
        if broker.mode()["mode"] == "direct-mcp":
            try:
                out["history"] = broker.orders(since_days=days)
            except Exception as e:
                out["history_error"] = str(e)[:200]
        else:
            out["history_error"] = ("order history needs the direct broker "
                                    "connection (python3 -m dashboard.rh_login)")
    out["journal"] = [a for a in readers.manual_actions(150)
                      if str(a.get("action", "")).startswith("order_")][:40]
    return out


def rx3_view():
    """RX-3 rotation engine — paper phase tracker: equity curve, current book,
    per-day decisions (leaders/defensive/RISKX/vol), promotion-ladder progress,
    and the live rotation config."""
    d = readers.rx3_paper()
    if not isinstance(d, dict):
        d = {}
    hist = [h for h in (d.get("history") or []) if isinstance(h, dict)]
    curve = [{"ts": h.get("date"), "value": h.get("equity")} for h in hist]
    positions = []
    for sym, p in sorted((d.get("positions") or {}).items()):
        if not isinstance(p, dict):
            continue
        q = quotes.quote(sym)
        px = (q or {}).get("price") or p.get("last_px")
        sh = float(p.get("shares") or 0)
        positions.append({"symbol": sym, "shares": sh,
                          "last_px": p.get("last_px"),
                          "price": (q or {}).get("price"),
                          "change_pct": (q or {}).get("change_pct"),
                          "value": round(float(px) * sh, 4) if px else None})
    start_eq = d.get("start_equity")
    ret_pct = ((float(d["equity"]) / float(start_eq) - 1) * 100
               if (d.get("equity") and start_eq) else None)
    start_day = str(d.get("start_date") or "")[:10]
    spy = [p for p in quotes.daily_series("SPY", "6mo")
           if not start_day or p["ts"] >= start_day]
    cfg = readers.strategy().get("rotation") or {}
    paper_days = len({h.get("date") for h in hist})
    return {"meta": {"note": d.get("_note"), "start_date": d.get("start_date"),
                     "start_equity": start_eq, "equity": d.get("equity"),
                     "cash": d.get("cash"), "last_run": d.get("last_run"),
                     "return_pct": round(ret_pct, 2) if ret_pct is not None else None},
            "latest": hist[-1] if hist else None,
            "leaders": d.get("leaders") or [],
            "positions": positions, "curve": curve, "history": hist[-60:][::-1],
            "paper_days": paper_days, "promotion_gate_days": 10,
            "config": {k: v for k, v in cfg.items() if k != "_note"},
            "spy_since_start": spy}


def health_view():
    """System deep-dive: bot/cycle/heartbeat state, every control-plane file,
    state-file freshness, watchdog + agent-log tails, broker path, server info."""
    state_files = [
        ("Heartbeat", "logs/heartbeat"),
        ("Cycle status", "logs/cycle_status.json"),
        ("Trade log", "trade_log.json"),
        ("Strategy", "strategy/strategy.json"),
        ("Equity curve", "logs/equity_curve.jsonl"),
        ("Stops snapshot", "logs/stops.json"),
        ("Risk state", "logs/risk_state.json"),
        ("RX-3 paper", "shadow/rx3_paper.json"),
        ("Options shadow", "shadow/options_shadow_log.json"),
        ("Momentum scan", "shadow/momentum_last_scan.json"),
        ("Watchdog log", "logs/watchdog.log"),
        ("Manual journal", "logs/manual_actions.jsonl"),
        ("Change events", "logs/change_events.jsonl"),
        ("Usage window", "logs/usage_state.json"),
        ("Preflight check", "logs/preflight.json"),
        ("Analysis queue", "logs/analysis_queue.jsonl"),
    ]
    files = []
    for label, rel in state_files:
        age = readers.file_age_seconds(rel)
        files.append({"label": label, "path": rel,
                      "age_seconds": age, "exists": age is not None})
    picks = next((f for f in readers.research_files() if f["kind"] == "picks"), None)
    curve = readers.equity_curve()
    overrides = {k: v for k, v in readers.stop_overrides().items()
                 if not str(k).startswith("_")}
    return {"bot": _bot_status(),
            "files": files,
            "control": {"pause": readers.pause_state(),
                        "halt": readers.halt_state(),
                        "dnt": readers.do_not_trade(),
                        "overrides": overrides},
            "usage": _usage_status(),
            "watchdog_log": readers.watchdog_recent(50),
            "agent_warnings": readers.agent_log_warnings(20),
            "latest_picks": picks,
            "broker": broker.mode(),
            "pin_configured": controls.pin_configured(),
            "server": {"started": _started["iso"],
                       "uptime_seconds": round(time.time() - _started["epoch"], 1),
                       "host": HOST, "port": PORT, "root": ROOT,
                       "python": sys.version.split()[0]},
            "equity_points": len(curve),
            "last_equity": curve[-1] if curve else None}


def learning_view():
    """The learning loop in one payload: analyses, source accuracy, verdicts,
    rewrite queue, learned rules, pending observations, skill/version churn."""
    s = readers.strategy()
    return {"postmortems": readers.postmortem_files(),
            "leaderboard": readers.source_leaderboard(),
            "verdicts": readers.source_verdicts(),
            "rewrite_queue": readers.rewrite_queue(),
            # list (not dict) so client-side snake_case key conversion can't
            # mangle skill names
            "skill_versions": [dict(skill=k, **v) for k, v in
                               sorted(readers.skill_versions().items())],
            "change_events": readers.change_events()[:40],
            "learned_rules": s.get("learned_rules") or [],
            "observations": s.get("observations_pending_confirmation") or [],
            "confidence_calibration": s.get("confidence_calibration") or {}}


def _bot_schedule(now=None):
    """Upcoming bot events over the next week (ET, weekdays).

    Mirrors the usage-window schedule in agent.py: each phase is placed so it
    lands in its own rolling 5-hour Claude session window. Times are derived
    from strategy.json → usage, so editing the config moves this view too."""
    from datetime import timedelta
    now = now or datetime.now(ET)
    u = (readers.strategy().get("usage") or {})
    anchor_min = int(u.get("anchor_minutes_before_open", 305))
    research_min = int(u.get("research_minutes_before_open", 35))
    maint_h = int(u.get("maintenance_hour_et", 19))
    maint_m = int(u.get("maintenance_minute_et", 35))
    events = []
    for d in range(0, 8):
        day = (now + timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        open_t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
        slots = [
            (open_t - timedelta(minutes=anchor_min),
             "Pre-market system check — anchors the 5h usage window", "preflight"),
            (open_t - timedelta(minutes=research_min),
             "Pre-market research (Opus)", "research"),
            (open_t, "Market open — trading loop (fresh usage window)", "open"),
            (datetime(day.year, day.month, day.day, 12, 0, tzinfo=ET),
             "Midweek review", "midweek"),
            (datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET),
             "Market close — trading stops", "close"),
            (datetime(day.year, day.month, day.day, maint_h, maint_m, tzinfo=ET),
             "Maintenance — postmortems + strategy rewrites", "maintenance"),
        ]
        for t, label, kind in slots:
            if kind == "midweek" and day.weekday() != 2:
                continue
            if t > now:
                events.append({"when": t.isoformat(timespec="seconds"),
                               "label": label, "kind": kind})
    events.sort(key=lambda e: e["when"])
    return events[:12]


def _usage_status():
    """Claude 5h-session-window state for the operator: how much of the current
    window is spent, when it expires, and whether a 429 cooldown is muting the
    non-protective tiers. Read-only and never raises — the dashboard must render
    even if the governor module is missing."""
    try:
        import usage_governor
        st = usage_governor.status()
        cfg = usage_governor.config()
        st["schedule"] = {
            "anchor_minutes_before_open": cfg.get("anchor_minutes_before_open"),
            "research_minutes_before_open": cfg.get("research_minutes_before_open"),
            "maintenance_et": (f"{cfg.get('maintenance_hour_et', 19):02d}:"
                               f"{cfg.get('maintenance_minute_et', 35):02d}"),
        }
        return st
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def calendar_view():
    """Market calendar: broker earnings feed, the strategy's blackout windows,
    and the bot's own upcoming schedule."""
    s = readers.strategy()
    held = [p["symbol"] for p in readers.trade_log().get("open_positions", [])]
    return {"earnings": _earnings(),
            "blackout_windows": s.get("blackout_windows") or {},
            "held": held, "watchlist": _watchlist(),
            "schedule": _bot_schedule(),
            "market_open": quotes.market_open_now(),
            "server_time": datetime.now(ET).isoformat(timespec="seconds")}


def meta_view():
    return {"server_time": datetime.now(ET).isoformat(timespec="seconds"),
            "market_open": quotes.market_open_now(),
            "broker": broker.mode(),
            "pin_configured": controls.pin_configured(),
            "poll_seconds": 20 if quotes.market_open_now() else 90,
            "account": broker_mod.ACCOUNT,
            "version": "1.0"}


# ================================================================ order flow
def order_preview(params, ip):
    symbol = str(params.get("symbol", "")).upper().strip()
    side = params.get("side")
    otype = params.get("type", "market")
    if not symbol or side not in ("buy", "sell"):
        return {"ok": False, "error": "symbol and side (buy/sell) required"}, 400
    if otype not in ("market", "limit", "stop_market", "stop_limit"):
        return {"ok": False, "error": f"bad order type {otype}"}, 400
    qty = params.get("quantity")
    notional = params.get("dollar_amount")
    if not qty and not notional:
        return {"ok": False, "error": "quantity or dollar_amount required"}, 400
    if qty and notional:
        return {"ok": False, "error": "provide only one of quantity / dollar_amount"}, 400
    if notional and otype != "market":
        return {"ok": False, "error": "dollar_amount only valid with market orders"}, 400
    warnings = []
    if symbol in readers.do_not_trade() and side == "buy":
        warnings.append(f"{symbol} is on YOUR do-not-trade list.")
    if _cycle_running():
        warnings.append("Bot cycle is running RIGHT NOW — it may act on the same "
                        "symbols within seconds.")
    if controls.manual_lock_active(symbol):
        return {"ok": False, "error": f"{symbol} is locked by another in-flight "
                                      "manual order — wait a moment"}, 409
    if not quotes.market_open_now():
        warnings.append("Market is CLOSED — the order will queue for the next open; "
                        "fractional/dollar orders may be rejected off-hours.")
    pf = (broker.cached().get("portfolio") or {})
    q = quotes.quote(symbol)
    est_cost = None
    px = (q or {}).get("price")
    if px:
        est_cost = float(notional) if notional else float(qty) * px
    if side == "buy" and est_cost and pf.get("buying_power") is not None \
            and est_cost > pf["buying_power"]:
        warnings.append(f"Estimated cost ${est_cost:,.2f} EXCEEDS buying power "
                        f"${pf['buying_power']:,.2f} (T+1 settled cash).")
    if side == "sell":
        held = {p["symbol"]: p for p in (broker.cached().get("positions") or [])}
        h = held.get(symbol)
        log_pos = next((p for p in readers.trade_log().get("open_positions", [])
                        if p["symbol"] == symbol), None)
        avail = (h or {}).get("sellable_shares") or (log_pos or {}).get("shares")
        if avail is None:
            warnings.append(f"No {symbol} position visible in the latest broker "
                            "snapshot — verify before selling.")
        elif qty and float(qty) > float(avail) + 1e-9:
            return {"ok": False,
                    "error": f"sell qty {qty} exceeds sellable {avail}"}, 400
    review = broker.review_order(symbol=symbol, side=side, type=otype,
                                 quantity=qty, dollar_amount=notional,
                                 limit_price=params.get("limit_price"),
                                 stop_price=params.get("stop_price"),
                                 time_in_force=params.get("time_in_force"))
    pid = secrets.token_urlsafe(12)
    with _previews_lock:
        now = time.time()
        for k in [k for k, v in _previews.items() if now - v["created"] > 180]:
            _previews.pop(k, None)
        _previews[pid] = {"params": {"symbol": symbol, "side": side, "type": otype,
                                     "quantity": qty, "dollar_amount": notional,
                                     "limit_price": params.get("limit_price"),
                                     "stop_price": params.get("stop_price"),
                                     "time_in_force": params.get("time_in_force")},
                          "created": now, "ip": ip}
    controls.journal("order_preview", _previews[pid]["params"],
                     {"ok": True, "warnings": warnings}, ip)
    return {"ok": True, "preview_id": pid, "params": _previews[pid]["params"],
            "quote": q, "est_cost": round(est_cost, 2) if est_cost else None,
            "warnings": warnings, "broker_review": review,
            "expires_in": 180}, 200


def order_place(params, ip):
    pid = params.get("preview_id")
    with _previews_lock:
        pv = _previews.pop(pid or "", None)
    if not pv:
        return {"ok": False, "error": "preview expired or unknown — preview again"}, 410
    p = pv["params"]
    sym = p["symbol"]
    if controls.manual_lock_active(sym):
        return {"ok": False, "error": f"{sym} locked by another manual order"}, 409
    controls.take_manual_lock(sym)
    try:
        res = broker.place_order(**{k: v for k, v in p.items() if v is not None})
        controls.journal("order_place", p, {"ok": True, **{k: v for k, v in res.items()
                                                           if k != "ok"}}, ip)
        # daemon so a pending lock-release can't block interpreter shutdown
        t = threading.Timer(120, controls.release_manual_lock, args=(sym,))
        t.daemon = True
        t.start()
        return {"ok": True, "result": res}, 200
    except Exception as e:
        controls.release_manual_lock(sym)
        controls.journal("order_place", p, {"ok": False, "error": str(e)[:300]}, ip)
        return {"ok": False, "error": str(e)[:400]}, 502


def order_cancel(params, ip):
    oid = params.get("order_id")
    if not oid:
        return {"ok": False, "error": "order_id required"}, 400
    try:
        res = broker.cancel_order(oid)
        controls.journal("order_cancel", {"order_id": oid}, {"ok": True}, ip)
        return {"ok": True, "result": res}, 200
    except Exception as e:
        controls.journal("order_cancel", {"order_id": oid},
                         {"ok": False, "error": str(e)[:300]}, ip)
        return {"ok": False, "error": str(e)[:400]}, 502


# ================================================================ HTTP layer
class Handler(BaseHTTPRequestHandler):
    server_version = "TradeCommand/1.0"

    # ---------------- plumbing
    def _ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return ip

    def _send(self, code, body, ctype="application/json", cache=False):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if not cache:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self):
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _armed(self):
        tok = self.headers.get("X-Arm-Token") or ""
        return controls.armory.check(tok, self._ip())

    def log_message(self, fmt, *args):
        if os.environ.get("DASHBOARD_QUIET") != "1":
            sys.stderr.write(f"{self.log_date_time_string()} {self._ip()} {fmt % args}\n")

    # ---------------- GET
    GETS = {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/sw.js", "/manifest.webmanifest", "/app.js", "/style.css",
                        "/favicon.ico") or path.startswith("/icons/"):
                return self._static(path.lstrip("/"))
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            fn = self.GETS.get(path)
            if fn:
                return self._send(200, fn(self))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)[:300]})

    def _static(self, rel):
        rel = rel or "index.html"
        p = os.path.normpath(os.path.join(STATIC, rel))
        if not p.startswith(STATIC) or not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        ctypes = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                  ".css": "text/css", ".webmanifest": "application/manifest+json",
                  ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        ext = os.path.splitext(p)[1]
        with open(p, "rb") as f:
            body = f.read()
        cache = ext in (".png", ".svg") and rel != "sw.js"
        return self._send(200, body, ctypes.get(ext, "application/octet-stream"), cache)

    # ---------------- POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._json_body()
        ip = self._ip()
        try:
            # --- auth
            if path == "/api/arm":
                return self._send(200, controls.armory.arm(body.get("pin"), ip))
            if path == "/api/disarm":
                controls.armory.disarm(self.headers.get("X-Arm-Token"))
                return self._send(200, {"ok": True})
            # --- non-danger utilities (rate-limited, journaled)
            if path == "/api/broker/refresh":
                now = time.time()
                if now - _refresh_gate["last"] < 20:
                    return self._send(429, {"ok": False, "error": "refresh rate-limited (20s)"})
                _refresh_gate["last"] = now
                st = broker.refresh(force=True)
                controls.journal("broker_refresh", {}, {"ok": st.get("error") is None,
                                                        "error": st.get("error")}, ip)
                pf = st.get("portfolio") or {}
                if pf.get("total"):
                    readers.append_equity_point(pf["total"], pf.get("cash"),
                                                source="dashboard")
                return self._send(200, {"ok": True, "state": broker.cached()})
            if path == "/api/broker/retry_direct":
                return self._send(200, {"ok": True, "broker": broker.retry_direct()})
            if path == "/api/watchlist":
                syms = [str(s).upper().strip() for s in body.get("symbols", [])
                        if str(s).strip()][:40]
                with open(WATCHLIST_FILE, "w") as f:
                    json.dump({"symbols": syms}, f, indent=2)
                controls.journal("watchlist_update", {"symbols": syms}, {"ok": True}, ip)
                return self._send(200, {"ok": True, "symbols": syms})
            # --- everything below moves money or flips bot controls: ARM required
            if not self._armed():
                return self._send(401, {"ok": False, "error": "not armed — enter PIN",
                                        "arm_required": True})
            if path == "/api/order/preview":
                out, code = order_preview(body, ip)
                return self._send(code, out)
            if path == "/api/order/place":
                out, code = order_place(body, ip)
                return self._send(code, out)
            if path == "/api/order/cancel":
                out, code = order_cancel(body, ip)
                return self._send(code, out)
            if path == "/api/bot/pause":
                return self._send(200, controls.pause(body.get("reason", ""), ip))
            if path == "/api/bot/resume":
                return self._send(200, controls.resume(ip))
            if path == "/api/bot/halt":
                return self._send(200, controls.manual_halt(body.get("reason", ""), ip))
            if path == "/api/bot/clear_halt":
                if body.get("confirm") != "CLEAR":
                    return self._send(400, {"ok": False,
                                            "error": 'type CLEAR to confirm'})
                return self._send(200, controls.clear_halt(ip))
            if path == "/api/dnt":
                sym = str(body.get("symbol", "")).upper().strip()
                if not sym:
                    return self._send(400, {"ok": False, "error": "symbol required"})
                return self._send(200, controls.set_do_not_trade(
                    sym, bool(body.get("blocked", True)), ip))
            if path == "/api/stop_override":
                sym = str(body.get("symbol", "")).upper().strip()
                if not sym:
                    return self._send(400, {"ok": False, "error": "symbol required"})
                return self._send(200, controls.set_stop_override(
                    sym, stop_price=body.get("stop_price"),
                    stop_pct=body.get("stop_pct"), trail_pct=body.get("trail_pct"),
                    clear=bool(body.get("clear")), ip=ip))
            if path == "/api/cash_flow":
                out = controls.record_cash_flow(body.get("amount"),
                                                body.get("note", ""), ip)
                return self._send(200 if out.get("ok") else 400, out)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": str(e)[:300]})


Handler.GETS = {
    "/api/overview": lambda h: overview(),
    "/api/thinking": lambda h: thinking(),
    "/api/performance": lambda h: performance(),
    "/api/trades": lambda h: trades_view(),
    "/api/strategy": lambda h: strategy_view(),
    "/api/risk": lambda h: risk_view(),
    "/api/news": lambda h: news_view(),
    "/api/market": lambda h: market_view(),
    "/api/shadow": lambda h: shadow_view(),
    "/api/alerts": lambda h: {"alerts": readers.alerts()},
    "/api/actions": lambda h: {"actions": readers.manual_actions()},
    "/api/symbol": symbol_view,
    "/api/screen": screen_view,
    "/api/logs": lambda h: {"events": readers.unified_log()},
    "/api/library": lambda h: {"research": readers.research_files(),
                               "postmortems": readers.postmortem_files()},
    "/api/signals": lambda h: signals_view(),
    "/api/orders": orders_view,
    "/api/rx3": lambda h: rx3_view(),
    "/api/health": lambda h: health_view(),
    "/api/learning": lambda h: learning_view(),
    "/api/calendar": lambda h: calendar_view(),
    "/api/meta": lambda h: meta_view(),
    "/api/cash_flows": lambda h: {"flows": readers.cash_flows(50)},
    "/api/postmortem": lambda h: {"file": _q(h, "file"),
                                  "text": readers.postmortem_text(_q(h, "file"))},
    "/api/research_file": lambda h: {"file": _q(h, "file"),
                                     "text": _research_text(_q(h, "file"))},
}


def _q(handler, key):
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(handler.path).query)
    return (q.get(key) or [""])[0]


def _research_text(fname):
    if "/" in fname or ".." in fname or not fname.endswith(".md"):
        return ""
    return readers.load_text(os.path.join("research", fname))


# ================================================================ pollers
def _poller(name, fn, market_s, off_s):
    def loop():
        while True:
            try:
                fn()
            except Exception as e:
                sys.stderr.write(f"[poller:{name}] {e}\n")
            time.sleep(market_s if quotes.market_open_now() else off_s)
    t = threading.Thread(target=loop, daemon=True, name=f"poll-{name}")
    t.start()
    return t


def _warm_quotes():
    brk = broker.cached()
    for s in _symbols_of_interest(brk=brk)[:35]:
        quotes.quote(s)
    for s, _label in quotes.INDICES:
        quotes.quote(s)


def _warm_ribbons():
    for s in _symbols_of_interest()[:25]:
        quotes.ribbon(s)


def _broker_poll():
    if broker.mode()["mode"] != "direct-mcp":
        return
    st = broker.refresh()
    pf = (st or {}).get("portfolio") or {}
    if pf.get("total"):
        readers.append_equity_point(pf["total"], pf.get("cash"), source="dashboard")


def _warm_news():
    quotes.general_news()
    for p in readers.trade_log().get("open_positions", [])[:8]:
        quotes.symbol_news(p["symbol"])


class _LazyBroker:
    """Defer Broker construction until first use (tests import this module
    without a broker; --set-pin shouldn't touch the network)."""
    _b = None

    def _get(self):
        if _LazyBroker._b is None:
            _LazyBroker._b = broker_mod.get_broker()
        return _LazyBroker._b

    def __getattr__(self, name):
        return getattr(self._get(), name)


broker = _LazyBroker()


def main():
    if "--set-pin" in sys.argv:
        pin = getpass.getpass("New dashboard PIN (min 4 chars): ")
        pin2 = getpass.getpass("Repeat PIN: ")
        if pin != pin2:
            print("PINs do not match.")
            sys.exit(1)
        controls.set_pin(pin)
        print("✓ PIN set (dashboard/.auth.json, chmod 600).")
        return
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print(f"TradeCommand starting on http://{HOST}:{port}")
    print(f"  broker path: {broker.mode()['mode']} ({broker.mode()['reason']})")
    print(f"  PIN configured: {controls.pin_configured()}"
          + ("" if controls.pin_configured()
             else "  →  python3 dashboard/server.py --set-pin"))
    # initial broker read: free on direct-mcp; skipped on claude-cli (on-demand only)
    if broker.mode()["mode"] == "direct-mcp":
        broker.refresh()
    _poller("quotes", _warm_quotes, 20, 120)
    _poller("ribbons", _warm_ribbons, 300, 900)
    _poller("broker", _broker_poll, 60, 600)
    _poller("news", _warm_news, 300, 900)
    srv = ThreadingHTTPServer((HOST, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
