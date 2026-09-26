"""Tests for the Polymarket paper desk: desk/engine_p.py (pure) and the
polymarket_desk.py runner (network and model calls mocked).

    python3 -m unittest test_pm_desk
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("USAGE_STATE_FILE",
                      os.path.join(tempfile.gettempdir(), "pm_desk_test_usage.json"))

import polymarket_desk as desk          # noqa: E402
from desk import engine_p               # noqa: E402

NOW = 1_790_000_000.0                   # a fixed "now" (epoch seconds)
DAY = 86400.0


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(mid="1", bid=0.40, ask=0.42, days=3.0, liq=20000, vol=10000, **kw):
    m = {"id": mid, "question": f"Q{mid}?", "active": True, "closed": False,
         "acceptingOrders": True, "enableOrderBook": True,
         "bestBid": bid, "bestAsk": ask, "liquidityNum": liq, "volume24hr": vol,
         "endDate": _iso(NOW + days * DAY), "orderMinSize": 5,
         "feesEnabled": True, "feeSchedule": {"rate": 0.04}, "description": "rules"}
    m.update(kw)
    return m


def event(eid="e1", markets=None, tags=("Politics",)):
    return {"id": eid, "title": f"E{eid}", "tags": [{"label": t} for t in tags],
            "markets": markets or [market()]}


class TestEligibility(unittest.TestCase):
    def ok(self, m):
        return engine_p.eligible_market(m, NOW, desk.iso_ts)

    def test_a_normal_market_is_eligible(self):
        self.assertTrue(self.ok(market()))

    def test_rejections(self):
        self.assertFalse(self.ok(market(closed=True)))
        self.assertFalse(self.ok(market(acceptingOrders=False)))
        self.assertFalse(self.ok(market(bid=0.30, ask=0.40)))        # 10c spread
        self.assertFalse(self.ok(market(bid=0.96, ask=0.97)))        # near-certain
        self.assertFalse(self.ok(market(bid=0.02, ask=0.03)))        # longshot
        self.assertFalse(self.ok(market(liq=100)))
        self.assertFalse(self.ok(market(vol=10)))
        self.assertFalse(self.ok(market(days=0.1)))                  # too close
        self.assertFalse(self.ok(market(days=45)))                   # too far
        self.assertFalse(self.ok(market(bid=None)))


class TestSelection(unittest.TestCase):
    def test_excluded_categories_are_skipped(self):
        c = engine_p.select_candidates(
            [event("s", tags=("Sports", "NFL")), event("w", tags=("Weather",)),
             event("p", tags=("Politics",))], NOW, desk.iso_ts)
        self.assertEqual([x["event_id"] for x in c], ["p"])

    def test_one_market_per_event_the_busiest(self):
        e = event("e", markets=[market("a", vol=3000), market("b", vol=9000)])
        c = engine_p.select_candidates([e], NOW, desk.iso_ts)
        self.assertEqual([x["market_id"] for x in c], ["b"])

    def test_already_forecast_markets_are_not_repeated(self):
        c = engine_p.select_candidates([event(markets=[market("1")])], NOW,
                                       desk.iso_ts, exclude_ids=["1"])
        self.assertEqual(c, [])

    def test_ranked_by_current_volume_and_limited(self):
        evs = [event(str(i), markets=[market(str(i), vol=3000 + i)], tags=(f"t{i}",))
               for i in range(15)]
        c = engine_p.select_candidates(evs, NOW, desk.iso_ts, limit=4)
        self.assertEqual([x["market_id"] for x in c], ["14", "13", "12", "11"])

    def test_correlated_events_are_capped_by_shared_tag(self):
        """2026-09-26 live pick: five separate 'Lula ...' events in one batch."""
        evs = [event(f"b{i}", markets=[market(f"b{i}", vol=9000 - i)],
                     tags=("Brazil", "Elections")) for i in range(5)]
        evs.append(event("x", markets=[market("x", vol=2500)], tags=("Box Office",)))
        c = engine_p.select_candidates(evs, NOW, desk.iso_ts)
        self.assertEqual([x["market_id"] for x in c], ["b0", "b1", "x"])

    def test_broad_tags_are_not_capped(self):
        evs = [event(f"p{i}", markets=[market(f"p{i}", vol=9000 - i)],
                     tags=("Politics", f"topic{i}")) for i in range(5)]
        c = engine_p.select_candidates(evs, NOW, desk.iso_ts)
        self.assertEqual(len(c), 5)

    def test_fee_rate_only_when_fees_enabled(self):
        c = engine_p.select_candidates(
            [event(markets=[market(feesEnabled=False)])], NOW, desk.iso_ts)
        self.assertEqual(c[0]["fee_rate"], 0.0)


class TestDecide(unittest.TestCase):
    CAND = {"bid": 0.40, "ask": 0.42, "fee_rate": 0.04, "min_shares": 5.0}

    def test_fee_formula(self):
        self.assertAlmostEqual(engine_p.fee_per_share(0.5, 0.04), 0.01)
        self.assertEqual(engine_p.fee_per_share(0.5, None), 0.0)

    def test_confident_yes_buys_yes_at_the_ask(self):
        bet = engine_p.decide(self.CAND, 0.70, 100.0)
        self.assertEqual(bet["side"], "YES")
        self.assertEqual(bet["price"], 0.42)
        self.assertGreater(bet["edge"], engine_p.MIN_EDGE)

    def test_confident_no_buys_no_at_one_minus_bid(self):
        bet = engine_p.decide(self.CAND, 0.15, 100.0)
        self.assertEqual(bet["side"], "NO")
        self.assertEqual(bet["price"], 0.60)

    def test_small_disagreement_is_not_bet(self):
        # 0.47 vs 0.42 ask + ~1c fee: under the 7-point bar
        self.assertIsNone(engine_p.decide(self.CAND, 0.47, 100.0))

    def test_low_confidence_is_never_bet(self):
        self.assertIsNone(engine_p.decide(self.CAND, 0.95, 100.0, "low"))

    def test_bad_probability_is_rejected(self):
        for p in (None, "x", 1.5, -0.1):
            self.assertIsNone(engine_p.decide(self.CAND, p, 100.0))

    def test_stake_is_capped(self):
        bet = engine_p.decide(self.CAND, 0.99, 100.0)
        self.assertLessEqual(bet["stake"], engine_p.MAX_BET_PCT * 100.0 + 1e-9)

    def test_below_minimum_order_size_is_skipped(self):
        # $2 of equity: 5% = $0.10 -> far under 5 shares
        self.assertIsNone(engine_p.decide(self.CAND, 0.90, 2.0))


class TestResolutionAndScore(unittest.TestCase):
    def test_resolution_states(self):
        self.assertEqual(engine_p.resolution({"closed": True, "outcomePrices": '["1", "0"]'}),
                         (True, 1.0))
        self.assertEqual(engine_p.resolution({"closed": True, "outcomePrices": ["0", "1"]}),
                         (True, 0.0))
        self.assertEqual(engine_p.resolution({"closed": True, "outcomePrices": '["0.5","0.5"]'}),
                         (True, 0.5))
        self.assertEqual(engine_p.resolution({"closed": False, "outcomePrices": '["1","0"]'}),
                         (False, None))
        # closed but not yet final (still trading at 0.97) is NOT resolved
        self.assertEqual(engine_p.resolution({"closed": True, "outcomePrices": '["0.97","0.03"]'}),
                         (False, None))
        self.assertEqual(engine_p.resolution(None), (False, None))

    def test_settle_both_sides(self):
        pos = {"side": "YES", "shares": 10.0, "stake": 4.3}
        self.assertEqual(engine_p.settle(pos, 1.0), (10.0, 5.7))
        self.assertEqual(engine_p.settle(pos, 0.0), (0.0, -4.3))
        no = {"side": "NO", "shares": 10.0, "stake": 6.1}
        self.assertEqual(engine_p.settle(no, 0.0), (10.0, 3.9))

    def test_scorecard_compares_model_and_market_brier(self):
        book = {"cash": 100.0, "positions": [], "forecasts": [
            {"p_model": 0.9, "p_market": 0.6, "yes_payout": 1.0},
            {"p_model": 0.2, "p_market": 0.4, "yes_payout": 0.0},
            {"p_model": 0.5, "p_market": 0.5, "yes_payout": None}]}
        s = engine_p.scorecard(book)
        self.assertEqual(s["forecasts_resolved"], 2)
        self.assertEqual(s["forecasts_open"], 1)
        self.assertLess(s["brier_model"], s["brier_market"])


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book_path = os.path.join(self.tmp, "pm_desk_p.json")
        self.p = patch.object(desk, "BOOK_PATH", self.book_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _run(self, events, verdicts, markets=None, force=True):
        with patch.object(desk, "fetch_open_events", return_value=events), \
             patch.object(desk, "fetch_market", side_effect=lambda i: (markets or {}).get(i)), \
             patch.object(desk, "brain_forecast", return_value=verdicts) as bf, \
             patch.object(desk.time, "time", return_value=NOW):
            book = desk.run_once(force_open=force)
        return book, bf

    def test_forecasts_are_recorded_and_a_strong_one_is_bet(self):
        evs = [event("a", markets=[market("1", bid=0.40, ask=0.42)]),
               event("b", markets=[market("2", bid=0.40, ask=0.42)])]
        book, _ = self._run(evs, {"1": {"p_yes": 0.80, "confidence": "high", "reason": "r"},
                                  "2": {"p_yes": 0.44, "confidence": "medium"}})
        self.assertEqual(len(book["forecasts"]), 2)          # every forecast is kept
        self.assertEqual([p["market_id"] for p in book["positions"]], ["1"])
        self.assertLess(book["cash"], engine_p.START_EQUITY)
        self.assertTrue(os.path.exists(self.book_path))

    def test_failed_brain_call_opens_nothing_but_stamps_the_attempt(self):
        book, _ = self._run([event()], {})
        self.assertEqual(book["positions"], [])
        self.assertIsNotNone(book["last_open_ts"])           # no retry every 30 min

    def test_open_pass_is_rate_limited(self):
        self._run([event()], {})
        _, bf = self._run([event("z", markets=[market("9")])], {}, force=False)
        bf.assert_not_called()

    def test_resolution_pays_out_and_grades(self):
        evs = [event("a", markets=[market("1", bid=0.40, ask=0.42)])]
        book, _ = self._run(evs, {"1": {"p_yes": 0.85, "confidence": "high"}})
        stake = book["positions"][0]["stake"]
        book, _ = self._run([], {}, markets={"1": {"closed": True, "outcomePrices": '["1","0"]'}},
                            force=False)
        pos = book["positions"][0]
        self.assertEqual(pos["status"], "closed")
        self.assertGreater(pos["pnl"], 0)
        self.assertAlmostEqual(book["cash"], engine_p.START_EQUITY - stake + pos["payout"], 3)
        self.assertEqual(book["forecasts"][0]["yes_payout"], 1.0)
        self.assertEqual(book["scorecard"]["bets_closed"], 1)

    def test_the_brain_is_never_shown_the_price(self):
        cands = engine_p.select_candidates(
            [event(markets=[market("1", bid=0.3712, ask=0.3791)])], NOW, desk.iso_ts)
        with patch.object(desk.agent, "run_model", return_value=("```json\n{}\n```", {})) as rm:
            desk.brain_forecast(cands)
        system, user = rm.call_args[0][:2]
        for leak in ("0.3712", "0.3791", "0.375"):
            self.assertNotIn(leak, system + user)
        self.assertEqual(rm.call_args.kwargs["tier"], desk.agent.TIER_SHADOW)

    def test_a_failed_brain_call_is_reported_not_parsed(self):
        """2026-09-26 smoke test: the CLI's error text carries JSON, which used
        to parse as an empty 'forecast' and fail with no message at all."""
        cands = engine_p.select_candidates([event(markets=[market("1")])], NOW, desk.iso_ts)
        err = '(claude -p error rc=1: {"is_error":true,"result":"Failed to authenticate"})'
        for text in (err, "(error: usage-governor deferred this call)",
                     '```json\n{"other": {"p_yes": 0.5}}\n```'):
            with patch.object(desk.agent, "run_model", return_value=(text, {})), \
                 patch("builtins.print") as out:
                self.assertEqual(desk.brain_forecast(cands), {})
            self.assertTrue(out.called, text)

    def test_a_good_brain_reply_is_returned(self):
        cands = engine_p.select_candidates([event(markets=[market("1")])], NOW, desk.iso_ts)
        reply = '```json\n{"1": {"p_yes": 0.7, "confidence": "high", "reason": "x"}}\n```'
        with patch.object(desk.agent, "run_model", return_value=(reply, {})):
            self.assertEqual(desk.brain_forecast(cands)["1"]["p_yes"], 0.7)

    def test_book_write_is_atomic_json(self):
        desk.save_book({"x": 1})
        with open(self.book_path) as f:
            self.assertEqual(json.load(f), {"x": 1})
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
