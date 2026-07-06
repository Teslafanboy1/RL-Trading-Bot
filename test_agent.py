#!/usr/bin/env python3
"""
test_agent.py — comprehensive edge-case + token-usage test suite.

Run:  python3 test_agent.py
      python3 test_agent.py -v          (verbose per-test output)

Covers:
  • signals.py — EMA math, BUY/SELL/NEUTRAL classification, transitions, insufficient data
  • agent.py   — stop-loss alerts, smart skip, JSON extraction, position tracking,
                  trade outcome P&L, weight rebalancing, monthly progress,
                  process_cycle_state, watchlist dedup, format blocks
  • Token usage — prompt-size estimate for every cycle type vs the skip path
"""

import csv
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import signals as sig_module
import agent as agent_module
import options_shadow as osh

# ─────────────────────────────────────────── synthetic price series ──────────

def _uptrend(n=120, start=100.0, jump_bars=15, step=1.5):
    """Flat base then a recent upward impulse → red TEMA(55) still climbing
    from the base → red is LOWEST → BUY. (A steady linear ramp is degenerate
    for TEMA: its lag-cancellation parks all three TEMAs on the trend line,
    so BUY/SELL states come from impulse moves, matching real breakouts.)"""
    flat = [start] * (n - jump_bars)
    return flat + [start + step * i for i in range(1, jump_bars + 1)]

def _downtrend(n=120, start=200.0, jump_bars=15, step=1.5):
    """Flat base then a recent downward impulse → red TEMA(55) still up at
    the base → red is HIGHEST → SELL."""
    flat = [start] * (n - jump_bars)
    return flat + [start - step * i for i in range(1, jump_bars + 1)]

def _flat(n=120, price=100.0):
    """Flat closes → all EMAs converge → NEUTRAL."""
    return [price] * n

def _short(n=59, price=100.0):
    """Below MIN_BARS → INSUFFICIENT_DATA."""
    return [price] * n


# ─────────────────────────────────────────── isolated temp dir mixin ─────────

class TmpDirMixin(unittest.TestCase):
    """Provides an isolated temp directory and patches agent.ROOT for every test."""

    _DEFAULT_STRATEGY = {
        "version": 1,
        "research": {
            "source_weights": {"news": 0.30, "social": 0.20,
                               "fundamental": 0.35, "macro": 0.15},
            "source_performance": {
                "news":        {"wins": 0, "losses": 0, "accuracy": 0},
                "social":      {"wins": 0, "losses": 0, "accuracy": 0},
                "fundamental": {"wins": 0, "losses": 0, "accuracy": 0},
                "macro":       {"wins": 0, "losses": 0, "accuracy": 0},
                "rss":         {"wins": 0, "losses": 0, "accuracy": 0},
            },
            "min_trades_before_weight_shift": 5,
            "min_confidence_to_trade": 60,
        },
        "risk_management": {"stop_loss_pct": 0.10, "leverage_adjusted_stop": True},
        "capital_allocation": {"max_single_position": 0.30},
        "position_sizing": {
            "model": "risk_budget_leverage_adjusted",
            "risk_per_trade_pct": 0.02,
            "leverage_factors": {"SOXL": 3, "TQQQ": 3, "SQQQ": 3, "SSO": 2},
            "band_ceilings": {"90_to_100": 0.30, "75_to_89": 0.20,
                              "60_to_74": 0.15, "below_60": 0.0},
            "max_single_position": 0.30,
            "cash_reserve": 0.10,
        },
        "factor_exposure_limits": {
            "max_sector_pct": 0.40,
            "max_positions_per_sector": 2,
            "leveraged_etf_counts_at_leverage": True,
            "sector_buckets": {
                "semis_ai_hardware": ["NVDA", "AMD", "AMAT", "SOXL"],
                "fintech_crypto": ["HOOD", "COIN"],
            },
        },
        "progress_tracking": {"month_start_value": 100.0},
        "version_history": [],
    }

    _DEFAULT_LOG = {
        "summary": {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0.0, "month_start_value": 100.0, "current_value": 100.0,
            "monthly_return_pct": 0.0, "monthly_goal": "100%", "on_track": True,
        },
        "open_positions": [],
        "trades": [],
        "_state": {"last_positions": [], "next_id": 1},
    }

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="trading_test_")
        for sub in ("data", "strategy/history", "research", "postmortems", "skills"):
            os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
        import copy
        self.strategy = copy.deepcopy(self._DEFAULT_STRATEGY)
        self.tradelog = copy.deepcopy(self._DEFAULT_LOG)
        self._flush_strategy()
        self._flush_log()
        self._root_patch = patch.object(agent_module, "ROOT", self.tmpdir)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _flush_strategy(self):
        path = os.path.join(self.tmpdir, "strategy", "strategy.json")
        with open(path, "w") as f:
            json.dump(self.strategy, f, indent=2)

    def _flush_log(self):
        path = os.path.join(self.tmpdir, "trade_log.json")
        with open(path, "w") as f:
            json.dump(self.tradelog, f, indent=2)

    def _write_csv(self, symbol, closes):
        path = os.path.join(self.tmpdir, "data", f"{symbol.upper()}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Close"])
            for i, c in enumerate(closes):
                w.writerow([f"2026-01-{(i % 28) + 1:02d}", round(c, 6)])

    def _open_pos(self, symbol="TEST", entry=100.0, shares=1.0, pos_id="T0001"):
        return {"id": pos_id, "symbol": symbol, "entry_price": entry,
                "entry_date": "2026-06-01T09:30:00", "shares": shares,
                "dollar_amount": round(entry * shares, 2),
                "confidence_score": 75, "sources_used": [], "thesis": "test"}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — EMA computation (pure math, no I/O)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEMAComputation(unittest.TestCase):

    def test_ema_empty(self):
        self.assertEqual(sig_module.ema([], 8), [])

    def test_ema_single(self):
        result = sig_module.ema([42.0], 8)
        self.assertEqual(result, [42.0])

    def test_ema_constant_series_equals_constant(self):
        closes = [100.0] * 100
        result = sig_module.ema(closes, 55)
        self.assertAlmostEqual(result[-1], 100.0, places=6)

    def test_ema_length_matches_input(self):
        closes = list(range(1, 101))
        for length in (8, 13, 21, 55):
            self.assertEqual(len(sig_module.ema(closes, length)), 100)

    def test_upward_impulse_red_lowest(self):
        """After a recent breakout, the slow red EMA(55) is still rising from
        the base and must be the lowest line (BUY zone)."""
        lines = sig_module.compute_lines(_uptrend(120))
        self.assertLess(lines["red"], min(lines["blue"], lines["green"], lines["yellow"]))

    def test_downward_impulse_red_highest(self):
        """After a recent breakdown, the slow red EMA(55) is still up at the
        base and must be the highest line (SELL zone)."""
        lines = sig_module.compute_lines(_downtrend(120))
        self.assertGreater(lines["red"], max(lines["blue"], lines["green"], lines["yellow"]))

    def test_ema_flat_all_equal(self):
        closes = _flat(120)
        lines = sig_module.compute_lines(closes)
        for name, val in lines.items():
            self.assertAlmostEqual(val, 100.0, places=4,
                                   msg=f"{name} EMA diverged on flat series")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Signal classification (BUY / SELL / NEUTRAL)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalClassification(unittest.TestCase):

    def test_buy_signal_red_lowest(self):
        lines = {"blue": 110, "green": 105, "yellow": 102, "red": 95}
        self.assertEqual(sig_module.classify_signal(lines), "BUY")

    def test_sell_signal_red_highest(self):
        lines = {"blue": 90, "green": 95, "yellow": 98, "red": 110}
        self.assertEqual(sig_module.classify_signal(lines), "SELL")

    def test_neutral_red_middle(self):
        lines = {"blue": 110, "green": 105, "yellow": 98, "red": 100}
        self.assertEqual(sig_module.classify_signal(lines), "NEUTRAL")

    def test_neutral_all_equal(self):
        lines = {"blue": 100, "green": 100, "yellow": 100, "red": 100}
        self.assertEqual(sig_module.classify_signal(lines), "NEUTRAL")

    def test_neutral_red_tied_with_min(self):
        """red == min(others) is NOT BUY (strict < required)."""
        lines = {"blue": 110, "green": 105, "yellow": 100, "red": 100}
        self.assertEqual(sig_module.classify_signal(lines), "NEUTRAL")

    def test_neutral_red_tied_with_max(self):
        """red == max(others) is NOT SELL (strict > required)."""
        lines = {"blue": 90, "green": 95, "yellow": 100, "red": 100}
        self.assertEqual(sig_module.classify_signal(lines), "NEUTRAL")

    def test_uptrend_data_produces_buy(self):
        lines = sig_module.compute_lines(_uptrend(120))
        self.assertEqual(sig_module.classify_signal(lines), "BUY")

    def test_downtrend_data_produces_sell(self):
        lines = sig_module.compute_lines(_downtrend(120))
        self.assertEqual(sig_module.classify_signal(lines), "SELL")

    def test_flat_data_produces_neutral(self):
        lines = sig_module.compute_lines(_flat(120))
        self.assertEqual(sig_module.classify_signal(lines), "NEUTRAL")

    def test_runup_then_shallow_fade_stays_buy_plain_ema(self):
        """Guards against reintroducing TEMA. The ribbon is FOUR PLAIN EMAs —
        the chart's "TEMA"-shorttitled "Three Moving Averages [AdventTrading]"
        script is `ema()` x3 (verified from its Pine source), not triple-EMA.
        After a sharp rally and a shallow fade, the slow EMA(55) is still
        climbing from the base and stays the LOWEST line, so the signal
        correctly remains BUY — matching the chart, which holds the position.
        A 2026-06-12 change swapped lag-reducing TEMA in here; it OVERSHOT,
        lifting red on top of the ribbon, flipped this to a false SELL, and
        force-liquidated the whole book on 2026-06-15."""
        base = [3.95] * 200
        rally = [3.95 + 1.10 * (i / 25.0) for i in range(1, 26)]
        fade = [5.05 - 0.47 * (i / 20.0) for i in range(1, 21)]
        shape = base + rally + fade

        lines = sig_module.compute_lines(shape)
        self.assertEqual(sig_module.classify_signal(lines), "BUY")
        # red(55) must stay the lowest line (clean fan) — never lifted on top.
        self.assertLess(lines["red"],
                        min(lines["blue"], lines["green"], lines["yellow"]))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Transition detection (ENTER_LONG / EXIT / HOLD / NO_ACTION)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransitionDetection(unittest.TestCase):

    def test_neutral_to_buy_is_enter_long(self):
        self.assertEqual(sig_module.detect_transition("NEUTRAL", "BUY"), "ENTER_LONG")

    def test_sell_to_buy_is_enter_long(self):
        self.assertEqual(sig_module.detect_transition("SELL", "BUY"), "ENTER_LONG")

    def test_buy_to_buy_is_hold(self):
        self.assertEqual(sig_module.detect_transition("BUY", "BUY"), "HOLD")

    def test_neutral_to_sell_is_exit(self):
        self.assertEqual(sig_module.detect_transition("NEUTRAL", "SELL"), "EXIT")

    def test_buy_to_sell_is_exit(self):
        self.assertEqual(sig_module.detect_transition("BUY", "SELL"), "EXIT")

    def test_sell_to_sell_is_no_action(self):
        self.assertEqual(sig_module.detect_transition("SELL", "SELL"), "NO_ACTION")

    def test_neutral_to_neutral_is_no_action(self):
        self.assertEqual(sig_module.detect_transition("NEUTRAL", "NEUTRAL"), "NO_ACTION")

    def test_buy_to_neutral_is_no_action(self):
        """Uptrend weakening but no SELL signal yet → hold but no new action."""
        self.assertEqual(sig_module.detect_transition("BUY", "NEUTRAL"), "NO_ACTION")

    def test_sell_to_neutral_is_no_action(self):
        self.assertEqual(sig_module.detect_transition("SELL", "NEUTRAL"), "NO_ACTION")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — signal_for: insufficient data + warmup warning
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalFor(unittest.TestCase):

    def _patched_signal(self, closes):
        """Call signal_for with mocked fetch_daily_closes."""
        with patch.object(sig_module, "fetch_daily_closes",
                          return_value=(closes, "mock")):
            return sig_module.signal_for("SYM")

    def test_insufficient_data_59_bars(self):
        s = self._patched_signal(_short(59))
        self.assertFalse(s["ok"])
        self.assertEqual(s["state"], "INSUFFICIENT_DATA")

    def test_exactly_60_bars_is_valid(self):
        s = self._patched_signal(_flat(60))
        self.assertTrue(s["ok"])

    def test_warmup_warning_below_165_bars(self):
        s = self._patched_signal(_uptrend(80))
        self.assertTrue(s["ok"])
        self.assertFalse(s["warmup_ok"])
        self.assertIn("not fully seeded", s["note"])

    def test_warmup_warning_at_120_bars(self):
        """120 bars was enough for a plain EMA(55) but not for TEMA's triple
        cascade, which carries seed bias ~3x longer."""
        s = self._patched_signal(_uptrend(120))
        self.assertTrue(s["ok"])
        self.assertFalse(s["warmup_ok"])

    def test_warmup_ok_at_165_bars(self):
        s = self._patched_signal(_uptrend(165))
        self.assertTrue(s["ok"])
        self.assertTrue(s["warmup_ok"])
        self.assertEqual(s["note"], "")

    def test_buy_signal_from_uptrend(self):
        s = self._patched_signal(_uptrend(120))
        self.assertTrue(s["ok"])
        self.assertEqual(s["state"], "BUY")

    def test_sell_signal_from_downtrend(self):
        s = self._patched_signal(_downtrend(120))
        self.assertTrue(s["ok"])
        self.assertEqual(s["state"], "SELL")

    def test_neutral_signal_from_flat(self):
        s = self._patched_signal(_flat(120))
        self.assertTrue(s["ok"])
        self.assertEqual(s["state"], "NEUTRAL")

    def test_returns_last_close(self):
        closes = _uptrend(120)
        s = self._patched_signal(closes)
        self.assertAlmostEqual(s["last_close"], closes[-1], places=2)

    def test_bars_count_matches_input(self):
        s = self._patched_signal(_uptrend(95))
        self.assertEqual(s["bars"], 95)

    def test_local_csv_priority_over_remote(self):
        """_read_local_csv result is used instead of Yahoo when present."""
        local_closes = _flat(120, price=42.0)
        remote_closes = _flat(120, price=99.0)
        with patch.object(sig_module, "_read_local_csv",
                          return_value=local_closes) as mock_local, \
             patch.object(sig_module, "_fetch_yahoo",
                          return_value=remote_closes) as mock_remote:
            s = sig_module.signal_for("SYM")
        self.assertAlmostEqual(s["last_close"], 42.0, places=2)
        mock_remote.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Stop-loss alert detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopLossAlerts(TmpDirMixin):

    def _make_log(self, symbol, entry, shares=1.0):
        return {
            "open_positions": [self._open_pos(symbol, entry, shares)],
            "_state": {}
        }

    def _run(self, symbol, entry, last_close, stop_loss_pct=0.10):
        self.strategy["risk_management"]["stop_loss_pct"] = stop_loss_pct
        self._flush_strategy()
        log = self._make_log(symbol, entry)
        sig_ret = {"ok": True, "last_close": last_close}
        with patch.object(agent_module.signals, "signal_for",
                          return_value=sig_ret):
            return agent_module.check_stop_loss_alerts(log)

    # ── boundary tests ──────────────────────────────────────────────────────

    def test_no_trigger_below_threshold(self):
        """9.9% loss → NOT triggered."""
        alerts = self._run("TEST", 100.0, 90.1)
        self.assertEqual(alerts, [])

    def test_trigger_exactly_at_threshold(self):
        """10.0% loss → triggered (condition is <=)."""
        alerts = self._run("TEST", 100.0, 90.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "TEST")

    def test_trigger_above_threshold(self):
        """10.1% loss → triggered."""
        alerts = self._run("TEST", 100.0, 89.9)
        self.assertEqual(len(alerts), 1)

    def test_custom_stop_loss_pct(self):
        """5% custom threshold — triggers at -5.0%, not -4.9%."""
        no_trigger = self._run("TEST", 100.0, 95.1, stop_loss_pct=0.05)
        trigger    = self._run("TEST", 100.0, 95.0, stop_loss_pct=0.05)
        self.assertEqual(no_trigger, [])
        self.assertEqual(len(trigger), 1)

    # ── skip conditions ─────────────────────────────────────────────────────

    def test_skip_zero_entry_price(self):
        log = {"open_positions": [
            self._open_pos("TEST", entry=0.0)], "_state": {}}
        with patch.object(agent_module.signals, "signal_for",
                          return_value={"ok": True, "last_close": 0.0}):
            alerts = agent_module.check_stop_loss_alerts(log)
        self.assertEqual(alerts, [])

    def test_skip_signal_not_ok(self):
        log = self._make_log("TEST", 100.0)
        with patch.object(agent_module.signals, "signal_for",
                          return_value={"ok": False}):
            alerts = agent_module.check_stop_loss_alerts(log)
        self.assertEqual(alerts, [])

    def test_skip_zero_last_price(self):
        alerts = self._run("TEST", 100.0, 0.0)
        self.assertEqual(alerts, [])

    def test_no_positions_no_alerts(self):
        log = {"open_positions": [], "_state": {}}
        alerts = agent_module.check_stop_loss_alerts(log)
        self.assertEqual(alerts, [])

    # ── alert fields ────────────────────────────────────────────────────────

    def test_alert_fields_correct(self):
        alerts = self._run("MSFT", 200.0, 170.0)  # -15%
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["symbol"], "MSFT")
        self.assertAlmostEqual(a["entry_price"], 200.0)
        self.assertAlmostEqual(a["last_price"], 170.0)
        self.assertAlmostEqual(a["loss_pct"], -15.0, places=1)
        self.assertAlmostEqual(a["threshold_price"], 180.0, places=2)

    def test_multiple_positions_partial_trigger(self):
        """Two positions: one triggers, one doesn't."""
        log = {
            "open_positions": [
                self._open_pos("A", 100.0, pos_id="T0001"),
                self._open_pos("B", 100.0, pos_id="T0002"),
            ],
            "_state": {}
        }
        def side_effect(sym):
            return {"ok": True, "last_close": 88.0 if sym == "A" else 95.0}
        with patch.object(agent_module.signals, "signal_for",
                          side_effect=side_effect):
            alerts = agent_module.check_stop_loss_alerts(log)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "A")


