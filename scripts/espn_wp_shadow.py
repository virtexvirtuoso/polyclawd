#!/usr/bin/env python3
"""ESPN win-probability SHADOW logger (log-only, no alerts).
Records PM moneyline price vs ESPN in-game win probability per live NFL game.
Purpose: calibration corpus for a future in-game fair-value alert. Writes
JSONL only; touches no production state. Auto-stops after MAX_HOURS.

2026-09-13 fix: PM join switched from the /api/espn/edge join endpoint (which
only surfaces UPCOMING games — the DK feed drops in-progress games, so every
live row logged pm=null all Sunday) to a direct PM-US SDK query per live game,
mirroring odds/espn_odds._fetch_pmus_fgw_for_game. SDK verified to return the
LIVE game (DAL@NYG bid/ask while in 2nd quarter)."""
import json, time, sys
import requests

OUT = "/var/www/virtuosocrypto.com/polyclawd/storage/espn_wp_shadow.jsonl"
UA = {"User-Agent": "Polyclawd/1.0"}
POLL = 60
MAX_HOURS = 44  # covers Sunday slate + Monday night DEN@KC

_client = None

def pm_client():
    global _client
    if _client is None:
        from polymarket_us import PolymarketUS
        _client = PolymarketUS()
    return _client

def get(url, timeout=15):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()

def now(): return time.strftime("%H:%M:%S", time.gmtime())

def scoreboard():
    return get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard")

def split_events(d):
    """live: {eid: {"short": "DAL @ NYG", "away": full, "home": full}}; done: [ev]."""
    live, done = {}, []
    for ev in d.get("events", []):
        st = ev.get("status", {}).get("type", {}).get("state")
        if st == "in":
            comps = ev.get("competitions", [{}])[0].get("competitors", [])
            away = home = None
            for c in comps:
                if c.get("homeAway") == "home":
                    home = c.get("team", {}).get("displayName")
                else:
                    away = c.get("team", {}).get("displayName")
            live[ev["id"]] = {"short": ev.get("shortName", ""), "away": away, "home": home}
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

def _match_side(pm_team, away, home):
    """Which ESPN side does the PM long-team name refer to? Substring match
    first (strong); token overlap fallback (weak). Returns 'away'/'home'/None.
    None = ambiguous or unmatched -> caller skips rather than guesses."""
    if not pm_team:
        return None
    p = pm_team.lower()
    strong = []
    for label, name in (("away", away), ("home", home)):
        if not name:
            continue
        n = name.lower()
        if p in n or n in p:
            strong.append(label)
    if len(strong) == 1:
        return strong[0]
    weak = []
    pt = set(p.split())
    for label, name in (("away", away), ("home", home)):
        if name and pt & set(name.lower().split()):
            weak.append(label)
    if len(weak) == 1:
        return weak[0]
    return None

def pm_prices(live):
    """{(away_full, home_full): {side: pm fields}} via PM-US SDK.

    FGW markets quote the LONG side via bestBidQuote/bestAskQuote; the short
    side's YES = 1 - mid. pm_price is the side-adjusted YES mid; pm_bid/pm_ask
    stay the raw long-side quotes (same semantics as the old join endpoint)."""
    rows = {}
    for info in live.values():
        away, home = info.get("away"), info.get("home")
        if not away or not home:
            continue
        try:
            raw = pm_client().search.query(
                {"query": f"{home} {away}", "status": "upcoming", "limit": 10})
        except Exception as e:
            print(now(), "pm ERR", info["short"], str(e)[:80])
            continue
        events = raw.get("events", []) if isinstance(raw, dict) else raw
        best = None
        for ev in events or []:
            if not ev.get("gameId"):
                continue
            for m in (ev.get("markets") or []):
                if m.get("sportsMarketType") != "football_team_full_game_winner":
                    continue
                sides = m.get("marketSides") or []
                long_team = next((s.get("team", {}).get("name") for s in sides
                                  if s.get("long") and s.get("team", {}).get("name")), None)
                if not long_team:
                    continue
                short_team = next((s.get("team", {}).get("name") for s in sides
                                   if not s.get("long") and s.get("team", {}).get("name")), None)
                try:
                    bid = float((m.get("bestBidQuote") or {}).get("value"))
                    ask = float((m.get("bestAskQuote") or {}).get("value"))
                except (TypeError, ValueError):
                    continue
                gs = (m.get("gameStartTime") or "")[:10]
                cand = {"game_start": gs, "long_team": long_team, "short_team": short_team,
                        "bid": bid, "ask": ask, "slug": m.get("slug")}
                if best is None or (gs or "9999") < (best["game_start"] or "9999"):
                    best = cand
        if not best:
            print(now(), "pm MISS", info["short"])
            continue
        side_long = _match_side(best["long_team"], away, home)
        if side_long is None:
            print(now(), "pm AMBIG", info["short"], "long_team:", best["long_team"])
            continue
        mid = (best["bid"] + best["ask"]) / 2.0
        home_px = mid if side_long == "home" else 1.0 - mid
        away_px = 1.0 - mid if side_long == "home" else mid
        rows[(away, home)] = {
            "away": {"team": best["long_team"] if side_long == "away" else best["short_team"],
                     "pm_price": round(away_px * 100, 1), "pm_bid": best["bid"], "pm_ask": best["ask"]},
            "home": {"team": best["long_team"] if side_long == "home" else best["short_team"],
                     "pm_price": round(home_px * 100, 1), "pm_bid": best["bid"], "pm_ask": best["ask"]},
        }
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
    import os
    print(now(), "espn_wp_shadow start, pid", os.getpid())
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
            pm = pm_prices(live)
            for eid, info in live.items():
                short = info["short"]
                a, h = info.get("away"), info.get("home")
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
            print(now(), "logged", len(live), "live game(s),", sum(1 for v in pm.values()), "with pm")
        except Exception as e:
            print(now(), "loop ERR", str(e)[:100])
        time.sleep(POLL)
    print(now(), "max runtime reached; exiting")

if __name__ == "__main__":
    main()