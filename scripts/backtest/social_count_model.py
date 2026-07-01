"""
Social Count Model — Free Data Pipeline

Sources (all free, no API keys):
1. Polymarket resolved events → actual counts (ground truth)
2. X/Twitter profile scrape → live tweet counter 
3. Truth Social profile scrape → live post counter
4. Wayback Machine → historical profile snapshots (tweet/post counts over time)
5. Google cache / web archive → fallback historical
"""
import json, urllib.request, re, sqlite3, time
from datetime import datetime, timedelta
from collections import defaultdict

DB = "/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db"

def fetch(url, timeout=10, headers=None):
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    if headers:
        hdrs.update(headers)
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == 0: time.sleep(1)
            else: return None

def fetch_json(url, timeout=10):
    raw = fetch(url, timeout)
    if raw:
        try:
            return json.loads(raw)
        except:
            return None
    return None

# ══════════════════════════════════════════════════════════════════
# SOURCE 1: Polymarket Resolved Events → Actual Counts
# ══════════════════════════════════════════════════════════════════
print("=" * 60)
print("SOURCE 1: Polymarket Resolved Events (Ground Truth)")
print("=" * 60)

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

# Get all social count brackets we have
brackets = db.execute("""
    SELECT * FROM social_count_brackets WHERE resolved=1
""").fetchall()

# Also reconstruct from paper_positions
pp = db.execute("""
    SELECT market_title, status, side, entry_price 
    FROM paper_positions WHERE archetype='social_count'
""").fetchall()

# For resolved events where exactly 1 bracket hit, we know the actual count
# Group by event (slug or event_title)
events = defaultdict(list)
for b in brackets:
    events[b["event_title"] or b["slug"]].append(dict(b))

print(f"\nResolved events with bracket data: {len(events)}")

actual_counts = []
for event_title, blist in events.items():
    # Find the winning bracket
    winners = [b for b in blist if b["hit"] == 1]
    if len(winners) == 1:
        w = winners[0]
        actual_range = (w["bracket_lo"], w["bracket_hi"])
        midpoint = (w["bracket_lo"] + w["bracket_hi"]) / 2
        actual_counts.append({
            "person": w["person"],
            "event": event_title,
            "actual_lo": w["bracket_lo"],
            "actual_hi": w["bracket_hi"],
            "midpoint": midpoint,
        })
        print(f"  {w['person']:>5} | actual in [{w['bracket_lo']}-{w['bracket_hi']}] | {event_title[:50]}")

print(f"\nKnown actual count ranges: {len(actual_counts)}")

# ══════════════════════════════════════════════════════════════════
# SOURCE 2: Wayback Machine → Historical Tweet/Post Counts
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SOURCE 2: Wayback Machine (Historical Profile Snapshots)")
print("=" * 60)

def get_wayback_snapshots(url, from_date="20250101", to_date="20260320"):
    """Get all Wayback Machine snapshots of a URL."""
    cdx_url = f"https://web.archive.org/cdx/search/cdx?url={url}&output=json&from={from_date}&to={to_date}&limit=500"
    data = fetch_json(cdx_url)
    if not data or len(data) < 2:
        return []
    # First row is headers
    headers = data[0]
    snapshots = []
    for row in data[1:]:
        snap = dict(zip(headers, row))
        snapshots.append(snap)
    return snapshots

# Musk's X profile
print("\nMusk (@elonmusk) Wayback snapshots:")
musk_snaps = get_wayback_snapshots("https://x.com/elonmusk")
if not musk_snaps:
    musk_snaps = get_wayback_snapshots("https://twitter.com/elonmusk")
print(f"  Found: {len(musk_snaps)} snapshots")

# Trump's Truth Social profile
print("\nTrump (@realDonaldTrump) Wayback snapshots:")
trump_snaps = get_wayback_snapshots("https://truthsocial.com/@realDonaldTrump")
print(f"  Found: {len(trump_snaps)} snapshots")

