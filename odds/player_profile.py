#!/usr/bin/env python3
"""
player_profile.py — Full stat dossier for any MLB player (pitcher or batter)

Usage:
  python3 odds/player_profile.py "Shohei Ohtani"
  python3 odds/player_profile.py "Mookie Betts" --prop hr --line 0.5
  python3 odds/player_profile.py "Jacob Misiorowski" --prop k --line 7.5
  python3 odds/player_profile.py "Freddie Freeman" --prop rbi --line 0.5
  python3 odds/player_profile.py "Aaron Judge" --season 2025
"""

import sys
import os
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from difflib import get_close_matches

MLB_API  = "https://statsapi.mlb.com/api/v1"
ODDS_KEY = os.environ.get("ODDS_API_KEY", "51efafc2aaa8df23e01020214bb7e594")

# Map CLI --prop aliases to Odds API market keys and stat keys
PROP_MAP = {
    # Batters
    "hr":    {"odds_market": "batter_home_runs",           "stat_key": "homeRuns",    "label": "HR"},
    "h":     {"odds_market": "batter_hits",                "stat_key": "hits",        "label": "Hits"},
    "hit":   {"odds_market": "batter_hits",                "stat_key": "hits",        "label": "Hits"},
    "hits":  {"odds_market": "batter_hits",                "stat_key": "hits",        "label": "Hits"},
    "rbi":   {"odds_market": "batter_rbis",                "stat_key": "rbi",         "label": "RBI"},
    "tb":    {"odds_market": "batter_total_bases",         "stat_key": "totalBases",  "label": "Total Bases"},
    "r":     {"odds_market": "batter_runs_scored",         "stat_key": "runs",        "label": "Runs"},
    "run":   {"odds_market": "batter_runs_scored",         "stat_key": "runs",        "label": "Runs"},
    "bb":    {"odds_market": "batter_walks",               "stat_key": "baseOnBalls", "label": "Walks"},
    "sb":    {"odds_market": "batter_stolen_bases",        "stat_key": "stolenBases", "label": "SB"},
    # Pitchers
    "k":     {"odds_market": "pitcher_strikeouts",         "stat_key": "strikeOuts",  "label": "Strikeouts"},
    "ks":    {"odds_market": "pitcher_strikeouts",         "stat_key": "strikeOuts",  "label": "Strikeouts"},
    "er":    {"odds_market": "pitcher_earned_runs",        "stat_key": "earnedRuns",  "label": "Earned Runs"},
    "ip":    {"odds_market": "pitcher_outs",               "stat_key": "inningsPitched", "label": "Innings Pitched"},
    "h_a":   {"odds_market": "pitcher_hits_allowed",       "stat_key": "hits",        "label": "Hits Allowed"},
    "bb_a":  {"odds_market": "pitcher_walks",              "stat_key": "baseOnBalls", "label": "Walks Allowed"},
}

# Which stats to show for each position type
BATTER_STAT_KEYS = [
    ("gamesPlayed", "G"), ("atBats", "AB"), ("hits", "H"), ("homeRuns", "HR"),
    ("rbi", "RBI"), ("runs", "R"), ("stolenBases", "SB"), ("baseOnBalls", "BB"),
    ("strikeOuts", "SO"), ("avg", "AVG"), ("obp", "OBP"), ("slg", "SLG"),
    ("ops", "OPS"), ("totalBases", "TB"),
]
PITCHER_STAT_KEYS = [
    ("gamesStarted", "GS"), ("inningsPitched", "IP"), ("strikeOuts", "K"),
    ("baseOnBalls", "BB"), ("hits", "H"), ("homeRuns", "HR"), ("earnedRuns", "ER"),
    ("era", "ERA"), ("whip", "WHIP"), ("strikeoutsPer9Inn", "K/9"),
    ("strikeoutWalkRatio", "K/BB"), ("avg", "AVG against"),
]


# ── MLB API helpers ────────────────────────────────────────────────────────────

