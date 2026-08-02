"""Tests for rotation_engine.py (RX-3 brain) and risk_guard.py."""

import os
import math
import json
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import rotation_engine as re_
import risk_guard as rg

ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------- synthetic data
def geo_series(n, total_ret, start=100.0):
    """n closes compounding smoothly to (1+total_ret)."""
    g = (1 + total_ret) ** (1 / (n - 1))
    return [start * g ** i for i in range(n)]


def leader_series(n=300):
    """Strong, clean uptrend: passes every gate (~+14%/21 bars, > 8% anchor)."""
    return geo_series(n, 6.0)


def laggard_series(n=300):
    """Mild uptrend that fails the 1m anchor (+8%)."""
    return geo_series(n, 0.10)


def downtrend_series(n=300):
    return geo_series(n, -0.4)


# ------------------------------------------------------------- selection
class TestSelection:
    def test_momentum_rank_positive_for_uptrend(self):
        assert re_.momentum_rank(leader_series()) > 0

    def test_rank_orders_by_strength(self):
        assert re_.momentum_rank(leader_series()) > re_.momentum_rank(laggard_series())

    def test_eligible_leader(self):
        ok, reason = re_.is_eligible(leader_series())
        assert ok, reason

    def test_ineligible_weak_anchor(self):
        ok, reason = re_.is_eligible(laggard_series())
        assert not ok and "anchor" in reason

    def test_ineligible_downtrend(self):
        ok, reason = re_.is_eligible(downtrend_series())
        assert not ok

    def test_ineligible_short_history(self):
        ok, reason = re_.is_eligible(geo_series(50, 0.5))
        assert not ok

    def test_select_top2(self):
        u = {"AAA": geo_series(300, 9.0), "BBB": geo_series(300, 6.0),
             "CCC": geo_series(300, 4.0), "DDD": laggard_series()}
        picks, scored = re_.select_leaders(u)
        assert picks == ["AAA", "BBB"]
        assert "DDD" not in [s for _, s in scored]

    def test_select_keeps_eligible_held(self):
        # held CCC is 3rd by rank but within top 2*top_n and still eligible -> kept
        u = {"AAA": geo_series(300, 9.0), "BBB": geo_series(300, 6.0),
             "CCC": geo_series(300, 4.0)}
        picks, _ = re_.select_leaders(u, held=["CCC"])
        assert "CCC" in picks and "AAA" in picks

    def test_sector_cap(self):
        u = {"AAA": geo_series(300, 9.0), "BBB": geo_series(300, 6.0),
             "CCC": geo_series(300, 4.0)}
        picks, _ = re_.select_leaders(
            u, sector_of=lambda s: "semis", max_per_sector=1)
        assert len(picks) == 1


# ------------------------------------------------------------- riskx / throttle
class TestRiskx:
    def _cc(self, direction):
        up = geo_series(60, 0.3)
        dn = geo_series(60, -0.3)
        flat = geo_series(60, 0.0)
        if direction == "on":
            return {"HYG": up, "IEF": flat, "XLY": up, "XLP": flat,
                    "CPER": up, "GLD": flat, "IWM": up, "SPY": flat,
                    "BTC-USD": up}
        return {"HYG": dn, "IEF": flat, "XLY": dn, "XLP": flat,
                "CPER": dn, "GLD": flat, "IWM": dn, "SPY": flat,
                "BTC-USD": dn}

    def test_all_on(self):
        assert re_.riskx_score(self._cc("on")) == 1.0

    def test_all_off(self):
        assert re_.riskx_score(self._cc("off")) == 0.25

    def test_missing_data_neutral(self):
        # all components missing -> 5 * 0.5 = 2.5 -> maps to 0.5
        assert re_.riskx_score({}) == 0.5

    def test_vol_scale_calm(self):
        assert re_.vol_scale([0.001] * 30) == 1.0

    def test_vol_scale_throttles_high_vol(self):
        rets = [0.06 if i % 2 else -0.06 for i in range(30)]
        s = re_.vol_scale(rets)
        assert 0 < s < 0.6

    def test_vol_scale_warmup(self):
        assert re_.vol_scale([0.05] * 5) == 1.0


class TestDefensive:
    def test_picks_strongest_positive(self):
        d = {"GLD": geo_series(60, 0.10), "TLT": geo_series(60, 0.02),
             "XLU": geo_series(60, -0.05)}
        pick, mom = re_.defensive_pick(d)
        assert pick == "GLD" and mom > 0

    def test_none_when_all_negative(self):
        d = {"GLD": geo_series(60, -0.1), "TLT": geo_series(60, -0.2)}
        pick, _ = re_.defensive_pick(d)
        assert pick is None


