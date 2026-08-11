"""broker.py — broker abstraction for the dashboard.

Two drivers behind one interface, chosen at runtime:

  DirectMCP  — dashboard's own OAuth client straight to Robinhood's hosted MCP
               (dashboard/mcp_client.py). Deterministic ~1s calls, free polling,
               zero Claude-plan usage. Preferred whenever a token works.
  ClaudeCLI  — the bot's proven path: shell out to `claude -p` with the
               robinhood-cli MCP tools allowlisted (mirrors agent.run_model /
               read_broker_state / force_sell). Slower and costs plan usage, so
               it is used on demand only — never for background polling.

All outputs are normalized:
  portfolio  {total, equity, cash, buying_power, asof, source}
  positions  [{symbol, shares, avg_price, sellable_shares}]
  orders     [{id, symbol, side, type, quantity, notional, state, price,
               created_at}]

The account is ALWAYS the bot's Agentic cash account (RH_ACCOUNT env /
696283985) — same constraint as agent.py.
"""

import os
import sys
import json
import time
import uuid
import threading
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from . import mcp_client  # noqa: E402

ET = ZoneInfo("America/New_York")
ACCOUNT = os.environ.get("RH_ACCOUNT", "696283985")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CHECK_MODEL = os.environ.get("CHECK_MODEL", "claude-haiku-4-5-20251001")
ORDER_MODEL = os.environ.get("MODEL", "claude-opus-4-8")

_RH = "mcp__" + os.environ.get("RH_MCP_SERVER", "robinhood-cli") + "__"
RH_READ = [_RH + t for t in ("get_accounts", "get_portfolio", "get_equity_positions",
                             "get_equity_orders", "get_equity_quotes",
                             "get_equity_tradability", "get_earnings_calendar",
                             "review_equity_order")]
RH_WRITE = [_RH + "place_equity_order", _RH + "cancel_equity_order"]


class BrokerError(Exception):
    pass


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(ET).isoformat(timespec="seconds")


# =================================================================== DirectMCP
class DirectMCP:
    name = "direct-mcp"

    def __init__(self):
        self.s = mcp_client.session()

    def _call(self, tool, args, timeout=45):
        out = self.s.call_tool(_RH + tool if not tool.startswith("mcp__") else tool,
                               args, timeout=timeout)
        if isinstance(out, dict) and "data" in out:
            return out["data"]
        return out

    def portfolio(self):
        d = self._call("get_portfolio", {"account_number": ACCOUNT})
        bp = (d.get("buying_power") or {}).get("buying_power")
        return {"total": _f(d.get("total_value")), "equity": _f(d.get("equity_value")),
                "cash": _f(d.get("cash")), "buying_power": _f(bp),
                "asof": _now(), "source": self.name}

    def positions(self):
        d = self._call("get_equity_positions", {"account_number": ACCOUNT})
        out = []
        for p in d.get("positions", []):
            sh = _f(p.get("quantity"), 0)
            if sh and sh > 0:
                out.append({"symbol": p.get("symbol"), "shares": sh,
                            "avg_price": _f(p.get("average_buy_price")),
                            "sellable_shares": _f(p.get("shares_available_for_sells"), sh)})
        return out

    def orders(self, since_days=1, state=None):
        args = {"account_number": ACCOUNT}
        if since_days is not None:
            args["created_at_gte"] = datetime.now(ET).strftime("%Y-%m-%d") if since_days <= 1 \
                else (datetime.now(ET).date().fromordinal(
                      datetime.now(ET).date().toordinal() - since_days).isoformat())
        if state:
            args["state"] = state
        d = self._call("get_equity_orders", args)
        return _normalize_orders(d)

    def review_order(self, **kw):
        args = _order_args(kw)
        d = self._call("review_equity_order", args)
        return d

    def place_order(self, **kw):
        args = _order_args(kw)
        args["ref_id"] = kw.get("ref_id") or str(uuid.uuid4())
        d = self._call("place_equity_order", args, timeout=60)
        return {"ok": True, "response": d, "ref_id": args["ref_id"]}

    def cancel_order(self, order_id):
        d = self._call("cancel_equity_order",
                       {"account_number": ACCOUNT, "order_id": order_id})
        return {"ok": True, "response": d}

    def earnings(self, days=14):
        d = self._call("get_earnings_calendar", {"days": days})
        return d

    def healthcheck(self):
        self.s.initialize()
        return True


