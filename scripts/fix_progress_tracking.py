#!/usr/bin/env python3
"""Repair corrupted progress_tracking / trade_log summary baseline values.

ROOT CAUSE (found 2026-09-26): strategy/history/strategy_v44.json shows
progress_tracking jumping from a sane (month_start=395.455, current=367.7174)
at v43 to (123484.515, 123456.7774) at v44 -- change_events.jsonl tags v44 as
"Manual operator-directed merge (2026-08-04, applied via SSH, not the skill_5
pipeline)". current_value=123456.7774 is unmistakably a leaked placeholder
(123456.78, sequential digits) rather than a real broker read -- the same
class of bug as the 2026-08-03 test-state-leak false halt, this time landing
in a hand-merged strategy.json instead of a pytest run.

v46 (also un-audited -- no change_events.jsonl entry, consistent with it being
the same kind of direct SSH hand-edit that armed RX-3 live that day) then
tried to patch it: current_value was reset to a sane 366.1445, but
month_start_value was fat-fingered to -122719.97 instead of back to something
sane. Because agent.update_monthly_progress() unconditionally recomputes
current_value = month_start_value + total_pnl every cycle, that one bad
month_start_value silently poisoned current_value again on the very next
write (v48: -122768.06) and has stayed corrupted since -- it never
self-healed because nothing re-derives month_start_value from the real broker
total; only a deposit/withdrawal delta ever touches it, and
risk_guard.detect_deposit() refuses to fire once tracked_total goes negative
(t <= 0 guard), so the corruption was permanently sticky.

total_pnl / wins / losses / win_rate are untouched by any of this -- they are
maintained straight from real closed-trade fills and were never part of the
bad edit (same as the two prior corrections in trade_log.json's summary
block).

FIX: given the current REAL broker total (pass with --broker-total, read live
via the Robinhood MCP immediately before running this), solve
month_start_value = broker_total - total_pnl so that
current_value = month_start_value + total_pnl reproduces the real broker
total on this and every future cycle, instead of drifting from a poisoned
baseline. Applied to both strategy.json (progress_tracking, via
snapshot_strategy so it gets a version bump + history snapshot + change
event -- same pattern as scripts/apply_stop_loss.py) and trade_log.json
(summary, with a dated correction note in the same style as the two existing
_corrected_* notes already in that file).

Run ON the machine running the bot (the Oracle VM), from the repo root:

    python3 scripts/fix_progress_tracking.py --broker-total 374.780355724 --show
    python3 scripts/fix_progress_tracking.py --broker-total 374.780355724
"""

import argparse
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


def describe(strategy, log):
    pt = strategy.get("progress_tracking", {})
    s = log.get("summary", {})
    return {
        "strategy.progress_tracking": {
            "month_start_value": pt.get("month_start_value"),
            "current_value": pt.get("current_value"),
            "current_return": pt.get("current_return"),
        },
        "trade_log.summary": {
            "month_start_value": s.get("month_start_value"),
            "current_value": s.get("current_value"),
            "monthly_return_pct": s.get("monthly_return_pct"),
        },
        "total_pnl (unaffected, real trades)": s.get("total_pnl"),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker-total", type=float, required=True,
                    help="REAL current broker total_value (get_portfolio), read live "
                         "immediately before running this -- not a cached/old figure")
    ap.add_argument("--show", action="store_true", help="print current state and exit")
    args = ap.parse_args()

    strategy = agent.load_strategy()
    log = agent.load_trade_log()

    if args.show:
        print(json.dumps(describe(strategy, log), indent=2))
        return 0

    if args.broker_total <= 0:
        print(f"REFUSING: --broker-total must be positive, got {args.broker_total}",
              file=sys.stderr)
        return 2

    total_pnl = float(log.get("summary", {}).get("total_pnl") or 0)
    new_current = round(args.broker_total, 4)
    new_start = round(new_current - total_pnl, 4)

    before = describe(strategy, log)
    print("before:\n" + json.dumps(before, indent=2))

    # --- strategy.json: versioned, audited edit ---
    pt = strategy.setdefault("progress_tracking", {})
    old_start = pt.get("month_start_value")
    old_current = pt.get("current_value")
    pt["month_start_value"] = new_start
    pt["current_value"] = new_current
    monthly_return = ((new_current - new_start) / new_start * 100) if new_start else 0.0
    pt["current_return"] = f"{monthly_return:.1f}%"
    pt["on_track"] = monthly_return >= 0
    pt["_corrected_20260926"] = (
        f"2026-09-26: month_start_value/current_value repaired. Root cause: a "
        f"2026-08-04 manual SSH merge (v44) leaked a placeholder-looking value "
        f"(123456.78-style) into progress_tracking; a later un-audited hand-edit "
        f"(v46) fat-fingered month_start_value to -122719.97 while fixing "
        f"current_value back to ~366, and update_monthly_progress()'s "
        f"current=start+total_pnl recompute silently re-poisoned current_value "
        f"every cycle after that (last seen: start={old_start}, current={old_current}). "
        f"detect_deposit()'s t<=0 guard meant the usual auto-correction never fires "
        f"once current_value goes negative, so it never self-healed. Fixed by solving "
        f"month_start_value = real_broker_total - total_pnl against a live broker read "
        f"({new_current}), so current_value reproduces the real account total now and "
        f"stays consistent on every future cycle. total_pnl/wins/losses/win_rate "
        f"(real closed-trade history) were never touched by the original bug and are "
        f"untouched by this fix."
    )
    reason = (f"v{strategy.get('version', 1) + 1}: repaired progress_tracking baseline "
              f"corrupted since 2026-08-04/13 (month_start_value was -122719.97, six "
              f"orders of magnitude off a ~$375 account) -- see progress_tracking."
              f"_corrected_20260926 for the full root cause. month_start_value "
              f"{old_start} -> {new_start}, current_value {old_current} -> {new_current}, "
              f"solved against a live broker read so the value is correct going forward, "
              f"not just for this instant.")
    agent.snapshot_strategy(strategy, reason, [])
    agent.save_json("strategy/strategy.json", strategy)

    # --- trade_log.json: same correction, documented the same way as the two
    # existing _corrected_* notes already in this file's summary block ---
    s = log.setdefault("summary", {})
    s["month_start_value"] = new_start
    s["current_value"] = new_current
    s["monthly_return_pct"] = round(monthly_return, 2)
    s["on_track"] = monthly_return >= 0
    s["_corrected_20260926"] = pt["_corrected_20260926"]
    agent.save_trade_log(log)

    print("\nAPPLIED.\nafter:\n" + json.dumps(describe(strategy, log), indent=2))
    print(f"\nstrategy.json bumped to v{strategy['version']}; change event + history "
          f"snapshot written; trade_log.json summary corrected in place. "
          f"Date used for version_history: {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
