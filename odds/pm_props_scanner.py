#!/usr/bin/env python3
"""
Polymarket US Props Scanner
===========================
Scans today's MLB prop markets via the polymarket-us SDK, cross-references against
Pinnacle devigged lines from The Odds API, and surfaces +EV edges.

Discovery method: c.search.query({'query': 'strikeouts 2026'}) returns all MLB game
events with embedded prop markets (sportsMarketType: baseball_player_strikeouts,
baseball_player_home_runs, etc.). BBO gives live bid/ask.

Usage:
    python3 odds/pm_props_scanner.py
    python3 odds/pm_props_scanner.py --sport strikeouts
    python3 odds/pm_props_scanner.py --sport home_runs
    python3 odds/pm_props_scanner.py --min-edge 5
"""

import os
import sys
import re
import json
import time
import argparse
import urllib.request
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Config ──────────────────────────────────────────────────────────────────
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
EDGE_FLOOR = 0.03   # 3% minimum edge (P1)
EDGE_CAP   = 0.15   # 15% maximum edge (P1 — beyond this = model error)
MIN_LIQUIDITY = 1.0  # minimum $1 open interest to consider

# Polymarket prop type → Odds API market key (pitcher props)
PM_TO_ODDS_PITCHER = {
    "baseball_player_strikeouts": "pitcher_strikeouts",
}
# Batter props
PM_TO_ODDS_BATTER = {
    "baseball_player_home_runs": "batter_home_runs",
}

# Regex to parse PM question: "Will {Player} record at least {N} pitching strikeouts in {Game}?"
K_RE  = re.compile(r"Will (.+?) record at least (\d+) pitching strikeouts", re.I)
HR_RE = re.compile(r"Will (.+?) record at least (\d+) home runs", re.I)

