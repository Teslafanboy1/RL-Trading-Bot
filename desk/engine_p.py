"""Claude Desk Engine P — the Polymarket forecaster. PAPER ONLY.

Operator (2026-09-26): make money on Polymarket, weekends included.
edge_lab6 measured the mechanical route and closed it: prices are well
calibrated and "buy the favourite" loses after fees. The one idea it could not
test is an INFORMED forecaster — the Brain reads the news on a market and bets
only where its own probability beats the price by more than the costs. That
cannot be backtested (the model already knows how past markets resolved), so
this engine tests it forward:

  * every forecast is written down with the market's price BEFORE resolution;
  * the Brain forecasts BLIND (it is never shown the price), so its number is
    independent information rather than an echo of the crowd;
  * the verdict is two numbers on resolved markets: Brier(model) vs
    Brier(market) over ALL forecasts, and net paper P&L over the bets.

If the model does not beat the market's Brier score, there is no edge, however
good individual bets look. Pure functions only; the I/O lives in
polymarket_desk.py.
"""
import json
import math

# --- market selection (all knowable before the outcome) ----------------------
# Categories where a news-reading forecaster has no plausible edge over the
# crowd: in-play sports and esports (fast, efficient books), crypto price
# ladders (random walks traded by bots), weather (a forecast-model engine is
# the right tool there), and tweet/mention counters.
EXCLUDE_TAGS = {"sports", "esports", "games", "crypto", "crypto prices", "bitcoin",
                "ethereum", "solana", "xrp", "weather", "up or down", "tweet markets",
                "mention markets", "mentions", "elon tweets", "finance updown"}
# Of 1,200 open events on 2026-09-26, only 8 cleared a 14-day / $5k-liquidity
# bar (sports and crypto are most of the short-dated book); these floors give
# ~45, enough for ~10 forecasts a day. $1k of liquidity still dwarfs a $5 bet.
MIN_DAYS, MAX_DAYS = 0.5, 30.0
MIN_PRICE, MAX_PRICE = 0.08, 0.92   # genuine uncertainty; tails are fee + upset traps
MAX_SPREAD = 0.04
MIN_LIQUIDITY = 1000.0
MIN_VOLUME_24H = 500.0
MAX_CANDIDATES = 10                 # per Brain call
# Separate events can still be one bet: "Lula wins", "Lula wins round one",
# "Lula finishes second" (live pick, 2026-09-26). No tag may be shared by more
# than this many candidates, so one wrong view can't sink half the batch.
# Broad tags ("politics", "elections") only describe a category and are not
# capped — capping them starved the batch to 4 markets.
MAX_PER_TAG = 2
GENERIC_TAGS = {"politics", "world", "elections", "global elections", "world elections",
                "main election", "macro election 2", "international election props",
                "geopolitics", "culture", "tech", "finance", "economy", "business",
                "recurring", "featured", "trending", "breaking", "new", "us politics",
                "moonshot"}

# --- betting ------------------------------------------------------------------
MIN_EDGE = 0.07                     # model prob minus all-in cost per share
KELLY_FRACTION = 0.25
MAX_BET_PCT = 0.05                  # of paper equity, per bet
MAX_OPEN = 15
MAX_NEW_PER_DAY = 4
START_EQUITY = 100.0


def fee_per_share(price, rate):
    """Polymarket taker fee: rate * p * (1 - p) per share (docs, 2026)."""
    p = float(price)
    return float(rate or 0.0) * p * (1.0 - p)


def _tags(event):
    return {str(t.get("label") or t.get("slug") or "").strip().lower()
            for t in (event.get("tags") or []) if isinstance(t, dict)}


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def eligible_market(m, now_ts, iso_ts):
    """True when a market is one the forecaster may look at. `iso_ts` parses
    gamma timestamps (injected so this stays pure and testable)."""
    if not (m.get("active") and not m.get("closed") and m.get("acceptingOrders")
            and m.get("enableOrderBook")):
        return False
    bid, ask = _num(m.get("bestBid")), _num(m.get("bestAsk"))
    if bid is None or ask is None or ask <= bid:
        return False
    mid = (bid + ask) / 2
    if not (MIN_PRICE <= mid <= MAX_PRICE) or ask - bid > MAX_SPREAD:
        return False
    if (_num(m.get("liquidityNum"), 0) < MIN_LIQUIDITY
            or _num(m.get("volume24hr"), 0) < MIN_VOLUME_24H):
        return False
    end = iso_ts(m.get("endDate"))
    if end is None:
        return False
    days = (end - now_ts) / 86400.0
    return MIN_DAYS <= days <= MAX_DAYS


