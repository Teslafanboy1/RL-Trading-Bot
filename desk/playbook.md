# Claude Desk — Playbook

The Brain reads this first, every run. Each lesson is backed by evidence from
this account or its test harnesses. A rule changes only after the same mistake
shows up 3 times, but a broken mechanism gets fixed the day it's found.

## Mechanism lessons (fixed immediately)
- **A bot that can't see the account is worse than a stopped bot.** 2026-08-31 → 09-22:
  the VM's Claude login expired, every broker read failed, no orders, no stops,
  heartbeat still fresh. Nobody was alerted. → watchdog BLIND check (2026-09-23).
  An alert webhook MUST be configured, or every alert is written to a log nobody reads.
- **Stops fill after the gap, not at the stop.** MU 2026-07-06: −14.55% on a −10% stop.
  Aug 31: −14.5%. Size positions assuming the stop slips ~5%.
- **Never trust a self-reported fill.** 2026-06-12: the model reported sells it never placed.
  Only the broker read counts.

## Strategy lessons (measured)
- **Speed does not add edge.** Shorter lookbacks lost to their baseline even at zero
  cost (edge_lab4, 2026-08-14).
- **Tight stops lose.** A 3% portfolio stop fired 147–326 times in 10y and lowered
  returns in every setup (2026-09-23).
- **Inverse ETFs as the short side hurt.** Engine B 10y: maxDD 79% with inverses,
  61% without (2026-09-23). Both variants are on paper so live data can overturn this.
- **Leverage on concentration destroys accounts.** All-in top-1 at 3x: $100 → $7 over 10y.
- **Aggressive full-deploy top-2 (RX-4 paper) is −17.8% since Aug 4** holding AMD/SOXL.
- **Hindsight lists inflate backtests.** Any universe picked from today's winners
  (NVDA, PLTR, COIN, HOOD...) overstates the past. Discount such numbers heavily.

## Open questions the scorecard must answer
- Does Engine B beat the core rotation on live paper, net of costs?
- Does the short variant (B_short) ever earn its drawdown?
- Crowd Hunter (Engine A): do options on momentum names with a catalyst make money
  after the bid/ask spread?
