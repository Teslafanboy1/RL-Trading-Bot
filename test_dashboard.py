#!/usr/bin/env python3
"""
test_dashboard.py — TradeCommand dashboard test suite.

Run:  python3 test_dashboard.py
      python3 -m pytest test_dashboard.py -q      (also pytest-compatible)

Covers:
  • dashboard.readers  — file parsers (picks md, momentum watch, queue,
                          change events, equity curve, go-live gates), all
                          missing-file-safe
  • dashboard.controls — PIN hashing/arming/lockout, pause/halt/DNT/stop
                          overrides, manual locks, journal
  • dashboard.broker   — order-arg normalization, order listing normalization
  • dashboard.mcp_client — SSE/JSON response parsing
  • dashboard.server   — order preview validation + danger-endpoint arming
  • agent.py control-plane integration — stop/trailing overrides, DNT wake
                          filtering, manual-close tagging skips the pipeline,
                          PAUSE branch runs protective exits only,
                          equity-point + cycle-status writers
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import agent as agent_module
import risk_guard
from dashboard import readers, controls, broker as broker_mod, mcp_client
from dashboard import server as server_mod


PICKS_MD = """# Weekend Research — 2026-07-06

## Header
- **Portfolio value:** $394.03 | **Settled cash:** $165.84

## Open position reassessment
- **AMAT** — holding, thesis intact. **Verdict: HOLD**

## Ranked picks

### #1 — LABU (Direxion Biotech Bull 3x) | Confidence: 66/100
- EMA state: **BUY / HOLD** (red below all)
- Momentum: at peak, +12.7% above entry
- Entry target: held @ $268.77 | Exit: trailing stop | Stop-loss: $227
- Allocation: hold existing ~$66 (17%); NO ADD
- Sources: news, fundamental
- **Invalidates if:** ribbon flips to SELL or 25% giveback

### #2 — BTSG (BrightSpring) | Confidence: 62/100
- EMA state: BUY / HOLD
- Entry target: ~$69.5-70 | Stop-loss: $62.95
- Sources: fundamental, news
- **Invalidates if:** no durable catalyst on re-validation

## Watchlist
- SPY — index, no edge

## MOMENTUM OPTIONS WATCH
- RXRX | conf 71 | catalyst: FDA fast-track designation Tuesday
- (ignore this malformed line)

