#!/usr/bin/env python3
"""
Historical odds scanner — fetches and stores historical sportsbook odds
for cross-referencing against Polymarket/Kalshi prediction markets.

Sources (in priority order):
  1. OddsPapi (free) — 69 sports, full price history, every line move
  2. ClearSportsAPI (free tier) — 7-14 day history
  3. The Odds API (existing keys) — live only, no deep history
  4. Pro-Football-Reference / Baseball-Reference — free HTML scrape

Quick start:
    export ODDSPAPI_KEY="your_key"

    # Fetch MLB 2025 closing lines
    python3 scripts/historical_odds_scanner.py --sport 13 --from 2025-03-27 --to 2025-10-01 --tournaments MLB --csv mlb_2025.csv
"""

import os, sys, json, csv, time, argparse, logging
from datetime import datetime, date, timedelta
from collections import deque
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("historical_odds")

# --- Sports ID map (OddsPapi) ---
SPORT_IDS = {
    "soccer": 10, "basketball": 11, "tennis": 12, "baseball": 13,
    "american_football": 14, "ice_hockey": 15, "mma": 20,
    "boxing": 21, "golf": 18,
}
SPORT_IDS_REVERSE = {v: k for k, v in SPORT_IDS.items()}

# --- API config ---
ODDSPAPI_KEY = os.environ.get("ODDSPAPI_KEY", "")
ODDSPAPI_BASE = os.environ.get("ODDSPAPI_BASE", "https://api.oddspapi.io/v4")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Global rate limiter: max 10 calls per 60s window
_rate_window = deque()


def _rate_limit(endpoint_type: str = "fixtures"):
    """Free plan: 3 calls per 60s. Simple approach: 20s between each call."""
    now = time.time()
    while _rate_window and _rate_window[0] < now - 60:
        _rate_window.popleft()
    if len(_rate_window) >= 3:
        wait = 20
    else:
        # Even when we have budget, don't burst — space by 20s from last call
        last_call = _rate_window[-1] if _rate_window else 0
        gap = 20 - (now - last_call)
        wait = max(0, gap)
    if wait > 0.5:
        time.sleep(wait)
    _rate_window.append(time.time())


# ========== OddsPapi ==========

def oddspapi_get(endpoint: str, params: dict = None, raise_on_404: bool = False, rate_type: str = "fixtures"):
    """Generic GET to OddsPapi. Returns parsed JSON or None on 404."""
    _rate_limit(endpoint_type=rate_type)
    if not params:
        params = {}
    params["apiKey"] = ODDSPAPI_KEY
    url = f"{ODDSPAPI_BASE}/{endpoint}"
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 404 and not raise_on_404:
        return None
    resp.raise_for_status()
    return resp.json()


def oddspapi_list_sports() -> list:
    data = oddspapi_get("sports")
    return [{"id": s["sportId"], "name": s["sportName"]} for s in data]


def oddspapi_fetch_fixtures(sport_id: int, from_date: str, to_date: str,
                            tournaments: list[str] = None) -> list[dict]:
    from_dt = datetime.fromisoformat(from_date) if "T" in from_date else datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.fromisoformat(to_date) if "T" in to_date else datetime.strptime(to_date, "%Y-%m-%d")
    all_fixtures = []
    current = from_dt
    while current < to_dt:
        window_end = min(current + timedelta(days=9), to_dt)
        params = {"sportId": sport_id, "from": current.strftime("%Y-%m-%d"), "to": window_end.strftime("%Y-%m-%d")}
        try:
            fixtures = oddspapi_get("fixtures", params)
            if not fixtures:
                fixtures = []
            log.info(f"  Fetched {len(fixtures)} fixtures for {current.date()} → {window_end.date()}")
            if tournaments:
                fixtures = [f for f in fixtures if f.get("tournamentName") in tournaments]
                log.info(f"  Filtered to {len(fixtures)} for tournaments: {tournaments}")
            all_fixtures.extend(fixtures)
        except Exception as e:
            log.warning(f"  Failed window {current.date()}-{window_end.date()}: {e}")
        current = window_end + timedelta(days=1)
    return all_fixtures


def oddspapi_fetch_market_catalog(sport_id: int) -> dict:
    _rate_limit()
    data = oddspapi_get("markets", {"sportId": sport_id}, rate_type="fixtures")
    if not data:
        return {"markets": {}, "outcomes": {}}
    market_names = {}
    outcome_names = {}
    for m in data:
        mid = m["marketId"]
        market_names[mid] = m.get("marketName", f"Market {mid}")
        for o in m.get("outcomes", []):
            oid = o["outcomeId"]
            outcome_names[(mid, oid)] = o.get("outcomeName", f"Outcome {oid}")
    return {"markets": market_names, "outcomes": outcome_names}


