"""polymarket_desk.py — the 24/7 Polymarket paper desk (operator, 2026-09-26).

Runs OUTSIDE the stock bot, on its own systemd timer (every 30 min, weekends
included), so nothing here can stall or crash real trading. PAPER ONLY: no
Polymarket account exists, and no engine may risk money before its forward
record beats the market (see desk/engine_p.py for the test).

Each pass:
  1. settle — free gamma reads; resolved markets pay out paper positions and
     grade every forecast (model AND market Brier).
  2. open   — at most once per OPEN_EVERY_HOURS: pick up to 10 eligible markets,
     ONE Brain call (Opus + web, TIER_SHADOW so it drains before any real
     trading work), paper-bet where the blind forecast beats the all-in price.

State: shadow/pm_desk_p.json — runtime state, never committed, so a deploy
never touches it.

    python3 polymarket_desk.py            # one pass (what the timer runs)
    python3 polymarket_desk.py --status   # scorecard only, no network
    python3 polymarket_desk.py --force-open
"""
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import agent
from desk import engine_p

GAMMA = "https://gamma-api.polymarket.com"
BOOK_PATH = os.path.join(agent.ROOT, "shadow", "pm_desk_p.json")
PLAYBOOK = "desk/pm_playbook.md"
OPEN_EVERY_HOURS = float(os.environ.get("PM_OPEN_EVERY_HOURS", "20"))
EVENT_PAGES = 12


