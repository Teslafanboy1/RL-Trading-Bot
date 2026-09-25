#!/usr/bin/env python3
"""Change the account-wide hard stop-loss threshold on the machine that owns the
book. Operator-directed 2026-09-16, after a weekend-gap MRVL exit realized a
14.5% loss despite the 10% stop (the poller can't beat a closed-market gap --
see risk_management.trailing_stop_note in strategy.json). Tightening the
percentage does NOT fix gap risk (nothing can sell while the market is
closed); it only reduces damage on slower intraday declines the poller *can*
catch. Position sizing (rotation.live.top_n / full_deploy, see
scripts/apply_rx4_sizing.py) is the actual gap defense.

WHY THIS IS A SCRIPT AND NOT A COMMIT: `strategy/strategy.json` is on the
deploy's PROTECTED_PATHS list -- the VM's live-learned copy always wins, so a
change committed to git would never reach the running bot. Hand-editing a
~900-line JSON file over SSH skips the version bump, the history snapshot,
and the change-event trail every other mutation in this repo goes through.
This does all three, the same way scripts/apply_rx4_sizing.py does for
rotation sizing.

Run it ON the machine running the bot (the Oracle VM), from the repo root:

    python3 scripts/apply_stop_loss.py --show              # current threshold
    python3 scripts/apply_stop_loss.py --pct 0.07           # tighten to 7%
    python3 scripts/apply_stop_loss.py --pct 0.10           # revert to 10%

Takes effect next cycle -- check_stop_loss_alerts() reads risk_management.stop_loss_pct
fresh from strategy.json every cycle, nothing is cached.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402  (imported for load_strategy/snapshot_strategy/save_json)


def describe(strategy):
    rm = strategy.get("risk_management") or {}
    return {
        "version": strategy.get("version"),
        "stop_loss_pct": rm.get("stop_loss_pct"),
        "trailing_stop_pct": rm.get("trailing_stop_pct"),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pct", type=float, default=None,
                    help="new stop_loss_pct as a fraction, e.g. 0.07 for 7%%")
    ap.add_argument("--show", action="store_true", help="print current threshold and exit")
    args = ap.parse_args()

    strategy = agent.load_strategy()
    before = describe(strategy)
    if args.show or args.pct is None:
        print(json.dumps(before, indent=2))
        return 0

    if not (0 < args.pct < 1):
        print(f"REFUSING: --pct must be a fraction in (0, 1), got {args.pct}", file=sys.stderr)
        return 2

    rm = strategy.setdefault("risk_management", {})
    old = rm.get("stop_loss_pct")
    if old == args.pct:
        print("Already at that threshold -- nothing to do.")
        print(json.dumps(before, indent=2))
        return 0

    rm["stop_loss_pct"] = args.pct
    reason = (
        "v{v} (operator-directed 2026-09-16): tightened risk_management.stop_loss_pct "
        f"{old} -> {args.pct}. Root cause: an Aug 31 MRVL exit realized a 14.5% loss "
        "(sold ~9:41am ET, 11min after Monday open) -- a weekend gap the poller "
        "could not react to before the market reopened, already past the old "
        f"{old} threshold. A tighter percentage does not close a closed-market gap; "
        "it only tightens the response to slower intraday declines the poller can "
        "actually see. Gap risk is bounded separately by rotation.live.top_n / "
        "full_deploy (position sizing), not by this field. "
        "trailing_stop_pct, exit_on_ribbon_sell, and every other risk_management "
        "field are unchanged."
    )
    agent.snapshot_strategy(strategy, reason.format(v=strategy.get("version", 1) + 1), [])
    agent.save_json("strategy/strategy.json", strategy)

    print(f"APPLIED: stop_loss_pct {old} -> {args.pct}")
    print("\nbefore:\n" + json.dumps(before, indent=2))
    print("\nafter:\n" + json.dumps(describe(strategy), indent=2))
    print("\nTakes effect next cycle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