def search_player(name: str) -> dict:
    last = name.split()[-1]
    r = requests.get(f"{MLB_API}/people/search", params={"names": last, "sportId": 1})
    people = r.json().get("people", [])
    for p in people:
        if name.lower() in p["fullName"].lower() or p["fullName"].lower() in name.lower():
            return p
    names = [p["fullName"] for p in people]
    close = get_close_matches(name, names, n=1, cutoff=0.6)
    if close:
        return next(p for p in people if p["fullName"] == close[0])
    return {}


def get_season_stats(pid: int, season: int, group: str) -> dict:
    r = requests.get(f"{MLB_API}/people/{pid}/stats",
                     params={"stats": "season", "group": group, "season": season, "sportId": 1})
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


def get_career_stats(pid: int, group: str) -> dict:
    r = requests.get(f"{MLB_API}/people/{pid}/stats",
                     params={"stats": "career", "group": group, "sportId": 1})
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


def get_game_log(pid: int, season: int, group: str) -> list:
    r = requests.get(f"{MLB_API}/people/{pid}/stats",
                     params={"stats": "gameLog", "group": group, "season": season, "sportId": 1})
    return r.json().get("stats", [{}])[0].get("splits", [])


def get_platoon_splits(pid: int, season: int, group: str) -> dict:
    r = requests.get(f"{MLB_API}/people/{pid}/stats",
                     params={"stats": "statSplits", "group": group, "season": season,
                             "sitCodes": "vr,vl", "sportId": 1})
    result = {}
    for sg in r.json().get("stats", []):
        for sp in sg.get("splits", []):
            code = sp.get("split", {}).get("code", "?")
            result[code] = sp.get("stat", {})
    return result


def get_transactions(pid: int) -> list:
    r = requests.get(f"{MLB_API}/transactions",
                     params={"playerId": pid, "sportId": 1,
                             "startDate": "2024-01-01", "endDate": "2026-12-31"})
    return r.json().get("transactions", [])


def find_todays_game(player_name: str, pid: int) -> dict | None:
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    r = requests.get(f"{MLB_API}/schedule",
                     params={"sportId": 1, "date": today,
                             "hydrate": "probablePitcher,lineups,team,venue,roster"})
    games = r.json().get("dates", [{}])[0].get("games", [])

    last = player_name.split()[-1].lower()

    for g in games:
        for side in ["away", "home"]:
            team_id = g.get("teams", {}).get(side, {}).get("team", {}).get("id")
            # Check probable pitcher
            sp = g.get("teams", {}).get(side, {}).get("probablePitcher", {})
            if last in sp.get("fullName", "").lower() or sp.get("id") == pid:
                opp = "home" if side == "away" else "away"
                return _build_game_info(g, side, opp, "pitcher")

            # Check lineups
            key = "homePlayers" if side == "home" else "awayPlayers"
            for p in g.get("lineups", {}).get(key, []):
                pname = p.get("fullName", p.get("person", {}).get("fullName", ""))
                if last in pname.lower() or p.get("id") == pid:
                    opp = "home" if side == "away" else "away"
                    return _build_game_info(g, side, opp, "batter")

    # Fallback: find by team roster using player's current team
    return None


def _build_game_info(g, player_side, opp_side, role):
    opp_key = "homePlayers" if opp_side == "home" else "awayPlayers"
    player_key = "homePlayers" if player_side == "home" else "awayPlayers"
    return {
        "role": role,
        "player_side": player_side,
        "opp_side": opp_side,
        "venue": g.get("venue", {}).get("name", "?"),
        "status": g.get("status", {}).get("detailedState", "?"),
        "player_team": g.get("teams", {}).get(player_side, {}).get("team", {}).get("abbreviation", "?"),
        "opp_team": g.get("teams", {}).get(opp_side, {}).get("team", {}).get("abbreviation", "?"),
        "opp_team_id": g.get("teams", {}).get(opp_side, {}).get("team", {}).get("id"),
        "opp_sp": g.get("teams", {}).get(opp_side, {}).get("probablePitcher", {}),
        "lineups": g.get("lineups", {}),
        "player_lineup_key": player_key,
        "opp_lineup_key": opp_key,
        "game_time": g.get("gameDate", "?"),
        "game": g,
    }


