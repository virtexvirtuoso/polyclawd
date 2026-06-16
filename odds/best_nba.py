#!/usr/bin/env python3
"""NBA Game 3 - Best bet with Poisson guardrail."""
import os, sys, json, urllib.request, math, re
sys.path.insert(0, os.path.expanduser("~/Desktop/polyclawd"))

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None

KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"

def fetch(u):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Polyclawd/2.0"}), timeout=15).read())

def devig(ov, un):
    o = 100/(ov+100) if ov>0 else abs(ov)/(abs(ov)+100)
    u = 100/(un+100) if un>0 else abs(un)/(abs(un)+100)
    if o+u <= 1: return o
    lo, hi = 1, 20
    for _ in range(80):
        mid = (lo+hi)/2
        t = o**mid + u**mid
        if abs(t-1) < 1e-9: break
        lo, hi = (mid, hi) if t > 1 else (lo, mid)
    return o**((lo+hi)/2)

def poisson(lam, k):
    return 1 - sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k+1))

AVG = {
    "Jalen Brunson": {"ast": 6.8, "pts": 26.5, "reb": 3.0, "thr": 2.5, "blk": 0.3},
    "Mikal Bridges": {"ast": 2.5, "pts": 17.0, "reb": 3.5, "thr": 1.5, "blk": 0.6},
    "Josh Hart": {"ast": 4.0, "pts": 10.0, "reb": 8.5, "thr": 0.8, "blk": 0.3},
    "Karl-Anthony Towns": {"ast": 3.0, "pts": 20.1, "reb": 11.9, "thr": 1.5, "blk": 0.7},
    "De'Aaron Fox": {"ast": 6.0, "pts": 23.0, "reb": 3.5, "thr": 1.8, "blk": 0.4},
    "Stephon Castle": {"ast": 5.0, "pts": 15.0, "reb": 5.5, "thr": 0.8, "blk": 0.4},
    "Victor Wembanyama": {"ast": 3.5, "pts": 28.0, "reb": 12.0, "thr": 1.2, "blk": 3.5},
    "OG Anunoby": {"ast": 2.0, "pts": 15.0, "reb": 5.0, "thr": 1.5, "blk": 0.6},
    "Dylan Harper": {"ast": 4.5, "pts": 14.5, "reb": 5.0, "thr": 1.5, "blk": 0.3},
    "Devin Vassell": {"ast": 3.0, "pts": 15.0, "reb": 3.5, "thr": 2.0, "blk": 0.4},
    "Mitchell Robinson": {"ast": 0.5, "pts": 5.5, "reb": 8.5, "blk": 1.5},
    "Miles McBride": {"ast": 2.0, "pts": 7.0, "reb": 1.5, "thr": 1.5, "blk": 0.1},
    "Julian Champagnie": {"ast": 1.5, "pts": 6.0, "reb": 3.0, "thr": 1.8, "blk": 0.3},
    "Landry Shamet": {"ast": 1.5, "pts": 7.5, "reb": 1.5, "thr": 1.5, "blk": 0.1},
    "Keldon Johnson": {"ast": 1.5, "pts": 10.0, "reb": 2.5, "thr": 0.8, "blk": 0.1},
    "Jose Alvarado": {"ast": 2.5, "pts": 5.0, "reb": 1.5, "thr": 0.5, "blk": 0.1},
}

SMT2KEY = {
    "basketball_player_points": "pts", "basketball_player_rebounds": "reb",
    "basketball_player_assists": "ast", "basketball_player_threes": "thr",
    "basketball_player_blocks": "blk",
}

def scan():
    if not KEY:
        print("No ODDS_API_KEY set")
        return [], None

    events = fetch(f"{BASE}/sports/basketball_nba/odds?apiKey={KEY}&bookmakers=pinnacle&markets=h2h")
    eid = events[0]["id"]

    book = {}
    for mk in ["player_points", "player_rebounds", "player_assists"]:
        short = mk.split("_")[1]
        d = fetch(f"{BASE}/sports/basketball_nba/events/{eid}/odds?apiKey={KEY}&markets={mk}&bookmakers=pinnacle&oddsFormat=american")
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
                    book[f"{pn.lower()}|{short}"] = {"line": ov["pt"], "fair": devig(ov["pr"], un["pr"]), "player": pn}

    client = PolymarketUS()
    resp = client.search.query({"query": "san antonio new york 2026"})

    results = []
    for ev in resp.get("events", []):
        for m in ev.get("markets", []):
            smt = m.get("sportsMarketType", "")
            short = SMT2KEY.get(smt)
            if not short:
                continue
            q = m.get("question", "")
            mtch = re.search(r"record at least (\d+)", q, re.I)
            if not mtch:
                continue
            thresh = int(mtch.group(1))
            meta = m.get("metadata", {})
            player = meta.get("playerName", "") or m.get("titleShort", "") or "?"

            bk = book.get(f"{player.lower()}|{short}")
            if not bk:
                continue

            try:
                bbo = client.markets.bbo(m["slug"])
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

            half_steps = (thresh - 0.5 - bk["line"]) / 0.5
            if half_steps < -1:
                continue

            step_cost = 0.07 if short == "reb" else (0.08 if short == "ast" else 0.06)
            adj = min(0.95, max(0.02, bk["fair"] - half_steps * step_cost))
            avg = AVG.get(player, {}).get(short)
            pf = poisson(avg, thresh - 1) if avg else None
            edge = adj - pm
            if edge < 0:
                continue

            results.append({
                "p": player, "s": short, "t": thresh,
                "pm": pm, "ln": bk["line"], "fa": adj,
                "st": half_steps, "pf": pf, "eg": edge
            })

    results.sort(key=lambda x: x["eg"], reverse=True)

    best = None
    print(f"  {'Player':<18} {'Prop':<8} {'>=N':<4} {'PM%':>5} {'Ln':>4} {'Fair':>6} {'Steps':>5} {'Poisson':>8} {'Edge':>6}  {'Grade'}")
    print(f"  {'-' * 72}")

    for r in results:
        st = r["st"]
        if st > 3:
            continue
        p_ok = r["pf"] is not None and r["pf"] >= r["pm"] - 0.08
        if st <= 1 and p_ok:
            gr = "A"
        elif st <= 1:
            gr = "B"
        elif st <= 2 and p_ok:
            gr = "C"
        elif st <= 2:
            gr = "D"
        else:
            continue

        pfs = f"{r['pf']*100:.0f}%" if r["pf"] else "N/A"
        print(f"  {r['p']:<18} {r['s']:<8} >= {r['t']:<2} {r['pm']*100:>4.0f}% {r['ln']:>4.1f} {r['fa']*100:>5.1f}% {r['st']:>4.0f} {pfs:>7} +{r['eg']*100:>4.1f}pp  {gr}")
        if gr == "A" and best is None:
            best = r

    return results, best

if __name__ == "__main__":
    results, best = scan()
    if best:
        print(f"\n  🏆 BEST BET: {best['p']} >= {best['t']} {best['s']} YES @ {best['pm']*100:.0f}c  edge +{best['eg']*100:.0f}pp  ({best['st']:.0f} steps, Poisson {best['pf']*100:.0f}%)")
    else:
        print("\n  No Grade A bets found")