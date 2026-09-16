#!/usr/bin/env python3
"""Survey props availability using event-level endpoint (the only one that works on our plan).
Usage: ODDS_API_KEY=<key> python3 check_props_survey.py"""
import os, sys, json, urllib.request

api_key = os.environ.get("ODDS_API_KEY") or os.environ.get("ODDS_API_KEY_2")
if not api_key:
    from pathlib import Path
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ODDS_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
                break
if not api_key:
    print("Set ODDS_API_KEY env var")
    sys.exit(1)
print(f"Key: {api_key[:8]}...{api_key[-4:]} (len={len(api_key)})")

# 1. Get active sports + event IDs for upcoming games
sports_url = f"https://api.the-odds-api.com/v4/sports?apiKey={api_key}"
req = urllib.request.Request(sports_url)
with urllib.request.urlopen(req, timeout=10) as resp:
    remaining = resp.headers.get("x-requests-remaining")
    all_sports = json.loads(resp.read().decode())
    active = {s["key"]: s.get("title", s["key"]) for s in all_sports if s.get("active") and 'baseball' in s['key']}
    print(f"Credits: {remaining}")

# Only check baseball for now (we know event endpoint works)
for sk in ["baseball_mlb", "basketball_nba", "americanfootball_nfl", "soccer_epl", "icehockey_nhl", "mma_mixed_martial_arts"]:
    if sk not in active:
        print(f"{sk} not active, skipping")
        continue
    title = active[sk]
    print(f"\n─── {title} [{sk}] ───")

    # Get events (first 3)
    ev_url = f"https://api.the-odds-api.com/v4/sports/{sk}/events?apiKey={api_key}"
    try:
        req_ev = urllib.request.Request(ev_url)
        with urllib.request.urlopen(req_ev, timeout=10) as resp_ev:
            events = json.loads(resp_ev.read().decode())
            print(f"  Events available: {len(events)}")
    except Exception as e:
        print(f"  Events error: {e}")
        continue

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat() + 'Z'
    upcoming = [e for e in events if e.get('commence_time','') > now]
    if not upcoming:
        print('  No upcoming events')
        continue
    print(f'  Upcoming: {len(upcoming)}')
    for e in upcoming[:2]:
        eid = e["id"]
        print(f"\n  {e.get('away_team')} @ {e.get('home_team')} [ID: {eid[:12]}...]")

        # Test each market key family via event endpoint
        if 'baseball' in sk:
            test_mkts = ["batter_home_runs", "pitcher_strikeouts", "batter_hits", "batter_total_bases",
                         "batter_rbis", "batter_runs_scored", "batter_strikeouts",
                         "pitcher_outs", "pitcher_hits_allowed", "pitcher_record_a_win"]
        elif 'basketball' in sk:
            test_mkts = ["player_points", "player_rebounds", "player_assists", "player_threes", "player_blocks", "player_steals"]
        elif 'football' in sk:
            test_mkts = ["player_pass_yds", "player_rush_yds", "player_receptions", "player_anytime_td", "player_first_td"]
        elif 'soccer' in sk:
            test_mkts = ["player_goals_scored", "player_shots_on_target", "player_assists", "player_shots", "player_cards"]
        elif 'hockey' in sk:
            test_mkts = ["player_points", "player_goals", "player_assists", "player_shots_on_goal", "player_saves"]
        elif 'fighting' in sk or 'mma' in sk or 'boxing' in sk:
            test_mkts = ["fighter_win_method", "player_win_method", "fighter_significant_strikes"]
        else:
            test_mkts = []
        
        for mkt in test_mkts:
            ev_odds_url = f"https://api.the-odds-api.com/v4/sports/{sk}/events/{eid}/odds?apiKey={api_key}&regions=us&markets={mkt}&oddsFormat=american"
            try:
                req_m = urllib.request.Request(ev_odds_url)
                with urllib.request.urlopen(req_m, timeout=8) as resp_m:
                    d = json.loads(resp_m.read().decode())
                    creds = resp_m.headers.get("x-requests-remaining", "?")
                    n_out = sum(len(o.get("outcomes",[])) for b in d.get("bookmakers",[]) for o in b.get("markets",[]))
                    if n_out > 0:
                        desc = ""
                        for b in d.get("bookmakers",[]):
                            for m in b.get("markets",[]):
                                if m.get("outcomes"):
                                    o = m["outcomes"][0]
                                    desc = o.get("description","") or o.get("name","?")
                                    break
                            if desc: break
                        print(f"    ✅ {mkt:30s} → {n_out} outcomes (eg: {desc[:45]}) [{creds} credits]")
            except urllib.error.HTTPError:
                pass

print(f"\nCredits: {remaining}")