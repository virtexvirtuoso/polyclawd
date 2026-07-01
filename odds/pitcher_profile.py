#!/usr/bin/env python3
"""
pitcher_profile.py — Full stat dossier for any MLB pitcher

Usage:
  python3 odds/pitcher_profile.py "Jacob Misiorowski"
  python3 odds/pitcher_profile.py "Corbin Burnes" --line 6.5
  python3 odds/pitcher_profile.py "Spencer Schwellenbach" --season 2025
"""

import sys
import os
import json
import requests
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import get_close_matches

MLB_API = "https://statsapi.mlb.com/api/v1"
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")


# ── Helpers ────────────────────────────────────────────────────────────────────


def search_player(name: str) -> dict:
    last = name.split()[-1]
    r = requests.get(f"{MLB_API}/people/search", params={"names": last, "sportId": 1})
    people = r.json().get("people", [])
    # Exact or close match
    for p in people:
        if name.lower() in p["fullName"].lower() or p["fullName"].lower() in name.lower():
            return p
    # Fuzzy fallback
    names = [p["fullName"] for p in people]
    close = get_close_matches(name, names, n=1, cutoff=0.6)
    if close:
        return next(p for p in people if p["fullName"] == close[0])
    return {}


def get_season_stats(pid: int, season: int) -> dict:
    r = requests.get(
        f"{MLB_API}/people/{pid}/stats", params={"stats": "season", "group": "pitching", "season": season, "sportId": 1}
    )
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


def get_career_stats(pid: int) -> dict:
    r = requests.get(f"{MLB_API}/people/{pid}/stats", params={"stats": "career", "group": "pitching", "sportId": 1})
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


def get_game_log(pid: int, season: int) -> list:
    r = requests.get(
        f"{MLB_API}/people/{pid}/stats",
        params={"stats": "gameLog", "group": "pitching", "season": season, "sportId": 1},
    )
    return r.json().get("stats", [{}])[0].get("splits", [])


