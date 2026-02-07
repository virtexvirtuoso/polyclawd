#!/usr/bin/env python3
"""
News Signal Source for Polyclawd

Free, reliable news sources:
1. Google News RSS - Real-time, comprehensive
2. Reddit JSON API - Sentiment on crypto/politics/sports
3. CryptoPanic API - Crypto-specific news (free tier)

Generates trading signals when breaking news affects active markets.
"""

import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree
from pathlib import Path
import html
import time

# ============================================================================
# Configuration
# ============================================================================

# Cache to prevent duplicate signals
CACHE_FILE = Path.home() / ".openclaw" / "polyclawd" / "news_cache.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

# News age threshold (only process news < X minutes old)
MAX_NEWS_AGE_MINUTES = 30

# Minimum confidence to generate signal
MIN_CONFIDENCE = 35

# Rate limiting
LAST_FETCH: Dict[str, float] = {}
MIN_FETCH_INTERVAL = 60  # seconds between fetches per source

# ============================================================================
# Google News RSS
# ============================================================================

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

def fetch_google_news(query: str, max_results: int = 10) -> List[Dict]:
    """Fetch news from Google News RSS feed."""
    # Rate limit
    cache_key = f"google_{query}"
    now = time.time()
    if cache_key in LAST_FETCH and now - LAST_FETCH[cache_key] < MIN_FETCH_INTERVAL:
        return []
    LAST_FETCH[cache_key] = now
    
    try:
        encoded_query = urllib.parse.quote(query)
        url = GOOGLE_NEWS_RSS.format(query=encoded_query)
        
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Polyclawd/1.0)"
        })
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read().decode('utf-8')
        
        root = ElementTree.fromstring(xml_data)
        articles = []
        
        for item in root.findall('.//item')[:max_results]:
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            source = item.find('source')
            
            if title is None or pub_date is None:
                continue
            
            # Parse publication date
            try:
                # Format: "Thu, 06 Feb 2025 19:30:00 GMT"
                pub_dt = datetime.strptime(
                    pub_date.text.replace(" GMT", ""), 
                    "%a, %d %b %Y %H:%M:%S"
                )
                age_minutes = (datetime.now() - pub_dt).total_seconds() / 60
            except:
                age_minutes = 999
            
            articles.append({
                "title": html.unescape(title.text) if title.text else "",
                "link": link.text if link is not None else "",
                "source": source.text if source is not None else "Unknown",
                "published": pub_date.text,
                "age_minutes": round(age_minutes, 1),
                "query": query,
            })
        
        return articles
    
    except Exception as e:
        return []


# ============================================================================
# Reddit API (No auth needed for public data)
# ============================================================================

REDDIT_JSON = "https://www.reddit.com/r/{subreddit}/search.json?q={query}&sort=new&t=day&limit=10"
REDDIT_HOT = "https://www.reddit.com/r/{subreddit}/hot.json?limit=25"

CRYPTO_SUBREDDITS = ["cryptocurrency", "bitcoin", "ethereum", "CryptoMarkets"]
POLITICS_SUBREDDITS = ["politics", "news", "worldnews"]
SPORTS_SUBREDDITS = ["nfl", "nba", "sports", "sportsbook"]

def fetch_reddit_posts(subreddit: str, query: Optional[str] = None) -> List[Dict]:
    """Fetch posts from Reddit JSON API."""
    cache_key = f"reddit_{subreddit}_{query or 'hot'}"
    now = time.time()
    if cache_key in LAST_FETCH and now - LAST_FETCH[cache_key] < MIN_FETCH_INTERVAL:
        return []
    LAST_FETCH[cache_key] = now
    
    try:
        if query:
            url = REDDIT_JSON.format(
                subreddit=subreddit, 
                query=urllib.parse.quote(query)
            )
        else:
            url = REDDIT_HOT.format(subreddit=subreddit)
        
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Polyclawd/1.0)"
        })
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        
        posts = []
        for child in data.get('data', {}).get('children', []):
            post = child.get('data', {})
            
            created_utc = post.get('created_utc', 0)
            age_minutes = (time.time() - created_utc) / 60
            
            posts.append({
                "title": post.get('title', ''),
                "subreddit": subreddit,
                "score": post.get('score', 0),
                "num_comments": post.get('num_comments', 0),
                "upvote_ratio": post.get('upvote_ratio', 0.5),
                "link": f"https://reddit.com{post.get('permalink', '')}",
                "age_minutes": round(age_minutes, 1),
                "query": query,
            })
        
        return posts
    
    except Exception as e:
        return []


