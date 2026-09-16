#!/usr/bin/env python3
"""
MLB Player Props Reconciliation — TheOddsAPI × Lineups × Live Feed
"""

import json, os, sys, time
from datetime import date, datetime, timezone
from pathlib import Path
import requests
from config.polymarket_urls import gamma_url  # polyproxy: central URL config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_env_key(key_name):
    """Load a key from env, then .env, then /etc/default/polyclawd."""
    val = os.environ.get(key_name, "")
    if val:
        return val
    for path in [
        Path(__file__).resolve().parent.parent / ".env",
        Path("/etc/default/polyclawd"),
    ]:
        if path.exists():
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(f"{key_name}="):
                            val = line.split("=", 1)[1].strip()
                            if val:
                                return val
            except Exception:
                pass
    return ""


ODDS_API_KEY = _load_env_key("ODDS_API_KEY")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MLB_STATS_API = "https://statsapi.mlb.com/api/v1"

# MLB player-prop markets. The Odds API uses batter_*/pitcher_* keys for MLB
# (NOT player_*, which are NFL/NBA). All keys below verified live on the 20K
# plan 2026-06-04 — player props ARE available; tiers differ by credit quota,
# not market access. Each prop market costs (markets x regions) credits per call.
PROP_MARKETS = {
    "batter_home_runs": "Batter Home Runs",
    "batter_hits": "Batter Hits",
    "batter_total_bases": "Batter Total Bases",
    "batter_rbis": "Batter RBIs",
    "batter_runs_scored": "Batter Runs Scored",
    "batter_strikeouts": "Batter Strikeouts",
    "pitcher_strikeouts": "Pitcher Strikeouts",
    "pitcher_hits_allowed": "Pitcher Hits Allowed",
    "pitcher_outs": "Pitcher Outs",
    "pitcher_record_a_win": "Pitcher Record a Win",
}
SHARP_BOOKS = {"Pinnacle", "FanDuel", "DraftKings", "BetMGM", "Caesars"}

# Credit guard: stop making Odds API calls if remaining credits drop below this
CREDITS_FLOOR = 300
_credits_remaining = None  # updated from response headers


def _fetch(url, timeout=15):
    global _credits_remaining
    try:
        r = requests.get(url, headers={"User-Agent": "Polyclawd/2.0"}, timeout=timeout)
        # Track credits from every response header
        remaining = r.headers.get("x-requests-remaining")
        if remaining is not None:
            _credits_remaining = int(remaining)
        # Record real balance + low-credit alert for PAID (the-odds-api) calls only
        if "api.the-odds-api.com" in url:
            try:
                from odds.the_odds_api import _track_credits_from_response

                _track_credits_from_response(r)
            except Exception:
                pass
        return r.json() if r.status_code == 200 else None
    except:
        return None


def _credits_ok(needed=1):
    """Return True if we have enough credits to proceed, or if count is unknown."""
    if _credits_remaining is None:
        return True  # haven't seen a header yet, allow
    if _credits_remaining < CREDITS_FLOOR:
        print(f"  ⚠️  Credits low ({_credits_remaining} remaining, floor={CREDITS_FLOOR}) — skipping API call")
        return False
    return True


def odds_to_american(decimal):
    if decimal >= 2.0:
        return f"+{int((decimal - 1) * 100)}"
    elif decimal > 1.0:
        return f"{int(-100 / (decimal - 1))}"
    return "?"


def imp_prob_from_american(am_str):
    if am_str == "?":
        return 0
    odds = int(am_str.replace("+", ""))
    return (100.0 / (odds + 100)) if odds > 0 else (abs(odds) / (abs(odds) + 100))


def get_mlb_schedule(date_str=None):
    if date_str is None:
        date_str = date.today().isoformat()
    data = _fetch(f"{MLB_STATS_API}/schedule?sportId=1&hydrate=lineups,probablePitcher,boxscore&date={date_str}")
    if not data:
        return []
    games = []
    for de in data.get("dates", []):
        games.extend(de.get("games", []))
    return games


def get_todays_odds_games():
    from odds.rate_limiter import can_make_call

    _ok, _why = can_make_call("normal")
    if not _ok:
        print(f"mlb_props_reconcile: Odds API credit gate — {_why}")
        return []
    data = _fetch(
        f"{ODDS_API_BASE}/sports/baseball_mlb/odds?apiKey={ODDS_API_KEY}&regions=us&markets=h2h&dateFormat=iso"
    )
    return data if isinstance(data, list) else []