# Try to extract tweet counts from a few snapshots
def extract_count_from_wayback(snapshot, person):
    """Fetch a Wayback snapshot and try to extract the post/tweet count."""
    ts = snapshot.get("timestamp", "")
    url = snapshot.get("original", "")
    wb_url = f"https://web.archive.org/web/{ts}/{url}"
    
    html = fetch(wb_url, timeout=15)
    if not html:
        return None
    
    count = None
    date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    
    if person == "musk":
        # Look for tweet count patterns in HTML
        # Twitter/X shows "XX.XK posts" or "XXXX posts"
        patterns = [
            r'"statuses_count":(\d+)',  # JSON in page
            r'(\d[\d,]+)\s*(?:Tweets?|Posts?)',  # visible text
            r'"tweets_count":(\d+)',
            r'data-count="(\d+)"',
        ]
    else:
        # Truth Social patterns
        patterns = [
            r'"statuses_count":(\d+)',
            r'(\d[\d,]+)\s*(?:Truths?|Posts?)',
            r'"posts_count":(\d+)',
        ]
    
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            count_str = m.group(1).replace(",", "")
            try:
                count = int(count_str)
                return {"date": date, "count": count, "timestamp": ts, "source": "wayback"}
            except:
                pass
    
    return None

# Sample a few snapshots to extract counts
musk_counts = []
if musk_snaps:
    # Sample every ~7 days
    step = max(1, len(musk_snaps) // 30)
    sampled = musk_snaps[::step][:30]
    print(f"\n  Sampling {len(sampled)} Musk snapshots...")
    for snap in sampled:
        result = extract_count_from_wayback(snap, "musk")
        if result:
            musk_counts.append(result)
            print(f"    {result['date']}: {result['count']:,} tweets")
        time.sleep(0.5)  # Be nice to Wayback

trump_counts = []
if trump_snaps:
    step = max(1, len(trump_snaps) // 30)
    sampled = trump_snaps[::step][:30]
    print(f"\n  Sampling {len(sampled)} Trump snapshots...")
    for snap in sampled:
        result = extract_count_from_wayback(snap, "trump")
        if result:
            trump_counts.append(result)
            print(f"    {result['date']}: {result['count']:,} posts")
        time.sleep(0.5)

# ══════════════════════════════════════════════════════════════════
# SOURCE 3: Nitter/Alternative Twitter Frontends
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SOURCE 3: Alternative Twitter Frontends")
print("=" * 60)

nitter_instances = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
    "https://nitter.esmailelbob.xyz",
]

def try_nitter(username):
    """Try multiple Nitter instances to get tweet count."""
    for instance in nitter_instances:
        url = f"{instance}/{username}"
        html = fetch(url, timeout=8)
        if not html:
            continue
        
        # Nitter shows "X,XXX Tweets" or "X.XK Tweets"
        m = re.search(r'(\d[\d,.]+[KMkm]?)\s*(?:Tweets?|Posts?)', html)
        if m:
            count_str = m.group(1).replace(",", "")
            multiplier = 1
            if count_str.endswith(("K", "k")):
                count_str = count_str[:-1]
                multiplier = 1000
            elif count_str.endswith(("M", "m")):
                count_str = count_str[:-1]
                multiplier = 1000000
            try:
                count = int(float(count_str) * multiplier)
                print(f"  {instance}: @{username} = {count:,} tweets")
                return {"count": count, "source": f"nitter:{instance}", "date": datetime.now().strftime("%Y-%m-%d")}
            except:
                pass
        
        # Also try to find it in meta tags or JSON
        m = re.search(r'"tweets_count":\s*(\d+)', html)
        if m:
            count = int(m.group(1))
            print(f"  {instance}: @{username} = {count:,} tweets (JSON)")
            return {"count": count, "source": f"nitter:{instance}", "date": datetime.now().strftime("%Y-%m-%d")}
    
    return None

print("\nTrying Nitter for @elonmusk...")
musk_nitter = try_nitter("elonmusk")
if not musk_nitter:
    print("  All Nitter instances failed")

# ══════════════════════════════════════════════════════════════════
# SOURCE 4: RSS/Atom feeds of trackers
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SOURCE 4: Public Trackers & RSS")
print("=" * 60)

# Social Blade (free page scrape)
def try_socialblade(username, platform="twitter"):
    url = f"https://socialblade.com/{platform}/user/{username}"
    html = fetch(url, timeout=10)
    if not html:
        return None
    
    # Social Blade shows daily stats
    # Look for tweet count or daily post data
    m = re.search(r'Tweets?\s*</span>\s*<span[^>]*>(\d[\d,]+)', html, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    
    m = re.search(r'"uploads":\s*(\d+)', html)
    if m:
        return int(m.group(1))
    
    return None

print("\nTrying Social Blade for @elonmusk...")
sb_count = try_socialblade("elonmusk")
if sb_count:
    print(f"  Social Blade: {sb_count:,} tweets")
else:
    print("  Social Blade: blocked or no data")

# ══════════════════════════════════════════════════════════════════
# SOURCE 5: X/Twitter Syndication API (free, no auth)
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SOURCE 5: Twitter Syndication/Embed API")
print("=" * 60)

def get_twitter_syndication(username):
    """Use Twitter's unauthenticated syndication API."""
    # This endpoint sometimes works without auth
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    html = fetch(url, timeout=10)
    if not html:
        return None
    
    # Count tweets visible in timeline
    tweet_count = len(re.findall(r'data-tweet-id', html))
    
    # Try to find total count
    m = re.search(r'"statuses_count":(\d+)', html)
    if m:
        return {"total": int(m.group(1)), "source": "syndication"}
    
    return {"visible_tweets": tweet_count, "source": "syndication"} if tweet_count else None

print("\nTrying Twitter syndication for @elonmusk...")
synd = get_twitter_syndication("elonmusk")
if synd:
    print(f"  Result: {synd}")
else:
    print("  Syndication API: no data")

# ══════════════════════════════════════════════════════════════════
# SOURCE 6: Google Search for daily tweet counts
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SOURCE 6: Tracker Websites (tweetcounter, xtracker, etc)")
print("=" * 60)

tracker_urls = [
    ("https://www.trackalytics.com/twitter/profile/elonmusk/", r'Tweets?\s*</td>\s*<td[^>]*>(\d[\d,]+)'),
    ("https://www.tweetstats.com/graphs/elonmusk", r'"count":\s*(\d+)'),
]

for url, pattern in tracker_urls:
    html = fetch(url, timeout=8)
    if html:
        m = re.search(pattern, html)
        if m:
            print(f"  {url[:50]}: {m.group(1)}")
        else:
            print(f"  {url[:50]}: no count found")
    else:
        print(f"  {url[:50]}: unreachable")

# ══════════════════════════════════════════════════════════════════
# COMPUTE MODEL FROM AVAILABLE DATA
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("MODEL COMPUTATION")
print("=" * 60)

# Derive daily rates from Wayback data (cumulative count diffs)
def compute_daily_rates(counts):
    """From cumulative total counts at different dates, compute daily posting rate."""
    if len(counts) < 2:
        return None
    
    # Sort by date
    sorted_c = sorted(counts, key=lambda x: x["date"])
    
    rates = []
    for i in range(1, len(sorted_c)):
        prev = sorted_c[i-1]
        curr = sorted_c[i]
        
        d1 = datetime.strptime(prev["date"], "%Y-%m-%d")
        d2 = datetime.strptime(curr["date"], "%Y-%m-%d")
        days = (d2 - d1).days
        
        if days > 0 and curr["count"] > prev["count"]:
            daily = (curr["count"] - prev["count"]) / days
            rates.append({
                "from": prev["date"],
                "to": curr["date"],
                "days": days,
                "posts": curr["count"] - prev["count"],
                "daily_rate": daily,
            })
    
    return rates

if musk_counts:
    print("\nMUSK daily rates (from Wayback diffs):")
    rates = compute_daily_rates(musk_counts)
    if rates:
        all_rates = [r["daily_rate"] for r in rates]
        avg_rate = sum(all_rates) / len(all_rates)
        std_rate = (sum((r - avg_rate)**2 for r in all_rates) / len(all_rates)) ** 0.5
        print(f"  Avg: {avg_rate:.1f} tweets/day")
        print(f"  Std: {std_rate:.1f} tweets/day")
        print(f"  CV: {std_rate/avg_rate:.2f}")
        
        for r in rates:
            print(f"    {r['from']} to {r['to']} ({r['days']}d): {r['posts']} tweets = {r['daily_rate']:.1f}/day")

if trump_counts:
    print("\nTRUMP daily rates (from Wayback diffs):")
    rates = compute_daily_rates(trump_counts)
    if rates:
        all_rates = [r["daily_rate"] for r in rates]
        avg_rate = sum(all_rates) / len(all_rates)
        std_rate = (sum((r - avg_rate)**2 for r in all_rates) / len(all_rates)) ** 0.5
        print(f"  Avg: {avg_rate:.1f} posts/day")
        print(f"  Std: {std_rate:.1f} posts/day")

# Also use our Polymarket data to infer rates
print("\n\nRates from Polymarket resolved events:")
for ac in actual_counts:
    # Parse duration from event title
    dm = re.findall(r"(\w+)\s+(\d+)", ac["event"])
    if len(dm) >= 2:
        try:
            start_day = int(dm[-2][1])
            end_day = int(dm[-1][1])
            days = end_day - start_day if end_day > start_day else end_day + 30 - start_day
            if days > 0:
                rate = ac["midpoint"] / days
                print(f"  {ac['person']:>5}: ~{ac['midpoint']:.0f} in {days}d = {rate:.1f}/day [{ac['event'][:50]}]")
        except:
            pass

# ══════════════════════════════════════════════════════════════════
# SAVE RESULTS
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SAVING DATA")
print("=" * 60)

# Create history table
db.execute("""CREATE TABLE IF NOT EXISTS social_count_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    date TEXT NOT NULL,
    cumulative_count INTEGER,
    daily_count INTEGER,
    source TEXT,
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(person, date, source)
)""")

# Save Wayback data
inserted = 0
for counts, person in [(musk_counts, "musk"), (trump_counts, "trump")]:
    for c in counts:
        try:
            db.execute("INSERT OR IGNORE INTO social_count_history (person, date, cumulative_count, source) VALUES (?,?,?,?)",
                (person, c["date"], c["count"], c["source"]))
            inserted += 1
        except: pass

# Save Nitter data
if musk_nitter:
    try:
        db.execute("INSERT OR IGNORE INTO social_count_history (person, date, cumulative_count, source) VALUES (?,?,?,?)",
            ("musk", musk_nitter["date"], musk_nitter["count"], musk_nitter["source"]))
        inserted += 1
    except: pass

db.commit()
total = db.execute("SELECT COUNT(*) FROM social_count_history").fetchone()[0]
print(f"  Inserted: {inserted} | Total rows: {total}")

# ══════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("DATA SOURCE SUMMARY")
print("=" * 60)

sources = {
    "Polymarket resolved": len(actual_counts),
    "Wayback (Musk)": len(musk_counts),
    "Wayback (Trump)": len(trump_counts),
    "Nitter": 1 if musk_nitter else 0,
    "Social Blade": 1 if sb_count else 0,
    "Syndication": 1 if synd else 0,
}

for name, count in sources.items():
    status = "✅" if count > 0 else "❌"
    print(f"  {status} {name}: {count} data points")

working = sum(1 for v in sources.values() if v > 0)
print(f"\n  Working sources: {working}/{len(sources)}")

if working < 2:
    print("\n  ⚠️  Not enough free sources working.")
    print("  Fallback plan: use Crawl4AI to scrape X profile pages")
    print("  Or: browser automation via Polyclawd's browser tool")

db.close()
print("\nDONE")