# ============================================================================
# CryptoPanic API (Free tier)
# ============================================================================

CRYPTOPANIC_API = "https://cryptopanic.com/api/v1/posts/?auth_token=free&filter=hot&currencies={symbol}"

def fetch_cryptopanic(symbol: str = "BTC") -> List[Dict]:
    """Fetch crypto news from CryptoPanic (free tier, limited)."""
    cache_key = f"cryptopanic_{symbol}"
    now = time.time()
    if cache_key in LAST_FETCH and now - LAST_FETCH[cache_key] < MIN_FETCH_INTERVAL * 2:
        return []
    LAST_FETCH[cache_key] = now
    
    try:
        # Free tier just shows recent news without auth
        url = f"https://cryptopanic.com/api/free/v1/posts/?currencies={symbol}"
        
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Polyclawd/1.0)"
        })
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        
        articles = []
        for post in data.get('results', [])[:10]:
            published = post.get('published_at', '')
            try:
                pub_dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
                age_minutes = (datetime.now(pub_dt.tzinfo) - pub_dt).total_seconds() / 60
            except:
                age_minutes = 999
            
            articles.append({
                "title": post.get('title', ''),
                "source": post.get('source', {}).get('title', 'Unknown'),
                "link": post.get('url', ''),
                "votes": post.get('votes', {}),
                "symbol": symbol,
                "age_minutes": round(age_minutes, 1),
            })
        
        return articles
    
    except Exception as e:
        return []


# ============================================================================
# Sentiment Analysis (Simple keyword-based)
# ============================================================================

BULLISH_KEYWORDS = [
    "surge", "soar", "jump", "rally", "gain", "rise", "up", "high", "record",
    "bullish", "moon", "pump", "breakout", "approval", "approved", "passes",
    "wins", "victory", "success", "positive", "beat", "exceeds", "strong",
    "breakthrough", "deal", "partnership", "adoption", "launch", "upgrade",
]

BEARISH_KEYWORDS = [
    "crash", "plunge", "drop", "fall", "decline", "down", "low", "dump",
    "bearish", "sell", "selloff", "fear", "panic", "reject", "rejected",
    "loses", "loss", "fail", "negative", "miss", "disappoints", "weak",
    "hack", "exploit", "breach", "ban", "lawsuit", "investigation", "fraud",
]

def analyze_sentiment(text: str) -> Dict[str, Any]:
    """Simple keyword-based sentiment analysis."""
    text_lower = text.lower()
    
    bullish_count = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
    bearish_count = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)
    
    total = bullish_count + bearish_count
    if total == 0:
        return {"sentiment": "neutral", "score": 0, "confidence": 20}
    
    if bullish_count > bearish_count:
        sentiment = "bullish"
        score = (bullish_count - bearish_count) / total
    elif bearish_count > bullish_count:
        sentiment = "bearish"
        score = (bearish_count - bullish_count) / total
    else:
        sentiment = "neutral"
        score = 0
    
    # Confidence based on keyword density
    confidence = min(60, 20 + total * 10)
    
    return {
        "sentiment": sentiment,
        "score": round(score, 2),
        "confidence": confidence,
        "bullish_keywords": bullish_count,
        "bearish_keywords": bearish_count,
    }


# ============================================================================
# Market Keyword Extraction
# ============================================================================

# Map market titles to search keywords
MARKET_PATTERNS = {
    # Crypto
    r"bitcoin|btc": ["bitcoin", "BTC"],
    r"ethereum|eth\b": ["ethereum", "ETH"],
    r"solana|sol\b": ["solana", "SOL"],
    r"crypto|cryptocurrency": ["crypto", "cryptocurrency"],
    
    # Politics
    r"trump": ["trump", "president trump"],
    r"biden": ["biden", "president biden"],
    r"election|vote": ["election", "vote"],
    r"congress|senate|house": ["congress", "legislation"],
    r"tariff": ["tariff", "trade war"],
    r"deport": ["deportation", "immigration"],
    
    # Sports
    r"super bowl": ["super bowl", "NFL"],
    r"\bnfl\b|patriots|seahawks|chiefs": ["NFL", "football"],
    r"nba|lakers|celtics|warriors": ["NBA", "basketball"],
    r"world series|mlb": ["MLB", "baseball"],
    
    # Tech
    r"apple|iphone": ["apple", "AAPL"],
    r"google|alphabet": ["google", "GOOGL"],
    r"tesla|elon musk": ["tesla", "TSLA", "elon musk"],
    r"openai|chatgpt|gpt": ["openai", "AI", "chatgpt"],
    r"gta|rockstar": ["GTA 6", "rockstar games"],
    r"nvidia|nvda": ["nvidia", "NVDA", "AI chips"],
    r"microsoft|msft": ["microsoft", "MSFT"],
    r"amazon|amzn": ["amazon", "AMZN"],
    r"meta|facebook": ["meta", "facebook"],
    r"spacex|starship": ["spacex", "elon musk"],
    
    # Economics
    r"inflation|cpi": ["inflation", "CPI", "federal reserve"],
    r"interest rate|fed rate|federal reserve": ["federal reserve", "interest rates"],
    r"recession": ["recession", "economy"],
    r"unemployment": ["unemployment", "jobs report"],
    r"gdp": ["GDP", "economy"],
    
    # Entertainment
    r"oscar|academy award": ["oscars", "academy awards"],
    r"grammy": ["grammys", "music awards"],
    r"netflix": ["netflix", "streaming"],
    r"disney": ["disney", "streaming"],
    
    # Geopolitics
    r"russia|ukraine|putin|zelensky": ["russia ukraine", "war"],
    r"china|taiwan|xi jinping": ["china", "taiwan"],
    r"iran|israel|gaza": ["israel", "middle east"],
}

