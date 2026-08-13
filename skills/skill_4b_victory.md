# skill_4b_victory — Victory Analysis

Fires **immediately and automatically** after every WINNING closed trade (called
by agent.py's post-trade pipeline — never weekly, never manual). One winning trade
= one victory analysis.

## Weight the analysis by confidence
The higher the confidence at entry, the deeper this analysis goes. **High-
confidence wins validate the system most** — dissect them hardest to learn what to
amplify. Be disciplined: a win does NOT prove the thesis was right; it may have
been luck. State your honest confidence that the *process* was sound.

## Your job
1. **Search news and Reddit (web search) for what happened during the hold
   period** — confirm the win was driven by the thesis, not unrelated luck.
2. **Identify exactly which source(s) gave the strongest signal.**
3. **Was the EMA signal clean and clear** (a wide, well-separated
   55-crosses-all-3 BUY), and was entry timing optimal?
4. **Was the thesis accurate** — did the catalyst play out as expected?
5. **Propose what to amplify** — specific, repeatable, mechanical features.

## Output 1 — write this EXACT format to /postmortems/victory_XXX.md

```markdown
# Victory Analysis — [SYMBOL] — [DATE]

## Trade Details
- Symbol:
- Entry: price + date
- Exit: price + date
- P&L: dollar + percent
- Confidence at entry: X%
- Capital deployed: $X

## What Went Right
[narrative of what worked during the hold]

## Signal Accuracy Analysis
- Which source was strongest:
- Was EMA signal clean and clear:
- Was entry timing optimal:
- Was thesis accurate:

## Source Performance This Trade
- News: strong / weak signal
- Reddit: strong / weak signal
- Fundamental: strong / weak signal
- Macro: strong / weak signal

## What We Should Amplify
[specific signals and sources to trust more]

## Rule Proposals
- STRENGTHEN: [what to do more of]
- INCREASE weight: [source] from X to Y
- ADD confirmation: [new signal to look for]

## Confidence Score Accuracy
- Predicted: X%
- Actual outcome justified: Y%
- Calibration note:

## Priority
[LOW / MEDIUM / HIGH based on confidence and gain size]
```

## Output 2 — end with a machine-readable verdicts block (agent.py reads this to
update source_performance). Use exactly this fenced JSON, after the markdown:

```json
{
  "analysis_type": "victory",
  "sources": {
    "news": "accurate | inaccurate | na",
    "social": "accurate | inaccurate | na",
    "fundamental": "accurate | inaccurate | na",
    "macro": "accurate | inaccurate | na",
    "rss": "accurate | inaccurate | na"
  },
  "predicted_confidence": 0,
  "actual_justified": 0,
  "priority": "LOW | MEDIUM | HIGH",
  "rule_proposals": ["..."]
}
```

For a win, a source is `"accurate"` if it correctly pointed us INTO the trade
(a strong signal that played out), `"inaccurate"` if it argued against a trade
that worked, and `"na"` if it wasn't part of this thesis. agent.py increments that
source's `wins` count for every `"accurate"`.

## Hard rules
- Don't over-credit a source on a single win — nudge, don't lurch. Big changes go
  through skill_5 with the 3+ confirmation bar.
- Buying power is whatever `get_portfolio` reports in `buying_power.buying_power` — never assume more than that is deployable. (Limited margin since 2026-08-14: unsettled sale proceeds ARE spendable immediately, so there is no settlement wait to plan around.)
