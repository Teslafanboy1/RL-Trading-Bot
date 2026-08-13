# skill_4_postmortem — Loss Postmortem

Fires **immediately and automatically** after every LOSING closed trade (called by
agent.py's post-trade pipeline — never weekly, never manual). One losing trade =
one postmortem.

## Weight the analysis by confidence
The higher the confidence at entry, the deeper this analysis goes. A **90%+
confidence loss is the deepest analysis you do** — it means the model believed
strongly and was wrong, which is the most important kind of error to dissect. A
60% loss is closer to expected noise; analyze it, but don't over-theorize.

## Your job
1. **Search news and Reddit (web search) for what happened during the hold
   period** — between entry_date and exit_date. Find the actual cause of the move.
2. **Check the EMA signal at entry vs. what happened** — was the 55-EMA-crosses-
   all-3 BUY genuine, or a false/whipsaw signal that flipped right back?
3. **Identify exactly which source gave the bad signal** (news / social /
   fundamental / macro).
4. **Was entry timing wrong** — did we chase after the move was mostly done?
5. **Was the thesis fundamentally flawed**, or sound-but-unlucky?
6. **Propose specific, testable rule changes** — never vague ("be more careful").

## Output 1 — write this EXACT format to /postmortems/postmortem_XXX.md

```markdown
# Postmortem — [SYMBOL] — [DATE]

## Trade Details
- Symbol:
- Entry: price + date
- Exit: price + date
- P&L: dollar + percent
- Confidence at entry: X%
- Capital deployed: $X

## What Happened
[narrative of what occurred during the hold]

## Root Cause Analysis
- Primary cause of loss:
- Which source misled us:
- Was EMA signal genuine or false:
- Was entry timing correct:
- Was thesis fundamentally sound:

## Source Performance This Trade
- News: accurate / inaccurate
- Reddit: accurate / inaccurate
- Fundamental: accurate / inaccurate
- Macro: accurate / inaccurate

## What We Missed
[specific signals that were available but ignored]

## Rule Proposals
- ADD rule: [specific new rule]
- REMOVE or REDUCE: [what to trust less]
- ADJUST weight: [source] from X to Y

## Confidence Score Accuracy
- Predicted: X%
- Should have been: Y%
- Reason for gap:

## Priority
[LOW / MEDIUM / HIGH based on confidence score and loss size]
```

## Output 2 — end with a machine-readable verdicts block (agent.py reads this to
update source_performance). Use exactly this fenced JSON, after the markdown:

```json
{
  "analysis_type": "postmortem",
  "sources": {
    "news": "accurate | inaccurate | na",
    "social": "accurate | inaccurate | na",
    "fundamental": "accurate | inaccurate | na",
    "macro": "accurate | inaccurate | na",
    "rss": "accurate | inaccurate | na"
  },
  "predicted_confidence": 0,
  "should_have_been": 0,
  "priority": "LOW | MEDIUM | HIGH",
  "rule_proposals": ["..."]
}
```

For a loss, a source is `"inaccurate"` if it pushed us INTO the trade and was
wrong, `"accurate"` if it warned against it (or correctly flagged the risk), and
`"na"` if it wasn't part of this thesis. agent.py increments that source's
`losses` count for every `"inaccurate"`.

## Hard rules
- Do NOT change a core buy/sell rule on one loss. Propose it; skill_5 needs 3+
  similar losses to act.
- Buying power is whatever `get_portfolio` reports in `buying_power.buying_power` — never assume more than that is deployable. (Limited margin since 2026-08-14: unsettled sale proceeds ARE spendable immediately, so there is no settlement wait to plan around.)
