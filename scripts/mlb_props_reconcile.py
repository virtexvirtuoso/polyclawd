#!/usr/bin/env python3
"""
MLB Player Props Reconciliation — TheOddsAPI × Lineups × Live Feed
"""

import json, os, sys, time
from datetime import date, datetime, timezone
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
if not ODDS_API_KEY:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ODDS_API_KEY="):
                    ODDS_API_KEY = line.split("=", 1)[1].strip()

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MLB_STATS_API = "https://statsapi.mlb.com/api/v1"

PROP_MARKETS = {
    "batter_home_runs": "HR",
    "pitcher_strikeouts": "K",
    "batter_hits": "Hits",
    "batter_rbis": "RBI",
    "batter_total_bases": "TB",
}
SHARP_BOOKS = {"Pinnacle", "FanDuel", "DraftKings", "BetMGM", "Caesars"}

def _fetch(url, timeout=15):
    try:
        r = requests.get(url, headers={"User-Agent": "Polyclawd/2.0"}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def odds_to_american(decimal):
    if decimal >= 2.0: return f"+{int((decimal-1)*100)}"
    elif decimal > 1.0: return f"{int(-100/(decimal-1))}"
    return "?"

def imp_prob_from_american(am_str):
    if am_str == "?": return 0
    odds = int(am_str.replace("+",""))
    return (100.0/(odds+100)) if odds > 0 else (abs(odds)/(abs(odds)+100))

def get_mlb_schedule(date_str=None):
    if date_str is None: date_str = date.today().isoformat()
    data = _fetch(f"{MLB_STATS_API}/schedule?sportId=1&hydrate=lineups,probablePitcher,boxscore&date={date_str}")
    if not data: return []
    games = []
    for de in data.get("dates", []): games.extend(de.get("games", []))
    return games

def get_todays_odds_games():
    data = _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/odds?apiKey={ODDS_API_KEY}&regions=us&markets=h2h&dateFormat=iso")
    return data if isinstance(data, list) else []

def get_props(game_id, market):
    return _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/events/{game_id}/odds?apiKey={ODDS_API_KEY}&regions=us,eu,au&markets={market}&dateFormat=iso", timeout=20)

def find_game(odds_games, schedule, away_hint=None, home_hint=None):
    """Find a game from odds and schedule data.
    
    If away_hint/home_hint are given (lowercase substrings), match that game.
    Otherwise pick the first odds game that starts soonest (next to play).
    """
    og = gi = sg = None

    if away_hint and home_hint:
        for g in odds_games:
            a, h = (g.get("away_team","") or "").lower(), (g.get("home_team","") or "").lower()
            if away_hint in a and home_hint in h:
                og, gi = g, g.get("id"); break
    
    if not og:
        # Auto-detect: pick the game starting soonest that hasn't started yet
        now = datetime.now(timezone.utc).isoformat()
        upcoming = [g for g in odds_games if (g.get("commence_time","") or "") >= now]
        if not upcoming:
            upcoming = odds_games  # all games started — pick first anyway
        if upcoming:
            upcoming.sort(key=lambda g: g.get("commence_time",""))
            og = upcoming[0]
            gi = og.get("id")

    if not og:
        return None, None, None

    # Match against MLB schedule for lineups
    og_away = (og.get("away_team","") or "").lower()
    og_home = (og.get("home_team","") or "").lower()
    for g in schedule:
        a = ((g.get("teams",{}).get("away",{}).get("team",{}).get("name","")) or "").lower()
        h = ((g.get("teams",{}).get("home",{}).get("team",{}).get("name","")) or "").lower()
        # Match by checking if any word from the odds team name appears in the schedule team name
        away_words = [w for w in og_away.split() if len(w) > 3]
        home_words = [w for w in og_home.split() if len(w) > 3]
        if any(w in a for w in away_words) and any(w in h for w in home_words):
            sg = g; break

    return og, sg, gi

def get_lineup(sg):
    res = {"away_batters":[],"home_batters":[],"away_pitcher":None,"home_pitcher":None,"teams":{}}
    if not sg: return res
    lu = sg.get("lineups",{})
    for key, side in [("awayPlayers","away"),("homePlayers","home")]:
        batters = []
        for p in lu.get(key,[]):
            fn = p.get("fullName",""); pos = p.get("primaryPosition",{}).get("abbreviation","")
            if fn: batters.append(f"{fn} ({pos})")
        res[f"{side}_batters"] = batters
    for side in ("away","home"):
        p = sg.get("teams",{}).get(side,{}).get("probablePitcher",{}).get("fullName","")
        res[f"{side}_pitcher"] = p
    res["teams"] = {s: sg.get("teams",{}).get(s,{}).get("team",{}).get("name","?") for s in ("away","home")}
    return res

def parse_props(data, mkt_key, lineup):
    if not data or not data.get("bookmakers"): return []
    results = []
    for bk_name in SHARP_BOOKS:
        bk = next((b for b in data["bookmakers"] if b["title"] == bk_name), None)
        if not bk: continue
        for market in bk.get("markets",[]):
            outcomes = market.get("outcomes",[]); pts = outcomes[0].get("point","-") if outcomes else "-"
            pairs = []
            i = 0
            while i < len(outcomes):
                o = outcomes[i]; u = outcomes[i+1] if i+1 < len(outcomes) else None
                def _ip(price):
                    am = odds_to_american(price)
                    return round(imp_prob_from_american(am)*100, 1) if am != "?" else 0
                pairs.append({"over":{"price":o["price"],"ip":_ip(o["price"])},"under":{"price":u["price"],"ip":_ip(u["price"])} if u else None})
                i += 2
            abb = lineup.get("away_batters",[]); hbb = lineup.get("home_batters",[])
            ap = lineup.get("away_pitcher",""); hp = lineup.get("home_pitcher","")
            ordered = [ap, hp] if mkt_key == "pitcher_strikeouts" else abb + hbb
            for idx, pair in enumerate(pairs):
                pn = ordered[idx] if idx < len(ordered) else f"Batter {idx+1}"
                results.append({"player":pn,"book":bk_name,"market":mkt_key,"line":pts,
                    "over_odds":odds_to_american(pair["over"]["price"]),"over_ip":pair["over"]["ip"],
                    "under_odds":odds_to_american(pair["under"]["price"]) if pair.get("under") else "?",
                    "under_ip":pair["under"]["ip"] if pair.get("under") else 0})
    return results

def print_recon(og, lineup, pbm):
    print("\n" + "="*80)
    print("MLB PLAYER PROPS RECONCILIATION — TheOddsAPI × Lineups × Screenshot")
    print("="*80)
    if og: print(f"\n📅 {og.get('away_team')} @ {og.get('home_team')}  ⏰ {og.get('commence_time','')[:19]}")
    t = lineup.get("teams",{})
    if t: print(f"🏟  {t.get('away')} @ {t.get('home')}")
    print(f"\n{'─'*40} LINEUPS {'─'*40}")
    for side in ("away","home"):
        name = t.get(side,"")
        batters = lineup.get(f"{side}_batters",[]); pitcher = lineup.get(f"{side}_pitcher","")
        print(f"  {side.upper()} ({name}):")
        for b in batters: print(f"    • {b}")
        if pitcher: print(f"    ⭐ SP: {pitcher}")

    # Game lines (live via alternate_spreads to show score context)
    print(f"\n{'─'*40} GAME LINES (Live/Alternate) {'─'*40}")
    gdata = _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/events/{og.get('id')}/odds?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals,alternate_spreads&dateFormat=iso")
    if gdata:
        for bk in gdata.get("bookmakers",[]):
            if bk["title"] in ("FanDuel","BetMGM","DraftKings"):
                for m in bk.get("markets",[]):
                    outcomes = ", ".join([f"{o.get('name','?')} {odds_to_american(o['price'])} {'o'+str(o.get('point','')) if o.get('point') else ''}" for o in m.get("outcomes",[])])
                    print(f"  {bk['title']:12s} {m['key']:18s}: {outcomes[:100]}")

    for mkt_key, mkt_label in PROP_MARKETS.items():
        entries = pbm.get(mkt_key,[]); entries.sort(key=lambda x: (x["book"], x["player"]))
        if not entries: continue
        print(f"\n{'─'*40} {mkt_label} {'─'*40}")
        print(f"{'Player':<28s} {'Book':<12s} {'Line':<6s} {'OVER':>12s} {'UNDER':>12s}")
        print(f"{'─'*70}")
        for e in entries:
            os_ = f"{e['over_odds']:>6s} ({e['over_ip']:05.1f}%)"
            us_ = f"{e['under_odds']:>6s} ({e['under_ip']:05.1f}%)" if e['under_odds']!='?' else '      N/A'
            print(f"{e['player']:<28s} {e['book']:<12s} o{str(e['line']):<4s} {os_:>12s} {us_:>12s}")

    # Cross-book comparison: Pinnacle vs other sharp books for all players
    print(f"\n{'─'*40} CROSS-BOOK COMPARISON {'─'*40}")
    for mkt_key, mkt_label in [("batter_home_runs", "HOME RUNS"), ("pitcher_strikeouts", "STRIKEOUTS")]:
        entries = pbm.get(mkt_key, [])
        if not entries:
            continue
        # Group by player
        players = {}
        for e in entries:
            players.setdefault(e["player"], []).append(e)
        print(f"\n🏏 {mkt_label}")
        print(f"{'Player':<28s} {'Pinnacle':>10s} {'FanDuel':>10s} {'DraftKings':>10s} {'Max Δ':>8s}")
        print(f"{'─'*70}")
        for pname, elist in sorted(players.items()):
            by_book = {e["book"]: e["over_ip"] for e in elist}
            pinn = by_book.get("Pinnacle")
            fd = by_book.get("FanDuel")
            dk = by_book.get("DraftKings")
            vals = [v for v in [pinn, fd, dk] if v is not None and v > 0]
            delta = f"{max(vals)-min(vals):+.1f}pp" if len(vals) >= 2 else ""
            ps = f"{pinn:.1f}%" if pinn else "N/A"
            fs = f"{fd:.1f}%" if fd else "N/A"
            ds = f"{dk:.1f}%" if dk else "N/A"
            print(f"{pname:<28s} {ps:>10s} {fs:>10s} {ds:>10s} {delta:>8s}")

    # Polymarket check: search for any player props from this game
    print(f"\n{'─'*40} POLYMARKET CHECK {'─'*40}")
    # Build player keyword list from lineup
    player_kws = set()
    for side in ("away", "home"):
        for b in lineup.get(f"{side}_batters", []):
            last = b.split("(")[0].strip().split()[-1].lower()
            if len(last) > 3:
                player_kws.add(last)
        p = lineup.get(f"{side}_pitcher", "")
        if p:
            last = p.split()[-1].lower()
            if len(last) > 3:
                player_kws.add(last)
    gm = _fetch("https://gamma-api.polymarket.com/markets?limit=500&closed=false")
    found = False
    if gm and player_kws:
        for m in gm if isinstance(gm, list) else gm.get("data", []):
            q = (m.get("question", "") or "").lower()
            if any(kw in q for kw in player_kws):
                prices = m.get("outcomePrices", [])
                print(f"  🟢 {m.get('question')}: {prices}")
                found = True
    if not found:
        print("  ⚪ No Polymarket markets → these are sportsbook props only")
    print("\n" + "="*80)

def main():
    print(f"\n📡 MLB Props — {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}  Key: {'✅' if ODDS_API_KEY else '❌'}")
    if not ODDS_API_KEY: print("ERROR"); sys.exit(1)
    schedule = get_mlb_schedule(); odds = get_todays_odds_games()
    print(f"  MLB: {len(schedule)} games | OddsAPI: {len(odds)}")
    og, sg, gi = find_game(odds, schedule)
    if not og: print("❌ Not found"); sys.exit(1)
    print(f"  ✅ {og.get('away_team')} @ {og.get('home_team')}")
    lineup = get_lineup(sg)
    pbm = {}
    for mk in PROP_MARKETS:
        data = get_props(gi, mk); parsed = parse_props(data, mk, lineup)
        if parsed: pbm[mk] = parsed
    print_recon(og, lineup, pbm)
    out = {"timestamp":datetime.now(timezone.utc).isoformat(),"game":f"{og.get('away_team')} @ {og.get('home_team')}","game_id":gi,"lineup":lineup,"props":pbm}
    out_dir = Path(__file__).resolve().parent.parent / "output"; out_dir.mkdir(exist_ok=True)
    with open(out_dir/f"mlb_props_{date.today().isoformat()}.json","w") as f: json.dump(out,f,indent=2,default=str)
    print(f"💾 output/mlb_props_{date.today().isoformat()}.json")

if __name__ == "__main__": main()