class TestTrailingStop(TmpDirMixin):
    """Trailing-stop ('let winners run') exit: peak tracking, giveback trigger,
    correct reason tagging, and the exit_on_ribbon_sell gate."""

    def _pos(self, symbol="TEST", entry=100.0, peak=None, shares=1.0, pos_id="T0001"):
        p = self._open_pos(symbol, entry, shares, pos_id)
        if peak is not None:
            p["peak_price"] = peak
        return p

    def _enable(self, trail=0.25, exit_on_ribbon=False):
        self.strategy["risk_management"]["trailing_stop_pct"] = trail
        self.strategy["risk_management"]["exit_on_ribbon_sell"] = exit_on_ribbon
        self._flush_strategy()

    # ── update_position_peaks ────────────────────────────────────────────────
    def test_peak_seeds_from_entry_and_ignores_lower(self):
        log = {"open_positions": [self._pos(entry=100.0)], "_state": {}}
        agent_module.update_position_peaks(log, {"TEST": 95.0})  # below entry
        self.assertEqual(log["open_positions"][0]["peak_price"], 100.0)

    def test_peak_climbs_on_new_high(self):
        log = {"open_positions": [self._pos(entry=100.0, peak=100.0)], "_state": {}}
        changed = agent_module.update_position_peaks(log, {"TEST": 130.0})
        self.assertTrue(changed)
        self.assertEqual(log["open_positions"][0]["peak_price"], 130.0)

    def test_peak_holds_on_pullback(self):
        log = {"open_positions": [self._pos(entry=100.0, peak=130.0)], "_state": {}}
        changed = agent_module.update_position_peaks(log, {"TEST": 120.0})
        self.assertFalse(changed)
        self.assertEqual(log["open_positions"][0]["peak_price"], 130.0)

    def test_peak_ignores_missing_price(self):
        log = {"open_positions": [self._pos(entry=100.0, peak=130.0)], "_state": {}}
        agent_module.update_position_peaks(log, {})
        self.assertEqual(log["open_positions"][0]["peak_price"], 130.0)

    # ── check_trailing_stop_alerts ───────────────────────────────────────────
    def test_inert_when_pct_absent(self):
        # fixture strategy has no trailing_stop_pct → trailing disabled entirely
        log = {"open_positions": [self._pos(entry=100.0, peak=200.0)], "_state": {}}
        self.assertEqual(
            agent_module.check_trailing_stop_alerts(log, prices={"TEST": 100.0}), [])

    def test_trigger_at_threshold(self):
        self._enable(0.25)
        log = {"open_positions": [self._pos(entry=100.0, peak=200.0)], "_state": {}}
        none = agent_module.check_trailing_stop_alerts(log, prices={"TEST": 150.5})
        hit = agent_module.check_trailing_stop_alerts(log, prices={"TEST": 150.0})
        self.assertEqual(none, [])
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["symbol"], "TEST")
        self.assertAlmostEqual(hit[0]["peak_price"], 200.0)
        self.assertAlmostEqual(hit[0]["threshold_price"], 150.0, places=2)
        self.assertAlmostEqual(hit[0]["giveback_pct"], 25.0, places=1)

    def test_measured_off_peak_not_entry(self):
        # -20% from entry but -60% off the 200 peak → trailing fires on the peak
        self._enable(0.25)
        log = {"open_positions": [self._pos(entry=100.0, peak=200.0)], "_state": {}}
        hit = agent_module.check_trailing_stop_alerts(log, prices={"TEST": 80.0})
        self.assertEqual(len(hit), 1)

    def test_peak_defaults_to_entry_when_missing(self):
        self._enable(0.25)
        log = {"open_positions": [self._pos(entry=100.0)], "_state": {}}  # no peak stored
        hit = agent_module.check_trailing_stop_alerts(log, prices={"TEST": 74.0})
        self.assertEqual(len(hit), 1)

    # ── reason must NOT be mis-tagged as a hard stop-loss ────────────────────
    def test_trailing_close_not_tagged_stop_loss(self):
        import copy
        log = copy.deepcopy(self._DEFAULT_LOG)
        log["open_positions"] = [self._pos(symbol="TEST", entry=100.0)]
        with patch.object(agent_module, "run_post_trade_pipeline"):
            agent_module.process_cycle_state(
                log, actions=[], broker_positions=[],
                exit_info={"TEST": {"reason": "trailing_stop", "price": 150.0}})
        self.assertEqual(len(log["trades"]), 1)
        self.assertFalse(log["trades"][0]["stop_loss"])     # NOT a stop-loss
        self.assertEqual(log["trades"][0]["outcome"], "WIN")  # 150 > 100 entry

    # ── should_skip_model_call wiring ────────────────────────────────────────
    def test_trailing_forces_non_skip(self):
        self._enable(0.25)
        log = {"open_positions": [self._pos(entry=100.0, peak=200.0)], "_state": {}}
        raw = {"TEST": {"ok": True, "state": "BUY", "transition": "NO_ACTION",
                        "last_close": 150.0}}
        with patch.object(agent_module, "check_stop_loss_alerts", return_value=[]):
            skip, reason = agent_module.should_skip_model_call(raw, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "trailing_stop_alert")

    def test_ribbon_sell_wake_gated_by_flag(self):
        recent = agent_module.now_iso()
        raw = {"CLOV": {"ok": True, "state": "SELL", "transition": "NO_ACTION",
                        "last_close": 100.0}}

        def mk():
            return {"open_positions": [self._pos("CLOV", entry=100.0, peak=100.0)],
                    "_state": {"last_execution_call_ts": recent}}
        with patch.object(agent_module, "check_stop_loss_alerts", return_value=[]), \
             patch.object(agent_module, "weekend_pick_symbols", return_value=[]):
            self._enable(0.25, exit_on_ribbon=True)
            skip_true, reason_true = agent_module.should_skip_model_call(raw, mk())
            self._enable(0.25, exit_on_ribbon=False)
            skip_false, _ = agent_module.should_skip_model_call(raw, mk())
        self.assertFalse(skip_true)
        self.assertEqual(reason_true, "ema_sell_held:CLOV")
        self.assertTrue(skip_false)  # ribbon SELL is advisory now → no wake

    # ── _format_ema_sell_block respects the flag ─────────────────────────────
    def _sell_sig(self):
        return {"ok": True, "state": "SELL", "last_close": 90.0,
                "lines": {"red": 95, "blue": 90, "green": 91, "yellow": 92}}

    def test_ema_sell_block_advisory_when_flag_false(self):
        self._enable(0.25, exit_on_ribbon=False)
        log = {"open_positions": [self._pos("CLOV", entry=100.0)]}
        block = agent_module._format_ema_sell_block({"CLOV": self._sell_sig()}, log)
        self.assertIn("ADVISORY", block)
        self.assertIn("thesis_break", block)
        self.assertNotIn("reason='ema_exit'", block)

    def test_ema_sell_block_forced_when_flag_true(self):
        self._enable(0.25, exit_on_ribbon=True)
        log = {"open_positions": [self._pos("CLOV", entry=100.0)]}
        block = agent_module._format_ema_sell_block({"CLOV": self._sell_sig()}, log)
        self.assertIn("reason='ema_exit'", block)

    # ── record_open_position seeds the high-water mark ───────────────────────
    def test_record_open_seeds_peak(self):
        import copy
        log = copy.deepcopy(self._DEFAULT_LOG)
        agent_module.record_open_position(
            log, {"symbol": "NVDA", "price": 120.0, "shares": 1})
        self.assertEqual(log["open_positions"][0]["peak_price"], 120.0)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5b — Risk model (Phase A): sizing, leverage stop, factor exposure
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskModel(TmpDirMixin):

    def _strat(self):
        self._flush_strategy()
        return agent_module.load_strategy()

    # ── leverage_factor / effective_stop_pct ────────────────────────────────
    def test_leverage_factor(self):
        s = self._strat()
        self.assertEqual(agent_module.leverage_factor("SOXL", s), 3.0)
        self.assertEqual(agent_module.leverage_factor("soxl", s), 3.0)  # case-insensitive
        self.assertEqual(agent_module.leverage_factor("AAPL", s), 1.0)  # unlisted -> 1

    def test_effective_stop_leveraged_is_tighter(self):
        s = self._strat()
        self.assertAlmostEqual(agent_module.effective_stop_pct("SOXL", s), 0.0333, places=4)
        self.assertAlmostEqual(agent_module.effective_stop_pct("AMD", s), 0.10, places=4)

    def test_effective_stop_disabled_falls_back_to_base(self):
        self.strategy["risk_management"]["leverage_adjusted_stop"] = False
        s = self._strat()
        self.assertAlmostEqual(agent_module.effective_stop_pct("SOXL", s), 0.10, places=4)

    # ── position_size_pct ───────────────────────────────────────────────────
    def test_size_normal_capped_by_risk_budget(self):
        """conf 95 normal name -> 20% (risk/stop budget), NOT the 30% band."""
        s = self._strat()
        self.assertAlmostEqual(agent_module.position_size_pct(95, "NVDA", s), 0.20, places=4)

    def test_size_band_floor(self):
        s = self._strat()
        self.assertAlmostEqual(agent_module.position_size_pct(65, "AMD", s), 0.15, places=4)

    def test_size_below_confidence_floor_is_zero(self):
        s = self._strat()
        self.assertEqual(agent_module.position_size_pct(55, "AMD", s), 0.0)

    def test_size_leveraged_haircut(self):
        """SOXL (3x) at conf 78 -> 20% / 3 = 6.67%."""
        s = self._strat()
        self.assertAlmostEqual(agent_module.position_size_pct(78, "SOXL", s), 0.0667, places=3)

    def test_size_missing_position_sizing_returns_band(self):
        """Old strategy without position_sizing -> falls back to the band ceiling."""
        strat = {"risk_management": {"stop_loss_pct": 0.10}}
        self.assertAlmostEqual(agent_module.position_size_pct(80, "AMD", strat), 0.20, places=4)

    # ── sector_for / sector_exposure ────────────────────────────────────────
    def test_sector_for(self):
        s = self._strat()
        self.assertEqual(agent_module.sector_for("AMD", s), "semis_ai_hardware")
        self.assertEqual(agent_module.sector_for("HOOD", s), "fintech_crypto")
        self.assertEqual(agent_module.sector_for("ZZZZ", s), "other")

    def test_sector_exposure_counts_leverage(self):
        """A 3x ETF eats 3x its dollar weight of the sector cap."""
        s = self._strat()
        # $10 SOXL (3x) + $20 AMD = $30 book; semis weighted = 10*3 + 20 = 50; 50/30 = 1.667
        book = [{"symbol": "SOXL", "market_value": 10.0},
                {"symbol": "AMD", "market_value": 20.0}]
        exp = agent_module.sector_exposure(book, s)
        self.assertEqual(exp["_total_equity"], 30.0)
        self.assertAlmostEqual(exp["semis_ai_hardware"]["pct"], 1.6667, places=3)
        self.assertEqual(exp["semis_ai_hardware"]["count"], 2)

    def test_sector_exposure_empty(self):
        s = self._strat()
        exp = agent_module.sector_exposure([], s)
        self.assertEqual(exp["_total_equity"], 0.0)

    def test_sector_exposure_total_equity_denominator(self):
        """With an explicit account total (incl. cash), weight is % of the ACCOUNT,
        not % of invested capital — else two positions always read ~100%/sector."""
        s = self._strat()
        book = [{"symbol": "AMD", "market_value": 20.0}]
        exp = agent_module.sector_exposure(book, s, total_equity=100.0)
        self.assertEqual(exp["_total_equity"], 100.0)
        self.assertAlmostEqual(exp["semis_ai_hardware"]["pct"], 0.20, places=4)

    # ── leveraged_sleeve_exposure (#3) ───────────────────────────────────────
    def test_leveraged_sleeve_exposure(self):
        """Sleeve % is NOTIONAL (not leverage-adjusted): $25 SOXL of a $100 account = 25%."""
        s = self._strat()
        book = [{"symbol": "SOXL", "market_value": 25.0},
                {"symbol": "AMD", "market_value": 75.0}]
        sl = agent_module.leveraged_sleeve_exposure(book, s, total_equity=100.0)
        self.assertAlmostEqual(sl["pct"], 0.25, places=4)
        self.assertAlmostEqual(sl["notional"], 25.0)
        self.assertEqual(sl["names"], ["SOXL"])

    def test_leveraged_sleeve_exposure_none(self):
        s = self._strat()
        sl = agent_module.leveraged_sleeve_exposure(
            [{"symbol": "AMD", "market_value": 50.0}], s, total_equity=100.0)
        self.assertEqual(sl["pct"], 0.0)
        self.assertEqual(sl["names"], [])

    # ── risk block surfaces the #2/#3 caps only when configured ──────────────
    def test_risk_block_shows_sleeve_and_concentration(self):
        self.strategy["factor_exposure_limits"]["leveraged_sleeve_max_pct"] = 0.25
        self.strategy["position_sizing"]["max_concurrent_positions"] = 5
        s = self._strat()
        log = {"open_positions": [self._open_pos("SOXL", 100.0, shares=0.25)],  # $25 mv
               "summary": {"current_value": 100.0}}
        raw = {"SOXL": {"ok": True, "state": "BUY", "last_close": 100.0}}
        block = agent_module._format_risk_block(raw, log, s)
        self.assertIn("LEVERAGED SLEEVE CAP = 25%", block)
        self.assertIn("AT/OVER CAP", block)            # 25% notional hits the 25% cap
        self.assertIn("CONCENTRATION", block)
        self.assertIn("at most 5 names", block)

    def test_risk_block_omits_caps_when_unset(self):
        s = self._strat()  # fixture has neither key
        log = {"open_positions": [self._open_pos("AMD", 100.0, shares=0.2)],
               "summary": {"current_value": 100.0}}
        raw = {"AMD": {"ok": True, "state": "BUY", "last_close": 100.0}}
        block = agent_module._format_risk_block(raw, log, s)
        self.assertNotIn("LEVERAGED SLEEVE CAP", block)
        self.assertNotIn("CONCENTRATION", block)

    # ── check_stop_loss_alerts integration: leveraged stop is honored ────────
    def test_stop_loss_honors_leveraged_stop(self):
        self._flush_strategy()

        def run(sym, last):
            log = {"open_positions": [self._open_pos(sym, 100.0)], "_state": {}}
            with patch.object(agent_module.signals, "signal_for",
                              return_value={"ok": True, "last_close": last}):
                return agent_module.check_stop_loss_alerts(log)

        self.assertEqual(len(run("SOXL", 96.0)), 1)  # -4% past the 3.3% leveraged stop
        self.assertEqual(run("SOXL", 98.0), [])       # -2% inside the leveraged stop
        self.assertEqual(run("AMD", 96.0), [])         # -4% inside the 10% base stop

    # ── weekend_pick_confidences ────────────────────────────────────────────
    def test_weekend_pick_confidences_parses(self):
        path = os.path.join(self.tmpdir, "research", "weekend_picks_2026-06-22.md")
        with open(path, "w") as f:
            f.write("### #1 — AMAT (Applied Materials) | Confidence: 70/100\nbody\n"
                    "### #2 — NVDA (NVIDIA) | Confidence: 72/100\n")
        self.assertEqual(agent_module.weekend_pick_confidences(),
                         {"AMAT": 70, "NVDA": 72})

    # ── _format_risk_block ──────────────────────────────────────────────────
    def test_risk_block_has_sizes_and_sectors(self):
        s = self._strat()
        log = {"open_positions": [self._open_pos("AMD", 100.0, shares=0.2)],  # conf 75, $20 mv
               "summary": {"current_value": 100.0}}
        raw = {"AMD": {"ok": True, "state": "BUY", "last_close": 100.0}}
        block = agent_module._format_risk_block(raw, log, s)
        self.assertIn("RISK MODEL", block)
        self.assertIn("semis_ai_hardware", block)
        self.assertIn("20%", block)            # $20 AMD / $100 account, not 100%
        self.assertIn("size <= 20.0%", block)  # held AMD conf 75 -> 20% band cap

    def test_risk_block_empty_without_position_sizing(self):
        strat = {"risk_management": {"stop_loss_pct": 0.10}}
        block = agent_module._format_risk_block({}, {"open_positions": []}, strat)
        self.assertEqual(block, "")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5c — Phase B options shadow (paper) engine
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsShadow(unittest.TestCase):
    CFG = {
        "shadow_mode": True,
        "risk": {"max_loss_per_trade_pct": 0.02, "options_sleeve_max_pct": 0.15},
        "selection": {"structure_v1": "long_call", "target_dte_min": 30,
                      "target_dte_max": 45, "max_bid_ask_pct_of_mid": 0.10},
        "shadow_exit": {"underlying_sell_state": True, "premium_stop_pct": 0.50,
                        "min_dte_close": 7},
    }
    TODAY = date(2026, 6, 22)

    def _contract(self, **kw):
        c = {"type": "call", "strike": 100.0, "expiry": "2026-07-24",  # 32 DTE
             "bid": 4.8, "ask": 5.0}
        c.update(kw)
        return c

    def test_dte(self):
        self.assertEqual(osh.dte("2026-07-24", self.TODAY), 32)
        self.assertIsNone(osh.dte("garbage", self.TODAY))

    def test_validate_ok(self):
        ok, reason = osh.validate_contract(self._contract(), 100.0, self.CFG, self.TODAY)
        self.assertTrue(ok, reason)

    def test_validate_illiquid_spread_rejected(self):
        c = self._contract(bid=2.0, ask=4.0)  # spread = 66% of mid
        ok, reason = osh.validate_contract(c, 100.0, self.CFG, self.TODAY)
        self.assertFalse(ok)
        self.assertIn("illiquid", reason)

    def test_validate_dte_window(self):
        c = self._contract(expiry="2026-06-29")  # 7 DTE < 30
        ok, reason = osh.validate_contract(c, 100.0, self.CFG, self.TODAY)
        self.assertFalse(ok)
        self.assertIn("dte_out_of_window", reason)

    def test_validate_not_atm(self):
        c = self._contract(strike=130.0)  # 30% from spot
        ok, reason = osh.validate_contract(c, 100.0, self.CFG, self.TODAY)
        self.assertFalse(ok)
        self.assertIn("not_atm", reason)

    def test_validate_bad_quote(self):
        ok, reason = osh.validate_contract(self._contract(bid=0, ask=0), 100.0, self.CFG, self.TODAY)
        self.assertFalse(ok)
        self.assertIn("bad_quote", reason)

    def test_entry_premium_is_ask(self):
        self.assertEqual(osh.entry_premium(self._contract(ask=5.0)), 5.0)

    def test_risk_oversized_on_tiny_account(self):
        # $5 ask -> $500 max loss vs 2% of $104 = $2.08 budget -> oversized (the point)
        ra = osh.risk_assessment(5.0, 104.0, self.CFG)
        self.assertTrue(ra["oversized"])
        self.assertEqual(ra["max_loss_usd"], 500.0)

    def test_should_close_underlying_sell(self):
        sh = {"entry_premium": 5.0, "expiry": "2026-07-24"}
        close, reason = osh.should_close(sh, "SELL", 5.2, self.CFG, self.TODAY)
        self.assertTrue(close)
        self.assertEqual(reason, "underlying_sell")

    def test_should_close_premium_stop(self):
        sh = {"entry_premium": 5.0, "expiry": "2026-07-24"}
        close, reason = osh.should_close(sh, "BUY", 2.4, self.CFG, self.TODAY)  # -52%
        self.assertTrue(close)
        self.assertEqual(reason, "premium_stop")

    def test_should_close_dte(self):
        sh = {"entry_premium": 5.0, "expiry": "2026-06-26"}  # 4 DTE < 7
        close, reason = osh.should_close(sh, "BUY", 5.0, self.CFG, self.TODAY)
        self.assertTrue(close)
        self.assertEqual(reason, "dte_expiry")

    def test_should_close_hold(self):
        sh = {"entry_premium": 5.0, "expiry": "2026-07-24"}
        close, _ = osh.should_close(sh, "BUY", 5.0, self.CFG, self.TODAY)
        self.assertFalse(close)

    def test_close_pnl_win(self):
        r = osh.close_pnl({"entry_premium": 5.0}, 7.5)
        self.assertEqual(r["pnl_per_contract_usd"], 250.0)
        self.assertEqual(r["pnl_pct_on_premium"], 50.0)

    def test_open_close_lifecycle_and_summary(self):
        log = {"positions": [], "_next_id": 1}
        rec = osh.open_shadow_record(
            log, underlying="AMD", contract=self._contract(), underlying_price=100.0,
            account_value=104.0, cfg=self.CFG, confidence=75, thesis="t",
            now_iso="2026-06-22T10:00")
        self.assertEqual(rec["id"], "S0001")
        self.assertTrue(rec["oversized_for_account"])
        self.assertTrue(osh.has_open_shadow(log, "AMD"))
        osh.close_shadow_record(log, "S0001", 7.5, "underlying_sell", now_iso="2026-06-25T10:00")
        s = osh.shadow_summary(log)
        self.assertEqual((s["closed"], s["wins"]), (1, 1))
        self.assertEqual(s["total_pnl_per_contract_usd"], 250.0)
        self.assertFalse(osh.has_open_shadow(log, "AMD"))

    def test_log_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "shadow.json")
        log = osh.load_shadow_log(path)  # fresh default
        osh.open_shadow_record(log, underlying="NVDA", contract=self._contract(),
                               underlying_price=100.0, account_value=104.0, cfg=self.CFG)
        osh.save_shadow_log(path, log)
        reloaded = osh.load_shadow_log(path)
        self.assertEqual(len(reloaded["positions"]), 1)
        self.assertIn("summary", reloaded)

    def test_shadow_enabled(self):
        self.assertTrue(osh.shadow_enabled({"options": {"shadow_mode": True}}))
        self.assertFalse(osh.shadow_enabled({"options": {"shadow_mode": False}}))
        self.assertFalse(osh.shadow_enabled({}))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5d — Phase B+ momentum screener (the operator's real edge)
# ═══════════════════════════════════════════════════════════════════════════════

import momentum_screen as msc


def _leader_closes():
    """Synthetic multi-timeframe momentum leader: a 6mo grind (~+40%) then a hot
    final month (~+35%, last week ~+8%). Qualifies under the default gate."""
    closes = [5.0] * 8
    closes += [5.0 * (1 + 0.0028) ** i for i in range(1, 127)]   # ~6mo grind
    base = closes[-1]
    closes += [base * (1 + 0.0145) ** i for i in range(1, 22)]   # hot final month
    return closes


def _grinder_closes():
    """Steady +0.3%/bar — strong 1y but only ~+6.5%/month, fails the 1m anchor."""
    return [10.0 * (1 + 0.003) ** i for i in range(260)]


class TestMomentumScreen(unittest.TestCase):
    CFG = {
        "targets": {"1w": 0.15, "1m": 0.30, "6m": 0.50, "1y": 1.00},
        "weights": {"1w": 0.20, "1m": 0.35, "6m": 0.25, "1y": 0.20},
        "anchor_timeframe": "1m", "min_score": 55,
        "require_all_positive": True, "positive_timeframes": ["1w", "1m", "6m"],
        "max_underlying_price": 30.0,
    }

    def test_period_return_basic(self):
        closes = [10.0] * 30 + [13.0]  # +30% over 1 bar back is wrong; use lookback
        self.assertAlmostEqual(msc.period_return([10, 11, 12, 13], 3), 0.30, places=6)

    def test_period_return_too_short(self):
        self.assertIsNone(msc.period_return([10, 11], 5))

    def test_period_return_bad_price(self):
        self.assertIsNone(msc.period_return([0.0, 0.0, 5.0], 2))

    def test_multi_timeframe_returns_keys(self):
        r = msc.multi_timeframe_returns(_leader_closes())
        self.assertEqual(set(r), {"1w", "1m", "6m", "1y"})
        self.assertGreater(r["1m"], 0.30)          # hot month
        self.assertIsNone(r["1y"])                 # <252 bars -> unknown, not 0

    def test_score_strong_beats_weak(self):
        strong = msc.momentum_score(msc.multi_timeframe_returns(_leader_closes()))
        weak = msc.momentum_score(msc.multi_timeframe_returns(_grinder_closes()))
        self.assertGreater(strong, weak)
        self.assertGreater(strong, 55)

    def test_score_renormalizes_on_missing_timeframe(self):
        # only the 1m present -> score still computed on that weight alone
        r = {"1w": None, "1m": 0.30, "6m": None, "1y": None}
        self.assertGreater(msc.momentum_score(r), 0)

    def test_gate_accepts_leader(self):
        ok, reason = msc.passes_gate(msc.multi_timeframe_returns(_leader_closes()), self.CFG)
        self.assertTrue(ok, reason)

    def test_gate_rejects_anchor_below_target(self):
        ok, reason = msc.passes_gate(msc.multi_timeframe_returns(_grinder_closes()), self.CFG)
        self.assertFalse(ok)
        self.assertIn("anchor_below_target", reason)

    def test_gate_rejects_negative_timeframe(self):
        r = {"1w": -0.05, "1m": 0.40, "6m": 0.60, "1y": 1.2}  # anchor ok but 1w red
        ok, reason = msc.passes_gate(r, self.CFG)
        self.assertFalse(ok)
        self.assertIn("negative_timeframe", reason)

    def test_affordable_gate(self):
        ok, _ = msc.affordable_underlying(12.0, self.CFG)
        self.assertTrue(ok)
        ok2, reason = msc.affordable_underlying(500.0, self.CFG)
        self.assertFalse(ok2)
        self.assertIn("too_pricey", reason)

    def test_evaluate_qualified_leader(self):
        closes = _leader_closes()
        ev = msc.evaluate("WIN", closes, closes[-1], self.CFG)
        self.assertTrue(ev["qualified"], ev["reason"])
        self.assertEqual(ev["reason"], "ok")

    def test_evaluate_pricey_leader_rejected(self):
        closes = _leader_closes()
        ev = msc.evaluate("WIN", closes, 500.0, self.CFG)  # qualifies on momentum, too pricey
        self.assertFalse(ev["qualified"])
        self.assertIn("too_pricey", ev["reason"])

    def test_screen_ranks_and_filters(self):
        cands = {
            "WIN": {"closes": _leader_closes(), "last_price": _leader_closes()[-1]},
            "GRIND": {"closes": _grinder_closes(), "last_price": _grinder_closes()[-1]},
        }
        evals = msc.screen(cands, self.CFG)
        self.assertEqual(evals[0]["symbol"], "WIN")          # highest score first
        quals = msc.qualified(evals)
        self.assertEqual([e["symbol"] for e in quals], ["WIN"])


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5e — Phase B+ momentum→shadow agent wiring
# ═══════════════════════════════════════════════════════════════════════════════

class TestMomentumShadowWiring(TmpDirMixin):
    def _strategy_with_momentum(self, **overrides):
        mom = {
            "enabled": True,
            "targets": {"1w": 0.15, "1m": 0.30, "6m": 0.50, "1y": 1.00},
            "weights": {"1w": 0.20, "1m": 0.35, "6m": 0.25, "1y": 0.20},
            "anchor_timeframe": "1m", "min_score": 55,
            "require_all_positive": True, "positive_timeframes": ["1w", "1m", "6m"],
            "max_underlying_price": 30.0, "max_contract_cost_usd": 50.0,
            "top_n_open": 3, "min_catalyst_confidence": 60,
            "scan_interval_hours": 24, "universe": ["WIN", "GRIND"],
        }
        mom.update(overrides)
        self.strategy["options"] = {
            "shadow_mode": True, "enabled": False,
            "max_open_shadows": 5, "max_opens_per_cycle": 1,
            "risk": {"max_loss_per_trade_pct": 0.02, "options_sleeve_max_pct": 0.15},
            "selection": {"structure_v1": "long_call", "target_dte_min": 30,
                          "target_dte_max": 45, "max_bid_ask_pct_of_mid": 0.10},
            "shadow_exit": {"underlying_sell_state": True, "premium_stop_pct": 0.50,
                            "min_dte_close": 7},
            "momentum": mom,
        }
        self._flush_strategy()

    def _contract(self, ask=0.40, strike=10.0):
        # 30-45 DTE from 2026-06-22 -> 2026-07-24 is 32 DTE; tight spread (<10% of mid)
        return {"type": "call", "strike": strike, "expiry": "2026-07-24",
                "bid": round(ask - 0.02, 2), "ask": ask, "underlying_price": strike}

    def _shadow_log(self):
        path = os.path.join(self.tmpdir, "shadow", "options_shadow_log.json")
        with open(path) as f:
            return json.load(f)

    def test_disabled_returns_early(self):
        self._strategy_with_momentum(enabled=False)
        log = {"summary": {"current_value": 100.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True):
            agent_module.process_momentum_shadow(log)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, "shadow", "options_shadow_log.json")))

    def test_time_gate_suppresses_rescan(self):
        self._strategy_with_momentum()
        log = {"summary": {"current_value": 100.0},
               "_state": {"last_momentum_scan_ts": agent_module.now_iso()}}
        called = []
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch",
                          side_effect=lambda: called.append("watch") or {}):
            agent_module.process_momentum_shadow(log)
        self.assertEqual(called, [])  # never reached the research read

    def test_no_watch_names_opens_nothing(self):
        # The reuse-research design has NO paid catalyst fallback: empty watch -> nothing.
        self._strategy_with_momentum()
        log = {"summary": {"current_value": 1000.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch", return_value={}), \
             patch.object(agent_module, "fetch_daily_closes") as fc, \
             patch.object(agent_module, "select_shadow_contract") as sel:
            agent_module.process_momentum_shadow(log)
        fc.assert_not_called()
        sel.assert_not_called()
        self.assertIn("last_momentum_scan_ts", log["_state"])  # still stamped

    def test_full_flow_opens_momentum_shadow(self):
        self._strategy_with_momentum()
        leader = _leader_closes()

        def fake_closes(sym):
            return leader if sym == "WIN" else _grinder_closes()

        log = {"summary": {"current_value": 1000.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch",
                          return_value={"WIN": {"confidence": 82, "catalyst": "earnings beat+raised"},
                                        "GRIND": {"confidence": 70, "catalyst": "steady"}}), \
             patch.object(agent_module, "fetch_daily_closes", side_effect=fake_closes), \
             patch.object(agent_module, "select_shadow_contract",
                          return_value=self._contract(ask=0.40, strike=round(leader[-1]))):
            agent_module.process_momentum_shadow(log)

        slog = self._shadow_log()
        opened = [p for p in slog["positions"] if p["underlying"] == "WIN"]
        self.assertEqual(len(opened), 1)
        rec = opened[0]
        self.assertEqual(rec["signal_source"], "momentum_research")
        self.assertEqual(rec["research_confidence"], 82)
        self.assertIn("earnings", rec["catalyst"])
        self.assertIn("momentum_score", rec)
        # GRIND is research-vetted but fails the momentum screen -> no shadow
        self.assertFalse(any(p["underlying"] == "GRIND" for p in slog["positions"]))
        self.assertIn("last_momentum_scan_ts", log["_state"])

    def test_low_research_confidence_skipped(self):
        # A watch name below min_catalyst_confidence is never even screened.
        self._strategy_with_momentum(min_catalyst_confidence=60)
        leader = _leader_closes()
        log = {"summary": {"current_value": 1000.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch",
                          return_value={"WIN": {"confidence": 45, "catalyst": "weak meme"}}), \
             patch.object(agent_module, "fetch_daily_closes",
                          side_effect=lambda s: leader) as fc, \
             patch.object(agent_module, "select_shadow_contract") as sel:
            agent_module.process_momentum_shadow(log)
        fc.assert_not_called()   # below conf floor -> not screened
        sel.assert_not_called()

    def test_affordability_cost_gate_skips_expensive_contract(self):
        self._strategy_with_momentum(max_contract_cost_usd=50.0)
        leader = _leader_closes()
        log = {"summary": {"current_value": 1000.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch",
                          return_value={"WIN": {"confidence": 90, "catalyst": "big contract win"}}), \
             patch.object(agent_module, "fetch_daily_closes",
                          side_effect=lambda s: leader if s == "WIN" else []), \
             patch.object(agent_module, "select_shadow_contract",
                          return_value=self._contract(ask=1.20, strike=round(leader[-1]))):
            # ask 1.20 -> $120/contract > $50 cap
            agent_module.process_momentum_shadow(log)
        path = os.path.join(self.tmpdir, "shadow", "options_shadow_log.json")
        if os.path.exists(path):
            self.assertFalse(any(p["underlying"] == "WIN" for p in self._shadow_log()["positions"]))

    def test_process_momentum_shadow_makes_no_model_call(self):
        # The whole point of the reuse-research design: zero extra run_model calls.
        self._strategy_with_momentum()
        leader = _leader_closes()
        log = {"summary": {"current_value": 1000.0}, "_state": {}}
        with patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "momentum_options_watch",
                          return_value={"WIN": {"confidence": 80, "catalyst": "ok"}}), \
             patch.object(agent_module, "fetch_daily_closes", side_effect=lambda s: leader), \
             patch.object(agent_module, "select_shadow_contract",
                          return_value=self._contract(strike=round(leader[-1]))), \
             patch.object(agent_module, "run_model") as rm:
            agent_module.process_momentum_shadow(log)
        rm.assert_not_called()  # select_shadow_contract is mocked; no other model call

    def test_momentum_options_watch_parses_research_block(self):
        sample = (
            "### #1 — NVDA | Confidence: 80/100\nequity pick\n\n"
            "## MOMENTUM OPTIONS WATCH\n"
            "- CIFR | conf 88 | catalyst: AI-datacenter pivot, sector tailwind\n"
            "- AMC | confidence: 45 | catalyst: meme squeeze, no backing\n\n"
            "## DEPLOYMENT SUMMARY\nblah\n")
        with patch.object(agent_module, "load_latest_research_file", return_value=sample):
            w = agent_module.momentum_options_watch()
        self.assertEqual(set(w), {"CIFR", "AMC"})       # equity NVDA pick excluded
        self.assertEqual(w["CIFR"]["confidence"], 88)
        self.assertIn("datacenter", w["CIFR"]["catalyst"])

    def test_momentum_options_watch_missing_block(self):
        with patch.object(agent_module, "load_latest_research_file",
                          return_value="### #1 — NVDA | Confidence: 80/100\n"):
            self.assertEqual(agent_module.momentum_options_watch(), {})

    def test_run_shadow_passes_invokes_both(self):
        log = {"summary": {"current_value": 100.0}, "_state": {}}
        with patch.object(agent_module, "process_options_shadow") as opt, \
             patch.object(agent_module, "process_momentum_shadow") as mom:
            agent_module._run_shadow_passes({"SPY": {}}, log)
        opt.assert_called_once()
        mom.assert_called_once()

    def test_run_shadow_passes_swallows_failures(self):
        log = {"summary": {}, "_state": {}}
        with patch.object(agent_module, "process_options_shadow",
                          side_effect=RuntimeError("boom")), \
             patch.object(agent_module, "process_momentum_shadow",
                          side_effect=RuntimeError("boom2")):
            agent_module._run_shadow_passes({}, log)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Smart skip logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestSmartSkip(TmpDirMixin):

    def _skip(self, raw_sigs, open_positions=None, stop_alerts=None):
        log = {"open_positions": open_positions or []}
        if stop_alerts is None:
            stop_alerts = []
        with patch.object(agent_module, "check_stop_loss_alerts",
                          return_value=stop_alerts):
            return agent_module.should_skip_model_call(raw_sigs, log)

    def _sig(self, transition, ok=True):
        return {"ok": ok, "transition": transition}

    def test_all_neutral_skip(self):
        raw = {"SPY": self._sig("NO_ACTION")}
        skip, reason = self._skip(raw)
        self.assertTrue(skip)
        self.assertEqual(reason, "all_neutral_no_action")

    def test_enter_long_no_skip(self):
        raw = {"SPY": self._sig("ENTER_LONG")}
        skip, _ = self._skip(raw)
        self.assertFalse(skip)

    def test_exit_held_position_no_skip(self):
        raw = {"AAPL": self._sig("EXIT")}
        positions = [self._open_pos("AAPL")]
        skip, reason = self._skip(raw, open_positions=positions)
        self.assertFalse(skip)
        self.assertEqual(reason, "exit:AAPL")

    def test_exit_unheld_position_skip(self):
        """EXIT signal for a symbol we don't hold → still skip (nothing to sell)."""
        raw = {"AAPL": self._sig("EXIT"), "SPY": self._sig("NO_ACTION")}
        skip, _ = self._skip(raw, open_positions=[])
        self.assertTrue(skip)

    def test_sell_state_held_no_skip_even_without_exit_edge(self):
        """A held position sitting in SELL state must wake the model even when
        the cross happened on an earlier bar (transition NO_ACTION) — e.g.
        after downtime or an indicator change. Regression for CLOV 2026-06-12."""
        raw = {"CLOV": {"ok": True, "transition": "NO_ACTION", "state": "SELL"}}
        positions = [self._open_pos("CLOV")]
        skip, reason = self._skip(raw, open_positions=positions)
        self.assertFalse(skip)
        self.assertEqual(reason, "ema_sell_held:CLOV")

    def test_sell_state_unheld_skips(self):
        """SELL state on a symbol we don't own → nothing to sell → skip."""
        raw = {"CLOV": {"ok": True, "transition": "NO_ACTION", "state": "SELL"},
               "SPY": self._sig("NO_ACTION")}
        skip, _ = self._skip(raw, open_positions=[])
        self.assertTrue(skip)

    def test_hold_signal_skip(self):
        """HOLD = already in BUY state, no new action needed → skip."""
        raw = {"SPY": self._sig("HOLD")}
        skip, _ = self._skip(raw)
        self.assertTrue(skip)

    def test_stop_loss_alert_no_skip(self):
        raw = {"SPY": self._sig("NO_ACTION")}
        alert = [{"symbol": "SPY"}]
        skip, reason = self._skip(raw, stop_alerts=alert)
        self.assertFalse(skip)
        self.assertEqual(reason, "stop_loss_alert")

    def test_unknown_signal_no_skip(self):
        """ok=False → cannot confirm safe → don't skip (be conservative)."""
        raw = {"SPY": self._sig("NO_ACTION", ok=False)}
        skip, reason = self._skip(raw)
        self.assertFalse(skip)
        self.assertIn("unknown_signal", reason)

    def test_mixed_signals_enter_long_wins(self):
        """One ENTER_LONG among many NEUTRAL → no skip."""
        raw = {
            "SPY":  self._sig("NO_ACTION"),
            "AAPL": self._sig("NO_ACTION"),
            "NVDA": self._sig("ENTER_LONG"),
        }
        skip, _ = self._skip(raw)
        self.assertFalse(skip)

    def test_signals_module_unavailable_no_skip(self):
        """If signals module is None, we can't confirm safety → no skip."""
        with patch.object(agent_module, "signals", None):
            skip, reason = agent_module.should_skip_model_call({}, {})
        self.assertFalse(skip)
        self.assertEqual(reason, "signals_unavailable")


class TestEmaSellBlock(TmpDirMixin):
    """_format_ema_sell_block: the SELL SIGNAL prompt alert for held positions."""

    def _sell_sig(self):
        return {"ok": True, "state": "SELL", "transition": "NO_ACTION",
                "last_close": 4.58,
                "lines": {"red": 4.83, "blue": 4.62, "green": 4.55, "yellow": 4.54}}

    def test_held_sell_state_produces_block(self):
        log = {"open_positions": [self._open_pos("CLOV")]}
        block = agent_module._format_ema_sell_block({"CLOV": self._sell_sig()}, log)
        self.assertIn("CLOV", block)
        self.assertIn("SELL SIGNAL ACTIVE", block)
        self.assertIn("reason='ema_exit'", block)
        self.assertIn("NO_ACTION does NOT cancel", block)

    def test_unheld_sell_state_no_block(self):
        block = agent_module._format_ema_sell_block(
            {"CLOV": self._sell_sig()}, {"open_positions": []})
        self.assertEqual(block, "")

    def test_held_buy_state_no_block(self):
        sig = dict(self._sell_sig(), state="BUY")
        log = {"open_positions": [self._open_pos("CLOV")]}
        block = agent_module._format_ema_sell_block({"CLOV": sig}, log)
        self.assertEqual(block, "")

    def test_not_ok_signal_no_block(self):
        """INSUFFICIENT_DATA / fetch errors must never fabricate a sell order."""
        sig = dict(self._sell_sig(), ok=False)
        log = {"open_positions": [self._open_pos("CLOV")]}
        block = agent_module._format_ema_sell_block({"CLOV": sig}, log)
        self.assertEqual(block, "")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6b — NEWS_CHECK_HOURS time gate + ENTER_LONG dedup (added 2026-06-10
# after a full day of 15-min cycles each triggered a model call)
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import timedelta


class TestTimeGateAndEnterLongDedup(TmpDirMixin):
    """The unified time-gate: open positions / pending buys only wake the model
    every NEWS_CHECK_HOURS; a repeated ENTER_LONG (partial-bar flicker) cannot
    re-wake it every cycle once the model has been shown that crossover."""

    def _sig(self, transition, state="BUY", ok=True):
        return {"ok": ok, "transition": transition, "state": state}

    def _skip(self, raw, log):
        with patch.object(agent_module, "check_stop_loss_alerts", return_value=[]):
            return agent_module.should_skip_model_call(raw, log)

    def _log(self, open_positions=None, last_call_ts=None, el_seen=None,
             last_exec_ts=None):
        state = {"last_positions": [], "next_id": 1}
        if last_call_ts is not None:
            state["last_model_call_ts"] = last_call_ts
        if last_exec_ts is not None:
            state["last_execution_call_ts"] = last_exec_ts
        if el_seen is not None:
            state["enter_long_seen"] = el_seen
        return {"open_positions": open_positions or [], "_state": state}

    def _ts(self, hours_ago, aware=False):
        now = datetime.now(agent_module.ET) if aware else datetime.now()
        return (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")

    # ---------------- forced news check gate ----------------
    def test_fresh_ts_with_open_positions_skips(self):
        log = self._log([self._open_pos("AAPL")], last_exec_ts=self._ts(0.2))
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertTrue(skip)
        self.assertEqual(reason, "all_neutral_no_action")

    def test_stale_ts_forces_news_check(self):
        log = self._log([self._open_pos("AAPL")], last_exec_ts=self._ts(5))
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "forced_news_check")

    def test_missing_ts_forces_news_check(self):
        log = self._log([self._open_pos("AAPL")])
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "forced_news_check")

    def test_timezone_aware_fresh_ts_still_skips(self):
        """Regression: an aware (offset-carrying) timestamp must not break the
        elapsed math and silently force a model call every cycle."""
        log = self._log([self._open_pos("AAPL")], last_exec_ts=self._ts(0.2, aware=True))
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertTrue(skip)

    def test_garbage_ts_forces_news_check(self):
        log = self._log([self._open_pos("AAPL")], last_exec_ts="not-a-date")
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "forced_news_check")

    def test_premarket_research_cannot_suppress_open(self):
        """THE 2026-06-11 bug, both halves. A 9:20 pre-market research run
        stamps last_model_call_ts minutes before the 9:30 first execution
        cycle. The gate must key off last_execution_call_ts only — with NO
        fallback to last_model_call_ts — or a log that has never stamped an
        execution call (e.g. first run after upgrading) re-skips the 9:30
        open until 13:20."""
        log = self._log([self._open_pos("AAPL")], last_call_ts=self._ts(0.2))
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "forced_news_check")

    def test_premarket_research_cannot_suppress_pending_buy(self):
        """Same poisoning scenario, but with no open positions — only an
        unowned weekend pick in BUY state waiting for the 9:30 open."""
        self._write_picks("MRVL")
        log = self._log(last_call_ts=self._ts(0.2))
        skip, reason = self._skip({"MRVL": self._sig("HOLD", state="BUY")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "pending_buy:MRVL")

    def test_gate_keyed_to_exec_ts_not_model_ts(self):
        """Fresh execution stamp + stale model stamp → still skip: only the
        execution stamp drives the gate."""
        log = self._log([self._open_pos("AAPL")],
                        last_call_ts=self._ts(6), last_exec_ts=self._ts(0.2))
        skip, reason = self._skip({"AAPL": self._sig("HOLD")}, log)
        self.assertTrue(skip)

    # ---------------- pending_buy gate (unowned weekend pick in BUY zone) ----
    def _write_picks(self, *symbols):
        path = os.path.join(self.tmpdir, "research", "weekend_picks_2026-06-10.md")
        with open(path, "w") as f:
            for i, s in enumerate(symbols, 1):
                f.write(f"### #{i} — {s} (Test Co) | Confidence: 80/100\n\n")

    def test_pending_buy_wakes_without_ts(self):
        self._write_picks("NVDA")
        log = self._log(last_call_ts=None)
        skip, reason = self._skip({"NVDA": self._sig("HOLD", state="BUY")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "pending_buy:NVDA")

    def test_pending_buy_gated_by_fresh_ts(self):
        self._write_picks("NVDA")
        log = self._log(last_exec_ts=self._ts(0.2))
        skip, _ = self._skip({"NVDA": self._sig("HOLD", state="BUY")}, log)
        self.assertTrue(skip)

    # ---------------- ENTER_LONG dedup ----------------
    def test_enter_long_unseen_wakes(self):
        log = self._log(last_call_ts=self._ts(0.2))
        skip, reason = self._skip({"NVDA": self._sig("ENTER_LONG")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "enter_long:NVDA")

    def test_enter_long_seen_recently_suppressed(self):
        """Same crossover re-reported within NEWS_CHECK_HOURS → no re-wake."""
        log = self._log(last_call_ts=self._ts(0.2),
                        el_seen={"NVDA": self._ts(0.3)})
        skip, reason = self._skip({"NVDA": self._sig("ENTER_LONG")}, log)
        self.assertTrue(skip)

    def test_enter_long_seen_long_ago_rewakes(self):
        log = self._log(last_call_ts=self._ts(0.2),
                        el_seen={"NVDA": self._ts(5)})
        skip, reason = self._skip({"NVDA": self._sig("ENTER_LONG")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "enter_long:NVDA")

    def test_enter_long_dedup_is_per_symbol(self):
        log = self._log(last_call_ts=self._ts(0.2),
                        el_seen={"NVDA": self._ts(0.3)})
        raw = {"NVDA": self._sig("ENTER_LONG"), "AMD": self._sig("ENTER_LONG")}
        skip, reason = self._skip(raw, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "enter_long:AMD")

    def test_exit_on_held_not_gated(self):
        """A SELL transition on a held position must always wake the model,
        regardless of how recent the last call was."""
        log = self._log([self._open_pos("AAPL")], last_call_ts=self._ts(0.05))
        skip, reason = self._skip({"AAPL": self._sig("EXIT", state="SELL")}, log)
        self.assertFalse(skip)
        self.assertEqual(reason, "exit:AAPL")


class TestHoursSince(unittest.TestCase):
    def test_none_and_garbage(self):
        self.assertIsNone(agent_module._hours_since(None))
        self.assertIsNone(agent_module._hours_since(""))
        self.assertIsNone(agent_module._hours_since("not-a-date"))

    def test_naive_assumed_local(self):
        ts = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        h = agent_module._hours_since(ts)
        self.assertAlmostEqual(h, 2.0, delta=0.1)

    def test_aware_et(self):
        ts = (datetime.now(agent_module.ET) - timedelta(hours=3)).isoformat(timespec="seconds")
        h = agent_module._hours_since(ts)
        self.assertAlmostEqual(h, 3.0, delta=0.1)


class TestRunModelErrorCapture(unittest.TestCase):
    """rc!=0 failures must surface the CLI's error detail. With
    --output-format json the CLI reports errors on STDOUT (stderr is often
    empty) — a full trading day was lost to blind '(rc=1: )' records."""

    def _fake(self, rc, stdout="", stderr=""):
        return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)

    def test_error_falls_back_to_stdout(self):
        fake = self._fake(1, stdout='{"type":"error","message":"usage limit reached"}')
        with patch.object(agent_module.subprocess, "run", return_value=fake):
            text, usage = agent_module.run_model("sys", "user")
        self.assertIn("usage limit reached", text)
        self.assertTrue(text.startswith("(claude -p error rc=1"))
        self.assertEqual(usage, agent_module._EMPTY_USAGE)

    def test_error_prefers_stderr_when_present(self):
        fake = self._fake(2, stdout="should not appear", stderr="real reason")
        with patch.object(agent_module.subprocess, "run", return_value=fake):
            text, _ = agent_module.run_model("sys", "user")
        self.assertIn("real reason", text)
        self.assertNotIn("should not appear", text)

    def test_skill_tools_disallowed_in_cmd(self):
        captured = {}
        def grab(cmd, **kw):
            captured["cmd"] = cmd
            return self._fake(0, stdout=json.dumps({"result": "ok", "usage": {}}))
        with patch.object(agent_module.subprocess, "run", side_effect=grab):
            agent_module.run_model("sys", "user")
        i = captured["cmd"].index("--disallowedTools")
        self.assertEqual(captured["cmd"][i + 1:i + 4], ["Skill", "Task", "Agent"])


class TestPersistPhaseOutput(TmpDirMixin):
    """Midweek/research output must be persisted by Python — the headless model
    has no file-write tool. A missing midweek_review_*.md makes active_skill()
    re-fire midweek_validation every remaining cycle of the day."""

    def _exists(self, fname):
        return os.path.exists(os.path.join(self.tmpdir, "research", fname))

    def _today(self):
        return datetime.now(agent_module.ET).strftime("%Y-%m-%d")

    def test_midweek_output_persisted(self):
        fname = agent_module.persist_phase_output("midweek_validation", "## Review\nHOLD all.")
        self.assertEqual(fname, f"midweek_review_{self._today()}.md")
        self.assertTrue(self._exists(fname))

    def test_error_output_not_persisted(self):
        for err in ("(claude -p error rc=1: usage limit)", "(error: claude -p timed out)"):
            self.assertIsNone(agent_module.persist_phase_output("midweek_validation", err))
        self.assertFalse(self._exists(f"midweek_review_{self._today()}.md"))

    def test_existing_file_never_clobbered(self):
        fname = f"midweek_review_{self._today()}.md"
        path = os.path.join(self.tmpdir, "research", fname)
        with open(path, "w") as f:
            f.write("original")
        self.assertIsNone(agent_module.persist_phase_output("midweek_validation", "new text"))
        with open(path) as f:
            self.assertEqual(f.read(), "original")

    def test_research_with_picks_persisted(self):
        text = "# Picks\n\n### #1 — NVDA (NVIDIA) | Confidence: 90/100\nthesis"
        fname = agent_module.persist_phase_output("research_and_prep", text)
        self.assertEqual(fname, f"weekend_picks_{self._today()}.md")
        # the persisted file must round-trip through weekend_pick_symbols()
        self.assertEqual(agent_module.weekend_pick_symbols(), ["NVDA"])

    def test_research_no_picks_marker_persisted_and_clears_watchlist(self):
        # A legit risk-off "no qualifying picks" run carries the machine-readable
        # marker and MUST be saved — otherwise execution inherits the prior day's
        # stale watchlist (the 2026-06-19 NVDA/AVGO bug).
        text = ("## Pre-market prep\n\n## RANKED PICKS: NONE\n"
                "SPY in ribbon SELL; entire high-beta universe reads SELL.\n")
        fname = agent_module.persist_phase_output("research_and_prep", text)
        self.assertEqual(fname, f"weekend_picks_{self._today()}.md")
        # zero `### #N` headings → fresh EMPTY watchlist, not a days-old one
        self.assertEqual(agent_module.weekend_pick_symbols(), [])

    def test_research_without_picks_or_marker_not_persisted(self):
        # genuinely malformed/prose output (no picks AND no marker) is still dropped
        # so run_agent's loud unsaved_*.md + WARNING fallback fires
        self.assertIsNone(agent_module.persist_phase_output("research_and_prep",
                                                            "No qualified candidates today."))

    def test_market_hours_check_never_persisted(self):
        self.assertIsNone(agent_module.persist_phase_output("market_hours_check", "### #1 — X\n"))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — JSON extraction from model output
# ═══════════════════════════════════════════════════════════════════════════════

class TestJSONExtraction(unittest.TestCase):

    def _ex(self, text):
        return agent_module.extract_last_json_block(text)

    def test_fenced_json_block(self):
        text = 'Some prose.\n```json\n{"cash": 100, "positions": []}\n```'
        result = self._ex(text)
        self.assertEqual(result["cash"], 100)

    def test_fenced_block_no_language_tag(self):
        text = 'Analysis done.\n```\n{"cash": 50}\n```'
        result = self._ex(text)
        self.assertEqual(result["cash"], 50)

    def test_bare_json_object(self):
        text = 'Here is the state: {"cash": 75, "positions": []}'
        result = self._ex(text)
        self.assertEqual(result["cash"], 75)

    def test_multiple_blocks_returns_last(self):
        text = ('```json\n{"cash": 1}\n```\n'
                'Some text.\n'
                '```json\n{"cash": 99}\n```')
        result = self._ex(text)
        self.assertEqual(result["cash"], 99)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(self._ex("not json at all"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._ex(""))

    def test_json_with_actions_taken(self):
        payload = {
            "cash": 200.0,
            "positions": [{"symbol": "SPY", "shares": 1.0,
                           "avg_price": 500.0, "last_price": 510.0}],
            "actions_taken": [{"type": "buy", "symbol": "SPY",
                               "shares": 1.0, "price": 500.0}]
        }
        text = f"Done.\n```json\n{json.dumps(payload)}\n```"
        result = self._ex(text)
        self.assertEqual(result["actions_taken"][0]["symbol"], "SPY")

    def test_malformed_then_valid(self):
        text = '{"bad": json}\n```json\n{"cash": 42}\n```'
        result = self._ex(text)
        self.assertEqual(result["cash"], 42)

    def test_nested_json(self):
        text = '```json\n{"a": {"b": {"c": 3}}}\n```'
        result = self._ex(text)
        self.assertEqual(result["a"]["b"]["c"], 3)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Position tracking: adopt, detect closed, record open
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionAdoption(TmpDirMixin):

    def test_new_position_adopted(self):
        log = {"open_positions": [],
               "_state": {"next_id": 1}, "summary": {}, "trades": []}
        broker_positions = [{"symbol": "AAPL", "shares": 2.0, "avg_price": 150.0}]
        adopted = agent_module.adopt_untracked_positions(log, broker_positions)
        self.assertEqual(adopted, ["AAPL"])
        self.assertEqual(len(log["open_positions"]), 1)
        self.assertEqual(log["open_positions"][0]["symbol"], "AAPL")
        self.assertAlmostEqual(log["open_positions"][0]["entry_price"], 150.0)
        self.assertTrue(log["open_positions"][0].get("adopted"))

    def test_already_tracked_not_duplicated(self):
        log = {"open_positions": [self._open_pos("AAPL")],
               "_state": {"next_id": 2}, "summary": {}, "trades": []}
        broker_positions = [{"symbol": "AAPL", "shares": 2.0, "avg_price": 155.0}]
        adopted = agent_module.adopt_untracked_positions(log, broker_positions)
        self.assertEqual(adopted, [])
        self.assertEqual(len(log["open_positions"]), 1)

    def test_zero_shares_skipped(self):
        log = {"open_positions": [],
               "_state": {"next_id": 1}, "summary": {}, "trades": []}
        broker_positions = [{"symbol": "ZERO", "shares": 0.0, "avg_price": 50.0}]
        adopted = agent_module.adopt_untracked_positions(log, broker_positions)
        self.assertEqual(adopted, [])
        self.assertEqual(len(log["open_positions"]), 0)

    def test_none_symbol_skipped(self):
        log = {"open_positions": [],
               "_state": {"next_id": 1}, "summary": {}, "trades": []}
        broker_positions = [{"symbol": None, "shares": 5.0, "avg_price": 50.0}]
        adopted = agent_module.adopt_untracked_positions(log, broker_positions)
        self.assertEqual(adopted, [])


class TestClosedPositionDetection(unittest.TestCase):

    def test_gone_symbol_detected_as_closed(self):
        open_pos = [{"symbol": "AAPL", "shares": 2.0}]
        current  = []
        closed = agent_module.detect_closed_positions(open_pos, current)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["symbol"], "AAPL")

    def test_zero_shares_detected_as_closed(self):
        open_pos = [{"symbol": "AAPL", "shares": 2.0}]
        current  = [{"symbol": "AAPL", "shares": 0.0}]
        closed = agent_module.detect_closed_positions(open_pos, current)
        self.assertEqual(len(closed), 1)

    def test_still_held_not_detected(self):
        open_pos = [{"symbol": "AAPL", "shares": 2.0}]
        current  = [{"symbol": "AAPL", "shares": 2.0}]
        closed = agent_module.detect_closed_positions(open_pos, current)
        self.assertEqual(closed, [])

    def test_mixed_some_closed_some_open(self):
        open_pos = [{"symbol": "A", "shares": 1.0},
                    {"symbol": "B", "shares": 1.0}]
        current  = [{"symbol": "B", "shares": 1.0}]
        closed = agent_module.detect_closed_positions(open_pos, current)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["symbol"], "A")


class TestRecordOpenPosition(TmpDirMixin):

    def test_buy_recorded(self):
        log = {"open_positions": [],
               "_state": {"next_id": 1}, "summary": {}, "trades": []}
        action = {"type": "buy", "symbol": "NVDA", "price": 500.0,
                  "shares": 0.2, "confidence": 85, "sources": ["news"],
                  "thesis": "GPU demand"}
        agent_module.record_open_position(log, action)
        self.assertEqual(len(log["open_positions"]), 1)
        pos = log["open_positions"][0]
        self.assertEqual(pos["symbol"], "NVDA")
        self.assertAlmostEqual(pos["entry_price"], 500.0)
        self.assertEqual(pos["confidence_score"], 85)
        self.assertEqual(pos["thesis"], "GPU demand")

    def test_scale_into_winner_blends(self):
        # Adding to an already-held name is a real scale-in, not a no-op: blend the
        # cost basis, sum the shares, keep the higher peak so the stop/trailing/P&L
        # machinery stays in sync with the broker.
        pos = self._open_pos("NVDA", entry=100.0, shares=1.0)
        pos["peak_price"] = 120.0
        log = {"open_positions": [pos],
               "_state": {"next_id": 2}, "summary": {}, "trades": []}
        action = {"type": "buy", "symbol": "NVDA", "price": 150.0, "shares": 1.0,
                  "confidence": 88}
        agent_module.record_open_position(log, action)
        self.assertEqual(len(log["open_positions"]), 1)  # still one position
        p = log["open_positions"][0]
        self.assertAlmostEqual(p["entry_price"], 125.0)        # blended avg cost
        self.assertAlmostEqual(p["shares"], 2.0)               # summed
        self.assertAlmostEqual(p["dollar_amount"], 250.0)      # blended * shares
        self.assertAlmostEqual(p["peak_price"], 150.0)         # raised to the add
        self.assertEqual(p["confidence_score"], 88)            # latest conviction
        self.assertEqual(p["id"], "T0001")                     # same trade id
        self.assertEqual(len(p["scaled_in"]), 1)               # add is audited

    def test_scale_in_keeps_existing_peak_when_adding_below_it(self):
        pos = self._open_pos("CAT", entry=900.0, shares=1.0)
        pos["peak_price"] = 1020.0
        log = {"open_positions": [pos], "_state": {"next_id": 2},
               "summary": {}, "trades": []}
        action = {"type": "buy", "symbol": "CAT", "price": 1000.0, "shares": 1.0}
        agent_module.record_open_position(log, action)
        # adding at 1000 < peak 1020 must NOT lower the high-water mark
        self.assertAlmostEqual(log["open_positions"][0]["peak_price"], 1020.0)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Trade outcome: P&L math, WIN/LOSS, stop-loss flag
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeOutcome(TmpDirMixin):

    def _close(self, entry, exit_price, shares=1.0, stop_loss=False):
        import copy
        log = copy.deepcopy(self.tradelog)
        pos = self._open_pos("TEST", entry, shares)
        log["open_positions"].append(pos)
        log["_state"]["next_id"] = 2
        self._flush_log()
        now = "2026-06-09T10:00:00"
        with patch.object(agent_module, "run_post_trade_pipeline"):
            trade = agent_module.log_trade_outcome(log, pos, exit_price, now,
                                                   stop_loss=stop_loss)
        return trade, log

    def test_win_detected(self):
        trade, _ = self._close(100.0, 110.0)
        self.assertEqual(trade["outcome"], "WIN")

    def test_loss_detected(self):
        trade, _ = self._close(100.0, 90.0)
        self.assertEqual(trade["outcome"], "LOSS")

    def test_breakeven_is_loss(self):
        """pnl=0 → not > 0 → LOSS."""
        trade, _ = self._close(100.0, 100.0)
        self.assertEqual(trade["outcome"], "LOSS")
        self.assertAlmostEqual(trade["pnl_dollar"], 0.0)
        self.assertAlmostEqual(trade["pnl_pct"], 0.0)

    def test_pnl_dollar_correct(self):
        trade, _ = self._close(100.0, 115.0, shares=2.0)
        self.assertAlmostEqual(trade["pnl_dollar"], 30.0, places=4)

    def test_pnl_pct_correct(self):
        trade, _ = self._close(200.0, 180.0)
        self.assertAlmostEqual(trade["pnl_pct"], -10.0, places=2)

    def test_stop_loss_flag_preserved(self):
        trade, _ = self._close(100.0, 88.0, stop_loss=True)
        self.assertTrue(trade["stop_loss"])

    def test_stop_loss_false_by_default(self):
        trade, _ = self._close(100.0, 110.0)
        self.assertFalse(trade["stop_loss"])

    def test_position_removed_from_open(self):
        _, log = self._close(100.0, 110.0)
        open_syms = [p["symbol"] for p in log["open_positions"]]
        self.assertNotIn("TEST", open_syms)

    def test_summary_totals_updated(self):
        _, log = self._close(100.0, 120.0, shares=1.0)
        s = log["summary"]
        self.assertEqual(s["total_trades"], 1)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 0)
        self.assertAlmostEqual(s["total_pnl"], 20.0, places=4)

    def test_win_rate_updated(self):
        _, log = self._close(100.0, 110.0)
        self.assertAlmostEqual(log["summary"]["win_rate"], 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Weight rebalancing
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeightRebalancing(unittest.TestCase):

    def _base_strategy(self, weights, perf):
        return {
            "research": {
                "source_weights": dict(weights),
                "source_performance": {
                    k: {"wins": w, "losses": l,
                        "accuracy": w / (w + l) if (w + l) else 0}
                    for k, (w, l) in perf.items()
                }
            }
        }

    def test_high_accuracy_source_gains_weight(self):
        strategy = self._base_strategy(
            {"news": 0.25, "social": 0.25, "fundamental": 0.25, "macro": 0.25},
            {"news": (9, 1), "social": (0, 0), "fundamental": (0, 0), "macro": (0, 0)},
        )
        before = strategy["research"]["source_weights"]["news"]
        agent_module.rebalance_source_weights(strategy)
        after = strategy["research"]["source_weights"]["news"]
        self.assertGreater(after, before)

    def test_low_accuracy_source_loses_weight(self):
        strategy = self._base_strategy(
            {"news": 0.30, "social": 0.25, "fundamental": 0.25, "macro": 0.20},
            {"news": (1, 9), "social": (5, 5), "fundamental": (5, 5), "macro": (5, 5)},
        )
        before = strategy["research"]["source_weights"]["news"]
        agent_module.rebalance_source_weights(strategy)
        after = strategy["research"]["source_weights"]["news"]
        self.assertLess(after, before)

    def test_max_delta_cap_0_05_per_update(self):
        """Shift can never exceed 0.05 per source in a single rebalance."""
        strategy = self._base_strategy(
            {"news": 0.25, "social": 0.25, "fundamental": 0.25, "macro": 0.25},
            {"news": (100, 0), "social": (0, 0), "fundamental": (0, 0), "macro": (0, 0)},
        )
        before = strategy["research"]["source_weights"]["news"]
        agent_module.rebalance_source_weights(strategy)
        after = strategy["research"]["source_weights"]["news"]
        self.assertLessEqual(after - before, 0.05 + 1e-9)

    def test_weights_sum_to_one_after_rebalance(self):
        strategy = self._base_strategy(
            {"news": 0.30, "social": 0.20, "fundamental": 0.35, "macro": 0.15},
            {"news": (8, 2), "social": (3, 7), "fundamental": (6, 4), "macro": (5, 5)},
        )
        agent_module.rebalance_source_weights(strategy)
        total = sum(strategy["research"]["source_weights"].values())
        self.assertAlmostEqual(total, 1.0, places=3)

    def test_no_trades_no_change(self):
        """All sources have 0 trades → total_acc=0 → early return, weights unchanged."""
        weights = {"news": 0.30, "social": 0.20, "fundamental": 0.35, "macro": 0.15}
        strategy = self._base_strategy(weights,
            {"news": (0, 0), "social": (0, 0), "fundamental": (0, 0), "macro": (0, 0)})
        before = dict(strategy["research"]["source_weights"])
        agent_module.rebalance_source_weights(strategy)
        self.assertEqual(strategy["research"]["source_weights"], before)

    def test_weight_bounded_at_0_6(self):
        """No single source can exceed 0.6."""
        strategy = self._base_strategy(
            {"news": 0.55, "social": 0.15, "fundamental": 0.15, "macro": 0.15},
            {"news": (100, 0), "social": (0, 100), "fundamental": (0, 100), "macro": (0, 100)},
        )
        agent_module.rebalance_source_weights(strategy)
        for w in strategy["research"]["source_weights"].values():
            self.assertLessEqual(w, 0.6 + 1e-9)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Monthly progress tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonthlyProgress(TmpDirMixin):

    def test_required_weekly_return_math(self):
        """compound rate: (target/current)^(1/weeks) - 1."""
        result = agent_module.required_weekly_return(100.0, 200.0, 4)
        expected = ((200.0 / 100.0) ** (1 / 4) - 1) * 100
        self.assertAlmostEqual(result, expected, places=2)

    def test_required_weekly_return_zero_weeks(self):
        self.assertEqual(agent_module.required_weekly_return(100.0, 200.0, 0), 0.0)

    def test_progress_updates_current_value(self):
        import copy
        log = copy.deepcopy(self.tradelog)
        log["summary"]["total_pnl"] = 15.0
        self.strategy["progress_tracking"]["month_start_value"] = 100.0
        self._flush_strategy()
        with patch.object(agent_module, "get_current_week_of_month", return_value=1):
            agent_module.update_monthly_progress(log)
        strategy = agent_module.load_strategy()
        self.assertAlmostEqual(strategy["progress_tracking"]["current_value"], 115.0, places=4)

    def test_on_track_when_ahead_of_pace(self):
        """Week 1 requires 25% — if we have 30%, we're on track."""
        import copy
        log = copy.deepcopy(self.tradelog)
        log["summary"]["total_pnl"] = 30.0  # 30% return on 100 start
        self.strategy["progress_tracking"]["month_start_value"] = 100.0
        self._flush_strategy()
        with patch.object(agent_module, "get_current_week_of_month", return_value=1):
            on_track = agent_module.update_monthly_progress(log)
        self.assertTrue(on_track)

    def test_not_on_track_when_behind_pace(self):
        """Week 2 requires 50% — if we have 10%, we're behind."""
        import copy
        log = copy.deepcopy(self.tradelog)
        log["summary"]["total_pnl"] = 10.0
        self.strategy["progress_tracking"]["month_start_value"] = 100.0
        self._flush_strategy()
        with patch.object(agent_module, "get_current_week_of_month", return_value=2):
            on_track = agent_module.update_monthly_progress(log)
        self.assertFalse(on_track)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Trade ID generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateTradeId(unittest.TestCase):

    def test_sequential_ids(self):
        log = {"_state": {"next_id": 1}}
        ids = [agent_module.generate_trade_id(log) for _ in range(5)]
        self.assertEqual(ids, ["T0001", "T0002", "T0003", "T0004", "T0005"])

    def test_id_format_zero_padded(self):
        log = {"_state": {"next_id": 99}}
        self.assertEqual(agent_module.generate_trade_id(log), "T0099")

    def test_id_four_digit_past_9999(self):
        log = {"_state": {"next_id": 10000}}
        self.assertEqual(agent_module.generate_trade_id(log), "T10000")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — Watchlist deduplication
# ═══════════════════════════════════════════════════════════════════════════════

class TestWatchlistSymbols(unittest.TestCase):

    def test_spy_always_first(self):
        log = {"open_positions": []}
        syms = agent_module.watchlist_symbols(log)
        self.assertEqual(syms[0], "SPY")

    def test_deduplication(self):
        """SPY in WATCHLIST env + SPY held → only one SPY in output."""
        log = {"open_positions": [{"symbol": "SPY"}]}
        with patch.object(agent_module, "WATCHLIST", ["SPY", "AAPL"]):
            syms = agent_module.watchlist_symbols(log)
        self.assertEqual(syms.count("SPY"), 1)

    def test_open_positions_included(self):
        log = {"open_positions": [{"symbol": "NVDA"}, {"symbol": "MSFT"}]}
        with patch.object(agent_module, "WATCHLIST", []):
            syms = agent_module.watchlist_symbols(log)
        self.assertIn("NVDA", syms)
        self.assertIn("MSFT", syms)

    def test_case_normalized_to_upper(self):
        log = {"open_positions": [{"symbol": "aapl"}]}
        with patch.object(agent_module, "WATCHLIST", ["nvda"]):
            syms = agent_module.watchlist_symbols(log)
        self.assertIn("AAPL", syms)
        self.assertIn("NVDA", syms)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — Stop-loss prompt block formatting
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatStopLossBlock(unittest.TestCase):

    def test_empty_alerts_returns_empty_string(self):
        self.assertEqual(agent_module._format_stop_loss_block([]), "")

    def test_alert_contains_symbol(self):
        alerts = [{"symbol": "AAPL", "entry_price": 100.0, "last_price": 88.0,
                   "loss_pct": -12.0, "threshold_price": 90.0, "shares": 2}]
        block = agent_module._format_stop_loss_block(alerts)
        self.assertIn("AAPL", block)
        self.assertIn("STOP-LOSS TRIGGERED", block)
        self.assertIn("-12.0%", block)

    def test_multiple_alerts_all_present(self):
        alerts = [
            {"symbol": "A", "entry_price": 100, "last_price": 88,
             "loss_pct": -12.0, "threshold_price": 90, "shares": 1},
            {"symbol": "B", "entry_price": 200, "last_price": 175,
             "loss_pct": -12.5, "threshold_price": 180, "shares": 2},
        ]
        block = agent_module._format_stop_loss_block(alerts)
        self.assertIn("A:", block)
        self.assertIn("B:", block)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14b — broker reconciliation primitives (read_broker_state / force_sell)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerReconciliation(unittest.TestCase):
    """The independent broker read and the deterministic forced sell must degrade
    SAFELY when claude -p fails (session limit, garbage output): never claim the
    account is flat, never claim a sell was placed."""

    def test_read_broker_state_failed_call_returns_none(self):
        """A failed/garbage model response → (None, set()), NOT an empty position
        list. None tells the caller 'unknown' so it skips close detection rather
        than phantom-closing everything."""
        with patch.object(agent_module, "run_model",
                          return_value=("(claude -p error rc=1: session limit)", {})):
            positions, sells = agent_module.read_broker_state()
        self.assertIsNone(positions)
        self.assertEqual(sells, set())

    def test_read_broker_state_parses_positions_and_sells(self):
        payload = ('```json\n{"positions": [{"symbol": "CLOV", "shares": 4.18, '
                   '"avg_price": 4.89, "last_price": 4.59}, '
                   '{"symbol": "DEAD", "shares": 0, "avg_price": 1.0}], '
                   '"sell_orders_today": ["clov"]}\n```')
        with patch.object(agent_module, "run_model", return_value=(payload, {})):
            positions, sells = agent_module.read_broker_state()
        self.assertEqual([p["symbol"] for p in positions], ["CLOV"])  # zero-share dropped
        self.assertEqual(sells, {"CLOV"})                              # upper-cased

    def test_force_sell_advisory_mode_is_noop(self):
        with patch.object(agent_module, "EXECUTION_MODE", "advisory"):
            placed, fill = agent_module.force_sell("CLOV", 4.18, "ema_exit")
        self.assertFalse(placed)
        self.assertIsNone(fill)

    def test_force_sell_live_reports_placement(self):
        payload = ('```json\n{"placed": true, "symbol": "CLOV", '
                   '"order_id": "abc-123", "fill_price": 4.59}\n```')
        with patch.object(agent_module, "EXECUTION_MODE", "live"), \
             patch.object(agent_module, "run_model", return_value=(payload, {})):
            placed, fill = agent_module.force_sell("CLOV", 4.18, "ema_exit")
        self.assertTrue(placed)
        self.assertEqual(fill, 4.59)

    def test_force_sell_live_unconfirmed_is_not_placed(self):
        """If the confirm step can't verify an order id, placed=false — we must
        not report a sell we can't confirm."""
        payload = '```json\n{"placed": false, "symbol": "CLOV", "fill_price": null}\n```'
        with patch.object(agent_module, "EXECUTION_MODE", "live"), \
             patch.object(agent_module, "run_model", return_value=(payload, {})):
            placed, fill = agent_module.force_sell("CLOV", 4.18, "ema_exit")
        self.assertFalse(placed)
        self.assertIsNone(fill)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14c — out-of-band operator alerts (notify_operator)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotifyOperator(unittest.TestCase):
    """The forced-exit / session-limit alert must reach the operator out of band
    without depending on the claude CLI, and must NEVER raise — an alerting
    failure can't be allowed to break the trading loop."""

    def test_no_webhook_is_stdout_only_no_post(self):
        """Unconfigured (no ALERT_WEBHOOK_URL) => stdout only, no HTTP attempt."""
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(agent_module.urllib.request, "urlopen") as mock_open:
            agent_module.notify_operator("subj", "body")
        mock_open.assert_not_called()

    def test_webhook_posts_subject_and_body(self):
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hook.example/x"}), \
             patch.object(agent_module.urllib.request, "urlopen") as mock_open:
            agent_module.notify_operator("forced exit BLOCKED", "CLOV not sold")
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://hook.example/x")
        sent = json.loads(req.data.decode("utf-8"))
        self.assertEqual(sent["title"], "forced exit BLOCKED")
        self.assertIn("CLOV not sold", sent["message"])
        self.assertIn("CLOV not sold", sent["text"])  # Slack/Discord field

    def test_webhook_failure_is_swallowed(self):
        """A dead webhook URL must not propagate an exception to the caller."""
        with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://hook.example/x"}), \
             patch.object(agent_module.urllib.request, "urlopen",
                          side_effect=OSError("connection refused")):
            try:
                agent_module.notify_operator("subj", "body")
            except Exception as e:
                self.fail(f"notify_operator must never raise, got {e!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — process_cycle_state integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessCycleState(TmpDirMixin):
    """process_cycle_state now takes (log, actions, broker_positions, exit_info).
    broker_positions is the AUTHORITATIVE independent broker read — closes fire
    only when the broker confirms a position is gone, never on the footer alone."""

    def _run_cycle(self, log, actions, broker_positions, exit_info=None):
        with patch.object(agent_module, "log_trade_outcome") as mock_close:
            mock_close.return_value = {"id": "T0001", "outcome": "WIN",
                                       "pnl_dollar": 10, "pnl_pct": 10}
            agent_module.process_cycle_state(log, actions, broker_positions, exit_info)
        return mock_close

    def test_buy_action_records_open_position(self):
        import copy
        log = copy.deepcopy(self.tradelog)
        self._flush_log()
        positions = [{"symbol": "NVDA", "shares": 0.1,
                      "avg_price": 900.0, "last_price": 905.0}]
        actions = [{"type": "buy", "symbol": "NVDA", "shares": 0.1, "price": 900.0}]
        with patch.object(agent_module, "log_trade_outcome"):
            agent_module.process_cycle_state(log, actions, positions)
        open_syms = [p["symbol"] for p in log["open_positions"]]
        self.assertIn("NVDA", open_syms)

    def test_fabricated_buy_not_recorded(self):
        """A buy action the broker does NOT confirm must not be recorded."""
        import copy
        log = copy.deepcopy(self.tradelog)
        self._flush_log()
        actions = [{"type": "buy", "symbol": "NVDA", "shares": 0.1, "price": 900.0}]
        with patch.object(agent_module, "log_trade_outcome"):
            agent_module.process_cycle_state(log, actions, [])  # broker shows nothing
        open_syms = [p["symbol"] for p in log["open_positions"]]
        self.assertNotIn("NVDA", open_syms)

    def test_sell_action_triggers_log_trade_outcome(self):
        import copy
        log = copy.deepcopy(self.tradelog)
        log["open_positions"].append(self._open_pos("AAPL", 150.0))
        log["_state"]["next_id"] = 2
        self._flush_log()
        actions = [{"type": "sell", "symbol": "AAPL", "shares": 1.0, "price": 160.0}]
        mock_close = self._run_cycle(log, actions, [])  # broker confirms AAPL gone
        mock_close.assert_called_once()
        args = mock_close.call_args
        self.assertEqual(args[0][1]["symbol"], "AAPL")  # open_pos arg
        self.assertEqual(args[0][2], 160.0)             # exit price from footer

    def test_phantom_sell_not_closed_when_broker_still_holds(self):
        """The core bug: footer claims a sell, but the broker still holds the
        position → it must NOT be marked closed."""
        import copy
        log = copy.deepcopy(self.tradelog)
        log["open_positions"].append(self._open_pos("CLOV", 4.89))
        log["_state"]["next_id"] = 2
        self._flush_log()
        actions = [{"type": "sell", "symbol": "CLOV", "shares": 4.18, "price": 4.59}]
        broker = [{"symbol": "CLOV", "shares": 4.18, "avg_price": 4.89,
                   "last_price": 4.59}]  # broker STILL holds CLOV
        with patch.object(agent_module, "log_trade_outcome") as mock_close:
            agent_module.process_cycle_state(log, actions, broker)
        mock_close.assert_not_called()
        self.assertIn("CLOV", [p["symbol"] for p in log["open_positions"]])

    def test_sell_stop_loss_flag(self):
        import copy
        log = copy.deepcopy(self.tradelog)
        log["open_positions"].append(self._open_pos("TEST", 100.0))
        log["_state"]["next_id"] = 2
        self._flush_log()
        actions = [{"type": "sell", "symbol": "TEST", "shares": 1.0,
                    "price": 90.0, "reason": "stop_loss"}]
        with patch.object(agent_module, "log_trade_outcome") as mock_close:
            mock_close.return_value = {}
            agent_module.process_cycle_state(log, actions, [])
        _, kwargs = mock_close.call_args
        self.assertTrue(kwargs.get("stop_loss"))

    def test_exit_info_reason_and_price_win(self):
        """A deterministic forced sell supplies reason + price via exit_info even
        when the footer carries no matching sell action."""
        import copy
        log = copy.deepcopy(self.tradelog)
        log["open_positions"].append(self._open_pos("TEST", 100.0))
        log["_state"]["next_id"] = 2
        self._flush_log()
        exit_info = {"TEST": {"reason": "stop_loss", "price": 88.5}}
        with patch.object(agent_module, "log_trade_outcome") as mock_close:
            mock_close.return_value = {}
            agent_module.process_cycle_state(log, [], [], exit_info)
        args, kwargs = mock_close.call_args
        self.assertEqual(args[2], 88.5)            # exit price from exit_info
        self.assertTrue(kwargs.get("stop_loss"))

    def test_vanished_position_safety_net(self):
        """Position gone from the broker read with no sell action → still closes."""
        import copy
        log = copy.deepcopy(self.tradelog)
        log["open_positions"].append(self._open_pos("GONE", 100.0))
        log["_state"]["next_id"] = 2
        self._flush_log()
        mock_close = self._run_cycle(log, [], [])  # broker confirms flat
        mock_close.assert_called_once()

    def test_snapshot_saved_to_state(self):
        import copy
        log = copy.deepcopy(self.tradelog)
        self._flush_log()
        positions = [{"symbol": "SPY", "shares": 1.0,
                      "avg_price": 500.0, "last_price": 505.0}]
        with patch.object(agent_module, "log_trade_outcome"):
            agent_module.process_cycle_state(log, [], positions)
        self.assertEqual(log["_state"]["last_positions"], positions)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16 — Miscellaneous agent helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestMiscHelpers(unittest.TestCase):

    def test_is_market_open_returns_bool(self):
        result = agent_module.is_market_open()
        self.assertIsInstance(result, bool)

    def test_next_market_open_is_future_weekday(self):
        opens = agent_module.next_market_open()
        self.assertGreater(opens, datetime.now(agent_module.ET))
        self.assertLess(opens.weekday(), 5)
        self.assertEqual(opens.hour, 9)
        self.assertEqual(opens.minute, 30)

    def test_get_current_week_of_month_range(self):
        week = agent_module.get_current_week_of_month()
        self.assertGreaterEqual(week, 1)
        self.assertLessEqual(week, 4)

    def test_strip_json_blocks(self):
        text = "Prose.\n```json\n{\"key\": \"val\"}\n```\nMore prose."
        stripped = agent_module.strip_json_blocks(text)
        self.assertNotIn("```json", stripped)
        self.assertIn("Prose.", stripped)
        self.assertIn("More prose.", stripped)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16b — Prompt-slimming helpers (strategy_for_prompt + skill_5 context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyForPrompt(TmpDirMixin):
    """The lean strategy view injected into read-only cycle prompts must drop the
    heavy version_history audit array WITHOUT touching the on-disk file (skill_5
    and Phase 4 read the complete history from disk)."""

    def _seed(self):
        self.strategy["version_history"] = [
            {"version": 1, "date": "2026-06-10", "change": "x" * 400, "trade_ids": ["T0001"]},
            {"version": 2, "date": "2026-06-11", "change": "y" * 400, "trade_ids": ["T0002"]},
        ]
        self.strategy["ema_strategy"] = {"buy_signal": "red lowest"}
        self._flush_strategy()

    def test_drops_version_history_but_keeps_rules(self):
        self._seed()
        lean = json.loads(agent_module.strategy_for_prompt())
        self.assertNotIn("version_history", lean)
        self.assertEqual(lean["ema_strategy"], {"buy_signal": "red lowest"})
        self.assertEqual(lean["risk_management"], self.strategy["risk_management"])

    def test_leaves_breadcrumb_note(self):
        self._seed()
        lean = json.loads(agent_module.strategy_for_prompt())
        self.assertIn("version_history_note", lean)
        self.assertIn("2 version_history entries", lean["version_history_note"])

    def test_on_disk_file_is_untouched(self):
        self._seed()
        agent_module.strategy_for_prompt()
        on_disk = json.load(open(os.path.join(self.tmpdir, "strategy", "strategy.json")))
        self.assertEqual(len(on_disk["version_history"]), 2)

    def test_is_materially_smaller_than_full(self):
        self._seed()
        full = open(os.path.join(self.tmpdir, "strategy", "strategy.json")).read()
        self.assertLess(len(agent_module.strategy_for_prompt()), len(full))

    def test_missing_strategy_does_not_crash(self):
        os.remove(os.path.join(self.tmpdir, "strategy", "strategy.json"))
        self.assertIsInstance(agent_module.strategy_for_prompt(), str)


class TestRewritePromptSlimming(TmpDirMixin):
    """skill_5's per-close context: compact trade log + referenced-postmortem-only."""

    def test_compact_trade_log_structure_and_focus(self):
        self.tradelog["trades"] = [
            {"id": "T0001", "symbol": "AAA", "outcome": "WIN", "pnl_pct": 5.0,
             "confidence_score": 80, "thesis": "z" * 500},
            {"id": "T0002", "symbol": "BBB", "outcome": "LOSS", "pnl_pct": -3.0,
             "confidence_score": 62, "thesis": "q" * 500},
        ]
        self._flush_log()
        view = json.loads(agent_module._compact_trade_log_for_rewrite(self.tradelog, "T0002"))
        self.assertIn("summary", view)
        self.assertIn("closed_trades_compact", view)
        self.assertEqual(len(view["closed_trades_compact"]), 2)
        # focus trade is included in full; non-focus trades are NOT dumped in full
        self.assertEqual(view["focus_trade_full"]["id"], "T0002")
        self.assertLess(len(agent_module._compact_trade_log_for_rewrite(self.tradelog, "T0002")),
                        len(json.dumps(self.tradelog, indent=2)))

    def test_compact_trade_log_unknown_focus_is_safe(self):
        self._flush_log()
        view = json.loads(agent_module._compact_trade_log_for_rewrite(self.tradelog, "T9999"))
        self.assertNotIn("focus_trade_full", view)

    def test_postmortems_referenced_full_others_indexed(self):
        d = os.path.join(self.tmpdir, "postmortems")
        for name, body in [("postmortem_001.md", "# CAT loss\nfull body here"),
                           ("postmortem_002.md", "# CLOV loss\nother body"),
                           ("victory_001.md", "# AMAT win\nwin body")]:
            with open(os.path.join(d, name), "w") as f:
                f.write(body)
        out = agent_module._postmortems_for_rewrite("postmortem_001.md")
        self.assertIn("REFERENCED ANALYSIS (postmortem_001.md)", out)
        self.assertIn("full body here", out)            # referenced one: full text
        self.assertIn("postmortem_002.md", out)         # others: named
        self.assertNotIn("other body", out)             # but not pasted in full
        self.assertIn("victory_001.md", out)

    def test_postmortems_missing_reference_is_safe(self):
        with open(os.path.join(self.tmpdir, "postmortems", "postmortem_001.md"), "w") as f:
            f.write("# only one")
        out = agent_module._postmortems_for_rewrite("nonexistent.md")
        self.assertIsInstance(out, str)
        self.assertIn("postmortem_001.md", out)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16c — Change-event log (Phase 4 input) + rolling audit log
# ═══════════════════════════════════════════════════════════════════════════════

class TestChangeEventLog(TmpDirMixin):
    """logs/change_events.jsonl is the structured, timestamped record of every
    strategy.json / skill-file version bump — the input Phase 4's regression
    detector reads. Must capture both kinds, append-only, never raise."""

    def _events(self):
        path = os.path.join(self.tmpdir, "logs", "change_events.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(l) for l in open(path) if l.strip()]

    def test_record_change_event_writes_structured_line(self):
        agent_module.record_change_event("strategy", "strategy.json", 5,
                                         trade_ids=["T0001"], severity="ROUTINE",
                                         summary="weight rebalance")
        evts = self._events()
        self.assertEqual(len(evts), 1)
        e = evts[0]
        self.assertEqual(e["kind"], "strategy")
        self.assertEqual(e["version"], 5)
        self.assertEqual(e["trade_ids"], ["T0001"])
        self.assertEqual(e["severity"], "ROUTINE")
        self.assertIn("timestamp", e)

    def test_record_change_event_is_append_only(self):
        agent_module.record_change_event("strategy", "strategy.json", 5)
        agent_module.record_change_event("skill", "skill_1_research", 2)
        self.assertEqual(len(self._events()), 2)

    def test_snapshot_strategy_records_routine_event(self):
        self._flush_strategy()
        agent_module.snapshot_strategy(self.strategy, "source-weight rebalance", ["T0003"])
        evts = self._events()
        self.assertTrue(any(e["kind"] == "strategy" and e["severity"] == "ROUTINE"
                            for e in evts))

    def test_version_skill_file_records_event(self):
        with open(os.path.join(self.tmpdir, "skills", "skill_1_research.md"), "w") as f:
            f.write("old content")
        ok = agent_module.version_skill_file("skill_1_research", "new content",
                                             reason="test", trade_ids=["T0004"])
        self.assertTrue(ok)
        evts = self._events()
        self.assertTrue(any(e["kind"] == "skill" and e["target"] == "skill_1_research"
                            for e in evts))

    def test_execution_skill_change_tagged_major(self):
        with open(os.path.join(self.tmpdir, "skills", "skill_2_execution.md"), "w") as f:
            f.write("old exec")
        agent_module.version_skill_file("skill_2_execution", "new exec")
        evts = self._events()
        exec_evt = next(e for e in evts if e["target"] == "skill_2_execution")
        self.assertEqual(exec_evt["severity"], "MAJOR")

    def test_record_change_event_never_raises(self):
        # Even with a junk severity / weird args it must not throw.
        try:
            agent_module.record_change_event("skill", "x", None, summary=None or "")
        except Exception as e:
            self.fail(f"record_change_event raised: {e}")


class TestRollingAuditLog(TmpDirMixin):
    """append_audit_log replaces 135+ per-cycle agent_run_*.md files with one
    monthly rolling log."""

    def test_appends_to_single_monthly_file(self):
        agent_module.append_audit_log("runs", "Run A", "body A")
        agent_module.append_audit_log("runs", "Run B", "body B")
        logs = os.listdir(os.path.join(self.tmpdir, "logs"))
        runs = [f for f in logs if f.startswith("runs_") and f.endswith(".md")]
        self.assertEqual(len(runs), 1)  # one file, not two
        content = open(os.path.join(self.tmpdir, "logs", runs[0])).read()
        self.assertIn("Run A", content)
        self.assertIn("Run B", content)
        self.assertIn("body A", content)

    def test_never_raises(self):
        try:
            agent_module.append_audit_log("runs", "t", "b")
        except Exception as e:
            self.fail(f"append_audit_log raised: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16d — run_agent off-hours gating + skill_5 apply path (integration)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOffHoursBrokerGating(TmpDirMixin):
    """run_agent() must skip the authoritative broker read when the market is
    closed (no orders can fill) and still perform it when the market is open."""

    def _mock_signals(self):
        sig = {"ok": True, "state": "NEUTRAL", "transition": "NO_ACTION",
               "last_close": 505.0, "lines": {"red": 1, "blue": 2, "green": 3, "yellow": 4}}
        msig = MagicMock()
        msig.signals_with_raw.return_value = ({"SPY": sig}, "SPY: NEUTRAL")
        msig.signal_for.return_value = sig
        return msig

    def test_market_closed_skips_broker_read(self):
        rbs = MagicMock(return_value=([], set()))
        with patch.object(agent_module, "signals", self._mock_signals()), \
             patch.object(agent_module, "is_market_open", return_value=False), \
             patch.object(agent_module, "read_broker_state", rbs), \
             patch.object(agent_module, "run_model",
                          return_value=('ok\n```json\n{"cash":100,"positions":[],"actions_taken":[]}\n```',
                                        agent_module._EMPTY_USAGE)), \
             patch.object(agent_module, "process_strategy_rewrite_queue", lambda: None):
            agent_module.run_agent()
        self.assertEqual(rbs.call_count, 0)

    def test_market_open_calls_broker_read(self):
        # held position so market_hours_check does not smart-skip
        self.tradelog["open_positions"] = [{"id": "T0009", "symbol": "SPY",
            "entry_price": 500.0, "shares": 1.0, "entry_date": "2026-06-16T10:00:00-04:00",
            "dollar_amount": 500.0}]
        self._flush_log()
        rbs = MagicMock(return_value=([{"symbol": "SPY", "shares": 1.0,
                        "avg_price": 500.0, "last_price": 505.0}], set()))
        with patch.object(agent_module, "signals", self._mock_signals()), \
             patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "read_broker_state", rbs), \
             patch.object(agent_module, "run_model",
                          return_value=('ok\n```json\n{"cash":100,"positions":[{"symbol":"SPY","shares":1.0,"avg_price":500.0,"last_price":505.0}],"actions_taken":[]}\n```',
                                        agent_module._EMPTY_USAGE)), \
             patch.object(agent_module, "process_strategy_rewrite_queue", lambda: None):
            agent_module.run_agent()
        self.assertGreaterEqual(rbs.call_count, 1)


class TestSkill5ApplyPathIntegration(TmpDirMixin):
    """process_strategy_rewrite_queue() must parse skill_5 output and apply
    strategy.json + skill-file updates, record change events, and mark DONE."""

    def test_full_apply_path(self):
        with open(os.path.join(self.tmpdir, "research", "strategy_rewrite_queue.md"), "w") as f:
            f.write("# queue\n\n- 2026-06-17T09:00:00-04:00 | T0002 CAT LOSS -0.62% | "
                    "analysis: postmortem_001.md | skill_5 review\n")
        with open(os.path.join(self.tmpdir, "postmortems", "postmortem_001.md"), "w") as f:
            f.write("# CAT loss\nno thesis")
        with open(os.path.join(self.tmpdir, "skills", "skill_2_execution.md"), "w") as f:
            f.write("# skill_2_execution\noriginal")
        new_strat = {"version": self.strategy["version"] + 1,
                     "ema_strategy": {"buy_signal": "red lowest"},
                     "risk_management": {"stop_loss_pct": 0.10},
                     "research": self.strategy["research"], "progress_tracking": {},
                     "version_history": [{"version": self.strategy["version"] + 1,
                        "date": "2026-06-17", "change": "T0002 review", "trade_ids": ["T0002"]}]}
        out = ("SEVERITY: MAJOR\n\nReviewed T0002.\n\n"
               "## SKILL FILE UPDATE: skill_2_execution\n# skill_2_execution\nNEW RULES\n"
               "## END SKILL FILE UPDATE\n\n```json\n" + json.dumps(new_strat) + "\n```\n")
        with patch.object(agent_module, "run_model",
                          return_value=(out, agent_module._EMPTY_USAGE)):
            agent_module.process_strategy_rewrite_queue()

        applied = json.load(open(os.path.join(self.tmpdir, "strategy", "strategy.json")))
        self.assertEqual(applied["version"], self.strategy["version"] + 1)
        self.assertIn("NEW RULES", open(os.path.join(self.tmpdir, "skills", "skill_2_execution.md")).read())
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "skills", "history", "skill_2_execution_v001.md")))
        self.assertIn("[DONE", open(os.path.join(self.tmpdir, "research", "strategy_rewrite_queue.md")).read())
        evt_path = os.path.join(self.tmpdir, "logs", "change_events.jsonl")
        evts = [json.loads(l) for l in open(evt_path)] if os.path.exists(evt_path) else []
        self.assertTrue(any(e["kind"] == "strategy" and e["severity"] == "MAJOR" for e in evts))
        self.assertTrue(any(e["kind"] == "skill" and e["target"] == "skill_2_execution" for e in evts))

    def test_missing_analysis_file_is_skipped_not_run(self):
        with open(os.path.join(self.tmpdir, "research", "strategy_rewrite_queue.md"), "w") as f:
            f.write("# queue\n\n- 2026-06-17T09:00:00-04:00 | T0099 X LOSS -1% | "
                    "analysis: nonexistent.md | skill_5 review\n")
        rm = MagicMock()
        with patch.object(agent_module, "run_model", rm):
            agent_module.process_strategy_rewrite_queue()
        rm.assert_not_called()  # must NOT burn a model call on a missing analysis
        self.assertIn("SKIPPED-missing-analysis",
                      open(os.path.join(self.tmpdir, "research", "strategy_rewrite_queue.md")).read())


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 17 — Token usage estimation (non-test, printed report)
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tokens(text):
    """Rough estimate: 1 token ≈ 3.5 chars (code/JSON-heavy content)."""
    return max(1, int(len(text) / 3.5))

def _load(relpath):
    full = os.path.join(PROJECT_ROOT, relpath)
    return open(full).read() if os.path.exists(full) else ""

def _build_prompt(skill_path, task, postmortems=False, strip_history=False):
    """Reconstruct the exact system+user prompt that run_agent() would send."""
    orchestrator = _load("skills/skill_0_orchestrator.md")
    skill        = _load(skill_path)
    strategy     = _load("strategy/strategy.json")
    trade_log    = json.loads(_load("trade_log.json") or "{}")
    pm_text      = ""

    if postmortems:
        d = os.path.join(PROJECT_ROOT, "postmortems")
        files = sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []
        pm_text = "\n\nALL POSTMORTEMS AND VICTORY ANALYSES:\n" + \
                  "\n\n---\n\n".join(_load(f"postmortems/{f}") for f in files)

    if strip_history:
        log_for_prompt = {"summary": trade_log.get("summary", {}),
                          "open_positions": trade_log.get("open_positions", [])}
    else:
        log_for_prompt = trade_log

    sig_block = "- SPY: NEUTRAL / NO_ACTION | last=510.00 | red(55)=508.00 blue(8)=511.00 green(13)=510.50 yellow(21)=509.80 | 120 bars, source=yahoo(1h)"

    system = f"""{orchestrator}

CURRENT SKILL ACTIVE:
{skill}

CURRENT STRATEGY (strategy/strategy.json):
{strategy}

TRADE LOG (trade_log.json):
{json.dumps(log_for_prompt, indent=2)}
{pm_text}
EXECUTION RULES:
- MODE = ADVISORY. In advisory mode you have READ-ONLY tools and CANNOT place orders — output proposals only. In live mode you may place/cancel orders via robinhood-cli.
- Trade ONLY in Robinhood account 696283985 (the Agentic cash account).
  Never use the default margin account.
- T+1 settlement: read SETTLED cash before any buy; never deploy unsettled funds.
- EMA signal: red(55) lowest = BUY, red highest = SELL; act on the transition.
- Honor blackout windows + min_confidence_to_trade. Real scoreboard = beat SPY;
  100% monthly is the stretch ceiling, not a reason to oversize risk."""

    user = (
        f"Task: {task}\nTime (ET): 2026-06-09 10:00 (Monday)\n\n"
        "COMPUTED EMA SIGNALS (authoritative):\n"
        f"{sig_block}\n\n"
        "1. Read settled cash + positions from the Robinhood MCP.\n"
        "2. Run the active skill.\n"
        "3. Report actions_taken.\n"
        + agent_module.FOOTER_INSTRUCTION
    )

    return system + "\n\n=== TASK ===\n" + user


def run_token_report():
    """Print a token-usage breakdown for all cycle types."""
    CHARS_PER_TOKEN = 3.5

    cycles = [
        ("market_hours_check (ENTER_LONG → model called)",
         "skills/skill_2_execution.md", "market_hours_check",
         False, True,
         agent_module.CHECK_MODEL, "Haiku 4.5"),
        ("market_hours_check (all NEUTRAL → SKIP)",
         None, None, None, None,
         None, None),
        ("research_and_prep (weekend, Opus)",
         "skills/skill_1_research.md", "research_and_prep",
         True, False,
         agent_module.MODEL, "Opus 4.8"),
        ("midweek_validation (Wednesday, Opus)",
         "skills/skill_3_midweek.md", "midweek_validation",
         True, False,
         agent_module.MODEL, "Opus 4.8"),
        ("postmortem / victory (fired after every close)",
         "skills/skill_4_postmortem.md", "postmortem",
         False, False,
         agent_module.MODEL, "Opus 4.8"),
    ]

    LINE = "─" * 78
    print(f"\n{'═'*78}")
    print("  TOKEN USAGE ESTIMATION  (1 token ≈ 3.5 chars; estimates only)")
    print(f"{'═'*78}")
    print(f"{'Cycle':<45} {'~Tokens':>8} {'Model':<30}")
    print(LINE)

    total_skip_tokens = 0
    total_skip_cycles_per_day = 0

    for label, skill_path, task, with_pm, strip_hist, model, model_label in cycles:
        if skill_path is None:
            # skip path
            print(f"{'  ' + label:<45} {'0':>8}  (no model call)")
            continue
        prompt = _build_prompt(skill_path, task,
                               postmortems=with_pm, strip_history=strip_hist)
        tokens = _estimate_tokens(prompt)
        print(f"{'  ' + label:<45} {tokens:>8,}  {model_label} ({model})")

    print(LINE)
    print()

    # Estimate per-day cost assuming 6.5h market day @ 15-min polls
    cycles_per_day = int(6.5 * 60 / agent_module.POLL_MINUTES)
    print(f"  Market-hours cycles per day (at POLL_MINUTES={agent_module.POLL_MINUTES}): {cycles_per_day}")
    print(f"  Smart-skip eliminates ~90% → ~{int(cycles_per_day * 0.1)} model calls/day during market hours")
    print()

    # Prompt size breakdown
    print(f"{'─'*78}")
    print("  Prompt component sizes (chars)")
    print(f"{'─'*78}")
    components = [
        ("skill_0_orchestrator.md",    _load("skills/skill_0_orchestrator.md")),
        ("skill_2_execution.md",        _load("skills/skill_2_execution.md")),
        ("skill_1_research.md",         _load("skills/skill_1_research.md")),
        ("skill_3_midweek.md",          _load("skills/skill_3_midweek.md")),
        ("strategy/strategy.json",      _load("strategy/strategy.json")),
        ("trade_log.json",              _load("trade_log.json")),
        ("postmortem_001.md",           _load("postmortems/postmortem_001.md")),
        ("FOOTER_INSTRUCTION",          agent_module.FOOTER_INSTRUCTION),
    ]
    for name, content in components:
        chars = len(content)
        toks = _estimate_tokens(content)
        print(f"  {name:<40} {chars:>7,} chars  ≈ {toks:>6,} tokens")

    print(f"{'═'*78}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 20 — thesis-integrity agent (skill_2b / run_thesis_check)
# ═══════════════════════════════════════════════════════════════════════════════

class TestThesisCheck(TmpDirMixin):
    """The dedicated thesis agent is DECISION-ONLY: it parses a verdicts block and
    returns {symbol: 'thesis_break'} for names that are broken OR decayed below the
    trade floor (60). It must never raise into the loop — any failure => {}."""

    def _verdict_text(self, verdicts):
        return "prose...\n```json\n" + json.dumps({"verdicts": verdicts}) + "\n```"

    def _run(self, positions, model_return):
        self.tradelog["open_positions"] = positions
        self._flush_log()
        log = agent_module.load_trade_log()
        with patch.object(agent_module, "run_model",
                          return_value=(model_return, {"input_tokens": 1, "output_tokens": 1})):
            return agent_module.run_thesis_check(log, {})

    def test_flags_broken_thesis(self):
        text = self._verdict_text([
            {"symbol": "MU", "confidence": 80, "thesis_broken": False, "reason": "ok"},
            {"symbol": "LABU", "confidence": 30, "thesis_broken": True, "reason": "FDA reject"}])
        broken = self._run([self._open_pos("MU", pos_id="M1"),
                            self._open_pos("LABU", pos_id="L1")], text)
        self.assertEqual(broken, {"LABU": "thesis_break"})

    def test_flags_confidence_below_floor_even_if_not_broken(self):
        text = self._verdict_text([
            {"symbol": "MU", "confidence": 45, "thesis_broken": False, "reason": "downgrade"}])
        broken = self._run([self._open_pos("MU", pos_id="M1")], text)
        self.assertEqual(broken, {"MU": "thesis_break"})

    def test_intact_positions_return_empty(self):
        text = self._verdict_text([
            {"symbol": "MU", "confidence": 82, "thesis_broken": False, "reason": "ok"}])
        self.assertEqual(self._run([self._open_pos("MU", pos_id="M1")], text), {})

    def test_no_open_positions_short_circuits(self):
        """Empty book => {} and run_model is never called (no token spend)."""
        log = agent_module.load_trade_log()
        with patch.object(agent_module, "run_model") as rm:
            self.assertEqual(agent_module.run_thesis_check(log, {}), {})
        rm.assert_not_called()

    def test_model_error_returns_empty(self):
        broken = self._run([self._open_pos("MU", pos_id="M1")],
                           "(claude -p error rc=1: session limit)")
        self.assertEqual(broken, {})

    def test_malformed_output_returns_empty(self):
        broken = self._run([self._open_pos("MU", pos_id="M1")], "no json at all here")
        self.assertEqual(broken, {})

    def test_verdict_for_unheld_symbol_is_ignored(self):
        text = self._verdict_text([
            {"symbol": "TSLA", "confidence": 10, "thesis_broken": True, "reason": "x"}])
        self.assertEqual(self._run([self._open_pos("MU", pos_id="M1")], text), {})

    def test_never_raises_on_internal_error(self):
        """If run_model itself blows up, the exception is swallowed and {} returned."""
        self.tradelog["open_positions"] = [self._open_pos("MU", pos_id="M1")]
        self._flush_log()
        log = agent_module.load_trade_log()
        with patch.object(agent_module, "run_model", side_effect=RuntimeError("boom")):
            try:
                self.assertEqual(agent_module.run_thesis_check(log, {}), {})
            except Exception as e:
                self.fail(f"run_thesis_check must never raise, got {e!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 20b — run_agent wiring: thesis break -> force_sell (integration)
# ═══════════════════════════════════════════════════════════════════════════════

class TestThesisWiringIntegration(TmpDirMixin):
    """End-to-end through run_agent: on a market-hours cycle with the thesis gate
    stale, a thesis-break verdict must be folded into must_sell, override the smart
    skip, and reach the deterministic force_sell path — never trusting a footer."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmpdir, "logs"), exist_ok=True)
        # skill_2b file so run_thesis_check has its system prompt (content irrelevant
        # to the mock, which branches on the web= kwarg).
        with open(os.path.join(self.tmpdir, "skills", "skill_2b_thesis.md"), "w") as f:
            f.write("thesis agent")
        self.tradelog["open_positions"] = [self._open_pos("MU", entry=1000.0, shares=0.1,
                                                          pos_id="M1")]
        self._flush_log()

    def test_thesis_break_reaches_force_sell_and_writes_heartbeat(self):
        raw = {"MU": {"ok": True, "state": "BUY", "transition": "NO_ACTION",
                      "last_close": 990.0}}
        fake_signals = MagicMock()
        fake_signals.signals_with_raw.return_value = (raw, "MU: BUY")
        fake_signals.signal_for.return_value = {"ok": True, "last_close": 990.0}

        usage = {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

        def fake_run_model(system, user, **kwargs):
            if kwargs.get("web"):  # the thesis agent call
                return ('```json\n{"verdicts":[{"symbol":"MU","confidence":20,'
                        '"thesis_broken":true,"reason":"catalyst gone"}]}\n```', usage)
            # the main execution turn — benign footer (its claims are NOT trusted)
            return ('```json\n{"cash":10,"positions":[],"actions_taken":[]}\n```', usage)

        force = MagicMock(return_value=(True, 990.0))
        with patch.object(agent_module, "signals", fake_signals), \
             patch.object(agent_module, "active_skill",
                          return_value=("SKILL", "market_hours_check")), \
             patch.object(agent_module, "is_market_open", return_value=True), \
             patch.object(agent_module, "run_model", side_effect=fake_run_model), \
             patch.object(agent_module, "read_broker_state",
                          return_value=([{"symbol": "MU", "shares": 0.1}], set())), \
             patch.object(agent_module, "force_sell", force), \
             patch.object(agent_module, "process_cycle_state"), \
             patch.object(agent_module, "process_strategy_rewrite_queue"), \
             patch.object(agent_module, "_run_shadow_passes"):
            agent_module.run_agent()

        # force_sell fired for MU with the thesis-break reason
        force.assert_called_once()
        args = force.call_args[0]
        self.assertEqual(args[0], "MU")
        self.assertEqual(args[2], "thesis_break")
        # gate stamped + heartbeat written
        log = agent_module.load_trade_log()
        self.assertIn("last_thesis_check_ts", log["_state"])
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "logs", "heartbeat.json")))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 21 — ops / heartbeat (write_heartbeat + check_heartbeat_health)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeartbeat(TmpDirMixin):
    """Model-free ops watchdog. write_heartbeat records the cycle; the health check
    fires notify_operator on dark-loop / overdue-stop / repeated-failure, de-dupes a
    standing condition, and never raises."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmpdir, "logs"), exist_ok=True)

    def _hb(self):
        return json.load(open(os.path.join(self.tmpdir, "logs", "heartbeat.json")))

    def test_write_heartbeat_schema_and_failure_counter(self):
        log = agent_module.load_trade_log()
        with patch.object(agent_module, "signals", None):
            agent_module.write_heartbeat(log, "market_hours_check", model_failed=True)
            self.assertEqual(self._hb()["consecutive_model_failures"], 1)
            agent_module.write_heartbeat(log, "market_hours_check", model_failed=True)
            self.assertEqual(self._hb()["consecutive_model_failures"], 2)
            # a successful turn resets the counter
            agent_module.write_heartbeat(log, "market_hours_check", model_ran=True)
        hb = self._hb()
        self.assertEqual(hb["consecutive_model_failures"], 0)
        self.assertIn("last_cycle_ts", hb)
        self.assertEqual(hb["mode"], agent_module.EXECUTION_MODE)

    def test_write_heartbeat_never_raises(self):
        try:
            agent_module.write_heartbeat("not-a-log-dict", "task")
        except Exception as e:
            self.fail(f"write_heartbeat must never raise, got {e!r}")

    def test_dark_loop_alert_fires_once(self):
        old = (datetime.now(agent_module.ET) - timedelta(hours=40)).isoformat()
        hb = {"last_cycle_ts": old, "open_positions": [], "last_alerted": {}}
        json.dump(hb, open(os.path.join(self.tmpdir, "logs", "heartbeat.json"), "w"))
        with patch.object(agent_module, "notify_operator") as notif:
            agent_module.check_heartbeat_health(agent_module.load_trade_log(), at_startup=True)
            agent_module.check_heartbeat_health(agent_module.load_trade_log(), at_startup=True)
        notif.assert_called_once()  # de-duped on the second identical check

    def test_overdue_stop_alert(self):
        hb = {"last_cycle_ts": datetime.now(agent_module.ET).isoformat(),
              "open_positions": [{"symbol": "MU", "pnl_pct": -17.0, "stop_pct": 10.0,
                                  "past_stop": True, "cycles_past_stop": 2}],
              "last_alerted": {}}
        json.dump(hb, open(os.path.join(self.tmpdir, "logs", "heartbeat.json"), "w"))
        with patch.object(agent_module, "notify_operator") as notif:
            agent_module.check_heartbeat_health(agent_module.load_trade_log())
        notif.assert_called_once()
        self.assertIn("stop-loss", notif.call_args[0][0].lower())

    def test_overdue_stop_not_fired_after_one_cycle(self):
        """A position past-stop for only 1 cycle is within the force_sell retry window."""
        hb = {"last_cycle_ts": datetime.now(agent_module.ET).isoformat(),
              "open_positions": [{"symbol": "MU", "pnl_pct": -17.0, "stop_pct": 10.0,
                                  "past_stop": True, "cycles_past_stop": 1}],
              "last_alerted": {}}
        json.dump(hb, open(os.path.join(self.tmpdir, "logs", "heartbeat.json"), "w"))
        with patch.object(agent_module, "notify_operator") as notif:
            agent_module.check_heartbeat_health(agent_module.load_trade_log())
        notif.assert_not_called()

    def test_repeated_model_failures_alert(self):
        hb = {"last_cycle_ts": datetime.now(agent_module.ET).isoformat(),
              "open_positions": [], "consecutive_model_failures": 3, "last_alerted": {}}
        json.dump(hb, open(os.path.join(self.tmpdir, "logs", "heartbeat.json"), "w"))
        with patch.object(agent_module, "notify_operator") as notif:
            agent_module.check_heartbeat_health(agent_module.load_trade_log())
        notif.assert_called_once()
        self.assertIn("unavailable", notif.call_args[0][0].lower())

    def test_healthy_state_is_silent(self):
        hb = {"last_cycle_ts": datetime.now(agent_module.ET).isoformat(),
              "open_positions": [{"symbol": "MU", "pnl_pct": 5.0, "stop_pct": 10.0,
                                  "past_stop": False, "cycles_past_stop": 0}],
              "consecutive_model_failures": 0, "last_alerted": {}}
        json.dump(hb, open(os.path.join(self.tmpdir, "logs", "heartbeat.json"), "w"))
        with patch.object(agent_module, "notify_operator") as notif:
            agent_module.check_heartbeat_health(agent_module.load_trade_log())
        notif.assert_not_called()

    def test_health_check_never_raises_on_missing_file(self):
        try:
            agent_module.check_heartbeat_health(agent_module.load_trade_log())
        except Exception as e:
            self.fail(f"check_heartbeat_health must never raise, got {e!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — run tests + print token report
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 1. Run token report first (always visible)
    run_token_report()

    # 2. Run the test suite
    loader  = unittest.TestLoader()
    suite   = unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]
    )
    runner  = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1,
                                      stream=sys.stdout)
    result  = runner.run(suite)

    # 3. Readiness summary
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed

    print(f"\n{'═'*78}")
    print(f"  READINESS SUMMARY — {date.today()}")
    print(f"{'═'*78}")
    print(f"  Tests run   : {total}")
    print(f"  Passed      : {passed}")
    print(f"  Failed      : {failed}")
    if result.failures:
        print(f"\n  FAILURES:")
        for test, tb in result.failures:
            print(f"    ✗ {test}")
            for line in tb.strip().split("\n")[-3:]:
                print(f"        {line}")
    if result.errors:
        print(f"\n  ERRORS:")
        for test, tb in result.errors:
            print(f"    ✗ {test}")
            for line in tb.strip().split("\n")[-3:]:
                print(f"        {line}")
    print(f"\n  {'READY FOR LIVE' if failed == 0 else 'NOT READY — fix failures above'}")
    print(f"{'═'*78}\n")

    sys.exit(0 if failed == 0 else 1)
