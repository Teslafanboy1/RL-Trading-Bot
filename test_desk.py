"""Claude Desk: Engine B + the Brain's scorecard (operator plan 2026-09-23)."""
import unittest
from datetime import date

import agent
from desk import engine_b as B, scorecard as S


def ramp(n, start=100.0, step=0.01):
    out, x = [], start
    for _ in range(n):
        out.append(x)
        x *= 1 + step
    return out


class TestEngineB(unittest.TestCase):
    def test_downtrend_is_ineligible(self):
        self.assertIsNone(B.score(ramp(200, step=-0.01)))

    def test_uptrend_scores(self):
        self.assertGreater(B.score(ramp(200)), 0)

    def test_short_history_is_ineligible(self):
        self.assertIsNone(B.score(ramp(50)))

    def test_picks_strongest_equal_weight(self):
        hist = {"A": ramp(200, step=0.02), "B": ramp(200, step=0.01),
                "C": ramp(200, step=0.005), "D": ramp(200, step=0.001),
                "E": ramp(200, step=-0.01)}
        tb = B.target_book(hist, top_n=3)
        self.assertEqual(set(tb["weights"]), {"A", "B", "C"})
        self.assertAlmostEqual(sum(tb["weights"].values()), 1.0, places=4)

    def test_nothing_eligible_is_all_cash(self):
        tb = B.target_book({"X": ramp(200, step=-0.01)})
        self.assertEqual(tb["weights"], {})

    def test_inverse_etfs_are_the_short_side(self):
        self.assertEqual(B.side_of("SQQQ"), "short")
        self.assertEqual(B.side_of("GLD"), "long")


class TestScorecard(unittest.TestCase):
    def test_long_hits_stop(self):
        c = S.make_call("e", "X", "long", 100, today=date(2026, 9, 1))
        g = S.grade(c, 89, today=date(2026, 9, 2))
        self.assertEqual((g["status"], g["result"]), ("closed", "stop"))
        self.assertAlmostEqual(g["ret"], -0.11)

    def test_long_hits_target(self):
        c = S.make_call("e", "X", "long", 100, today=date(2026, 9, 1))
        self.assertEqual(S.grade(c, 121, today=date(2026, 9, 2))["result"], "target")

    def test_open_stays_open_inside_band(self):
        c = S.make_call("e", "X", "long", 100, today=date(2026, 9, 1))
        self.assertEqual(S.grade(c, 105, today=date(2026, 9, 2))["status"], "open")

    def test_expiry_closes(self):
        c = S.make_call("e", "X", "long", 100, horizon_days=5, today=date(2026, 9, 1))
        g = S.grade(c, 103, today=date(2026, 9, 30))
        self.assertEqual((g["result"], g["ret"]), ("expired", 0.03))

    def test_short_direction_math(self):
        c = S.make_call("e", "X", "short", 100, today=date(2026, 9, 1))
        self.assertEqual(c["stop"], 110.0)
        self.assertEqual(S.grade(c, 79, today=date(2026, 9, 2))["result"], "target")

    def test_summary_and_brier(self):
        a = S.grade(S.make_call("e", "X", "long", 100, confidence=80,
                                today=date(2026, 9, 1)), 121, date(2026, 9, 2))
        b = S.grade(S.make_call("e", "Y", "long", 100, confidence=80,
                                today=date(2026, 9, 1)), 89, date(2026, 9, 2))
        s = S.summary([a, b])["e"]
        self.assertEqual((s["closed"], s["wins"], s["win_rate"]), (2, 1, 0.5))
        self.assertAlmostEqual(s["brier"], (0.2 ** 2 + 0.8 ** 2) / 2, places=4)

    def test_roundtrip(self):
        import os, tempfile
        p = os.path.join(tempfile.mkdtemp(), "sc.jsonl")
        c = S.make_call("e", "X", "long", 100)
        self.assertTrue(S.save([c], p))
        self.assertEqual(S.load(p), [c])


