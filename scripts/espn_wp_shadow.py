#!/usr/bin/env python3
"""ESPN win-probability SHADOW logger (log-only, no alerts).
Records PM moneyline price vs ESPN in-game win probability per live NFL game.
Purpose: calibration corpus for a future in-game fair-value alert. Writes
JSONL only; touches no production state. Auto-stops after MAX_HOURS."""
import json, time, sys
import requests

API = "http://localhost:8420"
OUT = "/var/www/virtuosocrypto.com/polyclawd/storage/espn_wp_shadow.jsonl"
UA = {"User-Agent": "Polyclawd/1.0"}
ALIAS = {"WAS": "WSH", "JAC": "JAX"}  # PM slug code -> ESPN abbreviation
POLL = 60
MAX_HOURS = 44  # covers Sunday slate + Monday night DEN@KC

def get(url, timeout=15):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()

def now(): return time.strftime("%H:%M:%S", time.gmtime())

def scoreboard():
    return get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard")

def split_events(d):
    live, done = {}, []
    for ev in d.get("events", []):
        st = ev.get("status", {}).get("type", {}).get("state")
        if st == "in":
            live[ev["id"]] = ev.get("shortName", "")
        elif st == "post":
            done.append(ev)
    return live, done

def wp_for(eid):
    s = get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=" + eid)
    wp = s.get("winprobability") or []
    if not wp:
        return None
    last = wp[-1]
    hw = last.get("homeWinPercentage")
    tie = last.get("tiePercentage") or 0.0
    if hw is None:
        return None
    return {"home_wp": round(hw, 4), "away_wp": round(1 - hw - tie, 4),
            "tie_wp": round(tie, 4), "playId": last.get("playId"), "n_points": len(wp)}

def pm_prices():
    """{(AWAY,HOME): {side: pm fields}} from the live join endpoint."""
    d = get(API + "/api/espn/edge?min_edge=0")
    rows = {}
    for e in d.get("edges", []):
        slug = e.get("polymarket_slug", "")
        parts = slug.split("-")
        if len(parts) < 5 or parts[0] != "aec":
            continue
        away, home = parts[2].upper(), parts[3].upper()
        away, home = ALIAS.get(away, away), ALIAS.get(home, home)
        game = e.get("game", "")
        team = e.get("team", "")
        side = "away" if team and game.startswith(team) else "home"
        rows.setdefault((away, home), {})[side] = {
            "team": team, "pm_price": e.get("polymarket_price"),
            "pm_bid": e.get("pm_bid"), "pm_ask": e.get("pm_ask")}
    return rows

def log(row):
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")

def selftest(done_events):
    """Prove WP extraction end-to-end on one completed game."""
    if not done_events:
        return
    ev = done_events[0]
    try:
        wp = wp_for(ev["id"])
        log({"ts": int(time.time()), "selftest": True, "matchup": ev.get("shortName", ""), "wp": wp})
        print(now(), "SELFTEST", ev.get("shortName", ""), "wp:", wp)
    except Exception as e:
        print(now(), "SELFTEST ERR", str(e)[:100])

def main():
    t0 = time.time()
    print(now(), "espn_wp_shadow start, pid", os.getpid() if (os := __import__("os")) else "?")
    try:
        live, done = split_events(scoreboard())
        selftest(done)
    except Exception as e:
        print(now(), "startup ERR", str(e)[:100])
    while time.time() - t0 < MAX_HOURS * 3600:
        try:
            live, _ = split_events(scoreboard())
            if not live:
                print(now(), "no live games; idle")
                time.sleep(POLL)
                continue
            pm = pm_prices()
            for eid, short in live.items():
                a, h = [x.strip() for x in short.split("@")] if "@" in short else (None, None)
                try:
                    wp = wp_for(eid)
                except Exception as e:
                    wp = None
                    print(now(), "wp ERR", short, str(e)[:60])
                sides = pm.get((a, h), {})
                gaps = {}
                if wp and sides:
                    for side in ("away", "home"):
                        px = sides.get(side, {}).get("pm_price")
                        if px is not None:
                            w = wp["away_wp"] if side == "away" else wp["home_wp"]
                            gaps[side] = round(px / 100.0 - w, 4)
                log({"ts": int(time.time()), "matchup": short, "wp": wp, "pm": sides or None, "gap": gaps or None})
            print(now(), "logged", len(live), "live game(s)")
        except Exception as e:
            print(now(), "loop ERR", str(e)[:100])
        time.sleep(POLL)
    print(now(), "max runtime reached; exiting")

if __name__ == "__main__":
    main()