## Deployment summary
- Sell: MU stop-loss
"""


class TmpRootMixin(unittest.TestCase):
    """Isolated tmp ROOT patched into agent, readers, and controls."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dash_test_")
        for sub in ("research", "postmortems", "logs", "strategy/history",
                    "shadow", "control/locks", "skills", "data", "dashboard"):
            os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
        self._patches = [
            patch.object(agent_module, "ROOT", self.tmpdir),
            patch.object(readers, "ROOT", self.tmpdir),
            patch.object(controls, "ROOT", self.tmpdir),
            patch.object(controls, "CONTROL_DIR", os.path.join(self.tmpdir, "control")),
            patch.object(controls, "LOCK_DIR", os.path.join(self.tmpdir, "control", "locks")),
            patch.object(controls, "PAUSE_FILE", os.path.join(self.tmpdir, "control", "PAUSE")),
            patch.object(controls, "DNT_FILE", os.path.join(self.tmpdir, "control", "do_not_trade.json")),
            patch.object(controls, "OVERRIDES_FILE", os.path.join(self.tmpdir, "control", "stop_overrides.json")),
            patch.object(controls, "CASH_FLOWS_FILE", os.path.join(self.tmpdir, "control", "cash_flows.json")),
            patch.object(controls, "HALT_FILE", os.path.join(self.tmpdir, "HALT")),
            patch.object(controls, "JOURNAL", os.path.join(self.tmpdir, "logs", "manual_actions.jsonl")),
            patch.object(controls, "AUTH_FILE", os.path.join(self.tmpdir, "dashboard", ".auth.json")),
            # risk_guard resolves its file paths from its OWN module location, not
            # agent.ROOT — without these, any test that exercises run_agent() (or
            # calls risk_guard directly) writes mocked equity into the REAL repo's
            # logs/risk_state.json / HALT (2026-08-03: this is what actually
            # produced the "544/589.72 -> 322" false halt — test fixture values
            # leaking into production state, not a bad live broker read).
            patch.object(risk_guard, "ROOT", self.tmpdir),
            patch.object(risk_guard, "HALT_FILE", os.path.join(self.tmpdir, "HALT")),
            patch.object(risk_guard, "STATE_FILE", os.path.join(self.tmpdir, "logs", "risk_state.json")),
            patch.object(risk_guard, "HEARTBEAT_FILE", os.path.join(self.tmpdir, "logs", "heartbeat")),
        ]
        for p in self._patches:
            p.start()
        self.strategy = {
            "version": 1,
            "research": {"source_performance": {"news": {"wins": 2, "losses": 1, "accuracy": 0.67}},
                         "source_weights": {"news": 0.4}},
            "risk_management": {"stop_loss_pct": 0.10, "trailing_stop_pct": 0.25,
                                "exit_on_ribbon_sell": False},
            "options": {"enabled": False, "shadow_mode": True,
                        "activation": {"long_calls_min_account_usd": 1000,
                                       "debit_spreads_min_account_usd": 1500,
                                       "premium_selling_min_account_usd": 5000}},
            "version_history": [{"version": 1, "date": "2026-07-01", "changes": "init"}],
        }
        self._write("strategy/strategy.json", json.dumps(self.strategy))
        self.log = {
            "summary": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                        "total_pnl": 0.0, "month_start_value": 100.0,
                        "current_value": 100.0, "monthly_goal": "100%"},
            "open_positions": [], "trades": [],
            "_state": {"last_positions": [], "next_id": 1},
        }
        self._flush_log()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, rel, content):
        path = os.path.join(self.tmpdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def _flush_log(self):
        self._write("trade_log.json", json.dumps(self.log))


# ─────────────────────────────────────────────────────────────── readers ────
class TestReaders(TmpRootMixin):

    def test_missing_files_are_safe(self):
        self.assertEqual(readers.change_events(), [])
        self.assertEqual(readers.rewrite_queue(), [])
        self.assertEqual(readers.equity_curve(), [])
        self.assertEqual(readers.rx3_paper(), {})
        self.assertEqual(readers.do_not_trade(), [])
        self.assertEqual(readers.stop_overrides(), {})
        self.assertFalse(readers.halt_state()["halted"])
        self.assertFalse(readers.pause_state()["paused"])
        self.assertIsNone(readers.heartbeat()["age_seconds"])

    def test_parse_picks_file(self):
        self._write("research/weekend_picks_2026-07-06.md", PICKS_MD)
        parsed = readers.parse_picks_file("weekend_picks_2026-07-06.md")
        self.assertEqual(len(parsed["picks"]), 2)
        p1 = parsed["picks"][0]
        self.assertEqual(p1["symbol"], "LABU")
        self.assertEqual(p1["confidence"], 66)
        self.assertEqual(p1["rank"], 1)
        self.assertIn("BUY", p1["fields"]["ema"])
        self.assertIn("ribbon flips", p1["fields"]["invalidates"])
        self.assertIn("news, fundamental", p1["fields"]["sources"])
        self.assertEqual(parsed["picks"][1]["symbol"], "BTSG")
        self.assertTrue(any("SPY" in w for w in parsed["watchlist"]))
        self.assertTrue(any("AMAT" in r for r in parsed["reassessment"]))

    def test_momentum_watch_parses_and_skips_malformed(self):
        self._write("research/weekend_picks_2026-07-06.md", PICKS_MD)
        mw = readers.parse_momentum_watch()
        self.assertEqual(list(mw), ["RXRX"])
        self.assertEqual(mw["RXRX"]["confidence"], 71)
        self.assertIn("FDA", mw["RXRX"]["catalyst"])

    def test_rewrite_queue_done_and_pending(self):
        self._write("research/strategy_rewrite_queue.md",
                    "# queue\n"
                    "- 2026-07-01T10:00:00 | T0001 CAT LOSS -0.62% | analysis: postmortem_001.md | skill_5 review [DONE 2026-07-02]\n"
                    "- 2026-07-06T15:15:52 | T0015 MU LOSS -14.55% | analysis: postmortem_005.md | skill_5 review\n")
        q = readers.rewrite_queue()
        self.assertEqual(len(q), 2)
        self.assertTrue(q[0]["done"])
        self.assertFalse(q[1]["done"])
        self.assertEqual(q[1]["trade_id"], "T0015")
        self.assertEqual(q[1]["symbol"], "MU")

    def test_change_events_newest_first(self):
        self._write("logs/change_events.jsonl",
                    '{"timestamp": "2026-07-01", "version": 1}\n'
                    'garbage line\n'
                    '{"timestamp": "2026-07-02", "version": 2}\n')
        ev = readers.change_events()
        self.assertEqual([e["version"] for e in ev], [2, 1])

    def test_equity_curve_append_and_dedupe(self):
        self.assertTrue(readers.append_equity_point(100.5, cash=50))
        self.assertFalse(readers.append_equity_point(101.0))  # same minute+source
        pts = readers.equity_curve()
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["total"], 100.5)
        self.assertEqual(pts[0]["cash"], 50)

    def test_go_live_checklist_gates(self):
        self._write("shadow/options_shadow_log.json", json.dumps({
            "positions": [], "summary": {"closed": 12, "win_rate": 0.6,
                                         "avg_pct_on_premium": 14.2}}))
        gl = readers.go_live_checklist(broker_total=1200)
        by_gate = {g["gate"]: g for g in gl["gates"]}
        long_calls = next(g for g in gl["gates"] if "Long calls" in g["gate"])
        spreads = next(g for g in gl["gates"] if "Debit spreads" in g["gate"])
        edge = next(g for g in gl["gates"] if "Proven shadow edge" in g["gate"])
        self.assertTrue(long_calls["met"])      # 1200 >= 1000
        self.assertFalse(spreads["met"])        # 1200 < 1500
        self.assertTrue(edge["met"])            # 12 closed, positive avg
        self.assertFalse(gl["enabled"])

    def test_source_leaderboard(self):
        rows = readers.source_leaderboard()
        self.assertEqual(rows[0]["source"], "news")
        self.assertEqual(rows[0]["wins"], 2)
        self.assertAlmostEqual(rows[0]["weight"], 0.4)

    def test_realized_pnl_series_cumulative(self):
        self.log["trades"] = [
            {"id": "T1", "symbol": "A", "exit_date": "2026-07-01", "pnl_dollar": 5.0},
            {"id": "T2", "symbol": "B", "exit_date": "2026-07-02", "pnl_dollar": -2.0},
        ]
        self._flush_log()
        s = readers.realized_pnl_series()
        self.assertEqual([p["cum_pnl"] for p in s], [5.0, 3.0])

    def test_cash_flows_newest_first(self):
        controls.record_cash_flow(94.97, "deposit")
        controls.record_cash_flow(-10, "withdrawal")
        flows = readers.cash_flows()
        self.assertEqual([f["amount"] for f in flows], [-10.0, 94.97])

    def test_postmortem_text_path_traversal_blocked(self):
        self._write("postmortems/postmortem_001.md", "# PM\nsecret ok")
        self.assertIn("secret ok", readers.postmortem_text("postmortem_001.md"))
        self.assertEqual(readers.postmortem_text("../trade_log.json"), "")
        self.assertEqual(readers.postmortem_text("x/../../etc"), "")


