#!/usr/bin/env python3
"""
Tweet Count Scanner for Polyclawd

Scans Polymarket Elon Musk (and other) tweet count bracket markets.
Uses xtracker.polymarket.com API for historical post data.
Runs Monte Carlo simulation to find mispriced brackets.

Edge source: market systematically underestimates posting rate.
Same structural pattern as weather_scanner.py.
"""

import json
import logging
import random
import statistics
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("polyclawd.tweet_count_scanner")

# ============================================================================
# Configuration
# ============================================================================

XTRACKER_API = "https://xtracker.polymarket.com/api"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Cache for xtracker data (avoid hammering their API)
CACHE_FILE = Path(__file__).parent.parent / "storage" / "tweet_count_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour

# Monte Carlo settings
MC_SIMULATIONS = 50_000
MC_SEED = None  # None = random seed each run for true randomness

# Minimum edge to surface a signal
MIN_EDGE_PCT = 3.0

# Tracked accounts (handle → slug pattern)
TRACKED_ACCOUNTS = {
    "elonmusk": {
        "name": "Elon Musk",
        "slug_pattern": "elon-musk-of-tweets-{slug_dates}",
        "slug_search": "elon-musk-of-tweets-",
        "rolling_days": 28,  # Use last N days for distribution
    },
    "Cobratate": {
        "name": "Andrew Tate",
        "slug_pattern": "andrew-tate-of-tweets-{slug_dates}",
        "slug_search": "andrew-tate-of-tweets-",
        "rolling_days": 28,
    },
}

# Bracket width (Polymarket uses 20-tweet increments)
BRACKET_WIDTH = 20


# ============================================================================
# HTTP helpers
# ============================================================================