def extract_keywords(market_title: str) -> List[str]:
    """
    Extract relevant search keywords from market title.
    
    Uses hybrid approach:
    1. Pattern matching for known topics (fast, reliable)
    2. Dynamic entity extraction for unknown topics (flexible)
    """
    title_lower = market_title.lower()
    keywords = []
    
    # Step 1: Pattern matching for known high-value topics
    for pattern, kws in MARKET_PATTERNS.items():
        if re.search(pattern, title_lower):
            keywords.extend(kws)
    
    # Step 2: If no patterns matched, extract dynamically
    if not keywords:
        keywords = extract_dynamic_keywords(market_title)
    
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for kw in keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            unique.append(kw)
    
    return unique[:3]  # Max 3 keywords per market


def extract_dynamic_keywords(text: str) -> List[str]:
    """
    Dynamically extract searchable keywords from any text.
    
    Uses simple NLP heuristics (no external dependencies):
    1. Extract capitalized phrases (likely proper nouns/names)
    2. Extract quoted phrases
    3. Extract numbers with context (dates, prices, etc.)
    4. Filter out common stop words
    """
    keywords = []
    
    # Common words to ignore
    STOP_WORDS = {
        "will", "the", "a", "an", "be", "by", "in", "on", "at", "to", "of",
        "for", "is", "are", "was", "were", "been", "being", "have", "has",
        "had", "do", "does", "did", "and", "or", "but", "if", "than", "then",
        "that", "this", "these", "those", "what", "which", "who", "whom",
        "before", "after", "during", "under", "over", "between", "into",
        "through", "about", "against", "above", "below", "any", "each",
        "more", "most", "other", "some", "such", "only", "own", "same",
        "so", "can", "just", "should", "now", "yes", "no", "how", "when",
        "where", "why", "all", "both", "few", "many", "much", "very",
    }
    
    # 1. Extract capitalized multi-word phrases (e.g., "Donald Trump", "Super Bowl")
    cap_phrases = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
    for phrase in cap_phrases:
        if len(phrase) > 3:
            keywords.append(phrase)
    
    # 2. Extract single capitalized words (proper nouns)
    cap_words = re.findall(r'\b([A-Z][a-z]{2,})\b', text)
    for word in cap_words:
        word_lower = word.lower()
        if word_lower not in STOP_WORDS and len(word) > 2:
            # Skip if it's just start of sentence (check if preceded by . or start)
            keywords.append(word)
    
    # 3. Extract quoted phrases
    quoted = re.findall(r'"([^"]+)"', text)
    keywords.extend(quoted)
    
    # 4. Extract $amount patterns (financial context)
    money = re.findall(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|M|B|k))?', text)
    keywords.extend(money[:1])  # Only first money reference
    
    # 5. Extract year patterns in context
    years = re.findall(r'\b(20\d{2})\b', text)
    for year in years[:1]:
        # Find what's near the year
        context = re.search(rf'(\w+)\s+{year}|{year}\s+(\w+)', text)
        if context:
            ctx_word = context.group(1) or context.group(2)
            if ctx_word.lower() not in STOP_WORDS:
                keywords.append(f"{ctx_word} {year}")
    
    # 6. Extract percentages in context
    pcts = re.findall(r'(\d+(?:\.\d+)?%)', text)
    keywords.extend(pcts[:1])
    
    # 7. Extract important uncapitalized nouns (domain-specific)
    IMPORTANT_NOUNS = [
        "inflation", "recession", "unemployment", "tariff", "impeachment",
        "indictment", "verdict", "settlement", "merger", "acquisition",
        "bankruptcy", "default", "ceasefire", "invasion", "sanctions",
        "strike", "shutdown", "outbreak", "pandemic", "vaccine",
    ]
    text_lower = text.lower()
    for noun in IMPORTANT_NOUNS:
        if noun in text_lower:
            keywords.append(noun)
    
    # Dedupe and limit
    seen = set()
    unique = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen and len(kw) > 2:
            seen.add(kw_lower)
            unique.append(kw)
    
    return unique[:5]