def _get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "trading-bot-pm-desk"})
            with urllib.request.urlopen(req, timeout=30,
                                        context=agent.signals._ssl_context()) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def iso_ts(s):
    """Gamma timestamps ('2026-06-15T00:00:00Z', '2026-06-18 00:32:19+00') ->
    epoch seconds, or None."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def load_book():
    book = agent.load_json(os.path.relpath(BOOK_PATH, agent.ROOT), None)
    return book or {
        "_note": "Claude Desk Engine P — Polymarket forecaster, PAPER. Blind "
                 "forecasts vs the market price; zero real money.",
        "engine": "P", "start_date": agent.now_iso(),
        "start_equity": engine_p.START_EQUITY, "cash": engine_p.START_EQUITY,
        "positions": [], "forecasts": [], "runs": [], "last_open_ts": None,
    }


def save_book(book):
    """Atomic write: a crash mid-save must not truncate the only record."""
    os.makedirs(os.path.dirname(BOOK_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(BOOK_PATH), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(book, f, indent=2)
    os.replace(tmp, BOOK_PATH)


def fetch_open_events(now):
    """Open events whose end falls inside the forecaster's window, busiest
    first (current 24h volume — knowable now, unlike final volume)."""
    lo = datetime.fromtimestamp(now + engine_p.MIN_DAYS * 86400, timezone.utc)
    hi = datetime.fromtimestamp(now + engine_p.MAX_DAYS * 86400, timezone.utc)
    out = []
    for page in range(EVENT_PAGES):
        q = urllib.parse.urlencode({
            "closed": "false", "active": "true", "limit": 100, "offset": page * 100,
            "end_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": "volume24hr", "ascending": "false",
        })
        batch = _get_json(f"{GAMMA}/events?{q}")
        if not batch:
            break
        out.extend(batch)
    return out


def fetch_market(market_id):
    try:
        return _get_json(f"{GAMMA}/markets/{market_id}")
    except Exception:
        return None


def brain_forecast(cands):
    """ONE Opus + web call for the whole batch. The Brain is NOT shown prices:
    a forecast that saw the price is an echo of the crowd, not a test of it.
    Returns {market_id: {"p_yes", "confidence", "reason"}}; {} on failure."""
    playbook = agent.load_file(PLAYBOOK) or ""
    lines = []
    for c in cands:
        lines.append(f"### {c['market_id']}\nQuestion: {c['question']}\n"
                     f"Event: {c['event_title']}\nCloses: {c['end_date']}\n"
                     f"Resolution rules: {c['rules']}\n")
    system = (
        "You are a careful forecaster for a PAPER prediction-market desk. "
        "Read-only: you never place orders. Give calibrated probabilities: most "
        "near-deadline questions resolve to the status quo, and a confident "
        "number needs specific, dated evidence.\n\nDesk playbook:\n" + playbook[:5000])
    user = (
        f"Today is {datetime.now(agent.ET):%A %Y-%m-%d %H:%M} ET.\n"
        "For EACH market below, search for the latest news (last 72 hours first), "
        "read the resolution rules exactly as written, and give the probability "
        "that it resolves YES.\n\n" + "\n".join(lines) + "\n"
        "Output ONLY one fenced ```json block, no prose, keyed by the market id:\n"
        '{"<id>": {"p_yes": <0..1>, "confidence": "low|medium|high", '
        '"reason": "<one line: the decisive evidence, with its date>"}, ...}\n'
        'Use confidence "low" when you found no specific recent evidence.')
    text, _ = agent.run_model(system, user, web=True, model=agent.MODEL, timeout=1500,
                              tier=agent.TIER_SHADOW)
    text = str(text or "")
    # A failed call must be reported as one. Its error text carries the CLI's
    # own JSON, which parses as a "forecast" with no market ids in it — the
    # first smoke test (2026-09-26, CLI not logged in) failed silently that way.
    if text.startswith("(claude -p error") or text.startswith("(error:"):
        print(f"  [pm-P] brain call FAILED: {text[:300]}")
        return {}
    block = agent.extract_last_json_block(text)
    ids = {c["market_id"] for c in cands}
    if not isinstance(block, dict) or not ids & set(block):
        print(f"  [pm-P] brain call returned no forecasts for these markets: {text[:300]}")
        return {}
    return block


def settle_pass(book):
    """Resolve open positions and ungraded forecasts. Free (gamma reads)."""
    ids = {p["market_id"] for p in book["positions"] if p["status"] == "open"}
    ids |= {f["market_id"] for f in book["forecasts"] if f.get("yes_payout") is None}
    settled = 0
    for mid in sorted(ids):
        done, yes_payout = engine_p.resolution(fetch_market(mid))
        if not done:
            continue
        for f in book["forecasts"]:
            if f["market_id"] == mid and f.get("yes_payout") is None:
                f["yes_payout"] = yes_payout
                f["resolved"] = agent.now_iso()
        for p in book["positions"]:
            if p["market_id"] == mid and p["status"] == "open":
                payout, pnl = engine_p.settle(p, yes_payout)
                book["cash"] = round(book["cash"] + payout, 4)
                p.update(status="closed", payout=payout, pnl=pnl, closed=agent.now_iso())
                settled += 1
                print(f"  [pm-P] SETTLED {p['side']} {p['question'][:70]} "
                      f"-> {'won' if pnl > 0 else 'lost'} {pnl:+.2f}")
    return settled


def open_pass(book, now):
    open_pos = [p for p in book["positions"] if p["status"] == "open"]
    if len(open_pos) >= engine_p.MAX_OPEN:
        print(f"  [pm-P] {len(open_pos)} open — at the cap, no new forecasts")
        return 0
    seen = {f["market_id"] for f in book["forecasts"]}
    cands = engine_p.select_candidates(fetch_open_events(now), now, iso_ts,
                                       exclude_ids=seen)
    print(f"  [pm-P] {len(cands)} candidate markets: "
          f"{[c['question'][:50] for c in cands]}")
    if not cands:
        return 0
    verdicts = brain_forecast(cands)
    if not verdicts:
        return 0
    eq = engine_p.equity(book)
    opened = 0
    for c in cands:
        v = verdicts.get(c["market_id"]) or {}
        p_yes = v.get("p_yes")
        try:
            p_yes = float(p_yes)
        except (TypeError, ValueError):
            continue
        book["forecasts"].append({
            "market_id": c["market_id"], "question": c["question"],
            "ts": agent.now_iso(), "end_date": c["end_date"],
            "p_model": round(p_yes, 4), "p_market": c["mid"],
            "confidence": v.get("confidence"), "reason": v.get("reason", ""),
            "yes_payout": None})
        if opened >= engine_p.MAX_NEW_PER_DAY or len(open_pos) + opened >= engine_p.MAX_OPEN:
            continue
        bet = engine_p.decide(c, p_yes, eq, v.get("confidence", "medium"))
        if not bet or bet["stake"] > book["cash"]:
            continue
        book["cash"] = round(book["cash"] - bet["stake"], 4)
        book["positions"].append({
            **bet, "market_id": c["market_id"], "event_id": c["event_id"],
            "question": c["question"], "end_date": c["end_date"],
            "p_market": c["mid"], "reason": v.get("reason", ""),
            "opened": agent.now_iso(), "status": "open"})
        opened += 1
        print(f"  [pm-P] PAPER BET {bet['side']} {c['question'][:70]} @ {bet['price']} "
              f"(model {bet['model_q']:.2f} vs cost {bet['cost_per_share']:.3f}, "
              f"stake ${bet['stake']:.2f}): {v.get('reason', '')[:100]}")
    return opened


def run_once(force_open=False):
    now = time.time()
    book = load_book()
    try:
        settled = settle_pass(book)
    except Exception as e:
        settled = 0
        print(f"  [pm-P] settle pass failed: {e}")
    opened = 0
    h = agent._hours_since(book.get("last_open_ts"))
    if force_open or h is None or h >= OPEN_EVERY_HOURS:
        book["last_open_ts"] = agent.now_iso()
        save_book(book)            # stamp first: a crashing call must not retry every 30 min
        try:
            opened = open_pass(book, now)
        except Exception as e:
            print(f"  [pm-P] open pass failed: {e}")
    book["runs"] = (book.get("runs") or [])[-500:] + [
        {"ts": agent.now_iso(), "settled": settled, "opened": opened}]
    book["scorecard"] = engine_p.scorecard(book)
    save_book(book)
    print(f"  [pm-P] scorecard: {json.dumps(book['scorecard'])}")
    return book


def main(argv):
    if "--status" in argv:
        print(json.dumps(engine_p.scorecard(load_book()), indent=2))
        return 0
    print(f"polymarket desk pass {agent.now_iso()} (PAPER)")
    run_once(force_open="--force-open" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
