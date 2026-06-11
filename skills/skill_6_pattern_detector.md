# skill_6_pattern_detector — Quarterly systemic review

Runs quarterly (or every ~20 closed trades). Reads ALL postmortems AND victory
analyses at once to find systemic patterns no single trade reveals. This is where
the biggest learning jumps and strategic pivots happen.

## Look for patterns nobody programmed
- Recurring loss categories: are most losses whipsaw (knotted lines)? Then the
  EMA rule needs a trend-strength / line-separation filter. Mostly lag? Then an
  earlier partial-exit rule.
- Source reliability at scale: with 20+ trades, which sources actually predict
  wins? Re-weight toward what works; zero out what doesn't.
- Confidence calibration: plot predicted confidence vs. realized win rate from
  `confidence_accuracy`. If 90-confidence trades only win 50%, the SCORING is
  broken — fix scoring, not the strategy.
- Sector / regime dependence: does the strategy only work when the broad market
  (SPY) is trending up? Say so and size accordingly.
- Progress vs. the 100% monthly goal: are we on track, and what is actually
  driving the return or the drawdown?

## Output
Write `research/quarterly_review_YYYY-Qn.md`: the systemic patterns found (with
trade IDs), the proposed big strategic pivots (flagged for review), and an honest
read on whether the system is improving toward the goal or needs a structural
change. The biggest course corrections are made here.