# ─────────────────────────────────────────────────────────────── controls ───
class TestControls(TmpRootMixin):

    def test_pin_set_verify_arm_and_lockout(self):
        controls.set_pin("4242")
        self.assertTrue(controls.pin_configured())
        arm = controls.Armory()
        r = arm.arm("4242", ip="1.2.3.4")
        self.assertTrue(r["ok"])
        self.assertTrue(arm.check(r["token"], ip="1.2.3.4"))
        self.assertFalse(arm.check(r["token"], ip="9.9.9.9"))   # ip-bound
        self.assertFalse(arm.check("bogus", ip="1.2.3.4"))
        for _ in range(controls.MAX_PIN_ATTEMPTS):
            self.assertFalse(arm.arm("0000", ip="1.2.3.4")["ok"])
        locked = arm.arm("4242", ip="1.2.3.4")                   # right PIN, locked out
        self.assertFalse(locked["ok"])
        self.assertIn("locked", locked["error"])

    def test_arm_token_expiry(self):
        controls.set_pin("4242")
        arm = controls.Armory()
        r = arm.arm("4242")
        tok = r["token"]
        arm._tokens[tok]["expires"] = time.time() - 1
        self.assertFalse(arm.check(tok))

    def test_pause_resume_halt_clear(self):
        controls.pause("testing")
        self.assertTrue(agent_module._control_paused())
        controls.resume()
        self.assertFalse(agent_module._control_paused())
        controls.manual_halt("scared")
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "HALT")))
        self.assertIn("MANUAL", open(os.path.join(self.tmpdir, "HALT")).read())
        controls.clear_halt()
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "HALT")))

    def test_dnt_and_overrides_roundtrip(self):
        controls.set_do_not_trade("nvda", True)
        controls.set_do_not_trade("MU", True)
        self.assertEqual(agent_module._do_not_trade(), {"NVDA", "MU"})
        controls.set_do_not_trade("NVDA", False)
        self.assertEqual(agent_module._do_not_trade(), {"MU"})
        controls.set_stop_override("amat", stop_price=550.0, trail_pct=0.30)
        ov = agent_module._stop_overrides()
        self.assertEqual(ov["AMAT"]["stop_price"], 550.0)
        self.assertEqual(ov["AMAT"]["trail_pct"], 0.30)
        controls.set_stop_override("AMAT", clear=True)
        self.assertEqual(agent_module._stop_overrides(), {})

    def test_record_cash_flow_roundtrip(self):
        dep = controls.record_cash_flow(94.97, "operator deposit", ip="1.2.3.4")
        self.assertTrue(dep["ok"])
        self.assertEqual(dep["entry"]["amount"], 94.97)
        self.assertFalse(dep["entry"]["applied"])
        wd = controls.record_cash_flow(-30, "withdrawal")
        self.assertTrue(wd["ok"])
        flows = controls.list_cash_flows()
        self.assertEqual(len(flows), 2)
        self.assertEqual(flows[0]["amount"], 94.97)
        self.assertEqual(flows[1]["amount"], -30.0)

    def test_record_cash_flow_rejects_zero_and_non_numeric(self):
        self.assertFalse(controls.record_cash_flow(0)["ok"])
        self.assertFalse(controls.record_cash_flow("not-a-number")["ok"])
        self.assertEqual(controls.list_cash_flows(), [])

    def test_manual_locks(self):
        controls.take_manual_lock("labu")
        self.assertTrue(agent_module._manual_lock_active("LABU"))
        self.assertTrue(controls.manual_lock_active("LABU"))
        controls.release_manual_lock("LABU")
        self.assertFalse(agent_module._manual_lock_active("LABU"))
        # stale lock ignored
        controls.take_manual_lock("OLD")
        p = os.path.join(self.tmpdir, "control", "locks", "OLD.manual.lock")
        os.utime(p, (time.time() - 9999, time.time() - 9999))
        self.assertFalse(agent_module._manual_lock_active("OLD"))

    def test_journal_and_recent_manual_sells(self):
        controls.journal("order_place", {"symbol": "MU", "side": "sell",
                                         "quantity": 0.11}, {"ok": True}, "ip")
        controls.journal("order_place", {"symbol": "BAD", "side": "sell"},
                         {"ok": False, "error": "x"}, "ip")
        controls.journal("order_place", {"symbol": "AAPL", "side": "buy"},
                         {"ok": True}, "ip")
        sells = agent_module._recent_manual_sells()
        self.assertIn("MU", sells)
        self.assertNotIn("BAD", sells)      # failed order doesn't count
        self.assertNotIn("AAPL", sells)     # buys don't count
        acts = readers.manual_actions()
        self.assertEqual(len(acts), 3)
        self.assertEqual(acts[0]["params"]["symbol"], "AAPL")  # newest first


# ─────────────────────────────────────────────────────────────── broker ─────
class TestBroker(unittest.TestCase):

    def test_order_args_normalization(self):
        a = broker_mod._order_args({"symbol": "mu", "side": "sell", "type": "market",
                                    "quantity": 0.110000})
        self.assertEqual(a["symbol"], "MU")
        self.assertEqual(a["quantity"], "0.11")
        self.assertNotIn("dollar_amount", a)
        b = broker_mod._order_args({"symbol": "SPY", "side": "buy", "type": "limit",
                                    "dollar_amount": 25, "limit_price": 630.5,
                                    "time_in_force": "gtc"})
        self.assertEqual(b["dollar_amount"], "25.00")
        self.assertEqual(b["limit_price"], "630.50")
        self.assertEqual(b["time_in_force"], "gtc")

    def test_normalize_orders(self):
        d = {"orders": [{"id": "abc", "symbol": "amat", "side": "sell",
                         "type": "market", "quantity": "0.08", "state": "filled",
                         "average_price": "630.11", "created_at": "2026-07-07"}]}
        out = broker_mod._normalize_orders(d)
        self.assertEqual(out[0]["symbol"], "AMAT")
        self.assertEqual(out[0]["quantity"], 0.08)
        self.assertEqual(out[0]["price"], 630.11)
        self.assertEqual(broker_mod._normalize_orders({}), [])
        self.assertEqual(broker_mod._normalize_orders(None), [])


# ─────────────────────────────────────────────────────────── mcp client ─────
class TestMCPClient(unittest.TestCase):

    def test_parse_json_body(self):
        msg = mcp_client._parse_body({"Content-Type": "application/json"},
                                     b'{"jsonrpc":"2.0","id":1,"result":{"x":1}}')
        self.assertEqual(msg["result"], {"x": 1})

    def test_parse_sse_body_takes_last_result(self):
        sse = (b'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
               b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
        msg = mcp_client._parse_body({"Content-Type": "text/event-stream"}, sse)
        self.assertEqual(msg["result"], {"ok": True})

    def test_parse_garbage_returns_none(self):
        self.assertIsNone(mcp_client._parse_body({"Content-Type": "application/json"},
                                                 b"<html>nope</html>"))


# ─────────────────────────────────────────────────────────────── server ─────
class _StubBroker:
    def __init__(self):
        self.placed = []
        self.state = {"portfolio": {"total": 322.49, "equity": 112.96, "cash": 209.53,
                                    "buying_power": 209.53, "asof": "t", "source": "stub"},
                      "positions": [{"symbol": "AMAT", "shares": 0.081696,
                                     "avg_price": 614.35, "sellable_shares": 0.081696}],
                      "orders": [], "updated": "t", "error": None}

    def cached(self):
        return dict(self.state, broker={"mode": "stub", "reason": "", "account": "x"})

    def mode(self):
        return {"mode": "stub", "reason": "", "account": "x"}

    def review_order(self, **kw):
        return {"ok": True, "review": {"quote": {"ask": "630.00"}}}

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"ok": True, "ref_id": "test-ref", "response": {}}

    def cancel_order(self, oid):
        return {"ok": True, "response": {}}