def oddspapi_fetch_historical_odds(fixture_id: str,
                                   bookmakers: str = "pinnacle",
                                   catalog: dict = None) -> list[dict]:
    params = {"fixtureId": fixture_id, "bookmakers": bookmakers}
    data = oddspapi_get("historical-odds", params, rate_type="historical")
    if data is None:
        return []
    rows = []
    for bm, bdata in data.get("bookmakers", {}).items():
        for mid_str, mdata in bdata.get("markets", {}).items():
            mid = int(mid_str)
            mkt_name = catalog["markets"].get(mid, f"Market {mid}") if catalog else f"Market {mid}"
            for oid_str, odata in mdata.get("outcomes", {}).items():
                oid = int(oid_str)
                out_name = catalog["outcomes"].get((mid, oid), f"Outcome {oid}") if catalog else f"Outcome {oid}"
                for _player, price_history in odata.get("players", {}).items():
                    for snap in price_history:
                        rows.append({
                            "fixture_id": fixture_id,
                            "bookmaker": bm,
                            "market_id": mid,
                            "market_name": mkt_name,
                            "outcome_id": oid,
                            "outcome_name": out_name,
                            "price": snap.get("price"),
                            "limit": snap.get("limit"),
                            "recorded_at": snap.get("createdAt"),
                        })
    return rows


def oddspapi_fetch_odds_summary(fixture_id: str, bookmakers: str = "pinnacle",
                                 catalog: dict = None) -> dict:
    prices = oddspapi_fetch_historical_odds(fixture_id, bookmakers, catalog)
    if not prices:
        return {}
    from itertools import groupby
    prices.sort(key=lambda r: (r["bookmaker"], r["market_id"], r["outcome_id"], r["recorded_at"] or ""))
    summary = {}
    for bm, bm_rows in groupby(prices, key=lambda r: r["bookmaker"]):
        bm_summary = {}
        for mkt_id, mkt_rows in groupby(bm_rows, key=lambda r: (r["market_id"], r["market_name"])):
            _, mkt_name = mkt_id
            mkt_data = {}
            for _, out_rows in groupby(mkt_rows, key=lambda r: r["outcome_name"]):
                out_list = list(out_rows)
                mkt_data[out_list[0]["outcome_name"]] = out_list[-1]["price"]
            bm_summary[mkt_name] = mkt_data
        summary[bm] = bm_summary
    return summary


# ========== The Odds API (existing) ==========

def theoddsapi_get(endpoint: str, params: dict = None) -> dict:
    if not params:
        params = {}
    params["apiKey"] = ODDS_API_KEY
    url = f"{ODDS_API_BASE}/{endpoint}"
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def theoddsapi_fetch_sports() -> list[dict]:
    data = theoddsapi_get("sports")
    return [{"key": s["key"], "title": s["title"], "active": s.get("active", False)} for s in data]


def theoddsapi_fetch_odds(sport: str = "upcoming", regions: str = "us",
                           markets: str = "h2h,spreads,totals") -> list[dict]:
    params = {"regions": regions, "markets": markets, "oddsFormat": "decimal"}
    if sport != "upcoming":
        params["commenceTimeFrom"] = datetime.utcnow().isoformat() + "Z"
    return theoddsapi_get(f"sports/{sport}/odds", params)


# ========== CLI ==========

