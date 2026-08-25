"""Social count analysis — use known event slugs to discover all related events."""
import json, urllib.request, time, re, sqlite3
from collections import defaultdict
from config.polymarket_urls import GAMMA_API as GAMMA  # polyproxy: central URL config

def fetch(url, timeout=10):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except:
            if attempt < 2: time.sleep(1)
    return None

DB = "/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db"

CLOB = "https://clob.polymarket.com"

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

# Step 1: Find all event slugs from cache
slugs = db.execute("""
    SELECT DISTINCT event_slug FROM event_slug_cache 
    WHERE event_slug LIKE '%tweet%' OR event_slug LIKE '%post%' 
       OR event_slug LIKE '%truth%' OR event_slug LIKE '%musk%' 
       OR event_slug LIKE '%trump%'
""").fetchall()
known_slugs = set(r[0] for r in slugs)
print(f"Known event slugs: {len(known_slugs)}")
for s in known_slugs:
    print(f"  {s}")

# Step 2: Extract base patterns to find MORE events
# e.g., "elon-musk-of-tweets" is common to all Musk tweet events
base_patterns = set()
for s in known_slugs:
    # Remove date parts, keep the core pattern
    base = re.sub(r"-(january|february|march|april|may|june|july|august|september|october|november|december)-\d+-\w+-\d+", "", s)
    base_patterns.add(base)
    # Also try shorter patterns
    if "elon-musk" in s:
        base_patterns.add("elon-musk-of-tweets")
    if "trump" in s:
        base_patterns.add("donald-trump-of-truth-social")

print(f"\nSearch patterns: {base_patterns}")

# Step 3: Crawl Gamma for all events matching these patterns
all_event_slugs = set(known_slugs)
all_events = []

for pat in base_patterns:
    offset = 0
    found_new = 0
    while offset < 500:
        data = fetch(f"{GAMMA}/events?limit=20&offset={offset}&slug_contains={pat}")
        if not data or len(data) == 0:
            break
        for e in data:
            title = e.get("title", "").lower()
            eslug = e.get("slug", "")
            # Must be about counting tweets or posts
            if re.search(r"(how many|number of).*(tweet|post|truth social)", title):
                if eslug not in all_event_slugs:
                    found_new += 1
                all_event_slugs.add(eslug)
                all_events.append(e)
        if len(data) < 20:
            break
        offset += 20
        time.sleep(0.3)
    if found_new:
        print(f"  {pat}: +{found_new} new events")

# Deduplicate events
seen = set()
unique_events = []
for e in all_events:
    s = e.get("slug", "")
    if s not in seen:
        seen.add(s)
        unique_events.append(e)

print(f"\nTotal unique events: {len(unique_events)}")
for e in unique_events:
    ms = e.get("markets", [])
    print(f"  {e.get('title','')[:65]} [{len(ms)} mkts]")

# Step 4: Parse brackets from all events
all_brackets = []
for e in unique_events:
    title = e.get("title", "")
    slug = e.get("slug", "")
    for m in e.get("markets", []):
        q = m.get("question", "")
        
        match = re.search(r"(\d+)-(\d+)", q)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            btype = "range"
        else:
            match = re.search(r"fewer than (\d+)", q, re.I)
            if match: lo, hi = 0, int(match.group(1))-1; btype = "below"
            else:
                match = re.search(r"(\d+)\s*or more", q, re.I)
                if match: lo, hi = int(match.group(1)), 99999; btype = "above"
                else: continue
        
        person = "musk" if any(w in q.lower() for w in ["musk","elon","tweet"]) else "trump"
        resolved = m.get("resolved", False)
        outcome = m.get("outcome", "")
        hit = 1 if outcome == "Yes" else 0 if outcome == "No" else None
        
        try:
            prices = m.get("outcomePrices", "")
            if isinstance(prices, str): prices = json.loads(prices)
            yes_price = float(prices[0]) if prices else None
        except: yes_price = None
        
        vol = float(m.get("volume", 0) or 0)
        
        all_brackets.append(dict(
            person=person, question=q, event_title=title,
            bracket_lo=lo, bracket_hi=hi, bracket_type=btype,
            resolved=resolved, hit=hit, yes_price=yes_price,
            volume=vol, market_id=m.get("conditionId",""), slug=slug,
        ))