class TestServerOrderFlow(TmpRootMixin):

    def setUp(self):
        super().setUp()
        self.stub = _StubBroker()
        self._srv_patches = [
            patch.object(server_mod, "broker", self.stub),
            patch.object(server_mod.quotes, "quote",
                         lambda s: {"symbol": s, "price": 630.0, "prev_close": 620.0,
                                    "change_pct": 1.6, "spark": []}),
            patch.object(server_mod.quotes, "market_open_now", lambda: True),
        ]
        for p in self._srv_patches:
            p.start()

    def tearDown(self):
        for p in self._srv_patches:
            p.stop()
        super().tearDown()

    def test_preview_validation_errors(self):
        out, code = server_mod.order_preview({"side": "buy"}, "ip")
        self.assertEqual(code, 400)                          # no symbol
        out, code = server_mod.order_preview(
            {"symbol": "SPY", "side": "buy", "type": "market"}, "ip")
        self.assertEqual(code, 400)                          # no size
        out, code = server_mod.order_preview(
            {"symbol": "SPY", "side": "buy", "type": "market",
             "quantity": 1, "dollar_amount": 100}, "ip")
        self.assertEqual(code, 400)                          # both sizes
        out, code = server_mod.order_preview(
            {"symbol": "SPY", "side": "buy", "type": "limit", "dollar_amount": 100},
            "ip")
        self.assertEqual(code, 400)                          # notional + limit

    def test_preview_warns_dnt_and_buying_power_and_blocks_oversell(self):
        controls.set_do_not_trade("SPY", True)
        out, code = server_mod.order_preview(
            {"symbol": "SPY", "side": "buy", "type": "market", "dollar_amount": 500},
            "ip")
        self.assertEqual(code, 200)
        self.assertTrue(any("do-not-trade" in w for w in out["warnings"]))
        self.assertTrue(any("EXCEEDS buying power" in w for w in out["warnings"]))
        out, code = server_mod.order_preview(
            {"symbol": "AMAT", "side": "sell", "type": "market", "quantity": 5},
            "ip")
        self.assertEqual(code, 400)
        self.assertIn("exceeds sellable", out["error"])

    def test_place_requires_fresh_preview_and_places_once(self):
        out, code = server_mod.order_preview(
            {"symbol": "AMAT", "side": "sell", "type": "market",
             "quantity": 0.081696}, "ip")
        self.assertEqual(code, 200)
        pid = out["preview_id"]
        res, code = server_mod.order_place({"preview_id": pid}, "ip")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.stub.placed), 1)
        self.assertEqual(self.stub.placed[0]["symbol"], "AMAT")
        res2, code2 = server_mod.order_place({"preview_id": pid}, "ip")
        self.assertEqual(code2, 410)                         # single-use preview
        res3, code3 = server_mod.order_place({"preview_id": "nope"}, "ip")
        self.assertEqual(code3, 410)

    def test_place_blocked_while_manual_lock_held(self):
        out, _ = server_mod.order_preview(
            {"symbol": "AMAT", "side": "sell", "type": "market",
             "quantity": 0.05}, "ip")
        controls.take_manual_lock("AMAT")
        res, code = server_mod.order_place({"preview_id": out["preview_id"]}, "ip")
        self.assertEqual(code, 409)
        controls.release_manual_lock("AMAT")

    def test_cycle_running_warning_respects_freshness(self):
        agent_module.write_cycle_status("running", "market_hours_check")
        out, _ = server_mod.order_preview(
            {"symbol": "AMAT", "side": "sell", "type": "market", "quantity": 0.01},
            "ip")
        self.assertTrue(any("cycle is running" in w.lower() for w in out["warnings"]))
        # stale running stamp must NOT warn
        p = os.path.join(self.tmpdir, "logs", "cycle_status.json")
        stale = json.load(open(p))
        stale["ts"] = "2020-01-01T00:00:00-04:00"
        json.dump(stale, open(p, "w"))
        out, _ = server_mod.order_preview(
            {"symbol": "AMAT", "side": "sell", "type": "market", "quantity": 0.01},
            "ip")
        self.assertFalse(any("cycle is running" in w.lower() for w in out["warnings"]))