class TestTargetBook:
    def _inputs(self, riskx="on"):
        u = {"AAA": geo_series(300, 9.0), "BBB": geo_series(300, 6.0)}
        up = geo_series(60, 0.3); dn = geo_series(60, -0.3); flat = geo_series(60, 0.0)
        cc = ({"HYG": up, "IEF": flat, "XLY": up, "XLP": flat, "CPER": up,
               "GLD": flat, "IWM": up, "SPY": flat, "BTC-USD": up}
              if riskx == "on" else
              {"HYG": dn, "IEF": flat, "XLY": dn, "XLP": flat, "CPER": dn,
               "GLD": flat, "IWM": dn, "SPY": flat, "BTC-USD": dn})
        dc = {"GLD": geo_series(60, 0.08), "TLT": geo_series(60, 0.02)}
        return u, cc, dc

    def test_risk_on_full_book(self):
        u, cc, dc = self._inputs("on")
        tb = re_.target_book(u, cc, dc, [0.001] * 30)
        assert tb["leaders"] == ["AAA", "BBB"]
        assert tb["riskx"] == 1.0
        assert abs(tb["weights"]["AAA"] - 0.5) < 0.01
        assert tb["defensive"] == {}
        total = sum(tb["weights"].values()) + sum(tb["defensive"].values()) + tb["cash"]
        assert abs(total - 1.0) < 0.01

    def test_risk_off_defensive_sleeve(self):
        u, cc, dc = self._inputs("off")
        tb = re_.target_book(u, cc, dc, [0.001] * 30)
        assert tb["riskx"] == 0.25
        assert tb["defensive"].get("GLD") == pytest.approx(0.75, abs=0.01)
        assert sum(tb["weights"].values()) <= 0.26

    def test_leverage_haircut(self):
        u, cc, dc = self._inputs("on")
        tb = re_.target_book(u, cc, dc, [0.001] * 30,
                             leverage_factor=lambda s: 3.0 if s == "AAA" else 1.0)
        assert tb["weights"]["AAA"] == pytest.approx(tb["weights"]["BBB"] / 3, abs=0.01)

    def test_weights_never_exceed_one(self):
        u, cc, dc = self._inputs("off")
        tb = re_.target_book(u, cc, dc, [0.06, -0.06] * 15)
        total = sum(tb["weights"].values()) + sum(tb["defensive"].values()) + tb["cash"]
        assert total <= 1.001


# ------------------------------------------------------------- risk guard
class TestRiskGuard:
    @pytest.fixture(autouse=True)
    def tmp_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rg, "HALT_FILE", str(tmp_path / "HALT"))
        monkeypatch.setattr(rg, "STATE_FILE", str(tmp_path / "risk_state.json"))
        monkeypatch.setattr(rg, "HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
        self.tmp = tmp_path

    def test_beat_writes_file(self):
        assert rg.beat("test")
        assert os.path.exists(rg.HEARTBEAT_FILE)

    def test_ok_within_budget(self):
        st, _ = rg.check_halt(100.0)
        assert st == "ok"
        st, _ = rg.check_halt(90.0)      # -10% from peak
        assert st == "ok"

    def test_halt_on_25pct_drawdown(self):
        rg.check_halt(100.0)
        st, detail = rg.check_halt(74.0)  # -26%
        assert st == "halt"
        assert rg.halted()
        st2, _ = rg.check_halt(74.0)
        assert st2 == "halted"            # sticky until file removed

    def test_peak_ratchets_up(self):
        rg.check_halt(100.0)
        rg.check_halt(200.0)
        st, _ = rg.check_halt(155.0)      # -22.5% from 200 peak
        assert st == "ok"
        st, _ = rg.check_halt(149.0)      # -25.5%
        assert st == "halt"

    def test_month_rollover_resets_peak(self):
        jan = datetime(2026, 1, 30, 10, 0, tzinfo=ET)
        feb = datetime(2026, 2, 2, 10, 0, tzinfo=ET)
        rg.check_halt(200.0, now=jan)
        st, _ = rg.check_halt(160.0, now=feb)   # new month: peak resets to 160
        assert st == "ok"
        st, _ = rg.check_halt(118.0, now=feb)   # -26% from 160
        assert st == "halt"

    def test_missing_equity_never_halts(self):
        st, _ = rg.check_halt(None)
        assert st == "ok"
        st, _ = rg.check_halt(0)
        assert st == "ok"

    def test_detect_deposit(self):
        assert rg.detect_deposit(395.54, 255.55) == pytest.approx(139.99, abs=0.01)
        assert rg.detect_deposit(101.0, 100.0) == 0.0          # noise, not deposit
        assert rg.detect_deposit(70.0, 100.0) == -30.0         # withdrawal
        assert rg.detect_deposit(None, 100.0) == 0.0

    def test_rebase_peak_withdrawal_avoids_false_halt(self):
        # 2026-07-22 scenario: peak 392.53, then a ~$120 withdrawal alone would
        # read as a 30.5% drawdown and wrongly trip the halt.
        rg.check_halt(392.53)
        rg.rebase_peak(-119.78)      # withdrawal detected same cycle
        st, _ = rg.check_halt(272.75)
        assert st == "ok"

    def test_rebase_peak_deposit_shifts_peak_up(self):
        rg.check_halt(100.0)
        rg.rebase_peak(50.0)         # deposit
        st, _ = rg.check_halt(115.0)  # would be +15% from 100 but is -23% from 150
        assert st == "ok"
        st, _ = rg.check_halt(110.0)  # -26.7% from rebased 150 peak
        assert st == "halt"

    def test_rebase_peak_noop_without_existing_state(self):
        rg.rebase_peak(-100.0)       # no state file yet this month
        assert not os.path.exists(rg.STATE_FILE)

    def test_rebase_peak_noop_while_halted(self):
        rg.check_halt(100.0)
        rg.check_halt(70.0)          # trips halt
        assert rg.halted()
        rg.rebase_peak(-50.0)        # must not move the goalposts mid-halt
        with open(rg.STATE_FILE) as f:
            st = json.load(f)
        assert st["peak"] == 100.0

    def test_rebase_peak_zero_delta_noop(self):
        rg.check_halt(100.0)
        rg.rebase_peak(0.0)
        with open(rg.STATE_FILE) as f:
            st = json.load(f)
        assert st["peak"] == 100.0
