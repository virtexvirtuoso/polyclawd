#!/usr/bin/env python3
"""NBA Purified Edge Scanner — Poisson-guardrail."""
import os, sys, json, urllib.request, math, re
sys.path.insert(0, "/var/www/virtuosocrypto.com/polyclawd")
try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None
KEY = os.environ.get("ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def devig(ov, un):
    o = 100/(ov+100) if ov>0 else abs(ov)/(abs(ov)+100)
    u = 100/(un+100) if un>0 else abs(un)/(abs(un)+100)
    if o+u <= 1.0: return o
    lo, hi = 1.0, 20.0
    for _ in range(80):
        mid = (lo+hi)/2
        t = o**mid + u**mid
        if abs(t-1)<1e-9: break
        lo, hi = (mid, hi) if t>1 else (lo, mid)
    return o**((lo+hi)/2)

def poisson_pge(lam, k):
    return 1 - sum(math.exp(-lam)*lam**i/math.factorial(i) for i in range(k+1))

AVG = {"Jalen Brunson":{"ast":6.8,"pts":26.5,"reb":3.0,"thr":2.5,"blk":0.3},"Mikal Bridges":{"ast":2.5,"pts":17.0,"reb":3.5,"thr":1.8,"blk":0.6},"Josh Hart":{"ast":4.0,"pts":10.0,"reb":8.5,"thr":0.9,"blk":0.3},"Karl-Anthony Towns":{"ast":3.0,"pts":20.1,"reb":11.9,"thr":1.5,"blk":0.7},"De'Aaron Fox":{"ast":6.0,"pts":23.0,"reb":3.5,"thr":1.8,"blk":0.4},"Stephon Castle":{"ast":5.0,"pts":15.0,"reb":5.5,"thr":0.8,"blk":0.4},"Victor Wembanyama":{"ast":3.5,"pts":28.0,"reb":12.0,"thr":1.2,"blk":3.5},"OG Anunoby":{"ast":2.0,"pts":15.0,"reb":5.0,"thr":1.5,"blk":0.6},"Dylan Harper":{"ast":4.5,"pts":14.5,"reb":5.0,"thr":1.5,"blk":0.3},"Devin Vassell":{"ast":3.0,"pts":15.0,"reb":3.5,"thr":2.0,"blk":0.4},"Mitchell Robinson":{"ast":0.5,"pts":5.5,"reb":8.5,"thr":0.0,"blk":1.5},"Miles McBride":{"ast":2.0,"pts":7.0,"reb":1.5,"thr":1.5,"blk":0.1},"Julian Champagnie":{"ast":1.5,"pts":8.0,"reb":3.0,"thr":1.8,"blk":0.3},"Landry Shamet":{"ast":1.5,"pts":7.5,"reb":1.5,"thr":1.5,"blk":0.1},"Keldon Johnson":{"ast":1.5,"pts":10.0,"reb":2.5,"thr":0.8,"blk":0.1},"Jose Alvarado":{"ast":2.5,"pts":5.0,"reb":1.5},"Luke Kornet":{"ast":1.0,"pts":4.0,"reb":3.5}}
STEP_COST = {"pts":0.06,"reb":0.07,"ast":0.08,"thr":0.10,"blk":0.12}
SMT_MAP = {"basketball_player_points":"pts","basketball_player_rebounds":"reb","basketball_player_assists":"ast"}
ODDS_SHORT = {"player_points":"pts","player_rebounds":"reb","player_assists":"ast"}

def fetch_book(eid):
    book = {}
    for mk, sh in ODDS_SHORT.items():
        try:
            d = fetch(f"{BASE}/sports/basketball_nba/events/{eid}/odds?apiKey={KEY}&markets={mk}&bookmakers=pinnacle&oddsFormat=american")
            for bk in d.get("bookmakers",[]):
                for m in bk.get("markets",[]):
                    players = {}
                    for o in m.get("outcomes",[]):
                        name = o.get("description","")
                        side = o.get("name","").lower()
                        price = o.get("price",0)
                        pt = o.get("point",0)
                        if name not in players: players[name] = {}
                        players[name][side] = {"price":price,"point":pt}
                    for pn, sides in players.items():
                        ov = sides.get("over")
                        un = sides.get("under")
                        if ov and un:
                            fair = devig(ov["price"], un["price"])
                            book[f"{pn.lower()}|{sh}"] = {"line":ov["point"],"fair":fair,"player":pn}
        except: pass
    return book

def scan():
    if not KEY or not PolymarketUS: return "", []
    events = fetch(f"{BASE}/sports/basketball_nba/odds?apiKey={KEY}&bookmakers=pinnacle&markets=h2h")
    if not events: return "", []
    eid = events[0]["id"]
    game = f"{events[0]['away_team']} @ {events[0]['home_team']}"
    book = fetch_book(eid)
    client = PolymarketUS()
    pm_markets = []
    try:
        resp = client.search.query({"query":"san antonio new york 2026"})
        for ev in resp.get("events",[]):
            for m in ev.get("markets",[]):
                if m.get("sportsMarketType","") in SMT_MAP: pm_markets.append(m)
    except: pass
    seen = set()
    results = []
    for m in pm_markets:
        slug = m.get("slug","")
        if not slug or slug in seen: continue
        seen.add(slug)
        short = SMT_MAP.get(m.get("sportsMarketType",""))
        if not short: continue
        q = m.get("question","")
        mtch = re.search(r"record at least (\d+)",q,re.I)
        if not mtch: continue
        thresh = int(mtch.group(1))
        meta = m.get("metadata",{})
        player = meta.get("playerName","") or m.get("titleShort","") or "?"
        bk = book.get(f"{player.lower()}|{short}")
        if not bk: continue
        try:
            bbo = client.markets.bbo(slug); md = bbo.get("marketData",{})
            cur = md.get("currentPx",{}); pm = float(cur["value"]) if cur and cur.get("value") else None
            if pm is None:
                lps = md.get("lastPriceSample",{})
                pm = float(lps["longPx"]["value"]) if lps and lps.get("longPx",{}).get("value") else None
        except: pm = None
        if pm is None or pm < 0.01: continue
        half_steps = (thresh - 0.5 - bk["line"]) / 0.5
        if half_steps < 0: continue
        adj = min(0.95, max(0.02, bk["fair"] - half_steps * STEP_COST.get(short, 0.07)))
        avg = AVG.get(player, {}).get(short)
        pf = poisson_pge(avg, thresh-1) if avg else None
        eg = adj - pm
        if eg < 0: continue
        pok = pf is not None and pf >= pm - 0.08
        if half_steps <= 1 and pok: gr = "A"
        elif half_steps <= 1: gr = "B"
        elif half_steps <= 2 and pok: gr = "C"
        elif half_steps <= 2: gr = "D"
        else: gr = "F"
        results.append({"p":player,"s":short,"t":thresh,"pm":pm,"ln":bk["line"],"bf":bk["fair"],"fa":adj,"st":half_steps,"pf":pf,"eg":eg,"gr":gr})
    results.sort(key=lambda x:x["eg"], reverse=True)
    return game, results

def print_report(game, results, grade_filter=None):
    if not results: print(f"\nNo NBA edges for {game}."); return
    if grade_filter:
        results = [r for r in results if r["gr"]==grade_filter]
        if not results: print(f"\nNo Grade {grade_filter}."); return
    print(f"\n{'='*90}")
    print(f"  {game}")
    print(f"  A=<=1 step+Poisson  B=<=1 step  C=<=2 steps+Poisson  D=<=2 steps")
    print(f"{'='*90}")
    print(f"  {'Player':<18} {'Prop':<6} {'>=N':<4} {'PM%':>5} {'Devig':>7} {'Fair%':>6} {'Steps':>6} {'Poisson':>8} {'Edge':>7}  Grade")
    print(f"  {'-'*75}")
    for r in results:
        pfs = f"{r['pf']*100:.0f}%" if r["pf"] else "N/A"
        print(f"  {r['p']:<18} {r['s']:<6} >= {r['t']:<2}  {r['pm']*100:>3.0f}% {r['bf']*100:>5.1f}% {r['fa']*100:>5.1f}% {r['st']:>4.0f}  {pfs:>6}  +{r['eg']*100:>4.1f}pp  {r['gr']}")
    for g in ["A","B","C","D"]:
        sub = [r for r in results if r["gr"]==g]
        if sub:
            print(f"\n  Grade {g} ({len(sub)} bets):")
            for r in sub[:5]:
                print(f"    {r['p']} >= {r['t']} {r['s']} YES @ {r['pm']*100:.0f}c  edge +{r['eg']*100:.0f}pp  ({r['st']:.0f} steps, Poisson {r['pf']*100:.0f}%)")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--grade", type=str, choices=["A","B","C","D"])
    args = parser.parse_args()
    game, results = scan()
    if game:
        print_report(game, results, grade_filter=args.grade)
    else:
        print("Scan failed.")
