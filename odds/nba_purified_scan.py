#!/usr/bin/env python3
"""
NBA Props Edge Scanner — Poisson-guardrail version.

METHODOLOGY:
  1. Fetch Pinnacle lines → devig → fair value for book's Over line
  2. Fetch PM prices via Polymarket US SDK
  3. Interpolate book fair to PM threshold (cap at 2 steps max)
  4. Poisson guardrail: P(≥threshold | player's RS average) must be >= PM price - 8pp
  5. Grade: A=≤1 step+Poisson✓  B=≤1 step  C=≤2 steps+Poisson✓  F=skip

Usage:
  python3 odds/nba_purified_scan.py
  python3 odds/nba_purified_scan.py --full    (show all grades)
  python3 odds/nba_purified_scan.py --grade A (filter to A only)
"""

import os, sys, json, urllib.request, math, re

sys.path.insert(0, os.path.expanduser("~/Desktop/polyclawd"))

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None
    print("WARN: polymarket-us SDK not available. pip install polymarket-us")

KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"

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


def poisson_pge(lam, k):
    """P(>= k) for Poisson distribution with mean lambda."""
    return 1 - sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k + 1))


# Player RS averages (source: Basketball Reference 2025-26)
# Fields: pts=points, reb=rebounds, ast=assists, thr=3PM, blk=blocks
PLAYER_AVG = {
    "Jalen Brunson": {"ast": 6.8, "pts": 26.5, "reb": 3.0, "thr": 2.5, "blk": 0.3},
    "Mikal Bridges": {"ast": 2.5, "pts": 17.0, "reb": 3.5, "thr": 1.8, "blk": 0.6},
    "Josh Hart": {"ast": 4.0, "pts": 10.0, "reb": 8.5, "thr": 0.9, "blk": 0.3},
    "Karl-Anthony Towns": {"ast": 3.0, "pts": 20.1, "reb": 11.9, "thr": 1.5, "blk": 0.7},
    "De'Aaron Fox": {"ast": 6.0, "pts": 23.0, "reb": 3.5, "thr": 1.8, "blk": 0.4},
    "Stephon Castle": {"ast": 5.0, "pts": 15.0, "reb": 5.5, "thr": 0.8, "blk": 0.4},
    "Victor Wembanyama": {"ast": 3.5, "pts": 28.0, "reb": 12.0, "thr": 1.2, "blk": 3.5},
    "OG Anunoby": {"ast": 2.0, "pts": 15.0, "reb": 5.0, "thr": 1.5, "blk": 0.6},
    "Dylan Harper": {"ast": 4.5, "pts": 14.5, "reb": 5.0, "thr": 1.5, "blk": 0.3},
    "Devin Vassell": {"ast": 3.0, "pts": 15.0, "reb": 3.5, "thr": 2.0, "blk": 0.4},
    "Mitchell Robinson": {"ast": 0.5, "pts": 5.5, "reb": 8.5, "thr": 0.0, "blk": 1.5},
    "Miles McBride": {"ast": 2.0, "pts": 7.0, "reb": 1.5, "thr": 1.5, "blk": 0.1},
    "Julian Champagnie": {"ast": 1.5, "pts": 8.0, "reb": 3.0, "thr": 1.8, "blk": 0.3},
    "Landry Shamet": {"ast": 1.5, "pts": 7.5, "reb": 1.5, "thr": 1.5, "blk": 0.1},
    "Keldon Johnson": {"ast": 1.5, "pts": 10.0, "reb": 2.5, "thr": 0.8, "blk": 0.1},
    "Jose Alvarado": {"ast": 2.5, "pts": 5.0, "reb": 1.5, "thr": 0.5, "blk": 0.0},
    "Luke Kornet": {"ast": 1.0, "pts": 4.0, "reb": 3.5, "thr": 0.0, "blk": 0.5},
}

# Per-half-step degradation rates (calibrated from historical data)
STEP_COST = {"pts": 0.06, "reb": 0.07, "ast": 0.08, "thr": 0.10, "blk": 0.12}