# ──────────────────────────────────────────── agent control-plane hooks ─────
class TestAgentControlPlane(TmpRootMixin):

    def _pos(self, symbol="AMAT", entry=600.0, peak=None, shares=0.1):
        return {"id": "T0001", "symbol": symbol, "entry_price": entry,
                "peak_price": peak if peak is not None else entry,
                "entry_date": "2026-07-01T10:00:00-04:00", "shares": shares,
                "dollar_amount": entry * shares, "confidence_score": 70,
                "sources_used": [], "thesis": "t"}

    def test_stop_override_absolute_price_triggers(self):
        self.log["open_positions"] = [self._pos(entry=600.0)]
        self._flush_log()
        controls.set_stop_override("AMAT", stop_price=595.0)
        with patch.object(agent_module, "signals") as sig:
            sig.signal_for.return_value = {"ok": True, "last_close": 594.0}
            alerts = agent_module.check_stop_loss_alerts(json.load(
                open(os.path.join(self.tmpdir, "trade_log.json"))))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["threshold_price"], 595.0)
        # without override, -1% is nowhere near the 10% stop
        controls.set_stop_override("AMAT", clear=True)
        with patch.object(agent_module, "signals") as sig:
            sig.signal_for.return_value = {"ok": True, "last_close": 594.0}
            alerts = agent_module.check_stop_loss_alerts(json.load(
                open(os.path.join(self.tmpdir, "trade_log.json"))))
        self.assertEqual(alerts, [])

    def test_stop_override_pct_tightens(self):
        self.log["open_positions"] = [self._pos(entry=100.0)]
        self._flush_log()
        controls.set_stop_override("AMAT", stop_pct=0.02)
        with patch.object(agent_module, "signals") as sig:
            sig.signal_for.return_value = {"ok": True, "last_close": 97.0}
            alerts = agent_module.check_stop_loss_alerts(json.load(
                open(os.path.join(self.tmpdir, "trade_log.json"))))
        self.assertEqual(len(alerts), 1)  # -3% breaches the 2% override

    def test_trailing_override_per_symbol(self):
        self.log["open_positions"] = [self._pos(entry=100.0, peak=200.0)]
        self._flush_log()
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        with patch.object(agent_module, "signals", object()):
            # default 25% trail: 170 is a 15% giveback -> no alert
            self.assertEqual(
                agent_module.check_trailing_stop_alerts(log, prices={"AMAT": 170.0}), [])
            controls.set_stop_override("AMAT", trail_pct=0.10)
            alerts = agent_module.check_trailing_stop_alerts(log, prices={"AMAT": 170.0})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["threshold_price"], 180.0)

    def test_stop_snapshot_reflects_overrides(self):
        self.log["open_positions"] = [self._pos(entry=100.0, peak=200.0)]
        self._flush_log()
        controls.set_stop_override("AMAT", stop_price=97.5, trail_pct=0.10)
        agent_module.write_stop_snapshot(
            json.load(open(os.path.join(self.tmpdir, "trade_log.json"))),
            self.strategy)
        snap = json.load(open(os.path.join(self.tmpdir, "logs", "stops.json")))
        self.assertEqual(snap["stops"]["AMAT"]["stop"], 97.5)
        self.assertEqual(snap["stops"]["AMAT"]["trail_stop"], 180.0)

    def _with_progress_tracking(self):
        self.strategy["progress_tracking"] = {
            "monthly_goal": "100%", "month_start_value": 100.0,
            "current_value": 100.0, "current_return": "0.0%", "on_track": True}
        self._write("strategy/strategy.json", json.dumps(self.strategy))

    def test_process_manual_cash_flows_applies_deposit(self):
        self._with_progress_tracking()
        controls.record_cash_flow(94.97, "deposit via app")
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        fake_rg = MagicMock()
        with patch.object(agent_module, "risk_guard", fake_rg):
            agent_module.process_manual_cash_flows(log)
        s = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))["summary"]
        self.assertAlmostEqual(s["month_start_value"], 194.97)
        self.assertAlmostEqual(s["current_value"], 194.97)
        fake_rg.rebase_peak.assert_called_once_with(94.97)
        strat = json.load(open(os.path.join(self.tmpdir, "strategy", "strategy.json")))
        self.assertAlmostEqual(strat["progress_tracking"]["current_value"], 194.97)
        flows = controls.list_cash_flows()
        self.assertTrue(flows[0]["applied"])

    def test_process_manual_cash_flows_withdrawal_and_idempotent(self):
        self._with_progress_tracking()
        controls.record_cash_flow(-25.5, "withdrawal")
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        with patch.object(agent_module, "risk_guard", MagicMock()):
            agent_module.process_manual_cash_flows(log)
            s1 = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))["summary"]
            self.assertAlmostEqual(s1["current_value"], 74.5)
            # second call must not re-apply the already-applied entry
            log2 = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
            agent_module.process_manual_cash_flows(log2)
        s2 = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))["summary"]
        self.assertAlmostEqual(s2["current_value"], 74.5)

    def test_process_manual_cash_flows_no_file_is_noop(self):
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        with patch.object(agent_module, "risk_guard", MagicMock()):
            agent_module.process_manual_cash_flows(log)   # must not raise
        s = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))["summary"]
        self.assertAlmostEqual(s["current_value"], 100.0)

    def test_dnt_suppresses_enter_long_wake(self):
        raw = {"NVDA": {"ok": True, "state": "BUY", "transition": "ENTER_LONG"}}
        log = self.log
        with patch.object(agent_module, "signals", object()):
            skip, reason = agent_module.should_skip_model_call(raw, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "enter_long:NVDA")
        controls.set_do_not_trade("NVDA", True)
        with patch.object(agent_module, "signals", object()):
            skip, reason = agent_module.should_skip_model_call(raw, log)
        self.assertTrue(skip)

    def test_manual_close_tagged_and_pipeline_skipped(self):
        self.log["open_positions"] = [self._pos(symbol="MU", entry=1000.0, shares=0.11)]
        self.log["_state"]["last_positions"] = [
            {"symbol": "MU", "shares": 0.11, "avg_price": 1000.0, "last_price": 1005.0}]
        self._flush_log()
        controls.journal("order_place", {"symbol": "MU", "side": "sell"},
                         {"ok": True}, "ip")
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        with patch.object(agent_module, "run_post_trade_pipeline") as pipe, \
             patch.object(agent_module, "update_monthly_progress") as ump:
            agent_module.process_cycle_state(log, [], [], exit_info=None)
        pipe.assert_not_called()          # manual close: no postmortem
        ump.assert_called_once()          # but monthly progress stays fresh
        trades = log["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "manual")
        self.assertFalse(trades[0]["stop_loss"])

    def test_bot_close_still_fires_pipeline(self):
        self.log["open_positions"] = [self._pos(symbol="MU", entry=1000.0, shares=0.11)]
        self._flush_log()
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        with patch.object(agent_module, "run_post_trade_pipeline") as pipe:
            agent_module.process_cycle_state(
                log, [], [], exit_info={"MU": {"reason": "stop_loss", "price": 900.0}})
        pipe.assert_called_once()
        self.assertTrue(log["trades"][0]["stop_loss"])
        self.assertEqual(log["trades"][0]["exit_reason"], "stop_loss")

    def test_cycle_status_and_equity_writers(self):
        agent_module.write_cycle_status("running", "market_hours_check", "x")
        st = json.load(open(os.path.join(self.tmpdir, "logs", "cycle_status.json")))
        self.assertEqual(st["state"], "running")
        agent_module.append_equity_point(322.49, cash=209.53)
        agent_module.append_equity_point(999.0)   # same minute -> deduped
        lines = open(os.path.join(self.tmpdir, "logs",
                                  "equity_curve.jsonl")).read().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["total"], 322.49)

    def test_paused_cycle_runs_protective_exits_only(self):
        """PAUSE: no model turn; forced exits + reconcile still run."""
        self.log["open_positions"] = [self._pos(symbol="MU", entry=1000.0, shares=0.11)]
        self._flush_log()
        controls.pause("test")
        broker_positions = [{"symbol": "MU", "shares": 0.11,
                             "avg_price": 1000.0, "last_price": 880.0}]
        with patch.object(agent_module, "signals") as sig, \
             patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "read_broker_state",
                          side_effect=[(broker_positions, set(), 322.0, 0.0),
                                       ([], set(), 322.0, 322.0)]), \
             patch.object(agent_module, "force_sell",
                          return_value=(True, 880.0)) as fs, \
             patch.object(agent_module, "run_model") as rm, \
             patch.object(agent_module, "run_post_trade_pipeline"), \
             patch.object(agent_module, "active_skill",
                          return_value=("skill", "market_hours_check")), \
             patch.object(agent_module, "_run_shadow_passes"), \
             patch.object(agent_module, "process_rx3_paper", create=True):
            sig.signals_with_raw.return_value = (
                {"MU": {"ok": True, "state": "SELL", "transition": "NO_ACTION",
                        "last_close": 880.0}}, "MU: SELL")
            sig.signal_for.return_value = {"ok": True, "last_close": 880.0}
            agent_module.run_agent()
        rm.assert_not_called()            # paused: never call the model
        fs.assert_called_once()           # but the -12% stop still force-sold
        self.assertEqual(fs.call_args[0][0], "MU")
        log = json.load(open(os.path.join(self.tmpdir, "trade_log.json")))
        self.assertEqual(len(log["trades"]), 1)  # close reconciled while paused

    def test_paused_force_sell_defers_to_manual_lock(self):
        self.log["open_positions"] = [self._pos(symbol="MU", entry=1000.0, shares=0.11)]
        self._flush_log()
        controls.pause("test")
        controls.take_manual_lock("MU")
        broker_positions = [{"symbol": "MU", "shares": 0.11,
                             "avg_price": 1000.0, "last_price": 880.0}]
        with patch.object(agent_module, "signals") as sig, \
             patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "read_broker_state",
                          side_effect=[(broker_positions, set(), 322.0, 0.0),
                                       (broker_positions, set(), 322.0, 0.0)]), \
             patch.object(agent_module, "force_sell") as fs, \
             patch.object(agent_module, "run_model") as rm, \
             patch.object(agent_module, "active_skill",
                          return_value=("skill", "market_hours_check")), \
             patch.object(agent_module, "_run_shadow_passes"):
            sig.signals_with_raw.return_value = (
                {"MU": {"ok": True, "state": "SELL", "transition": "NO_ACTION",
                        "last_close": 880.0}}, "MU: SELL")
            sig.signal_for.return_value = {"ok": True, "last_close": 880.0}
            agent_module.run_agent()
        fs.assert_not_called()            # deferred to the operator's in-flight order
        rm.assert_not_called()