class TestDeskPaperStep(unittest.TestCase):
    def closes(self):
        return {"NVDA": ramp(200, step=0.02), "GLD": ramp(200, step=0.01),
                "SPY": ramp(200, step=0.005), "SQQQ": ramp(200, step=0.03),
                "USO": ramp(200, step=-0.01)}

    def test_long_only_variant_never_holds_inverse(self):
        paper, new = agent.desk_paper_step({"cash": 100.0, "start_equity": 100.0},
                                           self.closes(), False, "2026-09-23")
        self.assertNotIn("SQQQ", paper["positions"])
        self.assertEqual({s for s, _ in new}, {"NVDA", "GLD", "SPY"})

    def test_short_variant_can_hold_inverse(self):
        paper, _ = agent.desk_paper_step({"cash": 100.0, "start_equity": 100.0},
                                         self.closes(), True, "2026-09-23")
        self.assertIn("SQQQ", paper["positions"])

    def test_equity_conserved_minus_costs(self):
        paper, _ = agent.desk_paper_step({"cash": 100.0, "start_equity": 100.0},
                                         self.closes(), False, "2026-09-23")
        self.assertAlmostEqual(paper["equity"], 100.0 - 0.05, delta=0.01)

    def test_only_rebalances_on_schedule(self):
        paper, _ = agent.desk_paper_step({"cash": 100.0, "start_equity": 100.0},
                                         self.closes(), False, "d1")
        held = dict(paper["positions"])
        c2 = self.closes()
        c2["USO"] = ramp(200, step=0.05)     # new leader appears mid-week
        paper, new = agent.desk_paper_step(paper, c2, False, "d2")
        self.assertEqual(new, [])
        self.assertEqual(set(paper["positions"]), set(held))


from desk import engine_a as A


def mover(sym="X", price=20, chg=8, vol=3e6, avg=1e6):
    return {"symbol": sym, "price": price, "change_pct": chg, "volume": vol,
            "avg_volume": avg}


class TestEngineA(unittest.TestCase):
    def test_call_on_crowd_buying_an_uptrend(self):
        self.assertEqual(A.classify(mover(), ramp(80, step=0.005))[0], "call")

    def test_no_call_on_a_pop_inside_a_downtrend(self):
        self.assertEqual(A.classify(mover(), ramp(80, step=-0.005)),
                         (None, "pop_without_trend"))

    def test_no_trade_without_a_crowd(self):
        self.assertEqual(A.classify(mover(vol=1e6), ramp(80))[1], "no_crowd")

    def test_put_when_a_big_run_cracks(self):
        closes = ramp(80, step=0.02)          # ~+50% over the last month
        self.assertEqual(A.classify(mover(chg=-8), closes)[0], "put")

    def test_no_put_on_a_drop_without_a_prior_run(self):
        self.assertEqual(A.classify(mover(chg=-8), ramp(80, step=0.001))[0], None)

    def test_price_band(self):
        self.assertEqual(A.classify(mover(price=1.5), ramp(80))[1], "price_out_of_band")
        self.assertEqual(A.classify(mover(price=900), ramp(80))[1], "price_out_of_band")

    def test_rank_orders_by_crowd_strength_and_dedupes(self):
        c = {"A": ramp(80, step=0.005), "B": ramp(80, step=0.005)}
        r = A.rank([mover("A", chg=6, vol=2.5e6), mover("B", chg=12, vol=5e6),
                    mover("A", chg=6)], c)
        self.assertEqual([x["symbol"] for x in r], ["B", "A"])

    def test_exit_rules(self):
        pos = {"entry_premium": 1.0, "expiry": "2026-10-30", "entry_date": "2026-09-23"}
        d = date(2026, 9, 25)
        self.assertEqual(A.should_exit(pos, 2.05, d), (True, "take_profit"))
        self.assertEqual(A.should_exit(pos, 0.49, d), (True, "premium_stop"))
        self.assertEqual(A.should_exit(pos, 1.1, d), (False, ""))
        self.assertEqual(A.should_exit(pos, 1.1, date(2026, 10, 7)), (True, "time_stop"))
        self.assertEqual(A.should_exit(dict(pos, expiry="2026-09-30"), 1.1, d),
                         (True, "dte_expiry"))


if __name__ == "__main__":
    unittest.main()