# PM sportsMarketType → stat key
SMT_MAP = {
    "basketball_player_points": "pts",
    "basketball_player_rebounds": "reb",
    "basketball_player_assists": "ast",
    "basketball_player_threes": "thr",
    "basketball_player_blocks": "blk",
}

# ─── scan logic ───


def scan_nba(api_key=None):
    """Run full scan. Returns (game_name, results_list)."""
    if api_key:
        globals()["KEY"] = api_key
    global KEY

    if not KEY:
        print("ERROR: No ODDS_API_KEY in environment")
        return "", []

    if not PolymarketUS:
        print("ERROR: polymarket-us SDK not installed")
        return "", []

    from odds.rate_limiter import can_make_call

    _ok, _why = can_make_call("normal")
    if not _ok:
        print(f"nba_purified_scan: Odds API credit gate — {_why}")
        return "", []

    # 1. Get NBA event
    events = fetch(f"{BASE}/sports/basketball_nba/odds?apiKey={KEY}&bookmakers=pinnacle&markets=h2h")
    if not events:
        print("No NBA events found")
        return "", []
    eid = events[0]["id"]
    game = f"{events[0]['away_team']} @ {events[0]['home_team']}"

    # 2. Build book lines from Pinnacle
    ODDS_SHORT = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast"}
    book = {}
    for mkt_key in ["player_points", "player_rebounds", "player_assists"]:
        short = ODDS_SHORT[mkt_key]
        try:
            d = fetch(
                f"{BASE}/sports/basketball_nba/events/{eid}/odds?"
                f"apiKey={KEY}&markets={mkt_key}&bookmakers=pinnacle&oddsFormat=american"
            )
            for bk in d.get("bookmakers", []):
                for m in bk.get("markets", []):
                    ov = un = None
                    pn = ""
                    for o in m.get("outcomes", []):
                        pn = o.get("description", "")
                        if "over" in o.get("name", "").lower():
                            ov = {"pt": o.get("point", 0), "pr": o.get("price", 0)}
                        if "under" in o.get("name", "").lower():
                            un = {"pt": o.get("point", 0), "pr": o.get("price", 0)}
                    if ov and un:
                        book[f"{pn.lower()}|{short}"] = {
                            "line": ov["pt"],
                            "fair": devig(ov["pr"], un["pr"]),
                            "player": pn,
                        }
        except Exception as e:
            if api_key:
                print(f"  {mkt_key}: {e}")

    # 3. Get PM player props
    client = PolymarketUS()
    pm_markets = []
    try:
        resp = client.search.query({"query": "san antonio new york 2026"})
        for ev in resp.get("events", []):
            for m in ev.get("markets", []):
                if m.get("sportsMarketType", "") in SMT_MAP:
                    pm_markets.append(m)
    except Exception as e:
        print(f"PM search error: {e}")
        return game, []

    # 4. Cross-reference and grade
    seen = set()
    results = []

    for m in pm_markets:
        slug = m.get("slug", "")
        if not slug or slug in seen:
            continue
        seen.add(slug)

        smt = m.get("sportsMarketType", "")
        short = SMT_MAP.get(smt)
        if not short:
            continue

        q = m.get("question", "")
        mtch = re.search(r"record at least (\d+)", q, re.I)
        if not mtch:
            continue
        thresh = int(mtch.group(1))

        meta = m.get("metadata", {})
        player = meta.get("playerName", "") or m.get("titleShort", "") or "?"
        bd = book.get(f"{player.lower()}|{short}")
        if not bd:
            continue

        # Get PM price
        try:
            bbo = client.markets.bbo(slug)
            md = bbo.get("marketData", {})
            cur = md.get("currentPx", {})
            pm = float(cur["value"]) if cur and cur.get("value") else None
            if pm is None:
                lps = md.get("lastPriceSample", {})
                pm = float(lps["longPx"]["value"]) if lps and lps.get("longPx", {}).get("value") else None
        except:
            pm = None
        if pm is None:
            continue

        # Steps from book line to PM threshold (in half-assist increments)
        half_steps = (thresh - 0.5 - bd["line"]) / 0.5
        if half_steps < 0:
            continue

        # Interpolate fair value
        step_cost = STEP_COST.get(short, 0.07)
        adj_fair = min(0.95, max(0.02, bd["fair"] - half_steps * step_cost))

        # Poisson guardrail
        avg = PLAYER_AVG.get(player, {}).get(short)
        poisson_val = poisson_pge(avg, thresh - 1) if avg else None

        edge = adj_fair - pm
        if edge < 0:
            continue

        # Grade
        poisson_ok = poisson_val is not None and poisson_val >= pm - 0.08  # 8pp tolerance
        if half_steps <= 1 and poisson_ok:
            grade = "A"
        elif half_steps <= 1:
            grade = "B"
        elif half_steps <= 2 and poisson_ok:
            grade = "C"
        elif half_steps <= 2:
            grade = "D"
        else:
            grade = "F"

        results.append(
            {
                "player": player,
                "prop": short,
                "thresh": thresh,
                "pm": pm,
                "book_line": bd["line"],
                "book_fair_pct": round(bd["fair"] * 100),
                "fair": adj_fair,
                "half_steps": half_steps,
                "poisson": poisson_val,
                "edge": edge,
                "grade": grade,
            }
        )

    results.sort(key=lambda x: x["edge"], reverse=True)
    return game, results