def get_all_props(game_id):
    """Fetch every PROP_MARKETS market in a single event-odds call.

    Cost = (markets that return data) x regions credits. We use regions=us only
    because every book in SHARP_BOOKS is a US book; adding eu,au would triple the
    credit cost for books the parser ignores. Returns the raw event-odds JSON.
    """
    markets = ",".join(PROP_MARKETS.keys())
    from odds.rate_limiter import can_make_call

    _ok, _why = can_make_call("normal")
    if not _ok:
        print(f"mlb_props_reconcile: Odds API credit gate — {_why}")
        return None
    if not _credits_ok(needed=len(PROP_MARKETS)):
        return None
    return _fetch(
        f"{ODDS_API_BASE}/sports/baseball_mlb/events/{game_id}/odds?apiKey={ODDS_API_KEY}&regions=us&markets={markets}&oddsFormat=decimal&dateFormat=iso",
        timeout=25,
    )


def find_game(odds_games, schedule, away_hint=None, home_hint=None):
    """Find a game from odds and schedule data.

    If away_hint/home_hint are given (lowercase substrings), match that game.
    Otherwise pick the first odds game that starts soonest (next to play).
    """
    og = gi = sg = None

    if away_hint and home_hint:
        for g in odds_games:
            a, h = (g.get("away_team", "") or "").lower(), (g.get("home_team", "") or "").lower()
            if away_hint in a and home_hint in h:
                og, gi = g, g.get("id")
                break

    if not og:
        # Auto-detect: pick the game starting soonest that hasn't started yet.
        # Do NOT fall back to in-progress games — Pinnacle closes props at first
        # pitch, so fetching a started game always produces "Pinnacle absent" noise.
        now = datetime.now(timezone.utc).isoformat()
        upcoming = [g for g in odds_games if (g.get("commence_time", "") or "") >= now]
        if upcoming:
            upcoming.sort(key=lambda g: g.get("commence_time", ""))
            og = upcoming[0]
            gi = og.get("id")

    if not og:
        return None, None, None

    # Match against MLB schedule for lineups
    og_away = (og.get("away_team", "") or "").lower()
    og_home = (og.get("home_team", "") or "").lower()
    for g in schedule:
        a = ((g.get("teams", {}).get("away", {}).get("team", {}).get("name", "")) or "").lower()
        h = ((g.get("teams", {}).get("home", {}).get("team", {}).get("name", "")) or "").lower()
        # Match by checking if any word from the odds team name appears in the schedule team name
        away_words = [w for w in og_away.split() if len(w) > 3]
        home_words = [w for w in og_home.split() if len(w) > 3]
        if any(w in a for w in away_words) and any(w in h for w in home_words):
            sg = g
            break

    return og, sg, gi


def get_lineup(sg):
    res = {"away_batters": [], "home_batters": [], "away_pitcher": None, "home_pitcher": None, "teams": {}}
    if not sg:
        return res
    lu = sg.get("lineups", {})
    for key, side in [("awayPlayers", "away"), ("homePlayers", "home")]:
        batters = []
        for p in lu.get(key, []):
            fn = p.get("fullName", "")
            pos = p.get("primaryPosition", {}).get("abbreviation", "")
            if fn:
                batters.append(f"{fn} ({pos})")
        res[f"{side}_batters"] = batters
    for side in ("away", "home"):
        p = sg.get("teams", {}).get(side, {}).get("probablePitcher", {}).get("fullName", "")
        res[f"{side}_pitcher"] = p
    res["teams"] = {s: sg.get("teams", {}).get(s, {}).get("team", {}).get("name", "?") for s in ("away", "home")}
    return res


def parse_props(data, mkt_key, lineup):
    if not data or not data.get("bookmakers"):
        return []
    results = []
    for bk_name in SHARP_BOOKS:
        bk = next((b for b in data["bookmakers"] if b["title"] == bk_name), None)
        if not bk:
            continue
        for market in bk.get("markets", []):
            if market.get("key") != mkt_key:
                continue  # response holds all PROP_MARKETS; keep only this one
            outcomes = market.get("outcomes", [])
            if not outcomes:
                continue
            pts = outcomes[0].get("point", "-")

            def _ip(price):
                am = odds_to_american(price)
                return round(imp_prob_from_american(am) * 100, 1) if am != "?" else 0

            # Outcomes come in Over/Under pairs per player.
            # The API includes "description" with the player name.
            i = 0
            while i < len(outcomes):
                o = outcomes[i]
                u = (
                    outcomes[i + 1]
                    if i + 1 < len(outcomes) and outcomes[i + 1].get("description") == o.get("description")
                    else None
                )
                player = o.get("description") or o.get("name") or f"Player {i // 2 + 1}"
                results.append(
                    {
                        "player": player,
                        "book": bk_name,
                        "market": mkt_key,
                        "line": pts,
                        "over_odds": odds_to_american(o["price"]),
                        "over_ip": _ip(o["price"]),
                        "under_odds": odds_to_american(u["price"]) if u else "?",
                        "under_ip": _ip(u["price"]) if u else 0,
                    }
                )
                i += 2 if u else 1
    return results


