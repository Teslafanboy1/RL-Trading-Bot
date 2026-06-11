# data/ — daily-close CSVs for the EMA signal

signals.py reads `data/<SYMBOL>.csv` FIRST (before trying Stooq). Use this to feed
the exact prices on your chart/account — required when trading a SANDBOX account
whose prices differ from the real market.

Format: header row with a `Close` column (a `Date` column is optional), OLDEST row
first. Provide >= ~120 rows so the 55-EMA is well-seeded.

Example — data/SPY.csv:
    Date,Close
    2025-06-09,531.20
    2025-06-10,533.07
    ...

Test it:  python3 signals.py SPY        (or:  python3 signals.py SPY BTSG CAT)
