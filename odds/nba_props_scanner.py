#!/usr/bin/env python3
"""
NBA Props Edge Scanner — PM vs Pinnacle (devigged)
Usage: python3 odds/nba_props_scanner.py
       python3 odds/nba_props_scanner.py --game "Spurs Knicks"
       python3 odds/nba_props_scanner.py --full   (dumps all raw data)
"""

import os, sys, json, re, time, urllib.request
from datetime import datetime, timezone

# Path setup
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.expanduser("~/Desktop/polyclawd"))

try:
    from polymarket_us import PolymarketUS

    PM_SDK = True
except ImportError:
    PM_SDK = False
    print("WARN: polymarket-us SDK not installed. Run: pip install polymarket-us")

# Config
KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"
# Per-half-line degradation rates (from MLB calibration)
DELTA_MAP = {
    "points": 0.06,
    "rebounds": 0.07,
    "assists": 0.07,
    "threes": 0.10,
    "blocks": 0.10,
    "steals": 0.10,
}

STAT_LABELS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_blocks": "blocks",
    "player_steals": "steals",
}

PM_SMT_LABELS = {
    "basketball_player_points": "points",
    "basketball_player_rebounds": "rebounds",
    "basketball_player_assists": "assists",
}

# ─── helpers ───


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        try:
            from odds.the_odds_api import _track_credits_from_response

            _track_credits_from_response(r)
        except Exception:
            pass
        return json.loads(r.read())


def devig(ov, un):
    """Power devig: returns true probability for Over."""
    o = 100 / (ov + 100) if ov > 0 else abs(ov) / (abs(ov) + 100)
    u = 100 / (un + 100) if un > 0 else abs(un) / (abs(un) + 100)
    if o + u <= 1.0:
        return o
    lo, hi = 1.0, 20.0
    for _ in range(80):
        mid = (lo + hi) / 2
        t = o**mid + u**mid
        if abs(t - 1) < 1e-9:
            break
        lo, hi = (mid, hi) if t > 1 else (lo, mid)
    return o ** ((lo + hi) / 2)


def american_to_implied(price):
    """Single american price → implied probability."""
    if price > 0:
        return 100 / (price + 100)
    return abs(price) / (abs(price) + 100)


# ─── core scan ───