def print_report(game, results, grade_filter=None):
    """Pretty-print results."""
    if not results:
        print(f"\nNo NBA prop edges found for {game}.")
        print("(PM player props typically appear 2-4 hrs before tipoff)")
        return

    filtered = results if not grade_filter else [r for r in results if r["grade"] == grade_filter]
    if grade_filter and not filtered:
        print(f"\nNo Grade {grade_filter} bets found.")
        return

    print(f"\n{'=' * 90}")
    print(f"  {game} — Purified Edge Scan")
    print(f"  A=≤1 step+Poisson✓  B=≤1 step  C=≤2 steps+Poisson✓  D=≤2 steps  F=skip")
    print(f"{'=' * 90}")
    print(
        f"  {'Player':<18} {'Prop':<6} {'≥N':<4} {'PM¢':>5} {'Devig%':>7} {'Fair%':>6} {'Steps':>6} {'Poisson':>8} {'Edge':>7}  {'Grade'}"
    )
    print(f"  {'-' * 75}")

    for r in filtered:
        pfs = f"{r['poisson'] * 100:.0f}%" if r["poisson"] else "N/A"
        flag = "⚡" if r["grade"] == "A" else " "
        print(
            f"  {flag}{r['player']:<17} {r['prop']:<6} ≥{r['thresh']:<2}  "
            f"{r['pm'] * 100:>3.0f}¢ {r['book_fair_pct']:>5}% {r['fair'] * 100:>5.1f}% "
            f"{r['half_steps']:>4.0f}  {pfs:>6}  +{r['edge'] * 100:>4.1f}pp  {r['grade']}"
        )

    # Summary
    grades = {}
    for r in filtered:
        grades.setdefault(r["grade"], []).append(r)

    print()
    for g in ["A", "B", "C", "D"]:
        if g in grades:
            print(f"  Grade {g} ({len(grades[g])} bets):")
            for r in grades[g][:5]:
                print(
                    f"    {r['player']} ≥{r['thresh']} {r['prop']} YES @ {r['pm'] * 100:.0f}¢  "
                    f"edge +{r['edge'] * 100:.0f}pp  ({r['half_steps']:.0f} steps, "
                    f"Poisson {r['poisson'] * 100:.0f}%)"
                )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Purified NBA Prop Edge Scanner")
    parser.add_argument("--full", action="store_true", help="Show all grades including D")
    parser.add_argument("--grade", type=str, choices=["A", "B", "C", "D"], help="Filter by grade")
    args = parser.parse_args()

    game, results = scan_nba()
    print_report(game, results, grade_filter=args.grade)