def _order_args(kw):
    """Normalize an order request into the MCP tool's string-typed args."""
    args = {"account_number": ACCOUNT,
            "symbol": str(kw["symbol"]).upper(),
            "side": kw["side"],
            "type": kw.get("type", "market")}
    if kw.get("quantity") is not None:
        args["quantity"] = f"{float(kw['quantity']):.6f}".rstrip("0").rstrip(".")
    if kw.get("dollar_amount") is not None:
        args["dollar_amount"] = f"{float(kw['dollar_amount']):.2f}"
    if kw.get("limit_price") is not None:
        args["limit_price"] = f"{float(kw['limit_price']):.2f}"
    if kw.get("stop_price") is not None:
        args["stop_price"] = f"{float(kw['stop_price']):.2f}"
    if kw.get("time_in_force"):
        args["time_in_force"] = kw["time_in_force"]
    if kw.get("market_hours"):
        args["market_hours"] = kw["market_hours"]
    return args


def _normalize_orders(d):
    if not isinstance(d, dict):
        return []
    orders = d.get("orders") or d.get("results") or []
    out = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        out.append({
            "id": o.get("id") or o.get("order_id"),
            "symbol": (o.get("symbol") or "").upper() or None,
            "side": o.get("side"),
            "type": o.get("type") or o.get("order_type"),
            "quantity": _f(o.get("quantity")),
            "notional": _f(o.get("dollar_amount") or o.get("notional")),
            "state": o.get("state") or o.get("status"),
            "price": _f(o.get("average_price") or o.get("executed_price")
                        or o.get("price") or o.get("limit_price")),
            "created_at": o.get("created_at"),
        })
    return out