def get_platoon_splits(pid: int, season: int) -> dict:
    r = requests.get(
        f"{MLB_API}/people/{pid}/stats",
        params={"stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "vr,vl", "sportId": 1},
    )
    result = {}
    for sg in r.json().get("stats", []):
        for sp in sg.get("splits", []):
            key = sp.get("split", {}).get("code", "?")
            result[key] = sp.get("stat", {})
    return result


def get_transactions(pid: int) -> list:
    r = requests.get(
        f"{MLB_API}/transactions",
        params={"playerId": pid, "sportId": 1, "startDate": "2024-01-01", "endDate": "2026-12-31"},
    )
    return r.json().get("transactions", [])


def find_todays_start(name: str) -> dict | None:
    """Find if pitcher is starting today and return opponent lineup info."""
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    r = requests.get(
        f"{MLB_API}/schedule", params={"sportId": 1, "date": today, "hydrate": "probablePitcher,lineups,team,venue"}
    )
    games = r.json().get("dates", [{}])[0].get("games", [])
    for g in games:
        for side in ["away", "home"]:
            sp = g.get("teams", {}).get(side, {}).get("probablePitcher", {})
            if name.lower() in sp.get("fullName", "").lower():
                opp_side = "home" if side == "away" else "away"
                return {
                    "game": g,
                    "pitcher_side": side,
                    "opp_side": opp_side,
                    "venue": g.get("venue", {}).get("name", "?"),
                    "status": g.get("status", {}).get("detailedState", "?"),
                    "opp_team": g.get("teams", {}).get(opp_side, {}).get("team", {}).get("abbreviation", "?"),
                    "opp_team_id": g.get("teams", {}).get(opp_side, {}).get("team", {}).get("id"),
                    "lineups": g.get("lineups", {}),
                    "game_time": g.get("gameDate", "?"),
                }
    return None


def get_opp_lineup_handedness(game_info: dict) -> dict:
    opp_side = game_info["opp_side"]
    key = "homePlayers" if opp_side == "home" else "awayPlayers"
    batters = game_info["lineups"].get(key, [])
    sides = {}
    for p in batters:
        name = p.get("fullName", p.get("person", {}).get("fullName", "?"))
        bat = p.get("batSide", {}).get("code", "?")
        if bat == "?":
            # Lookup individually
            last = name.split()[-1]
            r = requests.get(f"{MLB_API}/people/search", params={"names": last, "sportId": 1})
            for person in r.json().get("people", []):
                if name.lower() in person.get("fullName", "").lower():
                    bat = person.get("batSide", {}).get("code", "?")
                    break
        sides[name] = bat
    return sides


def get_opp_team_krate(team_id: int, season: int) -> dict:
    r = requests.get(
        f"{MLB_API}/teams/{team_id}/stats",
        params={"stats": "season", "group": "hitting", "season": season, "sportId": 1},
    )
    splits = r.json().get("stats", [{}])[0].get("splits", [])
    return splits[0].get("stat", {}) if splits else {}


def get_live_ks_lines(pitcher_name: str) -> list:
    """Fetch live pitcher K lines from Odds API."""
    from odds.rate_limiter import can_make_call

    _ok, _why = can_make_call("normal")
    if not _ok:
        print(f"pitcher_profile: Odds API credit gate — {_why}")
        return []
    # Find the game
    r = requests.get(
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/events", params={"apiKey": ODDS_KEY, "dateFormat": "iso"}
    )
    try:
        from odds.the_odds_api import _track_credits_from_response

        _track_credits_from_response(r)
    except Exception:
        pass
    events = r.json() if isinstance(r.json(), list) else []

    last_name = pitcher_name.split()[-1].lower()
    target_event = None
    for e in events:
        home = e.get("home_team", "").lower()
        away = e.get("away_team", "").lower()
        # Can't filter by pitcher here — check all MIL/team games later
        # Just return all events for now and filter by prop
        pass

    results = []
    for e in events:
        eid = e["id"]
        pr = requests.get(
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds",
            params={
                "apiKey": ODDS_KEY,
                "markets": "pitcher_strikeouts",
                "bookmakers": "draftkings,fanduel,betmgm,pinnacle,williamhill_us,caesars",
                "oddsFormat": "american",
            },
        )
        try:
            from odds.the_odds_api import _track_credits_from_response

            _track_credits_from_response(pr)
        except Exception:
            pass
        for b in pr.json().get("bookmakers", []):
            for mkt in b.get("markets", []):
                for out in mkt.get("outcomes", []):
                    desc = out.get("description", "")
                    if last_name in desc.lower():
                        results.append(
                            {
                                "book": b["title"],
                                "name": out.get("name"),
                                "point": out.get("point"),
                                "price": out.get("price"),
                                "desc": desc,
                            }
                        )
        if results:
            break  # found the game
    return results


def implied_prob(american: int) -> float:
    if american > 0:
        return 100 / (american + 100)
    else:
        return abs(american) / (abs(american) + 100)


def devig(over_american: int, under_american: int) -> tuple[float, float]:
    oi = implied_prob(over_american)
    ui = implied_prob(under_american)
    total = oi + ui
    return oi / total, ui / total


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="MLB pitcher stat dossier")
    parser.add_argument("name", help="Pitcher full or last name")
    parser.add_argument("--line", type=float, default=None, help="K line to analyze (e.g. 7.5)")
    parser.add_argument("--season", type=int, default=2026, help="Season year (default 2026)")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print(f"  PITCHER DOSSIER: {args.name.upper()}")
    print(f"{'=' * 60}\n")

    # ── 1. Find player ──────────────────────────────────────────────────────────
    print("🔍 Looking up player...")
    player = search_player(args.name)
    if not player:
        print(f"  ❌ Player not found: {args.name}")
        sys.exit(1)

    pid = player["id"]
    full_name = player["fullName"]
    print(f"  ✓ {full_name} (ID: {pid})")
    print(f"  Born: {player.get('birthDate', '?')} | Age: {player.get('currentAge', '?')}")
    print(f"  Height: {player.get('height', '?')} | Throws: {player.get('pitchHand', {}).get('description', '?')}")
    print(f"  MLB Debut: {player.get('mlbDebutDate', '?')}")

    # ── 2. Parallel data fetch ──────────────────────────────────────────────────
    print(f"\n📊 Fetching stats ({args.season} season + career + splits + game log)...")
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_season = pool.submit(get_season_stats, pid, args.season)
        f_career = pool.submit(get_career_stats, pid)
        f_log = pool.submit(get_game_log, pid, args.season)
        f_splits = pool.submit(get_platoon_splits, pid, args.season)
        f_trans = pool.submit(get_transactions, pid)
        f_start = pool.submit(find_todays_start, full_name)

    season = f_season.result()
    career = f_career.result()
    game_log = f_log.result()
    splits = f_splits.result()
    transactions = f_trans.result()
    start_info = f_start.result()

    # ── 3. Season totals ────────────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  {args.season} SEASON")
    print(f"{'─' * 50}")
    if season:
        gs = season.get("gamesStarted", 0)
        ip = season.get("inningsPitched", "?")
        k = season.get("strikeOuts", "?")
        bb = season.get("baseOnBalls", "?")
        era = season.get("era", "?")
        whip = season.get("whip", "?")
        k9 = season.get("strikeoutsPer9Inn", "?")
        kbb = season.get("strikeoutWalkRatio", "?")
        avg = season.get("avg", "?")
        print(f"  GS={gs}  IP={ip}  K={k}  BB={bb}  ERA={era}  WHIP={whip}")
        print(f"  K/9={k9}  K/BB={kbb}  AVG_against={avg}")
    else:
        print("  No season stats found.")

    # ── 4. Career ───────────────────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  CAREER MLB")
    print(f"{'─' * 50}")
    if career:
        print(
            f"  GS={career.get('gamesStarted')}  IP={career.get('inningsPitched')}  "
            f"K={career.get('strikeOuts')}  BB={career.get('baseOnBalls')}  "
            f"ERA={career.get('era')}  WHIP={career.get('whip')}  K/9={career.get('strikeoutsPer9Inn')}"
        )

    # ── 5. Platoon splits ───────────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  {args.season} PLATOON SPLITS")
    print(f"{'─' * 50}")
    vl = splits.get("vl", {})
    vr = splits.get("vr", {})
    if vl:
        print(
            f"  vs LHB: PA={vl.get('battersFaced')}  K={vl.get('strikeOuts')}  "
            f"K/9={vl.get('strikeoutsPer9Inn')}  AVG={vl.get('avg')}  BB={vl.get('baseOnBalls')}"
        )
    if vr:
        print(
            f"  vs RHB: PA={vr.get('battersFaced')}  K={vr.get('strikeOuts')}  "
            f"K/9={vr.get('strikeoutsPer9Inn')}  AVG={vr.get('avg')}  BB={vr.get('baseOnBalls')}"
        )

    # ── 6. Game log + K analysis ────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  {args.season} GAME LOG")
    print(f"{'─' * 50}")
    ks_list = []
    for g in game_log:
        s = g.get("stat", {})
        opp = g.get("opponent", {}).get("abbreviation", "???")
        dt = g.get("date", "")
        ip = s.get("inningsPitched", "?")
        k = s.get("strikeOuts", 0)
        h = s.get("hits", "?")
        bb = s.get("baseOnBalls", "?")
        er = s.get("earnedRuns", "?")
        ks_list.append(k)
        print(f"  {dt} vs {opp}: {ip}IP  {k}K  {h}H  {bb}BB  {er}ER")

    # ── 7. K line analysis ──────────────────────────────────────────────────────
    line = args.line
    if line is None and ks_list:
        avg_k = sum(ks_list) / len(ks_list)
        line = round(avg_k - 0.5)  # auto-suggest line near avg
        print(f"\n  (No --line specified, auto-analyzing at {line}.5)")
        line = line + 0.5

    if ks_list and line:
        print(f"\n{'─' * 50}")
        print(f"  K LINE ANALYSIS (line = {line})")
        print(f"{'─' * 50}")
        n = len(ks_list)
        over_all = sum(1 for k in ks_list if k > line)

        windows = {}
        for w in [5, 10, 15, 20]:
            if n >= w:
                recent = ks_list[-w:]
                windows[w] = sum(1 for k in recent if k > line) / w

        print(f"  Season:  {over_all}/{n} = {over_all / n * 100:.0f}%  OVER {line}")
        for w, rate in windows.items():
            print(f"  L{w}:      {int(rate * w)}/{w} = {rate * 100:.0f}%  OVER {line}")
        print(f"  Avg Ks:  {sum(ks_list) / n:.1f}")
        print(f"  Min/Max: {min(ks_list)} / {max(ks_list)}")
        print(f"  8+ K starts: {sum(1 for k in ks_list if k >= 8)}/{n}")
        print(f"  10+ K starts: {sum(1 for k in ks_list if k >= 10)}/{n}")

    # ── 8. Health / transactions ────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  TRANSACTIONS / HEALTH")
    print(f"{'─' * 50}")
    il_stints = [t for t in transactions if "IL" in t.get("typeDesc", "")]
    other_tx = [t for t in transactions if "IL" not in t.get("typeDesc", "")]
    if il_stints:
        for t in il_stints:
            print(f"  ⚠️  {t.get('date', '?')}: {t.get('typeDesc', '?')} — {t.get('description', '?')}")
    else:
        print(f"  ✓ No IL stints found")
    for t in other_tx[-3:]:
        print(f"  {t.get('date', '?')}: {t.get('typeDesc', '?')}")

    # ── 9. Tonight's start ──────────────────────────────────────────────────────
    if start_info:
        print(f"\n{'─' * 50}")
        print(f"  TONIGHT'S START")
        print(f"{'─' * 50}")
        print(f"  Opponent: {start_info['opp_team']} @ {start_info['venue']}")
        print(f"  Status: {start_info['status']} | Time: {start_info['game_time']}")

        lineup = start_info["lineups"]
        opp_side = start_info["opp_side"]
        key = "homePlayers" if opp_side == "home" else "awayPlayers"
        batters = lineup.get(key, [])

        if batters:
            print(f"\n  {start_info['opp_team']} LINEUP ({len(batters)} batters):")
            sides = get_opp_lineup_handedness(start_info)
            lhb = sum(1 for s in sides.values() if s == "L")
            rhb = sum(1 for s in sides.values() if s == "R")
            swi = sum(1 for s in sides.values() if s == "S")
            for name, side in sides.items():
                print(f"    {name} ({side})")
            print(f"\n  Handedness: {lhb}L / {rhb}R / {swi}S")
            total = len(sides)
            if total:
                lhb_pct = lhb / total
                print(f"  LHB%: {lhb_pct * 100:.0f}%")

                # Projected Ks using platoon splits
                if vl and vr and ks_list:
                    lhb_k_rate = int(vl.get("strikeOuts", 0)) / max(int(vl.get("battersFaced", 1)), 1)
                    rhb_k_rate = int(vr.get("strikeOuts", 0)) / max(int(vr.get("battersFaced", 1)), 1)
                    avg_ip = (
                        float(
                            ip
                            if isinstance(ip, (int, float))
                            else season.get("inningsPitched", "66").replace(".1", "0.33").replace(".2", "0.67")
                        )
                        if season
                        else 6.0
                    )
                    try:
                        avg_ip = float(season.get("inningsPitched", "66")) / max(len(game_log), 1)
                    except:
                        avg_ip = 6.0
                    bf_est = avg_ip * 4.3  # ~4.3 batters per inning
                    lhb_pa = bf_est * lhb_pct
                    rhb_pa = bf_est * (1 - lhb_pct)
                    proj_k = lhb_pa * lhb_k_rate + rhb_pa * rhb_k_rate
                    print(f"\n  Projected Ks (platoon-adjusted): {proj_k:.1f}")
                    if line:
                        print(f"  vs Line {line}: {'OVER' if proj_k > line else 'UNDER'} by {abs(proj_k - line):.1f}")

        # Fetch opp team K rate
        opp_team_id = start_info.get("opp_team_id")
        if opp_team_id:
            opp_stats = get_opp_team_krate(opp_team_id, args.season)
            so = opp_stats.get("strikeOuts", 0)
            pa = opp_stats.get("plateAppearances", 1)
            if pa:
                print(f"\n  {start_info['opp_team']} team K%: {int(so) / int(pa) * 100:.1f}%")

        # Live lines
        print(f"\n{'─' * 50}")
        print(f"  LIVE K LINES (Odds API)")
        print(f"{'─' * 50}")
        lines = get_live_ks_lines(full_name)
        if lines:
            # Group by point
            from collections import defaultdict

            by_point = defaultdict(list)
            for l in lines:
                by_point[l["point"]].append(l)
            for pt, entries in sorted(by_point.items()):
                print(f"  Line {pt}:")
                for e in entries:
                    side = e["name"]
                    price = e["price"]
                    sign = "+" if price > 0 else ""
                    print(f"    {e['book']:15s}: {side} {sign}{price}")
                # Devig Pinnacle if available
                pin_over = next((e["price"] for e in entries if "Pinnacle" in e["book"] and e["name"] == "Over"), None)
                pin_under = next(
                    (e["price"] for e in entries if "Pinnacle" in e["book"] and e["name"] == "Under"), None
                )
                if pin_over and pin_under:
                    do, du = devig(pin_over, pin_under)
                    print(f"    → Pinnacle devigged: Over={do * 100:.1f}%  Under={du * 100:.1f}%")
                    if line and pt == line and ks_list:
                        obs = sum(1 for k in ks_list if k > line) / len(ks_list)
                        print(
                            f"    → Observed hit rate: {obs * 100:.0f}%  |  Edge vs Pinnacle: {(obs - do) * 100:+.1f}pp"
                        )
        else:
            print("  No K props found for today (game may not be listed yet)")
    else:
        print(f"\n  (No start found for {full_name} today)")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
