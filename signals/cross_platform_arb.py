#!/usr/bin/env python3
"""
Cross-Platform Arbitrage Scanner — Kalshi vs Polymarket.

Uses TF-IDF + cosine similarity for semantic matching of markets across platforms,
then compares prices to find arbitrage opportunities.

Becker dataset analysis showed cross-platform spreads exist but require semantic
matching (keyword overlap fails due to different market phrasing styles).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Resilient fetch wrapper
try:
    from api.services.resilient_fetch import resilient_call
    from api.services.source_health import get_last_success_timestamp
    HAS_RESILIENT = True
except ImportError:
    HAS_RESILIENT = False

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Thresholds
MIN_SPREAD_PCT = 2       # Minimum spread in pp — even tight arbs are useful signals
MIN_SIMILARITY = 0.60    # Higher sim threshold — quality over quantity
MIN_VOLUME = 10000       # Minimum volume on both sides
MAX_RESULTS = 50         # Maximum arb opportunities to return

# Cache
_cache: Dict = {"data": None, "timestamp": 0}
CACHE_TTL = 300  # 5 min

# Stopwords for TF-IDF
STOPWORDS = {
    'will', 'the', 'a', 'an', 'in', 'on', 'at', 'to', 'of', 'by', 'or', 'and',
    'be', 'is', 'for', 'any', 'other', 'another', 'before', 'after', 'than',
    'this', 'that', 'does', 'do', 'has', 'have', 'their', 'more', 'less',
    'win', 'not', 'above', 'below', 'between', 'what', 'how', 'when', 'where',
    'which', 'who', 'whom', 'yes', 'no',
}


def _tokenize(text: str) -> List[str]:
    """Extract meaningful tokens from market title."""
    text = text.lower()
    # Extract numbers with units (e.g., "$64,000", "1,800")
    text = re.sub(r'[$,]', '', text)
    tokens = re.findall(r'[a-z]+|[0-9]+', text)
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def _cosine_similarity(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Simple TF-based cosine similarity between two token lists."""
    if not tokens_a or not tokens_b:
        return 0.0
    
    # Build term frequency vectors
    vocab = set(tokens_a) | set(tokens_b)
    vec_a = {t: tokens_a.count(t) for t in vocab}
    vec_b = {t: tokens_b.count(t) for t in vocab}
    
    dot = sum(vec_a.get(t, 0) * vec_b.get(t, 0) for t in vocab)
    mag_a = sum(v ** 2 for v in vec_a.values()) ** 0.5
    mag_b = sum(v ** 2 for v in vec_b.values()) ** 0.5
    
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _extract_subject(title: str) -> str:
    """Extract the core subject/entity from a market title for matching validation.
    
    Returns a normalized subject string. Two markets should only match if they
    share the same core subject (e.g., same person, same event, same metric).
    """
    t = title.lower()
    
    # Extract proper nouns (capitalized words from original)
    names = set(re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', title))
    
    # Extract numbers (prices, dates, thresholds)
    numbers = set(re.findall(r'\d+(?:,\d+)*', t))
    
    # Key entity patterns
    entities = set()
    for pattern in [
        r'(trump|biden|harris|kamala|newsom|desantis|vance|rubio|aoc|ocasio)',
        r'(bitcoin|ethereum|btc|eth|solana)',
        r'(fed\w*\s+rate|interest rate|cpi|gdp|inflation|recession)',
        r'(pope|mars|greenland|ukraine|russia|china|taiwan|iran)',
        r'(oscars?|grammy|emmy|super\s*bowl|world\s*cup|nba\s+finals)',
        r'(tiktok|openai|spacex|tesla|amazon|apple|google)',
    ]:
        match = re.search(pattern, t)
        if match:
            entities.add(match.group(1).strip())
    
    # Combine: names + specific numbers + entities
    parts = sorted(entities) + sorted(n for n in names if len(n) > 3) + sorted(numbers)
    return " ".join(parts).lower() if parts else ""


def _question_type(title: str) -> str:
    """Classify the question type to prevent matching different question kinds."""
    t = title.lower()
    
    # "Will X occur/happen" — existence questions
    if re.search(r'(occur|happen|take place|be held)', t):
        return "existence"
    
    # "Will X win Y" — winner questions  
    if re.search(r'(win |winner|champion|nominee|nominated)', t):
        # Extract the specific person/entity being asked about
        # "Will Kamala Harris win..." -> "harris_win"
        name_match = re.search(r'will\s+(\w+\s+\w+)\s+(win|be)', t)
        if name_match:
            return f"win_{name_match.group(1).replace(' ', '_')}"
        return "winner"
    
    # "Will X be above/below Y" — threshold questions
    if re.search(r'(above|below|over|under|between|price|reach)', t):
        return "threshold"
    
    # "Will X run for / announce" — action questions
    if re.search(r'(run for|announce|launch|sign|pass|approve)', t):
        return "action"
    
    # "Who will" — multi-outcome
    if t.startswith("who will"):
        return "who_will"
    
    return "general"


def _subjects_compatible(title_a: str, title_b: str, subj_a: str, subj_b: str) -> bool:
    """Check if two markets are about the same thing AND asking the same question.
    
    Prevents matching "Will 2028 election occur?" with "Will AOC win 2028?"
    """
    # Different question types = not compatible
    type_a = _question_type(title_a)
    type_b = _question_type(title_b)
    
    # "who_will" can match with specific "win_X" questions
    if type_a != type_b:
        if not ({type_a, type_b} & {"who_will"} and {type_a, type_b} & {t for t in [type_a, type_b] if t.startswith("win_")}):
            return False
    
    # For winner questions, require the same person/entity
    if type_a.startswith("win_") and type_b.startswith("win_"):
        if type_a != type_b:
            return False
    
    # Extract proper nouns from both titles (names, places, orgs)
    names_a = set(re.findall(r'[A-Z][a-z]{2,}', title_a))
    names_b = set(re.findall(r'[A-Z][a-z]{2,}', title_b))
    
    # Remove generic capitalized words
    generic_names = {'Will', 'The', 'Democratic', 'Republican', 'United', 'States',
                     'Presidential', 'President', 'Prime', 'Minister', 'Party',
                     'Best', 'Next', 'National', 'Supreme', 'February', 'March',
                     'January', 'April', 'May', 'June', 'July', 'August',
                     'September', 'October', 'November', 'December'}
    names_a -= generic_names
    names_b -= generic_names
    
    # Must share at least one proper noun (a person's name, country, org)
    shared_names = names_a & names_b
    if not shared_names:
        return False
    
    return True


def _fetch_json(url: str, params: dict = None, timeout: int = 15, source_name: str = None) -> Optional[dict]:
    """Fetch JSON with error handling and optional resilient wrapper."""
    def _do_fetch():
        r = httpx.get(url, params=params, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    
    if HAS_RESILIENT and source_name:
        return resilient_call(source_name, _do_fetch, retries=2, backoff_base=2.0)
    
    try:
        return _do_fetch()
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return None


def fetch_kalshi_active(limit: int = 500) -> List[Dict]:
    """Fetch active Kalshi markets via events endpoint (has nested markets with volume)."""
    markets = []
    seen_tickers = set()
    cursor = None
    
    for _ in range(5):  # Max 5 pages of 100 events each
        params = {"status": "open", "limit": 100, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        
        data = _fetch_json(f"{KALSHI_API}/events", params=params, source_name="kalshi")
        if not data:
            break
        
        for event in data.get("events", []):
            event_title = event.get("title", "")
            for m in event.get("markets", []):
                ticker = m.get("ticker", "")
                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                
                vol = m.get("volume", 0) or 0
                last_price = m.get("last_price", 0) or 0
                if vol >= MIN_VOLUME and 5 <= last_price <= 95:
                    # Use event title if market title is multi-outcome gibberish
                    title = m.get("title", "") or event_title
                    if title.startswith("yes ") or title.startswith("no "):
                        title = event_title  # Multi-outcome format, use event title
                    
                    markets.append({
                        "id": ticker,
                        "title": title,
                        "price_yes": last_price / 100,
                        "volume": vol,
                        "close_time": m.get("close_time", ""),
                        "platform": "kalshi",
                    })
        
        cursor = data.get("cursor")
        if not cursor or len(markets) >= limit:
            break
    
    logger.info(f"Kalshi: fetched {len(markets)} active markets with volume")
    return markets


def fetch_polymarket_active(limit: int = 200) -> List[Dict]:
    """Fetch active Polymarket markets with decent volume."""
    markets = []
    
    # Flat markets
    data = _fetch_json(f"{GAMMA_API}/markets", params={
        "active": "true", "closed": "false",
        "limit": limit, "order": "volume24hr", "ascending": "false"
    }, source_name="polymarket_gamma")
    if data:
        for m in data:
            vol = float(m.get("volume", 0) or 0)
            if vol < MIN_VOLUME:
                continue
            
            op = m.get("outcomePrices", "")
            try:
                if isinstance(op, str):
                    op = json.loads(op)
                yes_price = float(op[0])
            except Exception:
                yes_price = 0.5
            
            if 0.05 <= yes_price <= 0.95:
                markets.append({
                    "id": m.get("conditionId", m.get("id", "")),
                    "title": m.get("question", ""),
                    "price_yes": yes_price,
                    "volume": vol,
                    "close_time": m.get("endDate", ""),
                    "slug": m.get("slug", ""),
                    "platform": "polymarket",
                })
    
    # Events (nested markets)
    events = _fetch_json(f"{GAMMA_API}/events", params={
        "active": "true", "closed": "false",
        "limit": 50, "order": "volume24hr", "ascending": "false"
    }, source_name="polymarket_gamma")
    if events:
        seen = {m["id"] for m in markets}
        for event in events:
            for m in event.get("markets", []):
                mid = m.get("conditionId", m.get("id", ""))
                if mid in seen or not m.get("active") or m.get("closed"):
                    continue
                
                vol = float(m.get("volume", 0) or 0)
                if vol < MIN_VOLUME:
                    continue
                
                op = m.get("outcomePrices", "")
                try:
                    if isinstance(op, str):
                        op = json.loads(op)
                    yes_price = float(op[0])
                except Exception:
                    continue
                
                if 0.05 <= yes_price <= 0.95:
                    markets.append({
                        "id": mid,
                        "title": m.get("question", ""),
                        "price_yes": yes_price,
                        "volume": vol,
                        "close_time": m.get("endDate", ""),
                        "slug": m.get("slug", ""),
                        "platform": "polymarket",
                    })
                    seen.add(mid)
    
    return markets


def find_arb_opportunities(
    kalshi_markets: List[Dict],
    poly_markets: List[Dict],
    min_spread: float = MIN_SPREAD_PCT,
    min_sim: float = MIN_SIMILARITY,
) -> List[Dict]:
    """Find cross-platform arbitrage opportunities using semantic matching.
    
    Uses inverted index for efficient O(n*k) matching instead of O(n*m) brute force.
    """
    # Build inverted index on Polymarket tokens
    poly_index = {}  # token -> list of market_idx
    poly_tokens = []
    poly_subjects = []
    for i, pm in enumerate(poly_markets):
        toks = _tokenize(pm["title"])
        poly_tokens.append(toks)
        poly_subjects.append(_extract_subject(pm["title"]))
        for tok in set(toks):  # unique tokens only
            poly_index.setdefault(tok, []).append(i)
    
    opportunities = []
    
    for km in kalshi_markets:
        k_tokens = _tokenize(km["title"])
        if len(k_tokens) < 2:
            continue
        
        k_subject = _extract_subject(km["title"])
        
        # Find candidate Polymarket markets via inverted index
        # (markets sharing at least 2 tokens)
        candidate_counts = {}
        for tok in set(k_tokens):
            for pi in poly_index.get(tok, []):
                candidate_counts[pi] = candidate_counts.get(pi, 0) + 1
        
        for pi, shared_count in candidate_counts.items():
            if shared_count < 2:
                continue
            
            sim = _cosine_similarity(k_tokens, poly_tokens[pi])
            if sim < min_sim:
                continue
            
            pm = poly_markets[pi]
            
            # Subject compatibility check — prevent "election occurs?" vs "AOC wins?"
            if not _subjects_compatible(km["title"], pm["title"], k_subject, poly_subjects[pi]):
                continue
            
            # Calculate spread (both directions)
            # If Kalshi YES=70% and Poly YES=60%, spread=10pp
            spread = abs(km["price_yes"] - pm["price_yes"]) * 100
            
            if spread < min_spread:
                continue
            
            # Determine arb direction
            if km["price_yes"] > pm["price_yes"]:
                arb_direction = "Buy YES on Polymarket, sell YES on Kalshi"
                buy_platform = "polymarket"
                buy_price = pm["price_yes"]
                sell_price = km["price_yes"]
            else:
                arb_direction = "Buy YES on Kalshi, sell YES on Polymarket"
                buy_platform = "kalshi"
                buy_price = km["price_yes"]
                sell_price = pm["price_yes"]
            
            opportunities.append({
                "kalshi_title": km["title"][:100],
                "kalshi_id": km["id"],
                "kalshi_price": round(km["price_yes"] * 100, 1),
                "kalshi_volume": km["volume"],
                "poly_title": pm["title"][:100],
                "poly_id": pm["id"],
                "poly_price": round(pm["price_yes"] * 100, 1),
                "poly_volume": pm["volume"],
                "poly_slug": pm.get("slug", ""),
                "spread_pp": round(spread, 1),
                "similarity": round(sim, 3),
                "shared_tokens": shared_count,
                "direction": arb_direction,
                "buy_platform": buy_platform,
                "buy_price": round(buy_price * 100, 1),
                "sell_price": round(sell_price * 100, 1),
                "min_volume": min(km["volume"], pm["volume"]),
            })
    
    # Sort by spread descending, filter top results
    opportunities.sort(key=lambda x: x["spread_pp"], reverse=True)
    
    # Deduplicate: keep best spread per unique pair (both sides)
    seen_pairs = set()
    unique = []
    for opp in opportunities:
        pair_key = (opp["kalshi_id"], opp["poly_id"])
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            unique.append(opp)
    
    return unique[:MAX_RESULTS]


def scan_cross_platform_arb() -> Dict:
    """Main entry point: fetch markets from both platforms and find arb opportunities."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]
    
    logger.info("Scanning cross-platform arb opportunities...")
    
    kalshi = fetch_kalshi_active(limit=200)
    poly = fetch_polymarket_active(limit=200)
    
    logger.info(f"Fetched {len(kalshi)} Kalshi + {len(poly)} Polymarket active markets")
    
    arbs = find_arb_opportunities(kalshi, poly)
    
    # Staleness tags
    sources_used = []
    source_freshness = {}
    if HAS_RESILIENT:
        import time as _time
        now_ts = _time.time()
        for src in ["kalshi", "polymarket_gamma"]:
            ts = get_last_success_timestamp(src)
            if ts:
                age = now_ts - ts
                source_freshness[src] = round(age, 1)
                sources_used.append(src)
            else:
                source_freshness[src] = None
    
    # Tag each arb with source freshness
    for arb in arbs:
        arb["sources_used"] = sources_used
        kalshi_age = source_freshness.get("kalshi")
        poly_age = source_freshness.get("polymarket_gamma")
        arb["data_age_seconds"] = max(
            kalshi_age or 0, poly_age or 0
        )
    
    result = {
        "kalshi_markets": len(kalshi),
        "poly_markets": len(poly),
        "arb_opportunities": len(arbs),
        "arbs": arbs,
        "avg_spread": round(sum(a["spread_pp"] for a in arbs) / max(len(arbs), 1), 1),
        "max_spread": arbs[0]["spread_pp"] if arbs else 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources_used": sources_used,
        "source_freshness": source_freshness,
    }
    
    _cache["data"] = result
    _cache["timestamp"] = now
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Cross-Platform Arb Scanner ===\n")
    result = scan_cross_platform_arb()
    
    print(f"Kalshi: {result['kalshi_markets']} markets")
    print(f"Polymarket: {result['poly_markets']} markets")
    print(f"Arb opportunities: {result['arb_opportunities']}")
    print(f"Avg spread: {result['avg_spread']}pp")
    print(f"Max spread: {result['max_spread']}pp")
    
    for arb in result["arbs"][:10]:
        print(f"\n  K: {arb['kalshi_title'][:70]}")
        print(f"  P: {arb['poly_title'][:70]}")
        print(f"  Kalshi: {arb['kalshi_price']}¢ | Poly: {arb['poly_price']}¢ | Spread: {arb['spread_pp']}pp | Sim: {arb['similarity']}")