# Also add brackets from our paper_positions that might not be in event list
pp_rows = db.execute("""
    SELECT market_id, market_title, entry_price, side, status, pnl 
    FROM paper_positions WHERE archetype='social_count'
""").fetchall()

existing_mids = set(b["market_id"] for b in all_brackets)
for r in pp_rows:
    if r["market_id"] not in existing_mids:
        q = r["market_title"]
        match = re.search(r"(\d+)-(\d+)", q)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            person = "musk" if any(w in q.lower() for w in ["musk","elon","tweet"]) else "trump"
            resolved = r["status"] in ("won", "lost", "stopped")
            hit = 0 if r["status"] == "won" else 1 if r["status"] == "lost" else None
            all_brackets.append(dict(
                person=person, question=q, event_title=q,
                bracket_lo=lo, bracket_hi=hi, bracket_type="range",
                resolved=resolved, hit=hit, yes_price=r["entry_price"],
                volume=0, market_id=r["market_id"], slug="",
            ))

# Deduplicate
seen_ids = set()
deduped = []
for b in all_brackets:
    mid = b["market_id"]
    if mid and mid not in seen_ids:
        seen_ids.add(mid)
        deduped.append(b)
all_brackets = deduped

resolved_b = [b for b in all_brackets if b["resolved"] and b["hit"] is not None]
active_b = [b for b in all_brackets if not b["resolved"]]

print(f"\n{'='*60}")
print(f"TOTAL: {len(all_brackets)} brackets | {len(resolved_b)} resolved | {len(active_b)} active")
print(f"{'='*60}")

# ═══════ ANALYSIS ═══════

for person in ["musk", "trump"]:
    pb = [b for b in resolved_b if b["person"] == person]
    if not pb: continue
    hits = sum(1 for b in pb if b["hit"] == 1)
    vol = sum(b["volume"] for b in pb)
    name = "MUSK (tweets)" if person == "musk" else "TRUMP (Truth Social)"
    print(f"\n{name}:")
    print(f"  Resolved: {len(pb)} brackets, {hits} hits ({100*hits/len(pb):.1f}%), vol ${vol:,.0f}")
    
    widths = defaultdict(lambda: {"n":0,"hits":0})
    for b in pb:
        if b["bracket_type"] == "range":
            w = b["bracket_hi"] - b["bracket_lo"]
            widths[w]["n"] += 1
            widths[w]["hits"] += (b["hit"] or 0)
    if widths:
        print("  By bracket width:")
        for w in sorted(widths.keys()):
            s = widths[w]
            print(f"    {w:>3}-wide: {s['n']:>3} brackets, {s['hits']:>2} hits ({100*s['hits']/s['n']:.1f}%)")

# NO BACKTEST
print(f"\n{'='*60}")
print("NO STRATEGY BACKTEST ($100 per trade)")
print(f"{'='*60}")

