# skill_5_strategy_rewriter — Strategy & skill editor

Reads BOTH the loss postmortems AND the victory analyses before rewriting
anything. Updates `strategy.json` AND rewrites the skill files that need
improving. This is the most powerful skill — and the easiest to overfit with — so
be conservative.

## Rules
- Require 3+ similar outcomes before changing a CORE rule (the buy/sell EMA
  definition, allocation bands, min_confidence_to_trade, adding a stop-loss). One
  or two trades is noise.
- Minor edits auto-apply but bounded: source-weight nudges capped at +/-0.05 per
  update, then re-normalize `source_weights`. Target-setting and sizing tweaks.
- Major edits are written as proposals flagged for review with a rationale citing
  the specific trade IDs that justify them.
- Every change: bump `version`, append a `version_history` entry (what changed,
  why, trade IDs), and copy the prior `strategy.json` into `strategy/history/`.

## What you update across the whole system
- RESEARCH: which sources to trust more/less, which sectors to favor/avoid, how
  to score confidence more accurately, how to set better targets and size better.
- EMA EXECUTION: additional filters that improve signal accuracy (e.g. volume
  confirmation, market-health checks) — proposed, not silently applied.
- MIDWEEK: what early signals mean a pick is failing; when to cut vs. hold.
- EXIT: earlier exit signals before the full EMA crossover; whether a stop-loss
  is warranted.
- SKILL FILES: rewrite the text of any skill whose instructions a recurring
  failure proves wrong. Quote the old text and the new text.

## Guardrails
- Do not change the goal away from 100% monthly, but do not chase it by sizing up
  risk beyond the allocation bands either.
- Never loosen the T+1 / settled-funds rule.
