"""Google Trends client — detects election-related trending topics via RSS feed.

Uses the free, rate-limit-free Google Trends RSS feed to detect when election
candidates or topics are trending nationally. When a tracked candidate or keyword
appears in trending topics, it signals a major attention event.

Note: pytrends (interest_over_time) is permanently 429'd by Google as of 2026.
The RSS feed is the only reliable free endpoint.
"""

import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

CACHE_DIR = Path(__file__).parent.parent / "storage" / "gtrends_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 1800  # 30 min — RSS updates frequently
HISTORY_FILE = CACHE_DIR / "trend_history.json"
HISTORY_TTL = 86400 * 7  # 7 days of history for frequency analysis

RSS_URL = "https://trends.google.com/trending/rss?geo=US"

# Candidates and election keywords to match against trending topics
TRACKED_CANDIDATES = {
    # name fragments → metadata (case-insensitive matching)
    "ossoff": {"state": "GA", "race": "senate", "party": "D", "name": "Jon Ossoff"},
    "gary black": {"state": "GA", "race": "senate", "party": "R", "name": "Gary Black"},
    "slotkin": {"state": "MI", "race": "senate", "party": "D", "name": "Elissa Slotkin"},
    "susan collins": {"state": "ME", "race": "senate", "party": "R", "name": "Susan Collins"},
    "tillis": {"state": "NC", "race": "senate", "party": "R", "name": "Thom Tillis"},
    "shaheen": {"state": "NH", "race": "senate", "party": "D", "name": "Jeanne Shaheen"},
    "tina smith": {"state": "MN", "race": "senate", "party": "D", "name": "Tina Smith"},
    "ted cruz": {"state": "TX", "race": "senate", "party": "R", "name": "Ted Cruz"},
    "cornyn": {"state": "TX", "race": "senate", "party": "R", "name": "John Cornyn"},
    "ken paxton": {"state": "TX", "race": "senate", "party": "R", "name": "Ken Paxton"},
    "vance": {"state": "OH", "race": "senate", "party": "R", "name": "JD Vance"},
    "murkowski": {"state": "AK", "race": "senate", "party": "R", "name": "Lisa Murkowski"},
    "joni ernst": {"state": "IA", "race": "senate", "party": "R", "name": "Joni Ernst"},
    "stacey abrams": {"state": "GA", "race": "governor", "party": "D", "name": "Stacey Abrams"},
    "whitmer": {"state": "MI", "race": "governor", "party": "D", "name": "Gretchen Whitmer"},
    "tony evers": {"state": "WI", "race": "governor", "party": "D", "name": "Tony Evers"},
    "newsom": {"state": "", "race": "presidential", "party": "D", "name": "Gavin Newsom"},
    "desantis": {"state": "", "race": "presidential", "party": "R", "name": "Ron DeSantis"},
    "ocasio-cortez": {"state": "", "race": "presidential", "party": "D", "name": "AOC"},
    "aoc": {"state": "", "race": "presidential", "party": "D", "name": "AOC"},
    "rubio": {"state": "", "race": "presidential", "party": "R", "name": "Marco Rubio"},
}

# Broader election keywords (not candidate-specific, but signal election attention)
ELECTION_KEYWORDS = [
    "midterm", "midterms", "2026 election", "senate race", "senate election",
    "governor race", "gubernatorial", "ballot", "primary election",
    "congressional race", "swing state", "battleground",
    "2028 election", "presidential race", "campaign",
]