def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """Fetch JSON from URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Fetch failed %s: %s", url[:80], e)
        return None


# ============================================================================
# XTracker API
# ============================================================================

def fetch_post_history(handle: str) -> Optional[List[dict]]:
    """Fetch all historical posts for an account from xtracker API.
    
    Returns list of posts with 'createdAt' timestamps.
    Uses file cache to avoid excessive API calls.
    """
    # Check cache
    cache = _load_cache()
    cache_key = f"posts_{handle}"
    if cache_key in cache:
        entry = cache[cache_key]
        age = datetime.now(timezone.utc).timestamp() - entry.get("fetched_at", 0)
        if age < CACHE_TTL_SECONDS:
            logger.debug("Cache hit for %s (%d posts, %.0fs old)", handle, len(entry["posts"]), age)
            return entry["posts"]

    data = _fetch_json(f"{XTRACKER_API}/users/{handle}/posts")
    if not data or not data.get("success"):
        logger.warning("Failed to fetch posts for %s", handle)
        return None

    posts = data.get("data", [])
    logger.info("Fetched %d posts for @%s from xtracker", len(posts), handle)

    # Cache
    cache[cache_key] = {
        "posts": posts,
        "fetched_at": datetime.now(timezone.utc).timestamp(),
    }
    _save_cache(cache)
    return posts


def fetch_account_info(handle: str) -> Optional[dict]:
    """Fetch account info including active tracking windows."""
    data = _fetch_json(f"{XTRACKER_API}/users/{handle}")
    if not data or not data.get("success"):
        return None
    return data.get("data", {})


def get_daily_counts(posts: List[dict], rolling_days: int = 28) -> List[int]:
    """Convert raw posts into daily counts for the last N days.
    
    Returns list of daily post counts (one per day).
    Excludes today (partial day).
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff = today - timedelta(days=rolling_days)

    daily = {}
    for p in posts:
        created = p.get("createdAt", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            d = dt.date()
            if cutoff <= d < today:  # Exclude today (partial)
                daily[d] = daily.get(d, 0) + 1
        except (ValueError, TypeError):
            continue

    # Fill gaps with 0 (days with no posts)
    counts = []
    current = cutoff
    while current < today:
        counts.append(daily.get(current, 0))
        current += timedelta(days=1)

    return counts


def count_posts_in_window(posts: List[dict], start: datetime, end: datetime) -> int:
    """Count posts within a specific time window."""
    count = 0
    for p in posts:
        created = p.get("createdAt", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if start <= dt <= end:
                count += 1
        except (ValueError, TypeError):
            continue
    return count


# ============================================================================
# Monte Carlo Engine
# ============================================================================

def run_monte_carlo(daily_counts: List[int], window_days: float,
                    posts_so_far: int = 0, days_elapsed: float = 0,
                    simulations: int = MC_SIMULATIONS) -> Dict[str, float]:
    """Run Monte Carlo simulation for tweet count bracket probabilities.
    
    Args:
        daily_counts: Historical daily post counts (for sampling)
        window_days: Total window length in days
        posts_so_far: Posts already counted in current window
        days_elapsed: Days already elapsed in current window
        simulations: Number of MC runs
    
    Returns:
        Dict of bracket_key → probability (e.g. {"280-299": 0.144})
    """
    if not daily_counts:
        return {}

    remaining_days = max(0, window_days - days_elapsed)
    remaining_whole = int(remaining_days)
    remaining_frac = remaining_days - remaining_whole

    rng = random.Random(MC_SEED)
    bracket_hits: Dict[str, int] = {}

    for _ in range(simulations):
        # Sample remaining full days from historical distribution
        total = posts_so_far
        for _ in range(remaining_whole):
            total += rng.choice(daily_counts)

        # Partial day: sample and scale
        if remaining_frac > 0.1:
            total += int(rng.choice(daily_counts) * remaining_frac)

        # Map to bracket
        bracket_start = (total // BRACKET_WIDTH) * BRACKET_WIDTH
        if bracket_start >= 580:
            key = "580+"
        else:
            key = f"{bracket_start}-{bracket_start + BRACKET_WIDTH - 1}"
        bracket_hits[key] = bracket_hits.get(key, 0) + 1

    # Convert to probabilities
    return {k: v / simulations for k, v in bracket_hits.items()}


# ============================================================================
# Market Discovery
# ============================================================================

def discover_tweet_markets(handle: str = "elonmusk") -> List[dict]:
    """Find active tweet count bracket markets on Polymarket.
    
    Two-pass discovery:
    1. Slug-based: look up known tracking windows from xtracker
    2. Volume-based: scan top events for any we missed
    
    Returns list of market dicts with condition_id, question, prices.
    """
    config = TRACKED_ACCOUNTS.get(handle, {})
    slug_search = config.get("slug_search", f"{handle}-tweets-")
    seen_slugs = set()
    markets = []

    def _extract_event_markets(event: dict) -> List[dict]:
        """Extract bracket markets from a Gamma event."""
        result = []
        slug = event.get("slug", "")
        title = event.get("title", "")
        end_date = event.get("endDate", "")
        event_vol = event.get("volume", 0)

        for m in event.get("markets", []):
            condition_id = m.get("conditionId", "")
            question = m.get("question", "")
            prices_raw = m.get("outcomePrices", "")

            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                yes_price = float(prices[0]) if prices else 0
            except (json.JSONDecodeError, IndexError, TypeError):
                yes_price = 0

            volume = m.get("volumeNum", 0)
            bracket = _extract_bracket(question)
            if not bracket:
                continue

            result.append({
                "condition_id": condition_id,
                "question": question,
                "bracket": bracket,
                "yes_price": yes_price,
                "volume": volume,
                "event_title": title,
                "event_slug": slug,
                "event_end_date": end_date,
                "event_volume": event_vol,
                "handle": handle,
            })
        return result

    # Pass 1: Slug-based discovery from xtracker tracking windows
    account_info = fetch_account_info(handle)
    if account_info:
        trackings = account_info.get("trackings", [])
        for t in trackings:
            if not t.get("isActive"):
                continue
            # Build slug from tracking title
            title = t.get("title", "").lower()
            # Try known slug patterns by searching Gamma
            # Parse dates from title: "Elon Musk # tweets February 27 - March 6, 2026?"
            import re
            date_match = re.findall(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d+)', title)
            if len(date_match) >= 2:
                slug_candidate = f"{slug_search}{date_match[0][0]}-{date_match[0][1]}-{date_match[1][0]}-{date_match[1][1]}"
                data = _fetch_json(f"{GAMMA_API}/events?slug={slug_candidate}")
                if data and len(data) > 0:
                    event = data[0]
                    slug = event.get("slug", slug_candidate)
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        event_mkts = _extract_event_markets(event)
                        markets.extend(event_mkts)
                        logger.debug("Slug discovery: %s → %d markets", slug[:40], len(event_mkts))

            # Also try monthly pattern: "Elon Musk musk # tweets in March 2026?"
            month_match = re.search(r'in\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})', title)
            if month_match:
                slug_candidate = f"{slug_search}{month_match.group(1)}-{month_match.group(2)}"
                data = _fetch_json(f"{GAMMA_API}/events?slug={slug_candidate}")
                if data and len(data) > 0:
                    event = data[0]
                    slug = event.get("slug", slug_candidate)
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        markets.extend(_extract_event_markets(event))

    # Pass 2: Volume-based scan (catch any events slug discovery missed)
    data = _fetch_json(
        f"{GAMMA_API}/events?active=true&closed=false&limit=50"
        f"&order=volume24hr&ascending=false"
    )
    if data:
        for event in data:
            slug = event.get("slug", "")
            if slug_search in slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                markets.extend(_extract_event_markets(event))

    logger.info("Discovered %d bracket markets for @%s across %d events",
                len(markets), handle, len(seen_slugs))
    return markets


def _extract_bracket(question: str) -> Optional[str]:
    """Extract bracket range from market question.
    
    Examples:
        "Will Elon Musk post 200-219 tweets..." → "200-219"
        "Will Elon Musk post 580+ tweets..." → "580+"
        "Will Elon Musk post 0-19 tweets..." → "0-19"
    """
    import re
    # Match "N-M" or "N+" pattern
    match = re.search(r'(\d+)-(\d+)', question)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return f"{low}-{high}"

    match = re.search(r'(\d+)\+', question)
    if match:
        return f"{match.group(1)}+"

    # Also match "0-19" style
    match = re.search(r'(\d+)\s*[-–]\s*(\d+)', question)
    if match:
        return f"{match.group(1)}-{match.group(2)}"

    return None


def _parse_window_from_event(event_slug: str, event_end_date: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse the tracking window from event metadata.
    
    Returns (start_datetime, end_datetime) in UTC.
    """
    import re

    # Try to get from account's tracking windows (more reliable)
    # Fallback: parse from slug
    # Slug format: elon-musk-of-tweets-february-27-march-6
    months = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12
    }

    # Extract month-day pairs from slug
    slug_lower = event_slug.lower()
    matches = re.findall(r'(january|february|march|april|may|june|july|august|september|october|november|december)-(\d+)', slug_lower)

    if len(matches) >= 2:
        now = datetime.now(timezone.utc)
        year = now.year

        start_month = months[matches[0][0]]
        start_day = int(matches[0][1])
        end_month = months[matches[1][0]]
        end_day = int(matches[1][1])

        # Markets use noon ET (17:00 UTC)
        try:
            start = datetime(year, start_month, start_day, 17, 0, 0, tzinfo=timezone.utc)
            end = datetime(year, end_month, end_day, 17, 0, 0, tzinfo=timezone.utc)
            return start, end
        except ValueError:
            pass

    # Fallback: use event end date
    if event_end_date:
        try:
            end = datetime.fromisoformat(event_end_date.replace("Z", "+00:00"))
            start = end - timedelta(days=7)  # Assume 7-day window
            return start, end
        except ValueError:
            pass

    return None, None


# ============================================================================
# Signal Generation
# ============================================================================

def scan_tweet_markets(handle: str = "elonmusk") -> List[dict]:
    """Full scan pipeline for a single account.
    
    1. Fetch post history from xtracker
    2. Discover active bracket markets
    3. Run Monte Carlo for each event window
    4. Compare MC probabilities to market prices
    5. Return signals with edge
    """
    config = TRACKED_ACCOUNTS.get(handle, {})
    name = config.get("name", handle)
    rolling_days = config.get("rolling_days", 28)

    # 1. Fetch post history
    posts = fetch_post_history(handle)
    if not posts:
        logger.warning("No post history for @%s — skipping", handle)
        return []

    daily_counts = get_daily_counts(posts, rolling_days=rolling_days)
    if len(daily_counts) < 7:
        logger.warning("Only %d days of data for @%s — need 7+", len(daily_counts), handle)
        return []

    mean_daily = statistics.mean(daily_counts)
    stdev_daily = statistics.stdev(daily_counts) if len(daily_counts) > 1 else 0
    logger.info("@%s: %d days, mean=%.1f/day, stdev=%.1f, median=%.0f",
                handle, len(daily_counts), mean_daily, stdev_daily, statistics.median(daily_counts))

    # 2. Discover markets
    markets = discover_tweet_markets(handle)
    if not markets:
        logger.info("No active markets for @%s", handle)
        return []

    # 3. Group markets by event
    events: Dict[str, List[dict]] = {}
    for m in markets:
        slug = m["event_slug"]
        if slug not in events:
            events[slug] = []
        events[slug].append(m)

    # 4. Run MC per event, compare to market prices
    signals = []
    now = datetime.now(timezone.utc)

    for slug, event_markets in events.items():
        # Parse window
        sample = event_markets[0]
        start, end = _parse_window_from_event(slug, sample.get("event_end_date", ""))
        if not start or not end:
            logger.warning("Cannot parse window for %s", slug)
            continue

        window_days = (end - start).total_seconds() / 86400
        if window_days <= 0:
            continue

        # Calculate days elapsed and posts so far
        days_elapsed = max(0, (now - start).total_seconds() / 86400)
        if days_elapsed >= window_days:
            logger.debug("Window %s already closed", slug)
            continue

        posts_so_far = count_posts_in_window(posts, start, min(now, end))

        logger.info("Event %s: %.1f/%.1f days elapsed, %d posts so far",
                     slug[:40], days_elapsed, window_days, posts_so_far)

        # Run Monte Carlo
        mc_probs = run_monte_carlo(
            daily_counts, window_days,
            posts_so_far=posts_so_far,
            days_elapsed=days_elapsed,
        )

        if not mc_probs:
            continue

        # 5. Compare MC to market prices
        for m in event_markets:
            bracket = m["bracket"]
            yes_price = m["yes_price"]

            mc_yes = mc_probs.get(bracket, 0)
            mc_no = 1 - mc_yes
            market_no = 1 - yes_price

            # Calculate edges for both sides
            edge_no = (mc_no - market_no) * 100  # positive = NO is underpriced
            edge_yes = (mc_yes - yes_price) * 100  # positive = YES is underpriced

            # Pick better side
            if edge_no > edge_yes and edge_no > MIN_EDGE_PCT:
                side = "NO"
                edge = edge_no
                our_prob = mc_no
                effective_price = 1 - yes_price  # Cost of NO
            elif edge_yes > MIN_EDGE_PCT:
                side = "YES"
                edge = edge_yes
                our_prob = mc_yes
                effective_price = yes_price
            else:
                continue  # No edge

            # Skip garbage (too cheap or too expensive)
            if effective_price < 0.01 or effective_price > 0.98:
                continue

            confidence = min(0.95, our_prob)

            signals.append({
                "market_id": m["condition_id"],
                "market": m["question"][:120],
                "market_title": m["question"][:120],
                "side": side,
                "entry_price": yes_price,
                "market_price": yes_price,
                "confidence": round(confidence, 3),
                "edge_pct": round(edge, 1),
                "strategy": "tweet_count_mc",
                "archetype": "social_count",
                "platform": "polymarket",
                "source": "tweet_count_scanner",
                "bracket": bracket,
                "handle": handle,
                "account_name": name,
                "event_slug": slug,
                "event_title": sample.get("event_title", ""),
                "event_volume": sample.get("event_volume", 0),
                "volume": m.get("volume", 0),
                "mc_yes_prob": round(mc_yes, 4),
                "mc_no_prob": round(mc_no, 4),
                "posts_so_far": posts_so_far,
                "days_elapsed": round(days_elapsed, 1),
                "window_days": round(window_days, 1),
                "projected_total": round(posts_so_far / days_elapsed * window_days) if days_elapsed > 0.5 else round(mean_daily * window_days),
                "daily_mean": round(mean_daily, 1),
                "daily_stdev": round(stdev_daily, 1),
                "days_to_close": round(max(0.1, window_days - days_elapsed), 1),
            })

    signals.sort(key=lambda x: x["edge_pct"], reverse=True)
    logger.info("Tweet count scan: %d signals with >%.0f%% edge for @%s",
                len(signals), MIN_EDGE_PCT, handle)
    return signals


def scan_all_tweet_markets() -> Dict[str, any]:
    """Scan all tracked accounts and return combined results."""
    all_signals = []
    account_stats = {}

    for handle in TRACKED_ACCOUNTS:
        try:
            signals = scan_tweet_markets(handle)
            all_signals.extend(signals)
            account_stats[handle] = {
                "signals": len(signals),
                "top_edge": signals[0]["edge_pct"] if signals else 0,
            }
        except Exception as e:
            logger.error("Error scanning @%s: %s", handle, e)
            account_stats[handle] = {"signals": 0, "error": str(e)}

    return {
        "signals": all_signals,
        "accounts": account_stats,
        "total_signals": len(all_signals),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Portfolio Integration
# ============================================================================

def get_tweet_portfolio_signals(min_edge: float = 5.0, max_signals: int = 5) -> List[dict]:
    """Get tweet count signals formatted for paper_portfolio.process_signals().
    
    Deduplicates: best bracket per event (don't overload one event).
    """
    result = scan_all_tweet_markets()
    all_signals = result.get("signals", [])
    if not all_signals:
        return []

    # Filter by minimum edge
    filtered = [s for s in all_signals if s["edge_pct"] >= min_edge]

    # Deduplicate: best signal per event
    best_per_event = {}
    for s in filtered:
        key = s["event_slug"]
        if key not in best_per_event or s["edge_pct"] > best_per_event[key]["edge_pct"]:
            best_per_event[key] = s

    # Sort by edge, take top N
    top = sorted(best_per_event.values(), key=lambda x: x["edge_pct"], reverse=True)[:max_signals]

    # Already in portfolio signal format
    logger.info("Tweet portfolio signals: %d/%d pass min_edge=%.0f%%",
                len(top), len(all_signals), min_edge)
    return top


# ============================================================================
# Cache helpers
# ============================================================================

def _load_cache() -> Dict:
    """Load file-based cache."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: Dict):
    """Save file-based cache."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.warning("Failed to save cache: %s", e)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        result = scan_all_tweet_markets()
        signals = result["signals"]
        print(f"\n{'='*80}")
        print(f"Tweet Count Scanner — {result['total_signals']} signals found")
        print(f"{'='*80}\n")

        for handle, stats in result["accounts"].items():
            name = TRACKED_ACCOUNTS[handle]["name"]
            print(f"@{handle} ({name}): {stats['signals']} signals, top edge={stats.get('top_edge',0):.1f}%")

        if signals:
            print(f"\n{'Bracket':>12s}  {'Side':>4s}  {'Mkt YES':>8s}  {'MC YES':>8s}  {'Edge':>6s}  {'Market'}")
            print("-" * 90)
            for s in signals[:20]:
                print(f"{s['bracket']:>12s}  {s['side']:>4s}  {s['entry_price']:>7.1%}  "
                      f"{s['mc_yes_prob']:>7.1%}  {s['edge_pct']:>+5.1f}%  {s['market'][:45]}")

            print(f"\nProjections:")
            seen = set()
            for s in signals:
                slug = s["event_slug"]
                if slug not in seen:
                    seen.add(slug)
                    print(f"  {s['event_title'][:50]}: {s['posts_so_far']} posts in {s['days_elapsed']:.1f}d → projected {s['projected_total']}")
        else:
            print("\nNo signals with sufficient edge found.")

    elif cmd == "portfolio":
        signals = get_tweet_portfolio_signals()
        print(json.dumps(signals, indent=2))

    elif cmd == "history":
        handle = sys.argv[2] if len(sys.argv) > 2 else "elonmusk"
        posts = fetch_post_history(handle)
        if posts:
            counts = get_daily_counts(posts, rolling_days=28)
            print(f"@{handle}: {len(counts)} days, mean={statistics.mean(counts):.1f}, "
                  f"stdev={statistics.stdev(counts):.1f}, median={statistics.median(counts):.0f}")
            print(f"7-day projection: {statistics.mean(counts)*7:.0f} ± {statistics.stdev(counts)*7**0.5:.0f}")
            print("\nDaily counts (last 14 days):")
            for i, c in enumerate(counts[-14:]):
                d = (datetime.now(timezone.utc).date() - timedelta(days=14-i))
                print(f"  {d}: {c} posts")
    else:
        print("Usage: tweet_count_scanner.py [scan|portfolio|history [handle]]")
