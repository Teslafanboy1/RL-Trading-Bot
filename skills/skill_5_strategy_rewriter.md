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

## Severity tag (always emit one)
Start your response with a single line tagging the change set:

`SEVERITY: ROUTINE` — only weight nudges, target/sizing tweaks, or enforcement of a
rule that already exists in strategy.json. Auto-applied today.

`SEVERITY: MAJOR` — anything touching the core EMA/ribbon definition, allocation
bands, min_confidence_to_trade, a new hard rule, or any skill_2 (execution) change.
These always require operator sign-off — propose with trade IDs, never silently
apply, regardless of how many similar outcomes support them.

This tag is forward-looking: a later phase will gate auto-apply on it. Tag honestly
even on a NO-OP review (use `SEVERITY: ROUTINE` and say "no change").

## Output format for agent.py

agent.py runs you headless via `claude -p` and parses your output text directly.
You cannot write files yourself — agent.py does it from your output.

### strategy.json output
Always end your response with the COMPLETE updated strategy.json as a fenced
```json block. Include all fields from the current version — never a partial update.
Increment the version field. agent.py replaces the current file with this block.

### Skill file output
When a skill file needs updating, output the COMPLETE new file content using
EXACTLY this format (case-sensitive, exact whitespace):

## SKILL FILE UPDATE: skill_name_here
[complete file content — not a diff, the entire file]
## END SKILL FILE UPDATE

Replace skill_name_here with the filename without .md extension, e.g.:
skill_1_research, skill_2_execution, skill_4_postmortem, etc.

Rules:
- Only output a SKILL FILE UPDATE block if the evidence justifies it (3+ similar
  outcomes for behavioral changes; immediate for factual corrections)
- Always output the COMPLETE file — agent.py replaces the whole file
- You can output multiple SKILL FILE UPDATE blocks in one response
- Never output a SKILL FILE UPDATE block for skill_5_strategy_rewriter itself —
  that would create a self-modification loop
- The json block for strategy.json must come AFTER any skill update blocks