def get_lineup_handedness(lineups: dict, key: str) -> dict:
    batters = lineups.get(key, [])
    sides = {}
    for p in batters:
        name = p.get("fullName", p.get("person", {}).get("fullName", "?"))
        bat = p.get("batSide", {}).get("code", "?")
        if bat == "?":
            last = name.split()[-1]
            r = requests.get(f"{MLB_API}/people/search", params={"names": last, "sportId": 1})
            for person in r.json().get("people", []):
                if name.lower() in person.get("fullName", "").lower():
                    bat = person.get("batSide", {}).get("code", "?")
                    break
        sides[name] = bat
    return sides


def get_opp_pitcher_info(sp: dict) -> dict:
    if not sp or not sp.get("id"):
        return {}
    pid = sp["id"]
    r = requests.get(f"{MLB_API}/people/{pid}", params={"hydrate": "stats(group=pitching,type=season,season=2026)"})
    person = r.json().get("people", [{}])[0]
    hand = person.get("pitchHand", {}).get("code", "?")
    # Basic 2026 stats
    stats = {}
    for sg in person.get("stats", []):
        splits = sg.get("splits", [])
        if splits:
            stats = splits[0].get("stat", {})
    return {"name": sp.get("fullName", "TBD"), "hand": hand, "stats": stats}


def get_team_stats(team_id: int, season: int, group: str) -> dict:
    r = requests.get(f"{MLB_API}/teams/{team_id}/stats",
                     params={"stats": "season", "group": group, "season": season, "sportId": 1})
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


# ── Odds API helpers ───────────────────────────────────────────────────────────

def get_live_prop_lines(player_name: str, market_key: str) -> list:
    last = player_name.split()[-1].lower()
    r = requests.get("https://api.the-odds-api.com/v4/sports/baseball_mlb/events",
                     params={"apiKey": ODDS_KEY, "dateFormat": "iso"})
    events = r.json() if isinstance(r.json(), list) else []

    for e in events:
        eid = e["id"]
        pr = requests.get(
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds",
            params={
                "apiKey": ODDS_KEY,
                "markets": market_key,
                "bookmakers": "draftkings,fanduel,betmgm,pinnacle,williamhill_us,caesars",
                "oddsFormat": "american",
            }
        )
        results = []
        for b in pr.json().get("bookmakers", []):
            for mkt in b.get("markets", []):
                for out in mkt.get("outcomes", []):
                    desc = out.get("description", "")
                    if last in desc.lower():
                        results.append({
                            "book": b["title"],
                            "name": out.get("name"),
                            "point": out.get("point"),
                            "price": out.get("price"),
                        })
        if results:
            return results
    return []


def implied_prob(american: int) -> float:
    if american > 0:
        return 100 / (american + 100)
    return abs(american) / (abs(american) + 100)


def devig(over_p: int, under_p: int) -> tuple[float, float]:
    oi, ui = implied_prob(over_p), implied_prob(under_p)
    total = oi + ui
    return oi / total, ui / total


def devig_power(over_p: int, under_p: int) -> tuple[float, float]:
    """Power devig — better accuracy at price extremes (>65c favorites, <35c dogs)."""
    oi, ui = implied_prob(over_p), implied_prob(under_p)
    lo, hi = 0.5, 3.0
    for _ in range(64):
        mid = (lo + hi) / 2
        if oi ** (1.0 / mid) + ui ** (1.0 / mid) > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2
    po, pu = oi ** (1.0 / k), ui ** (1.0 / k)
    t = po + pu
    return po / t, pu / t


# ── Display helpers ────────────────────────────────────────────────────────────

def fmt_stat_line(stat: dict, keys: list) -> str:
    parts = []
    for key, label in keys:
        val = stat.get(key, "—")
        parts.append(f"{label}={val}")
    return "  " + "  ".join(parts)