def test_keyword_extraction():
    """Test dynamic keyword extraction."""
    test_cases = [
        "Will Elon Musk buy TikTok by March 2026?",
        "Will Taylor Swift announce a new album before summer?",
        "Will the FDA approve Neuralink's brain chip?",
        "Will inflation drop below 3% by December?",
        "Will SpaceX land humans on Mars by 2030?",
        "Will the Golden State Warriors win the NBA Finals?",
        "Will OpenAI release GPT-5 before July?",
    ]
    
    print("Dynamic Keyword Extraction Test:")
    print("-" * 60)
    for title in test_cases:
        kws = extract_keywords(title)
        print(f"{title[:50]:50} → {kws}")


if __name__ == "__main__":
    # Add test for dynamic extraction
    test_keyword_extraction()
    print()
    
    # Original tests...


# ============================================================================
# News Signal Cache
# ============================================================================

def load_news_cache() -> Dict:
    """Load news cache from disk."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {"seen_articles": [], "last_check": None}


def save_news_cache(cache: Dict):
    """Save news cache to disk."""
    try:
        # Keep only last 500 articles
        cache["seen_articles"] = cache.get("seen_articles", [])[-500:]
        cache["last_check"] = datetime.now().isoformat()
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except:
        pass


def is_article_seen(cache: Dict, article_key: str) -> bool:
    """Check if we've already processed this article."""
    return article_key in cache.get("seen_articles", [])


def mark_article_seen(cache: Dict, article_key: str):
    """Mark article as processed."""
    cache.setdefault("seen_articles", []).append(article_key)


# ============================================================================
# Main Signal Generator
# ============================================================================

def check_news_for_market(market: Dict) -> Optional[Dict]:
    """
    Check for breaking news related to a specific market.
    
    Returns signal dict if actionable news found, None otherwise.
    """
    market_title = market.get("title") or market.get("question", "")
    market_id = market.get("id") or market.get("condition_id", "")[:20]
    
    if not market_title:
        return None
    
    keywords = extract_keywords(market_title)
    if not keywords:
        return None
    
    cache = load_news_cache()
    all_articles = []
    
    # Fetch from Google News
    for kw in keywords[:2]:  # Limit queries
        articles = fetch_google_news(kw, max_results=5)
        all_articles.extend(articles)
    
    # Filter to recent, unseen articles
    fresh_articles = []
    for article in all_articles:
        article_key = f"{article.get('title', '')[:50]}_{article.get('source', '')}"
        
        if article.get("age_minutes", 999) > MAX_NEWS_AGE_MINUTES:
            continue
        if is_article_seen(cache, article_key):
            continue
        
        mark_article_seen(cache, article_key)
        fresh_articles.append(article)
    
    save_news_cache(cache)
    
    if not fresh_articles:
        return None
    
    # Analyze sentiment of all fresh articles
    combined_text = " ".join(a.get("title", "") for a in fresh_articles)
    sentiment = analyze_sentiment(combined_text)
    
    if sentiment["sentiment"] == "neutral":
        return None
    
    # Generate signal
    if sentiment["sentiment"] == "bullish":
        side = "YES"
    else:
        side = "NO"
    
    # Boost confidence based on number of articles and recency
    confidence = sentiment["confidence"]
    confidence += min(20, len(fresh_articles) * 5)  # +5 per article, max +20
    
    # Recency boost
    avg_age = sum(a.get("age_minutes", 30) for a in fresh_articles) / len(fresh_articles)
    if avg_age < 10:
        confidence += 15  # Very fresh
    elif avg_age < 20:
        confidence += 10
    
    confidence = min(85, confidence)  # Cap at 85
    
    if confidence < MIN_CONFIDENCE:
        return None
    
    return {
        "source": "news_breaking",
        "market": market_title[:60],
        "market_id": market_id,
        "side": side,
        "confidence": confidence,
        "reasoning": f"Breaking news ({len(fresh_articles)} articles, avg {avg_age:.0f}min old): {sentiment['sentiment']} sentiment",
        "articles": [
            {
                "title": a.get("title", "")[:80],
                "source": a.get("source", ""),
                "age_minutes": a.get("age_minutes"),
            }
            for a in fresh_articles[:3]
        ],
        "sentiment": sentiment,
        "platform": market.get("platform", "polymarket"),
    }