# ── Odds API helpers ─────────────────────────────────────────────────────────
def _fetch(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _us_to_implied(us: float) -> float:
    return (100 / (us + 100)) if us > 0 else (abs(us) / (abs(us) + 100))


def _devig_power(over_us: float, under_us: float) -> tuple[float, float]:
    """Power devig via bisection. Finds α s.t. p_over^α + p_under^α = 1."""
    p_over  = _us_to_implied(over_us)
    p_under = _us_to_implied(under_us)
    if p_over + p_under <= 1.0:
        return p_over, p_under
    # Bisection: find α > 1 such that sum(p_i^α) = 1.0
    lo, hi = 1.0, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        total = p_over**mid + p_under**mid
        if abs(total - 1.0) < 1e-9:
            break
        if total > 1.0:
            lo = mid
        else:
            hi = mid
    alpha = (lo + hi) / 2
    return p_over**alpha, p_under**alpha


def _get_mlb_event_ids() -> list[str]:
    """Fetch today's MLB event IDs — uses shared cache (FREE, 0 credits)."""
    try:
        from odds.odds_api_cache import get_mlb_events
        return [e["id"] for e in get_mlb_events()]
    except ImportError:
        pass
    url = f"{ODDS_API_BASE}/sports/baseball_mlb/events?apiKey={ODDS_API_KEY}&dateFormat=iso"
    try:
        return [e["id"] for e in _fetch(url)]
    except Exception as e:
        print(f"  [odds-api events] {e}", file=sys.stderr)
        return []


def fetch_book_props(market_key: str, event_ids: list[str]) -> dict:
    """
    Fetch player prop lines from Pinnacle across all events.
    Returns {player_name_lower: {line, over_fair, under_fair, over_us}}
    Uses shared cache (odds_api_cache) — each event costs 1 credit only when
    Pinnacle has active lines. Bundles all prop market types in one call per event.
    """
    if not ODDS_API_KEY:
        return {}

    try:
        from odds.odds_api_cache import get_prop_lines as _cached_props
        use_cache = True
    except ImportError:
        use_cache = False

    results: dict[str, dict] = {}
    for eid in event_ids:
        if use_cache:
            data = _cached_props(eid, markets=market_key, bookmakers="pinnacle")
        else:
            url = (f"{ODDS_API_BASE}/sports/baseball_mlb/events/{eid}/odds"
                   f"?apiKey={ODDS_API_KEY}&markets={market_key}"
                   f"&bookmakers=pinnacle&oddsFormat=american")
            try:
                data = _fetch(url)
            except Exception as e:
                print(f"  [odds-api event {eid[:8]}] {e}", file=sys.stderr)
                continue

        if not data:
            continue

        sharp_book = next(
            (bk for bk in data.get("bookmakers", []) if bk["key"] == "pinnacle"),
            None
        )
        if not sharp_book:
            continue

        for market in sharp_book.get("markets", []):
            if market.get("key") != market_key:
                continue
            players: dict[str, dict] = {}
            for o in market.get("outcomes", []):
                player = o.get("description", "")
                pt = o.get("point", 0)
                side = o.get("name", "")
                price = o.get("price", 0)
                if player not in players:
                    players[player] = {"line": pt}
                if "Over" in side:
                    players[player]["over_us"] = price
                else:
                    players[player]["under_us"] = price
            for player, d in players.items():
                if "over_us" in d and "under_us" in d:
                    fair_over, fair_under = _devig_power(d["over_us"], d["under_us"])
                    pkey = player.lower().strip()
                    results[pkey] = {
                        "line": d["line"],
                        "over_fair": fair_over,
                        "under_fair": fair_under,
                        "over_us": d["over_us"],
                    }
        if not use_cache:
            time.sleep(0.1)  # courtesy delay only when not using cache
    return results


# ── PM SDK helpers ───────────────────────────────────────────────────────────
def get_pm_client():
    from polymarket_us import PolymarketUS
    return PolymarketUS()


def fetch_pm_price(client, slug: str) -> Optional[float]:
    """Returns YES (long) price from BBO."""
    try:
        bbo = client.markets.bbo(slug)
        md = bbo.get("marketData", {})
        # Prefer currentPx, fall back to lastPriceSample longPx
        cur = md.get("currentPx", {})
        if cur and cur.get("value"):
            return float(cur["value"])
        lps = md.get("lastPriceSample", {})
        if lps and lps.get("longPx", {}).get("value"):
            return float(lps["longPx"]["value"])
    except Exception:
        pass
    return None


def fetch_pm_props(client, query: str = "2026") -> list[dict]:
    """
    Fetch all MLB prop markets for today via search.query.
    Returns list of dicts with: player, threshold, smt, slug, game_title.
    """
    resp = client.search.query({"query": f"mlb strikeouts home runs {query}"})
    events = resp.get("events", [])
    props = []
    for event in events:
        game = event.get("title", "")
        event_date = event.get("startDate", "")[:10]
        # Only today's games
        if event_date and event_date < str(date.today()):
            continue
        for m in event.get("markets", []):
            smt = m.get("sportsMarketType", "")
            if smt not in ("baseball_player_strikeouts", "baseball_player_home_runs"):
                continue
            question = m.get("question", "")
            slug = m.get("slug", "")
            # Parse player + threshold
            match = K_RE.search(question) or HR_RE.search(question)
            if not match:
                continue
            player = match.group(1).strip()
            threshold = int(match.group(2))
            props.append({
                "player": player,
                "player_key": player.lower().strip(),
                "threshold": threshold,
                "smt": smt,
                "slug": slug,
                "game": game,
                "event_date": event_date,
            })
    return props


# ── Edge calculation ─────────────────────────────────────────────────────────
def confidence_from_edge(edge: float) -> float:
    """Sqrt-dampened confidence. P1: edge capped at EDGE_CAP."""
    import math
    e = min(edge, EDGE_CAP)
    return round(0.5 + 0.5 * math.sqrt(e / EDGE_CAP), 4)


def scan_props(sport_filter: Optional[str] = None, min_edge: float = EDGE_FLOOR) -> list[dict]:
    client = get_pm_client()

    print("Fetching PM props from SDK...", flush=True)
    all_props = fetch_pm_props(client)
    print(f"  {len(all_props)} prop markets found across today's games")

    # Filter by sport type
    if sport_filter:
        if "strike" in sport_filter.lower():
            all_props = [p for p in all_props if p["smt"] == "baseball_player_strikeouts"]
        elif "home" in sport_filter.lower() or "hr" in sport_filter.lower():
            all_props = [p for p in all_props if p["smt"] == "baseball_player_home_runs"]
    print(f"  {len(all_props)} after sport filter")

    # Fetch Odds API book lines (pitcher strikeouts + batter HR)
    print("Fetching Odds API (Pinnacle) lines...", flush=True)
    event_ids = _get_mlb_event_ids()
    print(f"  {len(event_ids)} MLB events found in Odds API")
    book_k  = fetch_book_props("pitcher_strikeouts", event_ids)
    book_hr = fetch_book_props("batter_home_runs", event_ids)
    print(f"  {len(book_k)} pitcher K lines | {len(book_hr)} batter HR lines from Pinnacle")

    # Group PM props by player+smt — only look at each threshold independently
    print(f"\nFetching PM prices ({len(all_props)} slugs)...", flush=True)
    edges = []
    seen_slugs = set()
    for prop in all_props:
        if prop["slug"] in seen_slugs:
            continue
        seen_slugs.add(prop["slug"])

        # Get PM price — skip illiquid pre-market prices
        pm_yes = fetch_pm_price(client, prop["slug"])
        if pm_yes is None or pm_yes < 0.05:
            continue

        # Match to book line
        pkey = prop["player_key"]
        smt  = prop["smt"]
        threshold = prop["threshold"]

        book_line = None
        if smt == "baseball_player_strikeouts":
            book_data = book_k.get(pkey)
            if not book_data:
                continue
            book_line = book_data["line"]
            book_fair = book_data["over_fair"]
            # PM threshold N means "at least N" ≈ book line N-0.5
            # Only compare markets within 1 unit of the book line
            # e.g. book=6.5 → valid PM thresholds: 6 or 7
            pm_equiv_line = threshold - 0.5  # PM "≥6" ≈ book "o5.5"
            if abs(pm_equiv_line - book_line) > 1.0:
                continue  # too far from book line, skip
            # Positive delta = PM is easier (lower threshold) → fair value is higher
            threshold_delta = book_line - pm_equiv_line
            adjusted_fair = min(0.97, book_fair + threshold_delta * 0.08)
        elif smt == "baseball_player_home_runs":
            book_data = book_hr.get(pkey)
            if not book_data:
                continue
            book_line = book_data["line"]
            book_fair = book_data["over_fair"]
            # HR book lines are typically at 0.5 (= "hits at least 1")
            # PM ≥1 → pm_equiv = 0.5 → delta = 0 (same bet)
            # PM ≥2 → pm_equiv = 1.5 → delta = -1 (PM is harder → lower fair)
            pm_equiv_line = threshold - 0.5
            if abs(pm_equiv_line - book_line) > 1.0:
                continue
            threshold_delta = book_line - pm_equiv_line  # negative for ≥2 vs 0.5 book
            adjusted_fair = max(0.02, min(0.97, book_fair + threshold_delta * 0.06))
        else:
            continue

        edge = adjusted_fair - pm_yes
        if edge < min_edge or edge > EDGE_CAP:
            continue

        edges.append({
            "player": prop["player"],
            "prop": smt.replace("baseball_player_", ""),
            "threshold": threshold,
            "pm_slug": prop["slug"],
            "game": prop["game"],
            "pm_yes": round(pm_yes, 4),
            "book_line": book_line,
            "book_fair": round(book_fair, 4),
            "adj_fair": round(adjusted_fair, 4),
            "edge_pp": round(edge * 100, 2),
            "confidence": confidence_from_edge(edge),
        })
        time.sleep(0.05)  # rate limit courtesy

    return sorted(edges, key=lambda x: x["edge_pp"], reverse=True)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket US Props Scanner")
    parser.add_argument("--sport", default=None, help="strikeouts | home_runs")
    parser.add_argument("--min-edge", type=float, default=EDGE_FLOOR * 100, help="Minimum edge in pp (default 3)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  PM Props Scanner — {date.today()}  ")
    print(f"{'='*60}\n")

    edges = scan_props(sport_filter=args.sport, min_edge=args.min_edge / 100)

    if not edges:
        print("No edges found above threshold.")
        return

    print(f"\n{'='*60}")
    print(f"  TOP EDGES ({len(edges)} markets above {args.min_edge:.0f}pp threshold)")
    print(f"{'='*60}")
    print(f"{'Player':<22} {'Prop':<12} {'≥N':<4} {'PM%':>5} {'Book':>5} {'AdjFair':>8} {'Edge':>6}  Game")
    print("-"*90)
    for e in edges[:20]:
        flag = "⚡" if e["edge_pp"] >= 8 else "  "
        print(f"{flag}{e['player']:<20} {e['prop']:<12} ≥{e['threshold']:<3} "
              f"{e['pm_yes']*100:>4.0f}% {e['book_line']:>5.1f} {e['adj_fair']*100:>7.1f}% "
              f"{e['edge_pp']:>5.1f}pp  {e['game']}")

    print(f"\n  Pinnacle devig method: power | PM prices: currentPx/lastPriceSample")
    print(f"  Edge floor: {EDGE_FLOOR*100:.0f}pp | Edge cap: {EDGE_CAP*100:.0f}pp (P1)\n")


if __name__ == "__main__":
    main()