# ─────────────────────────────────────── command-center endpoints (Mac app) ────
class _FakeHandler:
    """Minimal stand-in — server GET views only read handler.path."""
    def __init__(self, path):
        self.path = path


class TestCommandCenterEndpoints(TmpRootMixin):
    """The endpoints added for the Mac app's expanded sidebar: /api/signals,
    /api/orders, /api/rx3, /api/health, /api/learning, /api/calendar. All must
    be offline-safe (canned quotes/ribbons, no network)."""

    def setUp(self):
        super().setUp()
        self.stub = _StubBroker()
        self._srv_patches = [
            patch.object(server_mod, "broker", self.stub),
            patch.object(server_mod.quotes, "quote",
                         lambda s: {"symbol": s, "price": 100.0, "prev_close": 98.0,
                                    "change_pct": 2.04, "spark": [98.0, 100.0]}),
            patch.object(server_mod.quotes, "ribbon",
                         lambda s: {"symbol": s, "ok": True, "state": "BUY",
                                    "transition": "HOLD",
                                    "lines": {"blue": 101.0, "green": 100.5,
                                              "yellow": 100.0, "red": 99.0},
                                    "last_close": 100.0, "bars": 200,
                                    "source": "test", "warmup_ok": True, "note": ""}),
            patch.object(server_mod.quotes, "daily_series", lambda s, r="6mo": []),
            patch.object(server_mod.quotes, "market_open_now", lambda: True),
        ]
        for p in self._srv_patches:
            p.start()

    def tearDown(self):
        for p in self._srv_patches:
            p.stop()
        super().tearDown()

    def test_signals_view_held_first_with_margin(self):
        self.log["open_positions"] = [{"id": "P1", "symbol": "ZZZ", "shares": 1,
                                       "entry_price": 90.0}]
        self._flush_log()
        out = server_mod.signals_view()
        rows = out["rows"]
        self.assertTrue(rows, "expected at least the held symbol + watchlist")
        self.assertEqual(rows[0]["symbol"], "ZZZ")          # held sorts first
        self.assertTrue(rows[0]["held"])
        # red(99) below the fast pack (min 100) → positive BUY margin ≈ 1.01%
        self.assertAlmostEqual(rows[0]["red_margin_pct"], 1.01, places=2)
        self.assertEqual(out["interval"], server_mod.signals.INTERVAL)

    def test_orders_view_history_gated_off_non_direct_broker(self):
        out = server_mod.orders_view(_FakeHandler("/api/orders?days=1"))
        self.assertEqual(out["days"], 1)
        self.assertIsNone(out["history"])
        self.assertIsNone(out["history_error"])
        out = server_mod.orders_view(_FakeHandler("/api/orders?days=7"))
        self.assertEqual(out["days"], 7)
        self.assertIsNone(out["history"])                    # stub ≠ direct-mcp
        self.assertIn("rh_login", out["history_error"])
        out = server_mod.orders_view(_FakeHandler("/api/orders?days=bogus"))
        self.assertEqual(out["days"], 1)                     # bad param degrades

    def test_orders_view_journal_only_order_actions(self):
        controls.journal("order_place", {"symbol": "SPY"}, {"ok": True}, "ip")
        controls.journal("bot_pause", {}, {"ok": True}, "ip")
        out = server_mod.orders_view(_FakeHandler("/api/orders"))
        self.assertEqual(len(out["journal"]), 1)
        self.assertEqual(out["journal"][0]["action"], "order_place")

    def test_rx3_view_parses_paper_state(self):
        self._write("shadow/rx3_paper.json", json.dumps({
            "_note": "test", "start_date": "2026-07-08T09:42:55-04:00",
            "start_equity": 100.0, "cash": 25.0, "equity": 110.0,
            "positions": {"AMAT": {"shares": 0.5, "last_px": 200.0}},
            "leaders": ["AMAT", "LABU"],
            "history": [
                {"date": "2026-07-08", "equity": 100.0, "leaders": ["AMAT"],
                 "defensive": [], "riskx": 1.0, "vol_scale": 1.0, "cash_w": 0.0},
                {"date": "2026-07-09", "equity": 110.0, "leaders": ["AMAT"],
                 "defensive": ["XLU"], "riskx": 0.75, "vol_scale": 0.9,
                 "cash_w": 0.25},
            ],
        }))
        out = server_mod.rx3_view()
        self.assertEqual(out["meta"]["return_pct"], 10.0)
        self.assertEqual(out["paper_days"], 2)
        self.assertEqual(len(out["curve"]), 2)
        self.assertEqual(out["history"][0]["date"], "2026-07-09")  # newest first
        pos = out["positions"][0]
        self.assertEqual(pos["symbol"], "AMAT")
        self.assertAlmostEqual(pos["value"], 50.0)           # 0.5 sh × $100 quote

    def test_rx3_view_missing_file_safe(self):
        out = server_mod.rx3_view()
        self.assertEqual(out["paper_days"], 0)
        self.assertEqual(out["positions"], [])
        self.assertIsNone(out["latest"])

    def _arm_live(self, capital_pct=1.0):
        s = readers.strategy()
        s["rotation"] = {"mode": "live", "top_n": 2,
                         "live": {"enabled": True, "capital_pct": capital_pct,
                                  "max_orders_per_day": 8}}
        self._write("strategy/strategy.json", json.dumps(s))

    def test_rx3_view_switches_to_the_live_desk_when_armed(self):
        """The badge is read from strategy.json, not from whether a state file
        happens to exist — the UI must never say 'paper' while real orders fly."""
        self._arm_live()
        self._write("logs/rx3_live.json", json.dumps({
            "mode": "live", "equity": 1000.0, "buying_power": 200.0,
            "targets": {"AAA": 0.5, "BBB": 0.5},
            "orders_today": {"date": "2026-08-13", "count": 2},
            "plan": [{"side": "buy", "symbol": "BBB", "dollar_amount": 300.0}],
            "orders": [{"ts": "2026-08-13T09:35:00-04:00", "side": "buy",
                        "symbol": "AAA", "dollar_amount": 500.0, "placed": True}],
            "history": [{"date": "2026-08-13", "equity": 1000.0,
                         "leaders": ["AAA", "BBB"], "defensive": [],
                         "riskx": 1.0, "vol_scale": 1.0, "cash_w": 0.0,
                         "orders": 2, "placed": 2}],
        }))
        self.log["open_positions"] = [{"id": "P1", "symbol": "AAA", "shares": 2.0,
                                       "entry_price": 90.0}]
        self._flush_log()
        out = server_mod.rx3_view()
        self.assertEqual(out["mode"], "live")
        self.assertEqual(out["meta"]["equity"], 1000.0)
        self.assertEqual(out["meta"]["orders_today"], 2)
        book = {r["symbol"]: r for r in out["book"]}
        # AAA: 2 shares × $100 quote = $200 of a $1,000 deployable book = 20%
        self.assertEqual(book["AAA"]["current_pct"], 20.0)
        self.assertEqual(book["AAA"]["target_pct"], 50.0)
        self.assertEqual(book["AAA"]["drift_usd"], 300.0)
        self.assertTrue(book["AAA"]["in_book"])
        self.assertEqual(book["BBB"]["shares"], 0)
        self.assertEqual(len(out["orders"]), 1)

    def test_rx3_live_keeps_the_pre_live_paper_record_visible(self):
        """The paper curve is the evidence the promotion decision rested on; it
        stays reachable after go-live instead of being replaced."""
        self._write("shadow/rx3_paper.json", json.dumps({
            "start_equity": 100.0, "equity": 102.0, "positions": {},
            "history": [{"date": "2026-07-08", "equity": 100.0},
                        {"date": "2026-07-09", "equity": 102.0}]}))
        self._arm_live()
        out = server_mod.rx3_view()
        self.assertEqual(out["paper_record"]["days"], 2)
        self.assertEqual(out["paper_record"]["meta"]["return_pct"], 2.0)

    def test_rx3_live_capital_pct_scales_the_reported_weights(self):
        self._arm_live(capital_pct=0.5)
        self._write("logs/rx3_live.json", json.dumps({
            "equity": 1000.0, "targets": {"AAA": 1.0}, "history": []}))
        self.log["open_positions"] = [{"id": "P1", "symbol": "AAA", "shares": 2.0,
                                       "entry_price": 90.0}]
        self._flush_log()
        book = {r["symbol"]: r for r in server_mod.rx3_view()["book"]}
        # $200 held against a HALF-size deployable book ($500) = 40%
        self.assertEqual(book["AAA"]["current_pct"], 40.0)

    def test_rx3_live_missing_state_file_is_safe(self):
        """Armed but not yet run (the state file appears on the first live pass):
        the desk still renders, and anything the account holds shows as
        out-of-book — which is exactly what the engine is about to sell."""
        self._arm_live()
        out = server_mod.rx3_view()
        self.assertEqual(out["mode"], "live")
        self.assertEqual(out["orders"], [])
        self.assertEqual(out["targets"], {})
        self.assertTrue(all(not r["in_book"] and r["target_pct"] == 0.0
                            for r in out["book"]))

    def test_rx3_live_equity_falls_back_before_the_first_pass(self):
        """Weights are meaningless without a denominator, so equity falls back
        to the broker snapshot, and to the last recorded equity point when even
        that is empty (the dashboard's claude-cli broker path never polls)."""
        self._arm_live()
        self._write("logs/equity_curve.jsonl",
                    '{"ts": "2026-08-12T15:00:00-04:00", "total": 367.72}\n')
        self.assertEqual(server_mod.rx3_view()["meta"]["equity"], 322.49)  # broker
        self.stub.state["portfolio"] = {}
        self.assertEqual(server_mod.rx3_view()["meta"]["equity"], 367.72)  # curve

    def test_rx4_view_parses_the_paper_track(self):
        self._write("shadow/rx4_paper.json", json.dumps({
            "_note": "rx4", "start_date": "2026-08-05T09:33:00-04:00",
            "start_equity": 100.0, "cash": 0.5, "equity": 93.0,
            "positions": {"SNOW": {"shares": 0.14, "last_px": 320.0}},
            "leaders": ["CRWD", "SNOW"],
            "history": [{"date": "2026-08-05", "equity": 100.0,
                         "leaders": ["SNOW"], "defensive": [], "riskx": 1.0,
                         "vol_scale": 1.0, "cash_w": 0.0},
                        {"date": "2026-08-06", "equity": 93.0,
                         "leaders": ["CRWD"], "defensive": [], "riskx": 0.5,
                         "vol_scale": 1.0, "cash_w": 0.0}],
        }))
        out = server_mod.rx4_view()
        self.assertEqual(out["mode"], "paper")
        self.assertEqual(out["meta"]["return_pct"], -7.0)
        self.assertEqual(out["days"], 2)
        self.assertEqual(out["history"][0]["date"], "2026-08-06")   # newest first
        self.assertEqual(out["positions"][0]["symbol"], "SNOW")

    def test_rx4_view_compares_against_the_live_book_once_rx3_is_live(self):
        self._arm_live()
        self._write("shadow/rx4_paper.json", json.dumps({
            "start_date": "2026-08-05T09:33:00-04:00", "start_equity": 100.0,
            "equity": 93.0, "positions": {},
            "history": [{"date": "2026-08-05", "equity": 100.0}]}))
        self._write("logs/equity_curve.jsonl",
                    '{"ts": "2026-08-05T10:00:00-04:00", "total": 400.0}\n'
                    '{"ts": "2026-08-06T10:00:00-04:00", "total": 420.0}\n')
        out = server_mod.rx4_view()
        self.assertEqual(out["rx3_comparison"]["source"], "live")
        # rebased to 100 at RX-4's start so the two curves are comparable
        self.assertEqual([p["value"] for p in out["rx3_comparison"]["curve"]],
                         [100.0, 105.0])

    def test_rx4_view_missing_file_safe(self):
        out = server_mod.rx4_view()
        self.assertEqual(out["days"], 0)
        self.assertEqual(out["positions"], [])
        self.assertEqual(out["rx3_comparison"]["curve"], [])

    def test_health_view_reports_freshness_and_control_plane(self):
        self._write("logs/heartbeat", "2026-07-10T09:35:00-04:00")
        controls.set_do_not_trade("TSLA", True)
        out = server_mod.health_view()
        by_label = {f["label"]: f for f in out["files"]}
        self.assertTrue(by_label["Heartbeat"]["exists"])
        self.assertLess(by_label["Heartbeat"]["age_seconds"], 60)
        self.assertFalse(by_label["Watchdog log"]["exists"])
        self.assertEqual(out["control"]["dnt"], ["TSLA"])
        self.assertFalse(out["control"]["halt"]["halted"])
        self.assertIn("uptime_seconds", out["server"])

    def test_learning_view_skill_versions_is_list(self):
        self._write("skills/history/skill_1_research_v001.md", "x")
        self._write("skills/history/skill_1_research_v002.md", "x")
        out = server_mod.learning_view()
        self.assertEqual(out["skill_versions"],
                         [{"skill": "skill_1_research", "versions": 2, "latest": 2}])
        self.assertEqual(out["learned_rules"], [])

    def test_bot_schedule_weekdays_and_midweek(self):
        from datetime import datetime as dt
        wed = dt(2026, 7, 8, 8, 0, tzinfo=server_mod.ET)      # Wednesday pre-market
        events = server_mod._bot_schedule(wed)
        self.assertTrue(events)
        kinds_today = [e["kind"] for e in events if e["when"].startswith("2026-07-08")]
        # 08:00 is past the 04:25 preflight anchor, so it is not listed again
        self.assertEqual(kinds_today,
                         ["research", "open", "midweek", "close", "maintenance"])

    def test_bot_schedule_includes_the_preflight_anchor(self):
        """The anchor is the appointment the whole usage schedule hangs off —
        it must be visible to the operator, and it must fall ~5h before the
        open so its window expires before the bell."""
        from datetime import datetime as dt
        pre = dt(2026, 7, 8, 1, 0, tzinfo=server_mod.ET)      # Wednesday, 1 AM
        events = server_mod._bot_schedule(pre)
        today = [e for e in events if e["when"].startswith("2026-07-08")]
        self.assertEqual(today[0]["kind"], "preflight")
        self.assertTrue(today[0]["when"].startswith("2026-07-08T04:25"),
                        f"anchor at {today[0]['when']}, expected 04:25 ET")
        sat = dt(2026, 7, 11, 8, 0, tzinfo=server_mod.ET)     # Saturday
        events = server_mod._bot_schedule(sat)
        self.assertTrue(all(not e["when"].startswith("2026-07-11")
                            and not e["when"].startswith("2026-07-12") for e in events))
        thu = dt(2026, 7, 9, 20, 0, tzinfo=server_mod.ET)     # Thu after close
        events = server_mod._bot_schedule(thu)
        midweeks = [e for e in events if e["kind"] == "midweek"]
        self.assertTrue(all(e["when"].startswith("2026-07-15") for e in midweeks))

    def test_calendar_view_offline(self):
        out = server_mod.calendar_view()
        self.assertTrue(out["schedule"])
        self.assertIn("earnings", out)
        self.assertEqual(out["blackout_windows"], {})        # not in fixture strategy


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules["__main__"])
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