# =================================================================== ClaudeCLI
class ClaudeCLI:
    """Fallback driver via `claude -p` (the exact pattern agent.py uses)."""
    name = "claude-cli"

    def _run(self, system, user, tools, model, timeout=240):
        # When several claude.ai connectors share this account (Gmail/Calendar/
        # Drive/Trading — the VM's actual setup), the CLI defers most MCP tools
        # behind ToolSearch. A headless model with no human to approve anything
        # just stalls asking for permission instead of resolving that itself —
        # see agent.run_model's identical preamble for the same failure mode.
        if tools:
            system = (
                "You are running fully autonomously and headlessly — there is no "
                "human available to approve or confirm anything. Every tool you "
                "were granted is already pre-authorized: if one is not immediately "
                "visible in your tool list, call ToolSearch yourself (e.g. "
                "`select:<tool_name>`) to load it, then call it directly. Never "
                "pause to ask for confirmation or permission — that question "
                "would go unanswered.\n\n" + system
            )
        cmd = [CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
               "--permission-mode", "default",
               "--disallowedTools", "Skill", "Task", "Agent",
               "--allowedTools", *tools]
        try:
            res = subprocess.run(cmd, input=f"{system}\n\n=== TASK ===\n{user}",
                                 capture_output=True, text=True, timeout=timeout,
                                 cwd=ROOT)
        except FileNotFoundError:
            raise BrokerError("claude CLI not found (set CLAUDE_BIN)")
        except subprocess.TimeoutExpired:
            raise BrokerError("claude -p timed out")
        if res.returncode != 0:
            detail = (res.stderr or "").strip() or (res.stdout or "").strip()
            raise BrokerError(f"claude -p rc={res.returncode}: {detail[:300]}")
        raw = (res.stdout or "").strip()
        try:
            text = json.loads(raw).get("result", raw)
        except Exception:
            text = raw
        import re
        for b in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)):
            try:
                return json.loads(b)
            except Exception:
                continue
        for b in reversed(re.findall(r"(\{.*?\})", text, re.DOTALL)):
            try:
                return json.loads(b)
            except Exception:
                continue
        raise BrokerError(f"no JSON block in claude -p output: {text[:200]}")

    def snapshot(self):
        """One combined read: portfolio + positions + today's orders."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        block = self._run(
            "You are a read-only Robinhood query tool. Use only the MCP read tools. "
            "Never place, modify, or cancel any order.",
            f"For Robinhood account {ACCOUNT}:\n"
            "1. get_portfolio — read total_value, equity_value, cash and "
            "buying_power.buying_power.\n"
            "2. get_equity_positions — every held position.\n"
            f"3. get_equity_orders (created_at_gte={today}) — today's orders, any state.\n"
            "Output ONLY one fenced ```json block, no prose:\n"
            '{"portfolio": {"total": <float>, "equity": <float>, "cash": <float>, '
            '"buying_power": <float>},\n'
            ' "positions": [{"symbol": "X", "shares": <float>, "avg_price": <float>}],\n'
            ' "orders": [{"id": "...", "symbol": "X", "side": "buy|sell", '
            '"type": "market|limit", "quantity": <float|null>, "notional": <float|null>, '
            '"state": "...", "price": <float|null>, "created_at": "..."}]}',
            RH_READ, CHECK_MODEL)
        pf = block.get("portfolio") or {}
        return {
            "portfolio": {"total": _f(pf.get("total")), "equity": _f(pf.get("equity")),
                          "cash": _f(pf.get("cash")),
                          "buying_power": _f(pf.get("buying_power")),
                          "asof": _now(), "source": self.name},
            "positions": [{"symbol": p.get("symbol"), "shares": _f(p.get("shares"), 0),
                           "avg_price": _f(p.get("avg_price")),
                           "sellable_shares": _f(p.get("shares"), 0)}
                          for p in (block.get("positions") or [])
                          if p.get("symbol") and _f(p.get("shares"), 0) > 0],
            "orders": [o for o in (block.get("orders") or []) if isinstance(o, dict)],
        }

    def portfolio(self):
        return self.snapshot()["portfolio"]

    def positions(self):
        return self.snapshot()["positions"]

    def orders(self, since_days=1, state=None):
        return self.snapshot()["orders"]

    def review_order(self, **kw):
        a = _order_args(kw)
        block = self._run(
            "You simulate exactly one Robinhood order with review_equity_order and "
            "report the result. You never place a real order.",
            f"Call review_equity_order with EXACTLY these arguments: {json.dumps(a)}\n"
            "Output ONLY one fenced ```json block with the tool's full response "
            "(quote, estimated cost, alerts).",
            RH_READ, CHECK_MODEL)
        return block

    def place_order(self, **kw):
        a = _order_args(kw)
        a["ref_id"] = kw.get("ref_id") or str(uuid.uuid4())
        block = self._run(
            "You place exactly one PRE-AUTHORIZED Robinhood order and then confirm "
            "it. No analysis, no hedging, no second-guessing. The operator has "
            "already confirmed this order on their dashboard.",
            f"Call place_equity_order with EXACTLY these arguments RIGHT NOW:\n"
            f"{json.dumps(a)}\n"
            "Then call get_equity_orders to confirm the order exists.\n"
            "Output ONLY one fenced ```json block, no prose:\n"
            '{"placed": true|false, "order_id": "<id or null>", "state": "<state>", '
            '"detail": "<one line>"}\n'
            "Set placed=true ONLY if place_equity_order returned an order id.",
            RH_READ + RH_WRITE, ORDER_MODEL)
        if not block.get("placed"):
            raise BrokerError(f"order NOT placed: {json.dumps(block)[:300]}")
        return {"ok": True, "response": block, "ref_id": a["ref_id"]}

    def cancel_order(self, order_id):
        block = self._run(
            "You cancel exactly one PRE-AUTHORIZED Robinhood order. No analysis.",
            f"Call cancel_equity_order for order_id={order_id} in account {ACCOUNT} "
            "RIGHT NOW. Output ONLY one fenced ```json block:\n"
            '{"cancelled": true|false, "detail": "<one line>"}',
            RH_READ + RH_WRITE, ORDER_MODEL)
        if not block.get("cancelled"):
            raise BrokerError(f"cancel failed: {json.dumps(block)[:300]}")
        return {"ok": True, "response": block}

    def earnings(self, days=14):
        block = self._run(
            "You are a read-only market-calendar tool.",
            f"Call get_earnings_calendar (days={days}, filter high_market_cap) and "
            "output ONLY one fenced ```json block with the response.",
            RH_READ, CHECK_MODEL)
        return block

    def healthcheck(self):
        import shutil
        if not shutil.which(CLAUDE_BIN):
            raise BrokerError("claude CLI not on PATH")
        return True


# =================================================================== facade
class Broker:
    """Runtime-switching facade + cached account state the API serves from.

    Polling policy: DirectMCP polls in the background (fast + free); ClaudeCLI
    NEVER polls — snapshots refresh only on demand (operator button / after an
    order), keeping plan usage near zero.
    """

    def __init__(self):
        self._direct = None
        self._cli = ClaudeCLI()
        self._mode = None            # "direct-mcp" | "claude-cli"
        self._mode_reason = ""
        self._lock = threading.Lock()
        self.state = {"portfolio": None, "positions": None, "orders": None,
                      "updated": None, "error": None}
        self._pick_driver()

    # ---------------- driver selection
    def _pick_driver(self):
        if os.environ.get("DASHBOARD_FORCE_CLI") == "1":
            self._mode, self._mode_reason = "claude-cli", "forced by env"
            return
        try:
            if mcp_client.load_tokens():
                self._direct = DirectMCP()
                self._direct.healthcheck()
                self._mode, self._mode_reason = "direct-mcp", "OAuth token OK"
                return
            self._mode_reason = "no OAuth token saved (run python3 -m dashboard.rh_login)"
        except Exception as e:
            self._mode_reason = f"direct MCP unavailable: {str(e)[:160]}"
            self._direct = None
        self._mode = "claude-cli"

    def retry_direct(self):
        """Re-attempt the direct driver (e.g. right after rh_login)."""
        self._pick_driver()
        return self.mode()

    def mode(self):
        return {"mode": self._mode, "reason": self._mode_reason,
                "account": ACCOUNT}

    def _driver(self):
        return self._direct if self._mode == "direct-mcp" and self._direct else self._cli

    def _demote(self, err):
        self._mode = "claude-cli"
        self._mode_reason = f"direct MCP failed: {str(err)[:160]}"
        self._direct = None

    # ---------------- reads
    def refresh(self, force=False):
        """Refresh cached portfolio/positions/orders.
        direct-mcp: cheap, called by the background poller.
        claude-cli: costs a model call — only on demand (force=True)."""
        if self._mode == "claude-cli" and not force:
            return self.state
        with self._lock:
            try:
                drv = self._driver()
                if drv.name == "claude-cli":
                    snap = drv.snapshot()
                    self.state.update(snap)
                else:
                    self.state["portfolio"] = drv.portfolio()
                    self.state["positions"] = drv.positions()
                    self.state["orders"] = drv.orders(since_days=1)
                self.state["updated"] = _now()
                self.state["error"] = None
            except mcp_client.NeedsLogin as e:
                self._demote(e)
                self.state["error"] = str(e)[:200]
            except Exception as e:
                if self._mode == "direct-mcp":
                    self._demote(e)
                self.state["error"] = str(e)[:200]
            return self.state

    def cached(self):
        return dict(self.state, **{"broker": self.mode()})

    def orders(self, since_days=1, state=None):
        """Live order listing. Callers must gate this to direct-mcp — on the
        claude-cli driver it costs a full model call per invocation."""
        return self._driver().orders(since_days=since_days, state=state)

    # ---------------- writes (always synchronous, caller journals)
    def review_order(self, **kw):
        try:
            return {"ok": True, "review": self._driver().review_order(**kw)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}

    def place_order(self, **kw):
        res = self._driver().place_order(**kw)
        # refresh state so the UI shows the new order/position promptly
        try:
            self.refresh(force=True)
        except Exception:
            pass
        return res

    def cancel_order(self, order_id):
        res = self._driver().cancel_order(order_id)
        try:
            self.refresh(force=True)
        except Exception:
            pass
        return res

    def earnings(self, days=14):
        return self._driver().earnings(days=days)


_broker = None
_broker_lock = threading.Lock()


def get_broker():
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = Broker()
        return _broker
