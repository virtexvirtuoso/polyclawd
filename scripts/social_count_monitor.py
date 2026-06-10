"""
Social Count Live Monitor — polls free APIs to build posting rate history.
Runs every 6 hours via scheduler.
Updated: 2026-03-20 (added FixTweet for Musk, improved parsers)
"""
import json, urllib.request, re, sqlite3, time
from datetime import datetime, timezone

DB = "/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db"

def fetch(url, timeout=15, headers=None):
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    if headers: hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None

def fetch_json(url, timeout=15):
    raw = fetch(url, timeout)
    try: 
        return json.loads(raw) if raw else None
    except: 
        return None

# ── DB Setup ─────────────────────────────────────────────────────

db = sqlite3.connect(DB)
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

# Also create social_count_snapshots for 6-hour granularity
db.execute("""CREATE TABLE IF NOT EXISTS social_count_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cumulative_count INTEGER,
    hourly_rate REAL,
    source TEXT,
    UNIQUE(person, timestamp, source)
)""")

now = datetime.now(timezone.utc)
timestamp = now.isoformat()
today = now.strftime("%Y-%m-%d")

results = {}

# ── Musk: FixTweet API (Primary) ─────────────────────────────────

print("=== Musk: FixTweet API ===")
musk_data = fetch_json("https://api.fxtwitter.com/elonmusk")
if musk_data and musk_data.get("code") == 200:
    user = musk_data.get("user", {})
    count = user.get("tweets")
    followers = user.get("followers")
    likes = user.get("likes")
    media = user.get("media_count")
    
    if count:
        results["musk_fxtweet"] = count
        print(f"  Tweets: {count:,}")
        print(f"  Followers: {followers:,}")
        print(f"  Likes: {likes:,}")
        print(f"  Media: {media:,}")
else:
    print(f"  FixTweet failed: {musk_data.get('message') if musk_data else 'no response'}")
    
    # Fallback: syndication
    print("\n  Fallback: Twitter Syndication")
    html = fetch("https://syndication.twitter.com/srv/timeline-profile/screen-name/elonmusk")
    if html:
        m = re.search(r'\"statuses_count\":(\d+)', html)
        if m:
            count = int(m.group(1))
            results["musk_syndication"] = count
            print(f"  Syndication: {count:,} tweets")
    else:
        print("  Syndication failed")

# ── Musk: Calculate Rate from Baseline ───────────────────────────

if "musk_fxtweet" in results:
    # Use 99692 as baseline (captured 2026-03-20 17:00 UTC)
    BASELINE_COUNT = 99692
    BASELINE_TIME = datetime(2026, 3, 20, 17, 0, 0, tzinfo=timezone.utc)
    current_count = results["musk_fxtweet"]
    
    hours_elapsed = (now - BASELINE_TIME).total_seconds() / 3600
    if hours_elapsed > 0:
        tweets_since = current_count - BASELINE_COUNT
        daily_rate = (tweets_since / hours_elapsed) * 24
        print(f"\n  Rate calculation:")
        print(f"    Hours since baseline: {hours_elapsed:.2f}")
        print(f"    Tweets since baseline: {tweets_since}")
        print(f"    Current daily rate: {daily_rate:.1f} tweets/day")

# ── Trump: Truth Social ─────────────────────────────────────────

print("\n=== Trump: Truth Social ===")

# Method 1: Mastodon API
ts_api = fetch_json("https://truthsocial.com/api/v1/accounts/lookup?acct=realDonaldTrump")
if ts_api:
    count = ts_api.get("statuses_count", 0)
    if count:
        results["trump_mastodon"] = count
        print(f"  Mastodon API: {count:,} posts")
        print(f"  Username: {ts_api.get('username')}")
        print(f"  Display: {ts_api.get('display_name')}")
else:
    print("  Mastodon API failed")
    
    # Fallback: HTML scrape
    print("\n  Fallback: HTML scrape")
    ts_html = fetch("https://truthsocial.com/@realDonaldTrump", timeout=15)
    if ts_html:
        patterns = [
            r'\"statuses_count\":(\d+)',
            r'(\d[\d,]+)\s*(?:Truths?|Posts?)',
            r'\"posts_count\":(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, ts_html, re.I)
            if m:
                count = int(m.group(1).replace(",", ""))
                results["trump_scrape"] = count
                print(f"  HTML scrape: {count:,} posts")
                break
        else:
            print("  Could not extract count")
    else:
        print("  Truth Social unreachable")

# ── Save Snapshots (6-hour granularity) ───────────────────────────

print("\n=== Saving Snapshots ===")
for key, count in results.items():
    person = "musk" if "musk" in key else "trump"
    source = key.split("_", 1)[1] if "_" in key else key
    
    try:
        db.execute("""INSERT OR REPLACE INTO social_count_snapshots 
            (person, timestamp, cumulative_count, source) 
            VALUES (?, ?, ?, ?)""",
            (person, timestamp, count, source))
        print(f"  Snapshot: {person} ({source}): {count:,}")
    except Exception as e:
        print(f"  Snapshot error: {e}")

# ── Save Daily Summary ──────────────────────────────────────────

print("\n=== Saving Daily ===")
for key, count in results.items():
    person = "musk" if "musk" in key else "trump"
    source = key.split("_", 1)[1] if "_" in key else key
    try:
        db.execute("""INSERT OR REPLACE INTO social_count_history 
            (person, date, cumulative_count, source, scraped_at) 
            VALUES (?, ?, ?, ?, ?)""",
            (person, today, count, source, timestamp))
    except Exception as e:
        print(f"  Daily save error: {e}")

db.commit()

# ── Compute Trends ───────────────────────────────────────────────

print("\n=== Recent Trends (24h) ===")
for person in ["musk", "trump"]:
    # Get last 5 snapshots
    rows = db.execute("""
        SELECT timestamp, cumulative_count, source 
        FROM social_count_snapshots 
        WHERE person=? AND cumulative_count IS NOT NULL
        ORDER BY timestamp DESC LIMIT 5
    """, (person,)).fetchall()
    
    if len(rows) >= 2:
        newest = rows[0]
        oldest = rows[-1]
        d1 = datetime.fromisoformat(oldest[0].replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(newest[0].replace("Z", "+00:00"))
        hours = (d2 - d1).total_seconds() / 3600
        
        if hours > 0:
            delta = newest[1] - oldest[1]
            hourly_rate = delta / hours
            daily_rate = hourly_rate * 24
            print(f"  {person}: {delta:+d} in {hours:.1f}h = {daily_rate:.1f}/day ({newest[2]})")
    else:
        print(f"  {person}: collecting data ({len(rows)} snapshots)")

total_daily = db.execute("SELECT COUNT(*) FROM social_count_history").fetchone()[0]
total_snapshots = db.execute("SELECT COUNT(*) FROM social_count_snapshots").fetchone()[0]
print(f"\nTotal records: {total_daily} daily, {total_snapshots} snapshots")

db.close()
print("\n=== DONE ===")