def main():
    parser = argparse.ArgumentParser(description="Historical odds scanner")
    parser.add_argument("--sport", type=int, choices=list(SPORT_IDS_REVERSE.keys()),
                        help="Sport ID (13=baseball, 14=nfl, 15=nhl, 11=nba)")
    parser.add_argument("--from", dest="from_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="End date YYYY-MM-DD")
    parser.add_argument("--books", default="pinnacle",
                        help="Bookmakers (comma-sep, max 3). Free plan: pinnacle")
    parser.add_argument("--tournaments", nargs="*", help="Filter by tournament names (e.g. MLB)")
    parser.add_argument("--csv", help="Export to CSV")
    parser.add_argument("--json", help="Export to JSON")
    parser.add_argument("--list-sports", action="store_true")
    parser.add_argument("--list-theoddsapi", action="store_true")
    parser.add_argument("--summary", action="store_true",
                        help="Only show closing line summaries per fixture")
    parser.add_argument("--limit", type=int, default=50, help="Max fixtures")

    args = parser.parse_args()

    if args.list_sports:
        if not ODDSPAPI_KEY:
            print("ODDSPAPI_KEY not set. Sign up at https://oddspapi.io")
            sys.exit(1)
        sports = oddspapi_list_sports()
        print(f"\n{'ID':>4}  {'Sport':<25}")
        print("-" * 30)
        for s in sports:
            print(f"{s['id']:>4}  {s['name']:<25}")
        return

    if args.list_theoddsapi:
        if not ODDS_API_KEY:
            print("ODDS_API_KEY not set.")
            sys.exit(1)
        sports = theoddsapi_fetch_sports()
        print(f"\n{'Active':<8} {'Key':<35} {'Title'}")
        print("-" * 80)
        for s in sports:
            act = "✓" if s["active"] else " "
            print(f"{act:<8} {s['key']:<35} {s['title']}")
        return

    if not args.sport:
        parser.print_help()
        return

    if not ODDSPAPI_KEY:
        print("ODDSPAPI_KEY not set. Get one free at https://oddspapi.io")
        sys.exit(1)

    from_date = args.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = args.to_date or date.today().isoformat()
    sport_name = SPORT_IDS_REVERSE.get(args.sport, f"sport_{args.sport}")

    print(f"\n📊 {sport_name.upper()} — {from_date} → {to_date}")
    print(f"   Bookmakers: {args.books}")
    if args.tournaments:
        print(f"   Tournaments: {', '.join(args.tournaments)}")
    print()

    # 1. Fetch market catalog
    catalog = oddspapi_fetch_market_catalog(args.sport)
    time.sleep(3)

    # 2. Fetch fixtures
    fixtures = oddspapi_fetch_fixtures(args.sport, from_date, to_date, args.tournaments)
    print(f"\n📋 Total fixtures: {len(fixtures)}")
    if not fixtures:
        print("No fixtures found.")
        return

    # 3. Process fixtures
    all_rows = []
    summaries = []
    time.sleep(2)
    for i, fx in enumerate(fixtures[:args.limit]):
        fid = fx["fixtureId"]
        home = fx.get("participant1Name", "?")
        away = fx.get("participant2Name", "?")
        start = fx.get("startTime", "?")
        tourney = fx.get("tournamentName", "")

        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/{min(len(fixtures), args.limit)}] {home} vs {away} ({tourney})")

        try:
            if args.summary:
                summary = oddspapi_fetch_odds_summary(fid, args.books, catalog)
                if summary:
                    summaries.append({"fixture": fx, "summary": summary})
            else:
                rows = oddspapi_fetch_historical_odds(fid, args.books, catalog)
                if rows:
                    for r in rows:
                        r["home"] = home
                        r["away"] = away
                        r["start_time"] = start
                        r["tournament"] = tourney
                    all_rows.extend(rows)
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code
            if code == 404:
                pass
            elif code == 429:
                log.warning(f"  Rate limited — sleeping 30s")
                time.sleep(30)
            else:
                log.warning(f"  Failed {fid} ({home} vs {away}): {e}")
        except Exception as e:
            log.warning(f"  Failed {fid} ({home} vs {away}): {e}")

    # 4. Output
    if args.summary:
        print(f"\n📊 Closing line summaries for {len(summaries)} fixtures:")
        for s in summaries:
            fx = s["fixture"]
            print(f"\n{fx.get('participant1Name')} vs {fx.get('participant2Name')}:")
            for bm, markets in s["summary"].items():
                for mkt, outcomes in markets.items():
                    print(f"  {bm} | {mkt}: {outcomes}")
        if args.json:
            with open(args.json, "w") as f:
                json.dump(summaries, f, indent=2)
            print(f"Wrote summaries to {args.json}")
    else:
        print(f"\n📊 Total price snapshots: {len(all_rows):,}")
        if args.csv and all_rows:
            keys = ["fixture_id", "start_time", "home", "away", "tournament",
                    "bookmaker", "market_id", "market_name",
                    "outcome_id", "outcome_name", "price", "recorded_at"]
            with open(args.csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for r in all_rows:
                    w.writerow({k: r.get(k) for k in keys})
            print(f"  → {args.csv}")
        if args.json and all_rows:
            with open(args.json, "w") as f:
                json.dump(all_rows, f, indent=2)
            print(f"  → {args.json}")


if __name__ == "__main__":
    main()