def _fetch_rss() -> list[dict]:
    """Fetch and parse Google Trends RSS feed for US."""
    cache_path = CACHE_DIR / "rss_latest.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req = urllib.request.Request(RSS_URL, headers={
        "User-Agent": "polyclawd/1.0 (election market analysis)",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        logger.debug("Google Trends RSS fetch error: {}", e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        logger.debug("Google Trends RSS parse error: {}", e)
        return []

    ns = {"ht": "https://trends.google.com/trending/rss"}
    items = []

    for item in root.findall(".//item"):
        title = item.findtext("title", "")
        traffic = item.findtext("ht:approx_traffic", "", ns)
        pub_date = item.findtext("pubDate", "")

        # Extract news headlines from the trend
        news = []
        for news_item in item.findall("ht:news_item", ns):
            headline = news_item.findtext("ht:news_item_title", "", ns)
            source = news_item.findtext("ht:news_item_source", "", ns)
            if headline:
                news.append({"headline": headline, "source": source})

        # Parse traffic (e.g., "200,000+" → 200000)
        traffic_num = 0
        if traffic:
            traffic_clean = re.sub(r"[^0-9]", "", traffic)
            traffic_num = int(traffic_clean) if traffic_clean else 0

        items.append({
            "topic": title,
            "traffic": traffic_num,
            "traffic_str": traffic,
            "published": pub_date,
            "news": news[:3],
        })

    with open(cache_path, "w") as f:
        json.dump(items, f)

    logger.debug("Google Trends RSS: fetched {} trending topics", len(items))
    return items


def _match_candidates(trending: list[dict]) -> list[dict]:
    """Match trending topics against tracked candidates and election keywords."""
    matches = []

    for item in trending:
        topic_lower = item["topic"].lower()
        # Also check news headlines
        headlines_lower = " ".join(
            n["headline"].lower() for n in item.get("news", [])
        )
        search_text = f"{topic_lower} {headlines_lower}"

        # Check candidate matches
        for fragment, meta in TRACKED_CANDIDATES.items():
            if fragment in search_text:
                matches.append({
                    "candidate": meta["name"],
                    "state": meta["state"],
                    "race": meta["race"],
                    "party": meta["party"],
                    "trending_topic": item["topic"],
                    "traffic": item["traffic"],
                    "traffic_str": item["traffic_str"],
                    "news": item["news"],
                    "match_type": "candidate",
                    "is_spike": True,
                })
                break  # One match per trending topic

        # Check election keyword matches (only if no candidate matched)
        if not any(m["trending_topic"] == item["topic"] for m in matches):
            for kw in ELECTION_KEYWORDS:
                if kw in search_text:
                    matches.append({
                        "candidate": None,
                        "state": "",
                        "race": "",
                        "party": "",
                        "trending_topic": item["topic"],
                        "traffic": item["traffic"],
                        "traffic_str": item["traffic_str"],
                        "news": item["news"],
                        "keyword_match": kw,
                        "match_type": "election_topic",
                        "is_spike": True,
                    })
                    break

    # Sort by traffic descending
    matches.sort(key=lambda x: -x["traffic"])
    return matches


def _update_history(matches: list[dict]):
    """Track trend history for frequency analysis."""
    now = time.time()
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []

    # Prune old entries
    history = [h for h in history if now - h.get("ts", 0) < HISTORY_TTL]

    # Add new matches
    for m in matches:
        history.append({
            "ts": now,
            "topic": m["trending_topic"],
            "candidate": m.get("candidate"),
            "traffic": m["traffic"],
            "match_type": m["match_type"],
        })

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

    return history


def fetch_trending_elections() -> list[dict]:
    """Fetch Google Trends and detect election-related trending topics."""
    trending = _fetch_rss()
    if not trending:
        return []

    matches = _match_candidates(trending)
    _update_history(matches)

    logger.info("Google Trends: {} trending topics, {} election matches",
                len(trending), len(matches))
    return matches


def build_gtrends_overlay() -> dict:
    """Build Google Trends overlay for election report."""
    matches = fetch_trending_elections()

    # Separate candidate matches from general election topics
    candidate_spikes = [m for m in matches if m["match_type"] == "candidate"]
    election_topics = [m for m in matches if m["match_type"] == "election_topic"]

    # Load history for frequency stats
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass

    # Count how many times each candidate has trended in last 7 days
    trend_counts = {}
    for h in history:
        cand = h.get("candidate")
        if cand:
            trend_counts[cand] = trend_counts.get(cand, 0) + 1

    return {
        "gtrends_spikes": candidate_spikes,
        "gtrends_election_topics": election_topics,
        "gtrends_all": matches,
        "gtrends_tracked": len(matches),
        "gtrends_trend_counts": trend_counts,
    }
