# Polymarket Desk — Playbook

The forecaster reads this first, every run. Same rule as the stock desk: a
broken mechanism is fixed the day it is found; a forecasting rule changes only
after the same mistake shows up 3 times on resolved markets.

## Measured before the first forecast (edge_lab6, 2026-09-26)
- **Polymarket prices are well calibrated.** A 95c contract wins ~95% of the
  time. Beating the price needs information the crowd has not priced yet, not
  a rule of thumb.
- **Costs are real.** Taker fee = rate × p × (1 − p) per share on top of the
  spread. An edge smaller than ~5 points does not survive them.
- **Upsets cluster in the busiest markets.** High volume often means news is
  moving the price right now — the crowd may know something you have not found.
- **Where a public model disagrees with the crowd, the crowd is usually right.**
  Temperature markets vs free weather models (edge_lab7, 2026-09-26): the
  crowd won every month; betting the disagreements lost ~46% per bet. A huge
  gap between your number and a liquid price is more often a misread
  resolution source than a mispriced market — re-read the rules first.

## How to forecast
- Read the resolution rules literally: the source, the deadline, the time zone,
  and what counts. Many markets resolve on a technicality.
- Short deadlines favour the status quo. "Will X happen by Friday?" is usually
  no unless something is already scheduled or under way.
- Find dated evidence. A confident number with no specific recent source is
  a guess — mark it confidence "low" (low-confidence forecasts are never bet).
- Prefer official schedules, filings and primary sources over commentary.

## Open questions the scorecard must answer
- Does the blind forecast beat the market's Brier score on resolved markets?
- Are the bets (edge ≥ 7 points after costs) profitable net of fees?
- Which categories (politics, economics, companies, culture) carry any edge?