def scan_all_markets_for_news(markets: List[Dict]) -> List[Dict]:
    """
    Scan all active markets for breaking news signals.
    
    Args:
        markets: List of active market dicts with title/question and id
    
    Returns:
        List of news signals
    """
    signals = []
    
    for market in markets:
        signal = check_news_for_market(market)
        if signal:
            signals.append(signal)
        
        # Small delay to avoid rate limiting
        time.sleep(0.5)
    
    return signals


def get_trending_reddit_signals(category: str = "crypto") -> List[Dict]:
    """
    Get signals from trending Reddit posts.
    
    Categories: crypto, politics, sports
    """
    signals = []
    
    if category == "crypto":
        subreddits = CRYPTO_SUBREDDITS[:2]
    elif category == "politics":
        subreddits = POLITICS_SUBREDDITS[:2]
    elif category == "sports":
        subreddits = SPORTS_SUBREDDITS[:2]
    else:
        return []
    
    cache = load_news_cache()
    
    for subreddit in subreddits:
        posts = fetch_reddit_posts(subreddit)
        
        for post in posts:
            # Only high-engagement, recent posts
            if post.get("score", 0) < 100:
                continue
            if post.get("age_minutes", 999) > 60:
                continue
            
            post_key = f"reddit_{post.get('title', '')[:50]}"
            if is_article_seen(cache, post_key):
                continue
            
            mark_article_seen(cache, post_key)
            
            sentiment = analyze_sentiment(post.get("title", ""))
            if sentiment["sentiment"] == "neutral":
                continue
            
            # Reddit engagement boosts confidence
            confidence = sentiment["confidence"]
            if post.get("score", 0) > 500:
                confidence += 15
            elif post.get("score", 0) > 200:
                confidence += 10
            
            if post.get("upvote_ratio", 0.5) > 0.9:
                confidence += 10
            
            confidence = min(75, confidence)  # Cap lower for Reddit
            
            if confidence < MIN_CONFIDENCE:
                continue
            
            signals.append({
                "source": "reddit_sentiment",
                "market": f"[{category}] {post.get('title', '')[:50]}",
                "market_id": f"reddit_{subreddit}_{int(time.time())}",
                "side": "YES" if sentiment["sentiment"] == "bullish" else "NO",
                "confidence": confidence,
                "reasoning": f"Reddit r/{subreddit}: {post.get('score')} upvotes, {post.get('upvote_ratio', 0):.0%} ratio",
                "sentiment": sentiment,
                "platform": "general",  # Not market-specific
                "subreddit": subreddit,
            })
    
    save_news_cache(cache)
    return signals


# ============================================================================
# CLI Test
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("NEWS SIGNAL SOURCE TEST")
    print("=" * 60)
    
    # Test Google News
    print("\n1. Google News (Bitcoin):")
    articles = fetch_google_news("bitcoin", max_results=5)
    for a in articles[:3]:
        print(f"   [{a['age_minutes']:.0f}m] {a['title'][:60]}")
    
    # Test Reddit
    print("\n2. Reddit (r/cryptocurrency):")
    posts = fetch_reddit_posts("cryptocurrency")
    for p in posts[:3]:
        print(f"   [{p['score']} pts] {p['title'][:50]}")
    
    # Test sentiment
    print("\n3. Sentiment Analysis:")
    test_headlines = [
        "Bitcoin surges past $100,000 as ETF inflows hit record",
        "Crypto market crashes amid regulatory fears",
        "Markets trade sideways as investors await Fed decision",
    ]
    for headline in test_headlines:
        s = analyze_sentiment(headline)
        print(f"   {s['sentiment']:8} ({s['confidence']:2}): {headline[:45]}")
    
    # Test market signal
    print("\n4. Market Signal Test:")
    test_market = {
        "title": "Will Bitcoin hit $150,000 by end of 2025?",
        "id": "btc-150k-2025",
        "platform": "polymarket",
    }
    signal = check_news_for_market(test_market)
    if signal:
        print(f"   Signal: {signal['side']} @ {signal['confidence']}%")
        print(f"   Reason: {signal['reasoning']}")
    else:
        print("   No signal (no fresh breaking news)")
    
    print("\n" + "=" * 60)