def print_recon(og, lineup, pbm):
    print("\n" + "=" * 80)
    print("MLB PLAYER PROPS RECONCILIATION — TheOddsAPI × Lineups × Screenshot")
    print("=" * 80)
    if og:
        print(f"\n📅 {og.get('away_team')} @ {og.get('home_team')}  ⏰ {og.get('commence_time', '')[:19]}")
    t = lineup.get("teams", {})
    if t:
        print(f"🏟  {t.get('away')} @ {t.get('home')}")
    print(f"\n{'─' * 40} LINEUPS {'─' * 40}")
    for side in ("away", "home"):
        name = t.get(side, "")
        batters = lineup.get(f"{side}_batters", [])
        pitcher = lineup.get(f"{side}_pitcher", "")
        print(f"  {side.upper()} ({name}):")
        for b in batters:
            print(f"    • {b}")
        if pitcher:
            print(f"    ⭐ SP: {pitcher}")

    # Game lines (live via alternate_spreads to show score context)
    print(f"\n{'─' * 40} GAME LINES {'─' * 40}")
    gdata = (
        _fetch(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events/{og.get('id')}/odds?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals,alternate_spreads&dateFormat=iso"
        )
        if _credits_ok()
        else None
    )
    if gdata:
        for bk in gdata.get("bookmakers", []):
            if bk["title"] in ("FanDuel", "BetMGM", "DraftKings"):
                for m in bk.get("markets", []):
                    outcomes = ", ".join(
                        [
                            f"{o.get('name', '?')} {odds_to_american(o['price'])} {'o' + str(o.get('point', '')) if o.get('point') else ''}"
                            for o in m.get("outcomes", [])
                        ]
                    )
                    print(f"  {bk['title']:12s} {m['key']:18s}: {outcomes[:100]}")

    # Player props are available on the current plan; an empty pbm means no book
    # has posted props for THIS game yet (common for early/day games — lines fill
    # in closer to first pitch), not a plan restriction.
    if not pbm:
        print(f"\n{'─' * 40} PLAYER PROPS {'─' * 40}")
        print("  ⚪ No player props posted by sharp books for this game yet")
        print("     (props typically fill in closer to first pitch — re-run later)")
        print(f"\n{'─' * 40} MONEYLINE CONSENSUS (Weighted Devig) {'─' * 40}")
        print(f"{'Team':<28s} {'Consensus':>10s} {'Books':>8s} {'Pinnacle':>10s} {'FD':>10s}")
        print(f"{'─' * 70}")
        if og:
            from odds.sports_edge_common import consensus_devig_2way, BOOK_WEIGHTS
            from odds.the_odds_api import _american_to_implied_prob

            cons = consensus_devig_2way(og)
            if cons:
                for team, prob in sorted(cons.items(), key=lambda x: -x[1]):
                    pct = f"{prob * 100:.1f}%"
                    nbooks = len([bk for bk in og.get("bookmakers", []) if bk.get("key", "") in BOOK_WEIGHTS])
                    # Get pinnacle and FD for comparison
                    pin = fd = "N/A"
                    for bk in og.get("bookmakers", []):
                        k = bk.get("key", "")
                        if k == "pinnacle":
                            for m in bk.get("markets", []):
                                if m.get("key") == "h2h":
                                    for o in m.get("outcomes", []):
                                        if o.get("name") == team:
                                            raw = 1.0 / float(o["price"])  # decimal odds
                                            pin = f"{raw * 100:.1f}%"
                        if k == "fanduel":
                            for m in bk.get("markets", []):
                                if m.get("key") == "h2h":
                                    for o in m.get("outcomes", []):
                                        if o.get("name") == team:
                                            raw = 1.0 / float(o["price"])  # decimal odds
                                            fd = f"{raw * 100:.1f}%"
                    print(f"{team:<28s} {pct:>10s} {nbooks:>8d} {pin:>10s} {fd:>10s}")
    else:
        for mkt_key, mkt_label in PROP_MARKETS.items():
            entries = pbm.get(mkt_key, [])
            entries.sort(key=lambda x: (x["book"], x["player"]))
            if not entries:
                continue
            print(f"\n{'─' * 40} {mkt_label} {'─' * 40}")
            print(f"{'Player':<28s} {'Book':<12s} {'Line':<6s} {'OVER':>12s} {'UNDER':>12s}")
            print(f"{'─' * 70}")
            for e in entries:
                os_ = f"{e['over_odds']:>6s} ({e['over_ip']:05.1f}%)"
                us_ = f"{e['under_odds']:>6s} ({e['under_ip']:05.1f}%)" if e["under_odds"] != "?" else "      N/A"
                print(f"{e['player']:<28s} {e['book']:<12s} o{str(e['line']):<4s} {os_:>12s} {us_:>12s}")

        # Cross-book comparison
        print(f"\n{'─' * 40} CROSS-BOOK COMPARISON {'─' * 40}")
        for mkt_key, mkt_label in PROP_MARKETS.items():
            entries = pbm.get(mkt_key, [])
            if not entries:
                continue
            print(f"\n🏏 {mkt_label}")
            print(f"{'Player':<28s} {'Pinnacle':>10s} {'FanDuel':>10s} {'DraftKings':>10s} {'Max Δ':>8s}")
            print(f"{'─' * 70}")
            players = {}
            for e in entries:
                players.setdefault(e["player"], []).append(e)
            for pname, elist in sorted(players.items()):
                by_book = {e["book"]: e["over_ip"] for e in elist}
                pinn = by_book.get("Pinnacle")
                fd = by_book.get("FanDuel")
                dk = by_book.get("DraftKings")
                vals = [v for v in [pinn, fd, dk] if v is not None and v > 0]
                delta = f"{max(vals) - min(vals):+.1f}pp" if len(vals) >= 2 else ""
                ps = f"{pinn:.1f}%" if pinn else "N/A"
                fs = f"{fd:.1f}%" if fd else "N/A"
                ds = f"{dk:.1f}%" if dk else "N/A"
                print(f"{pname:<28s} {ps:>10s} {fs:>10s} {ds:>10s} {delta:>8s}")

    # Polymarket check: find game-level markets (spread/moneyline) for this game
    # Polymarket has game lines for MLB but NOT individual player props.
    # Use /events?tag_slug=baseball to get only baseball markets (not all markets).
    print(f"\n{'─' * 40} POLYMARKET CHECK {'─' * 40}")
    away_team = (og.get("away_team", "") or "").lower()
    home_team = (og.get("home_team", "") or "").lower()
    # Use team city/nickname words (>4 chars) to match event titles
    away_words = [w for w in away_team.split() if len(w) > 4]
    home_words = [w for w in home_team.split() if len(w) > 4]
    events = _fetch(gamma_url("/events?tag_slug=baseball&limit=100&closed=false"))
    found = False
    if events:
        event_list = events if isinstance(events, list) else events.get("data", [])
        for ev in event_list:
            title = (ev.get("title", "") or "").lower()
            if any(w in title for w in away_words) and any(w in title for w in home_words):
                for m in ev.get("markets", []):
                    q = m.get("question", "")
                    prices = m.get("outcomePrices", [])
                    outcomes = m.get("outcomes", [])
                    try:
                        out_labels = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                        price_list = json.loads(prices) if isinstance(prices, str) else prices
                        pairs = list(zip(out_labels, [f"{float(p) * 100:.1f}%" for p in price_list]))
                        price_str = " | ".join(f"{k}: {v}" for k, v in pairs)
                    except Exception:
                        price_str = str(prices)
                    print(f"  🟢 {q}: {price_str}")
                    found = True
    if not found:
        print("  ⚪ No Polymarket game markets found for this matchup")
    print("\n" + "=" * 80)