def select_candidates(events, now_ts, iso_ts, *, exclude_ids=(), limit=MAX_CANDIDATES):
    """One market per event (buckets of one event are not independent bets),
    the event's most-traded eligible market, events ranked by 24h volume.
    Ranking by CURRENT volume is fine — it is known now; only FINAL volume is
    outcome-contaminated (edge_lab6)."""
    exclude_ids = {str(i) for i in exclude_ids}
    out = []
    for e in events:
        if _tags(e) & EXCLUDE_TAGS:
            continue
        ms = [m for m in (e.get("markets") or [])
              if str(m.get("id")) not in exclude_ids and eligible_market(m, now_ts, iso_ts)]
        if not ms:
            continue
        m = max(ms, key=lambda x: _num(x.get("volume24hr"), 0))
        bid, ask = float(m["bestBid"]), float(m["bestAsk"])
        fee = m.get("feeSchedule") or {}
        out.append({
            "market_id": str(m.get("id")),
            "event_id": str(e.get("id")),
            "event_title": e.get("title", ""),
            "question": m.get("question", ""),
            "rules": (m.get("description") or e.get("description") or "")[:900],
            "end_date": m.get("endDate"),
            "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4),
            "fee_rate": float(fee.get("rate") or 0) if m.get("feesEnabled") else 0.0,
            "min_shares": _num(m.get("orderMinSize"), 5.0),
            "volume24hr": _num(m.get("volume24hr"), 0),
            "tags": sorted(_tags(e)),
        })
    out.sort(key=lambda c: -c["volume24hr"])
    picked, per_tag = [], {}
    for c in out:
        topics = [t for t in c["tags"] if t not in GENERIC_TAGS]
        if any(per_tag.get(t, 0) >= MAX_PER_TAG for t in topics):
            continue
        for t in topics:
            per_tag[t] = per_tag.get(t, 0) + 1
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked


def decide(cand, p_yes, equity, confidence="medium"):
    """The bet the forecast justifies, or None. Buys whichever side's edge over
    its all-in cost (ask + taker fee) clears MIN_EDGE; NO is bought at
    1 - best YES bid. Sized at quarter-Kelly, capped at MAX_BET_PCT."""
    p = _num(p_yes)
    if p is None or not (0.0 <= p <= 1.0) or str(confidence).lower() == "low":
        return None
    yes_px = cand["ask"]
    no_px = round(1.0 - cand["bid"], 4)
    options = []
    for side, q, px in (("YES", p, yes_px), ("NO", 1.0 - p, no_px)):
        if not (0.0 < px < 1.0):
            continue
        cost = px + fee_per_share(px, cand.get("fee_rate", 0.0))
        options.append((q - cost, side, q, px, cost))
    if not options:
        return None
    edge, side, q, px, cost = max(options)
    if edge < MIN_EDGE:
        return None
    kelly = (q - cost) / (1.0 - cost) if cost < 1 else 0.0
    stake = min(KELLY_FRACTION * kelly, MAX_BET_PCT) * float(equity)
    shares = math.floor(stake / cost * 100) / 100
    if shares < cand.get("min_shares", 5.0) or stake <= 0:
        return None
    return {"side": side, "price": px, "cost_per_share": round(cost, 4),
            "shares": shares, "stake": round(shares * cost, 4),
            "model_q": round(q, 4), "edge": round(edge, 4)}


def resolution(market):
    """(resolved: bool, yes_payout: float|None) from a gamma market payload.
    A market counts as resolved only once closed with final prices — 1/0 for a
    normal resolution, 0.5/0.5 for a void."""
    if not market or not market.get("closed"):
        return False, None
    raw = market.get("outcomePrices")
    try:
        prices = [float(x) for x in (raw if isinstance(raw, list) else json.loads(raw or "[]"))]
    except (TypeError, ValueError):
        return False, None
    if len(prices) != 2:
        return False, None
    if prices in ([1.0, 0.0], [0.0, 1.0], [0.5, 0.5]):
        return True, prices[0]
    return False, None


def settle(position, yes_payout):
    """Paper payout and P&L of a position once its market resolved."""
    per_share = yes_payout if position["side"] == "YES" else 1.0 - yes_payout
    payout = round(position["shares"] * per_share, 4)
    return payout, round(payout - position["stake"], 4)


def brier(p, outcome):
    return (float(p) - float(outcome)) ** 2


def scorecard(book):
    """The verdict numbers. Brier is over EVERY resolved forecast (bet or not):
    the model has an edge only if it beats the market's own price."""
    fc = [f for f in book.get("forecasts", []) if f.get("yes_payout") is not None]
    res = {"forecasts_resolved": len(fc), "forecasts_open":
           sum(1 for f in book.get("forecasts", []) if f.get("yes_payout") is None)}
    if fc:
        res["brier_model"] = round(sum(brier(f["p_model"], f["yes_payout"]) for f in fc) / len(fc), 4)
        res["brier_market"] = round(sum(brier(f["p_market"], f["yes_payout"]) for f in fc) / len(fc), 4)
    closed = [p for p in book.get("positions", []) if p.get("status") == "closed"]
    res["bets_open"] = sum(1 for p in book.get("positions", []) if p.get("status") == "open")
    res["bets_closed"] = len(closed)
    if closed:
        staked = sum(p["stake"] for p in closed)
        pnl = sum(p["pnl"] for p in closed)
        res["win_rate"] = round(sum(1 for p in closed if p["pnl"] > 0) / len(closed), 3)
        res["pnl"] = round(pnl, 2)
        res["roi_per_dollar"] = round(pnl / staked, 4) if staked else None
    res["equity"] = round(equity(book), 2)
    return res


def equity(book):
    """Cash plus open positions at cost (conservative: no mark-to-market)."""
    return float(book.get("cash", START_EQUITY)) + sum(
        p["stake"] for p in book.get("positions", []) if p.get("status") == "open")