def scan_nba_props(api_key=None, verbose=False):
    """Main scan: fetches Pinnacle NBA props + PM prices, returns edges."""
    if api_key:
        globals()["KEY"] = api_key

    if not KEY:
        print("ERROR: No ODDS_API_KEY found. Set in .env or export.")
        return []

    if not PM_SDK:
        print("ERROR: polymarket-us SDK required.")
        return []

    from odds.rate_limiter import can_make_call

    _ok, _why = can_make_call("normal")
    if not _ok:
        print(f"nba_props_scanner: Odds API credit gate — {_why}")
        return []

    client = PolymarketUS()

    # Step 1: Get today's/next NBA events
    print("Fetching NBA games from Odds API...")
    try:
        events = fetch(f"{BASE}/sports/basketball_nba/odds?apiKey={KEY}&bookmakers=pinnacle&markets=h2h")
    except Exception as e:
        print(f"  ERROR fetching NBA games: {e}")
        return []

    if not events:
        print("  No active NBA games found.")
        return []

    game_data = []
    for ev in events:
        gid = ev["id"]
        away = ev["away_team"]
        home = ev["home_team"]
        start = ev.get("commence_time", "")
        print(f"  Game: {away} @ {home}  ({start[:19]})")

        # Step 2: Fetch player props from Pinnacle
        book_props = {}
        for mkt_key in STAT_LABELS:
            short = STAT_LABELS[mkt_key]
            try:
                d = fetch(
                    f"{BASE}/sports/basketball_nba/events/{gid}/odds?"
                    f"apiKey={KEY}&markets={mkt_key}&bookmakers=pinnacle&oddsFormat=american"
                )
                for bk in d.get("bookmakers", []):
                    for m in bk.get("markets", []):
                        players = {}
                        for o in m.get("outcomes", []):
                            name = o.get("description", "").lower()
                            pt = o.get("point", 0)
                            side = o.get("name", "")
                            pr = o.get("price", 0)
                            if name not in players:
                                players[name] = {"line": pt, "player": o.get("description", "")}
                            players[name][side] = pr

                        for name, s in players.items():
                            ov = s.get("Over", 0)
                            un = s.get("Under", 0)
                            if ov and un:
                                fair = devig(ov, un)
                                key = f"{name}|{short}"
                                book_props[key] = {
                                    "player": s.get("player", name.title()),
                                    "prop": short,
                                    "line": s["line"],
                                    "fair": fair,
                                    "price": ov,
                                    "ip": american_to_implied(ov),
                                }
            except Exception as ex:
                if verbose:
                    print(f"    {mkt_key}: {ex}")
            time.sleep(0.1)

        if verbose:
            print(f"  Pinnacle props loaded: {len(book_props)}")

        # Step 3: Get PM player props
        pm_markets = []
        for query in [
            f"{away} {home} 2026",
            "nba finals player points rebounds assists",
        ]:
            try:
                resp = client.search.query({"query": query})
                for evt in resp.get("events", []):
                    for m in evt.get("markets", []):
                        smt = m.get("sportsMarketType", "")
                        if smt in PM_SMT_LABELS:
                            pm_markets.append(m)
            except Exception as ex:
                if verbose:
                    print(f"  PM query '{query[:30]}': {ex}")
            time.sleep(0.3)

        # Deduplicate PM markets by slug
        seen_slugs = set()
        unique_markets = []
        for m in pm_markets:
            slug = m.get("slug", "")
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                unique_markets.append(m)

        if verbose:
            print(f"  PM player props found: {len(unique_markets)} (unique)")

        # Step 4: Match PM props to Pinnacle lines → edges
        edges = []
        for m in unique_markets:
            smt = m.get("sportsMarketType", "")
            if smt not in PM_SMT_LABELS:
                continue

            q = m.get("question", "")
            mtch = re.search(r"record at least (\d+)", q, re.I)
            if not mtch:
                continue
            thresh = int(mtch.group(1))

            meta = m.get("metadata", {})
            player = meta.get("playerName", "") or m.get("titleShort", "") or "?"
            pkey = player.lower()
            short_label = PM_SMT_LABELS[smt]
            delta_mult = DELTA_MAP.get(short_label, 0.06)

            book_key = f"{pkey}|{short_label}"
            bd = book_props.get(book_key)
            if not bd:
                continue

            # Adjust: PM threshold - 0.5 = equivalent book Over line
            pm_equiv = thresh - 0.5
            line_diff = abs(pm_equiv - bd["line"])

            # If thresholds don't match within tolerance, skip
            if line_diff > 2.0:
                continue

            # Convert book's fair value to the PM threshold
            delta = bd["line"] - pm_equiv
            adj_fair = min(0.95, max(0.02, bd["fair"] + delta * delta_mult))

            # Get PM price
            try:
                bbo = client.markets.bbo(m["slug"])
                md = bbo.get("marketData", {})
                cur = md.get("currentPx", {})
                pm_price = float(cur["value"]) if cur and cur.get("value") else None
                if pm_price is None:
                    lps = md.get("lastPriceSample", {})
                    pm_price = float(lps["longPx"]["value"]) if lps and lps.get("longPx", {}).get("value") else None
            except Exception:
                pm_price = None

            if pm_price is None or pm_price < 0.02:
                continue

            edge = adj_fair - pm_price
            if edge < 0 or edge > 0.20:
                continue

            edges.append(
                {
                    "player": player,
                    "prop": short_label,
                    "thresh": thresh,
                    "pm_price": pm_price,
                    "pm_cents": round(pm_price * 100),
                    "book_line": bd["line"],
                    "fair_value": adj_fair,
                    "fair_pct": round(adj_fair * 100),
                    "edge": edge,
                    "edge_pp": round(edge * 100, 1),
                    "slug": m.get("slug", ""),
                    "game": f"{away} @ {home}",
                    "game_time": start,
                }
            )

        edges.sort(key=lambda x: x["edge"], reverse=True)
        game_data.append(
            {
                "game": f"{away} @ {home}",
                "time": start,
                "edges": edges,
                "book_count": len(book_props),
                "pm_count": len(unique_markets),
            }
        )

    return game_data


def print_report(game_data, show_all=False):
    """Pretty-print the scan results."""
    for gd in game_data:
        edges = gd["edges"]
        print(f"\n{'=' * 70}")
        print(f"  {gd['game']}")
        print(f"  {gd['time'][:19]}")
        print(f"  Book lines: {gd['book_count']}  |  PM props matched: {len(edges)}")
        print(f"{'=' * 70}")

        if not edges:
            print("  No actionable edges found.")
            print("  (PM player props typically appear 2-4 hrs before tipoff)")
            continue

        # Header
        print(f"  {'Player':<22} {'Prop':<10} {'≥N':<4} {'PM¢':>5} {'Book Ln':>7} {'Fair%':>6} {'Edge':>7}")
        print(f"  {'-' * 64}")

        for e in edges:
            flag = "⚡" if e["edge_pp"] >= 5.0 else " "
            print(
                f"  {flag} {e['player']:<20} {e['prop']:<10} ≥{e['thresh']:<2}  {e['pm_cents']:>3}¢ {e['book_line']:>5.1f}  {e['fair_pct']:>3}%  +{e['edge_pp']:>4.1f}pp"
            )

        # Summary
        strong = [e for e in edges if e["edge_pp"] >= 5.0]
        if strong:
            print(f"\n  ⚡ **STRONG SIGNALS ({len(strong)}):**")
            for e in strong[:5]:
                print(
                    f"    - {e['player']} ≥{e['thresh']} {e['prop']} YES @ {e['pm_cents']}¢ → fair ~{e['fair_pct']}% ({e['edge_pp']:+.1f}pp)"
                )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NBA Props Edge Scanner")
    parser.add_argument("--full", action="store_true", help="Verbose debug output")
    parser.add_argument("--game", type=str, help="Filter by game name substring")
    args = parser.parse_args()

    results = scan_nba_props(verbose=args.full)

    if args.game:
        results = [g for g in results if args.game.lower() in g["game"].lower()]

    print_report(results, show_all=args.full)