def analyze_game_log(game_log: list, stat_key: str, line: float | None, label: str):
    values = []
    for g in game_log:
        v = g.get("stat", {}).get(stat_key)
        if v is not None:
            try:
                values.append(float(v))
            except:
                pass

    if not values:
        return values

    print(f"\n{'─'*55}")
    print(f"  GAME LOG — {label.upper()}")
    print(f"{'─'*55}")
    for g, v in zip(game_log, values):
        opp = g.get("opponent", {}).get("abbreviation", "???")
        dt  = g.get("date", "")
        ip  = g.get("stat", {}).get("inningsPitched", "")
        ip_str = f"  {ip}IP" if ip else ""
        flag = ""
        if line is not None:
            flag = " ✓" if v > line else " ✗"
        print(f"  {dt} vs {opp}{ip_str}: {int(v) if v == int(v) else v}{label[0]}{flag}")

    if line is not None:
        n = len(values)
        print(f"\n  LINE ANALYSIS (over {line}):")
        print(f"  Season: {sum(1 for v in values if v > line)}/{n} = {sum(1 for v in values if v > line)/n*100:.0f}% OVER")
        for w in [5, 10, 15, 20]:
            if n >= w:
                recent = values[-w:]
                over_w = sum(1 for v in recent if v > line)
                print(f"  L{w}:     {over_w}/{w} = {over_w/w*100:.0f}% OVER")
        print(f"  Avg: {sum(values)/n:.2f}  |  Min: {min(values)}  |  Max: {max(values)}")

    return values


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MLB player stat dossier (pitcher or batter)")
    parser.add_argument("name", help="Player full or last name")
    parser.add_argument("--prop", default=None,
                        help=f"Prop to analyze: {', '.join(PROP_MAP.keys())}")
    parser.add_argument("--line", type=float, default=None,
                        help="Prop line to analyze (e.g. 0.5 for HR, 7.5 for Ks)")
    parser.add_argument("--season", type=int, default=2026, help="Season year (default 2026)")
    args = parser.parse_args()

    prop_cfg = PROP_MAP.get(args.prop.lower(), None) if args.prop else None

    print(f"\n{'='*60}")
    print(f"  PLAYER DOSSIER: {args.name.upper()}")
    print(f"{'='*60}\n")

    # ── 1. Find player ──────────────────────────────────────────────────────────
    print("🔍 Looking up player...")
    player = search_player(args.name)
    if not player:
        print(f"  ❌ Not found: {args.name}")
        sys.exit(1)

    pid       = player["id"]
    full_name = player["fullName"]
    pos_type  = player.get("primaryPosition", {}).get("type", "")
    is_pitcher = pos_type == "Pitcher"
    group     = "pitching" if is_pitcher else "hitting"
    stat_keys = PITCHER_STAT_KEYS if is_pitcher else BATTER_STAT_KEYS

    print(f"  ✓ {full_name} (ID: {pid})")
    print(f"  Born: {player.get('birthDate','?')} | Age: {player.get('currentAge','?')}")
    print(f"  {player.get('height','?')} | "
          f"{'Throws' if is_pitcher else 'Bats'}: "
          f"{player.get('pitchHand' if is_pitcher else 'batSide',{}).get('description','?')}")
    print(f"  Position: {player.get('primaryPosition',{}).get('name','?')} | "
          f"#: {player.get('primaryNumber','?')}")
    print(f"  MLB Debut: {player.get('mlbDebutDate','?')}")

    # ── 2. Parallel data fetch ──────────────────────────────────────────────────
    print(f"\n📊 Fetching {args.season} stats + career + splits + game log...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_season = pool.submit(get_season_stats, pid, args.season, group)
        f_career = pool.submit(get_career_stats, pid, group)
        f_log    = pool.submit(get_game_log, pid, args.season, group)
        f_splits = pool.submit(get_platoon_splits, pid, args.season, group)
        f_trans  = pool.submit(get_transactions, pid)
        f_game   = pool.submit(find_todays_game, full_name, pid)

    season  = f_season.result()
    career  = f_career.result()
    log     = f_log.result()
    splits  = f_splits.result()
    trans   = f_trans.result()
    game    = f_game.result()

    # ── 3. Season totals ────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  {args.season} SEASON")
    print(f"{'─'*55}")
    if season:
        print(fmt_stat_line(season, stat_keys))
    else:
        print("  No season stats found.")

    # ── 4. Career ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  CAREER MLB")
    print(f"{'─'*55}")
    if career:
        print(fmt_stat_line(career, stat_keys))

    # ── 5. Platoon splits ───────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  {args.season} PLATOON SPLITS")
    print(f"{'─'*55}")
    for code, label in [("vl", "vs LHP" if not is_pitcher else "vs LHB"),
                         ("vr", "vs RHP" if not is_pitcher else "vs RHB")]:
        sp = splits.get(code, {})
        if sp:
            print(f"  {label}: {fmt_stat_line(sp, stat_keys).strip()}")

    # ── 6. Game log + prop analysis ─────────────────────────────────────────────
    stat_key = prop_cfg["stat_key"] if prop_cfg else (
        "strikeOuts" if is_pitcher else "hits"
    )
    label = prop_cfg["label"] if prop_cfg else ("K" if is_pitcher else "H")
    values = analyze_game_log(log, stat_key, args.line, label)

    # ── 7. Health ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  TRANSACTIONS / HEALTH")
    print(f"{'─'*55}")
    il = [t for t in trans if "IL" in t.get("typeDesc", "")]
    other = [t for t in trans if "IL" not in t.get("typeDesc", "")]
    if il:
        for t in il:
            print(f"  ⚠️  {t.get('date','?')}: {t.get('typeDesc','?')} — {t.get('description','?')}")
    else:
        print("  ✓ No IL stints found (2024-present)")
    for t in other[-3:]:
        print(f"  {t.get('date','?')}: {t.get('typeDesc','?')}")

    # ── 8. Tonight's game context ───────────────────────────────────────────────
    if game:
        print(f"\n{'─'*55}")
        print(f"  TONIGHT'S GAME")
        print(f"{'─'*55}")
        print(f"  {game['player_team']} vs {game['opp_team']} @ {game['venue']}")
        print(f"  Status: {game['status']} | {game['game_time']}")

        # Opp SP info (relevant for batters; for pitchers show opp lineup)
        if is_pitcher:
            lineup_key = game["opp_lineup_key"]
            opp_label = game["opp_team"]
        else:
            lineup_key = game["opp_lineup_key"]
            opp_label = game["opp_team"]
            # Show opposing pitcher
            opp_sp = game.get("opp_sp", {})
            if opp_sp:
                sp_info = get_opp_pitcher_info(opp_sp)
                hand = sp_info.get("hand", "?")
                sp_stats = sp_info.get("stats", {})
                print(f"\n  Opposing SP: {sp_info.get('name','?')} ({hand}HP)")
                if sp_stats:
                    print(f"    ERA={sp_stats.get('era','?')} WHIP={sp_stats.get('whip','?')} "
                          f"K/9={sp_stats.get('strikeoutsPer9Inn','?')} AVG_against={sp_stats.get('avg','?')}")
                # Show platoon edge
                opp_hand_key = "vr" if hand == "R" else "vl"
                opp_split = splits.get(opp_hand_key, {})
                if opp_split:
                    split_label = f"vs R{'H' if not is_pitcher else 'H'}P" if hand == "R" else f"vs L{'H' if not is_pitcher else 'H'}P"
                    stat_val = opp_split.get(stat_key, "?")
                    ab = opp_split.get("atBats", opp_split.get("battersFaced", "?"))
                    print(f"    Player {split_label}: {stat_key}={stat_val} (in {ab} AB/PA)")

        # Opp lineup
        opp_sides = get_lineup_handedness(game["lineups"], lineup_key)
        if opp_sides:
            print(f"\n  {opp_label} LINEUP:")
            for name, side in opp_sides.items():
                print(f"    {name} ({side})")
            lhb = sum(1 for s in opp_sides.values() if s == "L")
            rhb = sum(1 for s in opp_sides.values() if s == "R")
            swi = sum(1 for s in opp_sides.values() if s == "S")
            total = len(opp_sides)
            print(f"  → {lhb}L / {rhb}R / {swi}S  |  LHB%: {lhb/total*100:.0f}%")

            # Platoon projection for pitchers
            if is_pitcher and values:
                vl_sp = splits.get("vl", {})
                vr_sp = splits.get("vr", {})
                if vl_sp and vr_sp:
                    lhb_k_rate = int(vl_sp.get(stat_key, 0)) / max(int(vl_sp.get("battersFaced", 1)), 1)
                    rhb_k_rate = int(vr_sp.get(stat_key, 0)) / max(int(vr_sp.get("battersFaced", 1)), 1)
                    avg_ip_per_start = float(season.get("inningsPitched", "0") or 0) / max(len(log), 1)
                    bf_est = avg_ip_per_start * 4.3
                    lhb_pct = lhb / total
                    proj = bf_est * lhb_pct * lhb_k_rate + bf_est * (1 - lhb_pct) * rhb_k_rate
                    print(f"\n  Projected {label} (platoon-adjusted): {proj:.1f}")
                    if args.line:
                        diff = proj - args.line
                        print(f"  vs Line {args.line}: {'OVER' if diff > 0 else 'UNDER'} by {abs(diff):.1f}")

        # Opp team stats
        opp_team_id = game.get("opp_team_id")
        if opp_team_id:
            opp_t = get_team_stats(opp_team_id, args.season, "hitting" if is_pitcher else "pitching")
            if is_pitcher:
                so = opp_t.get("strikeOuts", 0)
                pa = opp_t.get("plateAppearances", 1)
                avg = opp_t.get("avg", "?")
                ops = opp_t.get("ops", "?")
                print(f"\n  {game['opp_team']} vs pitching: K%={int(so)/int(pa)*100:.1f}%  AVG={avg}  OPS={ops}")
            else:
                era = opp_t.get("era", "?")
                whip = opp_t.get("whip", "?")
                k9 = opp_t.get("strikeoutsPer9Inn", "?")
                avg = opp_t.get("avg", "?")
                print(f"\n  {game['opp_team']} pitching staff: ERA={era}  WHIP={whip}  K/9={k9}  AVG_against={avg}")

    # ── 9. Live prop lines ──────────────────────────────────────────────────────
    if prop_cfg:
        print(f"\n{'─'*55}")
        print(f"  LIVE LINES — {prop_cfg['label'].upper()} (Odds API)")
        print(f"{'─'*55}")
        lines = get_live_prop_lines(full_name, prop_cfg["odds_market"])
        if lines:
            by_point = defaultdict(list)
            for l in lines:
                by_point[l["point"]].append(l)
            for pt, entries in sorted(by_point.items()):
                print(f"  Line {pt}:")
                for e in entries:
                    sign = "+" if e["price"] > 0 else ""
                    print(f"    {e['book']:15s}: {e['name']} {sign}{e['price']}")
                pin_over  = next((e["price"] for e in entries if "Pinnacle" in e["book"] and e["name"] == "Over"), None)
                pin_under = next((e["price"] for e in entries if "Pinnacle" in e["book"] and e["name"] == "Under"), None)
                if pin_over and pin_under:
                    do, du   = devig(pin_over, pin_under)
                    dpo, dpu = devig_power(pin_over, pin_under)
                    print(f"    → Pinnacle fair (proportional): Over={do*100:.2f}%  Under={du*100:.2f}%")
                    print(f"    → Pinnacle fair (power):        Over={dpo*100:.2f}%  Under={dpu*100:.2f}%  (delta={((dpo-do)*100):+.2f}pp)")
                    if args.line and pt == args.line and values:
                        obs = sum(1 for v in values if v > pt) / len(values)
                        edge_prop  = (obs - do)  * 100
                        edge_power = (obs - dpo) * 100
                        print(f"    → Observed (season): {obs*100:.0f}%  |  Edge (prop): {edge_prop:+.1f}pp  |  Edge (power): {edge_power:+.1f}pp")
        else:
            print(f"  No {prop_cfg['label']} lines found (game may not be listed yet)")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