def main():
    print(f"\n📡 MLB Props — {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}  Key: {'✅' if ODDS_API_KEY else '❌'}")
    if not ODDS_API_KEY:
        print("ERROR")
        sys.exit(1)
    schedule = get_mlb_schedule()
    odds = get_todays_odds_games()
    cr = f"{_credits_remaining} remaining" if _credits_remaining is not None else "unknown"
    print(f"  MLB: {len(schedule)} games | OddsAPI: {len(odds)} | Credits: {cr}")
    og, sg, gi = find_game(odds, schedule)
    if not og:
        # No pre-game games found — all games have started or slate is empty.
        # Exit silently so the cron fires NO_REPLY instead of a noisy alert.
        print("NO_REPLY")
        sys.exit(0)
    print(f"  ✅ {og.get('away_team')} @ {og.get('home_team')}")
    lineup = get_lineup(sg)

    # Player props are available on the current plan. Fetch all prop markets in
    # one event-odds call (regions=us), then split the response per market.
    pbm = {}
    props_data = get_all_props(gi)
    if props_data:
        for mk in PROP_MARKETS:
            parsed = parse_props(props_data, mk, lineup)
            if parsed:
                pbm[mk] = parsed
    else:
        print("  Props: no event-odds returned (credit floor or no lines yet)")

    print_recon(og, lineup, pbm)
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "game": f"{og.get('away_team')} @ {og.get('home_team')}",
        "game_id": gi,
        "lineup": lineup,
        "props": pbm,
    }
    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"mlb_props_{date.today().isoformat()}.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"💾 output/mlb_props_{date.today().isoformat()}.json")


if __name__ == "__main__":
    main()
