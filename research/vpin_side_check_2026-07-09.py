"""Cross-check Polymarket data-api trade `side` vs independent estimates.
1) Quote rule: trade price vs current CLOB best bid/ask (recent trades only).
2) Tick rule: Lee-Ready style price-change classification over the tape.
"""
import json, time, urllib.request

UA = {"User-Agent": "Polyclawd-QA/1.0"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

# 1. Find top liquid active markets
markets = get("https://gamma-api.polymarket.com/markets?limit=15&active=true&closed=false&_sort=liquidityNum&_order=desc")
picked = []
for m in markets:
    try:
        toks = json.loads(m.get("clobTokenIds", "[]"))
        cid = m.get("conditionId", "")
        if toks and cid.startswith("0x"):
            picked.append((m.get("slug", "?"), cid, toks[0]))
    except Exception:
        pass
    if len(picked) >= 4:
        break

now = time.time()
for slug, cid, tok in picked:
    trades = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500&takerOnly=true")
    trades = [t for t in trades if str(t.get("asset")) == str(tok)]
    if len(trades) < 50:
        print(f"{slug}: only {len(trades)} trades for YES token, skipping")
        continue
    trades.sort(key=lambda t: t.get("timestamp", 0))

    # Quote rule vs current book — only trades in last 20 min
    book = get(f"https://clob.polymarket.com/book?token_id={tok}")
    bids = sorted((float(x["price"]) for x in book.get("bids", [])), reverse=True)
    asks = sorted(float(x["price"]) for x in book.get("asks", []))
    bb, ba = (bids[0] if bids else None), (asks[0] if asks else None)
    q_agree = q_tot = 0
    if bb and ba:
        mid = (bb + ba) / 2
        for t in trades:
            if now - int(t.get("timestamp", 0)) > 1200:
                continue
            p = float(t["price"]); s = str(t.get("side", "")).upper()
            if abs(p - mid) < 1e-9:
                continue
            est = "BUY" if p > mid else "SELL"
            q_tot += 1
            q_agree += (est == s)

    # Tick rule over full sample
    t_agree = t_tot = 0
    last_p = None; last_dir = None
    for t in trades:
        p = float(t["price"]); s = str(t.get("side", "")).upper()
        if last_p is not None:
            if p > last_p: d = "BUY"
            elif p < last_p: d = "SELL"
            else: d = last_dir
            if d:
                t_tot += 1
                t_agree += (d == s)
            last_dir = d
        last_p = p

    span_h = (trades[-1]["timestamp"] - trades[0]["timestamp"]) / 3600
    print(f"{slug}: n={len(trades)} span={span_h:.1f}h book bb={bb} ba={ba}")
    print(f"  quote-rule (<=20min): {q_agree}/{q_tot}" + (f" = {q_agree/q_tot*100:.1f}%" if q_tot else " (no recent trades)"))
    print(f"  tick-rule (full):     {t_agree}/{t_tot}" + (f" = {t_agree/t_tot*100:.1f}%" if t_tot else ""))
    sides = {}
    for t in trades:
        sides[t.get("side")] = sides.get(t.get("side"), 0) + 1
    print(f"  side distribution: {sides}")