for label, pkey in [("ALL", None), ("MUSK", "musk"), ("TRUMP", "trump")]:
    pb = resolved_b if pkey is None else [b for b in resolved_b if b["person"] == pkey]
    if not pb: continue
    print(f"\n{label}:")
    for min_no in [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        trades = wins = 0; pnl = 0.0
        for b in pb:
            if b["yes_price"] is None: continue
            no_price = 1 - b["yes_price"]
            if no_price < min_no: continue
            trades += 1
            if b["hit"] == 0:
                wins += 1
                pnl += 100 * (1 - no_price) / no_price
            else:
                pnl -= 100
        if trades:
            wr = 100 * wins / trades
            be = min_no * 100
            print(f"  NO>={min_no*100:.0f}c: {trades:>4}T {wins:>4}W ({wr:.1f}%) BE={be:.0f}% edge={wr-be:+.1f}% P&L=${pnl:+,.0f}")

# PREDICTABILITY
print(f"\n{'='*60}")
print("PREDICTABILITY — How wide is the uncertainty?")
print(f"{'='*60}")

for person in ["musk", "trump"]:
    hits = [b for b in resolved_b if b["person"] == person and b["hit"] == 1 and b["bracket_type"] == "range"]
    if len(hits) < 2: continue
    mids = [(b["bracket_lo"] + b["bracket_hi"]) / 2 for b in hits]
    avg_m = sum(mids) / len(mids)
    std_m = (sum((m - avg_m)**2 for m in mids) / len(mids)) ** 0.5 if len(mids) > 1 else 0
    ws = [b["bracket_hi"] - b["bracket_lo"] for b in hits]
    avg_w = sum(ws) / len(ws)
    name = "MUSK" if person == "musk" else "TRUMP"
    print(f"\n{name}:")
    print(f"  Where actuals land: avg={avg_m:.0f} std={std_m:.0f} (CV={std_m/avg_m:.2f})")
    print(f"  Bracket width: {avg_w:.0f}")
    if avg_w > 0:
        ratio = std_m / avg_w
        print(f"  Uncertainty = {ratio:.1f} brackets")
        print(f"  Compare: weather = 1-2 brackets")
        if ratio > 3:
            print(f"  >>> UNPREDICTABLE — {ratio:.0f}x bracket width = gambling")
        elif ratio > 2:
            print(f"  >>> BORDERLINE — thin edge at best")
        else:
            print(f"  >>> PREDICTABLE — exploitable like weather")

# CALIBRATION
print(f"\n{'='*60}")
print("CALIBRATION — Market vs Reality")
print(f"{'='*60}")

cal = defaultdict(lambda: {"n":0,"hits":0})
for b in resolved_b:
    if b["yes_price"] is not None:
        bucket = round(b["yes_price"] * 10) / 10
        cal[bucket]["n"] += 1
        cal[bucket]["hits"] += (b["hit"] or 0)

for p in sorted(cal.keys()):
    s = cal[p]
    if s["n"] < 2: continue
    actual = 100 * s["hits"] / s["n"]
    diff = actual - p * 100
    flag = " << MISPRICED" if abs(diff) > 10 else ""
    print(f"  YES@{p*100:.0f}c: {s['n']:>3}N {s['hits']:>3}hits actual={actual:.1f}% diff={diff:+.1f}%{flag}")

# HEAD-TO-HEAD
print(f"\n{'='*60}")
print("HEAD-TO-HEAD: Weather vs Social Count (our paper trades)")
print(f"{'='*60}")

for arch in ["weather", "social_count"]:
    row = db.execute(f"""
        SELECT COUNT(*) as n,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN status='stopped' THEN 1 ELSE 0 END) as stopped,
               ROUND(SUM(pnl),2) as pnl,
               ROUND(AVG(bet_size),0) as avg_bet,
               ROUND(AVG(edge_pct),3) as avg_edge
        FROM paper_positions 
        WHERE archetype='{arch}' AND status IN ('won','lost','stopped')
    """).fetchone()
    if row and row["n"]:
        wr = 100 * row["wins"] / row["n"]
        print(f"\n  {arch.upper():>15}: {row['n']}T  {row['wins']}W/{row['losses']}L/{row['stopped']}S  WR={wr:.1f}%  P&L=${row['pnl']}  avg_bet=${row['avg_bet']}  avg_edge={100*row['avg_edge']:.1f}%")

# Save to DB
db.execute("DROP TABLE IF EXISTS social_count_brackets")
db.execute("""CREATE TABLE social_count_brackets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT, event_title TEXT, question TEXT,
    bracket_lo INTEGER, bracket_hi INTEGER, bracket_type TEXT,
    resolved INTEGER, hit INTEGER, yes_final_price REAL,
    volume REAL, market_id TEXT UNIQUE, slug TEXT
)""")
for b in all_brackets:
    try:
        db.execute("INSERT OR IGNORE INTO social_count_brackets VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["person"], b["event_title"], b["question"], b["bracket_lo"], b["bracket_hi"],
             b["bracket_type"], 1 if b["resolved"] else 0, b["hit"], b["yes_price"],
             b["volume"], b["market_id"], b["slug"]))
    except: pass
db.commit()
total = db.execute("SELECT COUNT(*) FROM social_count_brackets").fetchone()[0]
print(f"\nDB: {total} brackets saved")
db.close()
print("\nDONE")
