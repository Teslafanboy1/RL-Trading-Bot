#!/usr/bin/env python3
"""Arm (or disarm) RX-3 LIVE's full-deploy sizing on the machine that owns the
book. Operator-directed 2026-08-14.

WHY THIS IS A SCRIPT AND NOT A COMMIT: `strategy/strategy.json` is on the deploy's
PROTECTED_PATHS list — the VM's live-learned copy always wins, so a sizing change
committed to git would never reach the running bot. Hand-editing a ~900-line JSON
file over SSH is the obvious alternative and the worse one: it skips the version
bump, the history snapshot, and the change-event trail that every other mutation
in this repo goes through. This does all three.

Run it ON the machine running the bot (the Oracle VM), from the repo root:

    python3 scripts/apply_rx4_sizing.py --show           # what is armed now
    python3 scripts/apply_rx4_sizing.py --full-deploy --top-n 2
    python3 scripts/apply_rx4_sizing.py --throttled      # revert to plain RX-3

The change takes effect on the NEXT cycle: `rx3_decide_targets` treats the sizing
as part of its daily-decision cache key, so re-arming mid-session re-decides
immediately rather than leaving the book pointed at the old target until tomorrow.

What full-deploy changes (measured, research/edge_lab4_speed.py, 10y @ 5bps/side):

    RX-3 throttled, top2   22.1%/yr  Sharpe 0.87  maxDD 52%   0 kill-switch trips
    full-deploy,    top2   28.5%/yr  Sharpe 0.90  maxDD 63%   1 kill-switch trip
    full-deploy,    top4   26.9%/yr  Sharpe 1.04  maxDD 50%   0 kill-switch trips

It drops the RISKX defensive carve-out and the vol throttle, so the book stays
100% invested in the leaders instead of parking the throttled fraction in
GLD/TLT/XLU/XLP. The per-name leverage haircut on 3x ETFs still applies — that is
a safety rule, not a buying-power throttle — as do every stop, the daily order
cap, PAUSE, do_not_trade, and the monthly kill-switch.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402  (imported for snapshot_strategy + save_json + ROOT)


def describe(strategy):
    r = (strategy.get("rotation") or {})
    live = (r.get("live") or {})
    armed = (str(r.get("mode", "paper")).lower() == "live"
             and bool(live.get("enabled", False)))
    return {
        "version": strategy.get("version"),
        "rotation_live_armed": armed,
        "full_deploy": bool(live.get("full_deploy", False)),
        "top_n": live.get("top_n") or "engine default (2)",
        "recycle_proceeds_same_cycle": live.get(
            "recycle_proceeds_same_cycle", "(code default: true)"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full-deploy", action="store_true",
                      help="stay 100%% invested in the leaders (no RISKX carve-out, "
                           "no vol throttle)")
    mode.add_argument("--throttled", action="store_true",
                      help="revert to plain RX-3 (RISKX + vol throttle back on)")
    ap.add_argument("--top-n", type=int, default=None,
                    help="how many leaders to hold (omit to leave unchanged)")
    ap.add_argument("--show", action="store_true", help="print current sizing and exit")
    args = ap.parse_args()

    strategy = agent.load_strategy()
    before = describe(strategy)
    if args.show or not (args.full_deploy or args.throttled or args.top_n):
        print(json.dumps(before, indent=2))
        return 0

    rot = strategy.setdefault("rotation", {})
    live = rot.setdefault("live", {})
    if not before["rotation_live_armed"]:
        # Sizing is meaningless while the deterministic desk is not the thing
        # trading. Refuse rather than write a setting that silently does nothing.
        print("REFUSING: RX-3 live is not armed on this machine "
              f"(rotation.mode={rot.get('mode')!r}, live.enabled="
              f"{live.get('enabled')!r}). Nothing changed.", file=sys.stderr)
        return 2

    changes = []
    if args.full_deploy and not live.get("full_deploy"):
        live["full_deploy"] = True
        changes.append("full_deploy false -> true")
    if args.throttled and live.get("full_deploy"):
        live["full_deploy"] = False
        changes.append("full_deploy true -> false")
    if args.top_n is not None and live.get("top_n") != args.top_n:
        changes.append(f"top_n {live.get('top_n')} -> {args.top_n}")
        live["top_n"] = args.top_n
    if not changes:
        print("Already in that state — nothing to do.")
        print(json.dumps(before, indent=2))
        return 0

    reason = ("v{v} (operator-directed 2026-08-14): RX-3 live sizing — "
              + "; ".join(changes) +
              ". Measured in research/edge_lab4_speed.py over 10y at 5bps/side, "
              "driving rotation_engine.target_book() itself: throttled top2 "
              "22.1%/yr (Sharpe 0.87, maxDD 52%) vs full-deploy top2 28.5%/yr "
              "(Sharpe 0.90, maxDD 63%, one 25%-monthly kill-switch trip in 10y). "
              "Operator chose the higher-return/higher-drawdown book knowingly, "
              "consistent with the 2026-08-04 RX-4 top_n=2 decision. Drops the "
              "RISKX defensive carve-out and the vol throttle ONLY; the 3x-ETF "
              "leverage haircut, hard/trailing stops, daily order cap, PAUSE, "
              "do_not_trade and the monthly kill-switch are all unchanged.")
    agent.snapshot_strategy(strategy, reason.format(v=strategy.get("version", 1) + 1), [])
    agent.save_json("strategy/strategy.json", strategy)

    print("APPLIED: " + "; ".join(changes))
    print("\nbefore:\n" + json.dumps(before, indent=2))
    print("\nafter:\n" + json.dumps(describe(strategy), indent=2))
    print("\nTakes effect next cycle (the sizing is part of the decision cache key).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
