#!/usr/bin/env python3
"""
Self-Learning Keyword System for Polyclawd

Learns which keywords lead to successful news-based trades.
Automatically discovers new keywords from market titles.
Weights keywords by their historical success rate.

No external API needed - learns from your own trading data.
"""

import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple, Optional
from collections import Counter

# ============================================================================
# Storage
# ============================================================================

LEARNER_DIR = Path.home() / ".openclaw" / "polyclawd" / "keyword_learner"
LEARNER_DIR.mkdir(parents=True, exist_ok=True)

LEARNED_KEYWORDS_FILE = LEARNER_DIR / "learned_keywords.json"
KEYWORD_STATS_FILE = LEARNER_DIR / "keyword_stats.json"
MARKET_ENTITIES_FILE = LEARNER_DIR / "market_entities.json"


def load_json(path: Path, default: dict = None) -> dict:
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except:
        pass
    return default or {}


def save_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ============================================================================
# Entity Extraction (Local NLP - No API needed)
# ============================================================================

# Common words to always ignore
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
    "first", "last", "next", "new", "old", "high", "low", "good", "bad",
    "end", "start", "begin", "happen", "become", "make", "get", "go",
    "take", "come", "see", "know", "think", "want", "say", "tell",
    "ask", "use", "find", "give", "try", "call", "keep", "let", "put",
    "set", "seem", "help", "show", "hear", "play", "run", "move", "live",
    "believe", "hold", "bring", "happen", "write", "provide", "sit",
    "stand", "lose", "pay", "meet", "include", "continue", "learn",
}


def extract_entities(text: str) -> List[Dict]:
    """
    Extract named entities from text using pattern matching.
    Returns list of {entity, type, confidence}
    """
    entities = []
    
    # 1. Multi-word capitalized phrases (e.g., "Elon Musk", "Taylor Swift")
    for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text):
        phrase = match.group(1)
        if len(phrase) > 3:
            entities.append({
                "entity": phrase,
                "type": "PERSON_OR_ORG",
                "confidence": 0.9,
            })
    
    # 2. Single capitalized words (proper nouns)
    for match in re.finditer(r'\b([A-Z][a-z]{2,})\b', text):
        word = match.group(1)
        if word.lower() not in STOP_WORDS:
            entities.append({
                "entity": word,
                "type": "PROPER_NOUN",
                "confidence": 0.7,
            })
    
    # 3. All-caps words (acronyms like FBI, SEC, FDA)
    for match in re.finditer(r'\b([A-Z]{2,5})\b', text):
        word = match.group(1)
        if word not in {"YES", "NO", "AND", "THE", "FOR"}:
            entities.append({
                "entity": word,
                "type": "ACRONYM",
                "confidence": 0.8,
            })
    
    # 4. Money amounts
    for match in re.finditer(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|M|B|k|K))?', text):
        entities.append({
            "entity": match.group(0),
            "type": "MONEY",
            "confidence": 0.95,
        })
    
    # 5. Percentages
    for match in re.finditer(r'\d+(?:\.\d+)?%', text):
        entities.append({
            "entity": match.group(0),
            "type": "PERCENTAGE",
            "confidence": 0.95,
        })
    
    # 6. Dates and years
    for match in re.finditer(r'\b(20\d{2})\b', text):
        entities.append({
            "entity": match.group(1),
            "type": "YEAR",
            "confidence": 0.9,
        })
    
    for match in re.finditer(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s*\d{0,2},?\s*\d{0,4}\b', text, re.I):
        entities.append({
            "entity": match.group(0),
            "type": "DATE",
            "confidence": 0.9,
        })
    
    return entities


def extract_searchable_terms(text: str) -> List[str]:
    """Extract terms suitable for news search."""
    entities = extract_entities(text)
    
    # Prioritize by type
    priority = {
        "PERSON_OR_ORG": 1,
        "ACRONYM": 2,
        "PROPER_NOUN": 3,
        "MONEY": 4,
        "PERCENTAGE": 5,
        "YEAR": 6,
        "DATE": 6,
    }
    
    # Sort by priority and confidence
    entities.sort(key=lambda e: (priority.get(e["type"], 10), -e["confidence"]))
    
    # Return unique terms
    seen = set()
    terms = []
    for e in entities:
        term = e["entity"]
        if term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    
    return terms[:5]


# ============================================================================
# Keyword Learning
# ============================================================================

def record_keyword_usage(keywords: List[str], market_id: str, outcome: Optional[str] = None):
    """
    Record that keywords were used for a market.
    outcome: "win", "loss", or None (pending)
    """
    stats = load_json(KEYWORD_STATS_FILE, {"keywords": {}})
    
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in stats["keywords"]:
            stats["keywords"][kw_lower] = {
                "total_uses": 0,
                "wins": 0,
                "losses": 0,
                "pending": 0,
                "markets": [],
                "first_seen": datetime.now().isoformat(),
                "last_used": None,
            }
        
        entry = stats["keywords"][kw_lower]
        entry["total_uses"] += 1
        entry["last_used"] = datetime.now().isoformat()
        
        if outcome == "win":
            entry["wins"] += 1
        elif outcome == "loss":
            entry["losses"] += 1
        else:
            entry["pending"] += 1
        
        # Track which markets used this keyword
        if market_id not in entry["markets"]:
            entry["markets"].append(market_id)
            entry["markets"] = entry["markets"][-50:]  # Keep last 50
    
    save_json(KEYWORD_STATS_FILE, stats)


def update_keyword_outcome(market_id: str, outcome: str):
    """Update keyword stats when a trade resolves."""
    stats = load_json(KEYWORD_STATS_FILE, {"keywords": {}})
    
    for kw, entry in stats["keywords"].items():
        if market_id in entry.get("markets", []):
            if entry["pending"] > 0:
                entry["pending"] -= 1
            if outcome == "win":
                entry["wins"] += 1
            elif outcome == "loss":
                entry["losses"] += 1
    
    save_json(KEYWORD_STATS_FILE, stats)


def get_keyword_weights() -> Dict[str, float]:
    """
    Get learned weights for keywords based on historical performance.
    Returns {keyword: weight} where weight > 1 means above average.
    """
    stats = load_json(KEYWORD_STATS_FILE, {"keywords": {}})
    weights = {}
    
    for kw, entry in stats["keywords"].items():
        total = entry.get("wins", 0) + entry.get("losses", 0)
        if total >= 3:  # Need at least 3 resolved trades
            win_rate = entry["wins"] / total
            # Weight = win_rate / 0.5 (so 60% = 1.2x, 40% = 0.8x)
            weights[kw] = win_rate / 0.5
    
    return weights


def get_top_keywords(n: int = 20) -> List[Dict]:
    """Get top performing keywords."""
    stats = load_json(KEYWORD_STATS_FILE, {"keywords": {}})
    
    keywords = []
    for kw, entry in stats["keywords"].items():
        total = entry.get("wins", 0) + entry.get("losses", 0)
        if total >= 2:
            win_rate = entry["wins"] / total if total > 0 else 0
            keywords.append({
                "keyword": kw,
                "win_rate": round(win_rate, 2),
                "total_trades": total,
                "wins": entry["wins"],
                "losses": entry["losses"],
                "pending": entry.get("pending", 0),
            })
    
    keywords.sort(key=lambda x: (-x["win_rate"], -x["total_trades"]))
    return keywords[:n]


# ============================================================================
# Market Entity Discovery
# ============================================================================

def learn_from_markets(markets: List[Dict]):
    """
    Scan markets and learn new entities/keywords.
    Call periodically with active markets to discover new patterns.
    """
    stored = load_json(MARKET_ENTITIES_FILE, {"entities": {}, "last_scan": None})
    
    for market in markets:
        title = market.get("title") or market.get("question", "")
        market_id = market.get("id") or market.get("condition_id", "")[:20]
        
        if not title:
            continue
        
        entities = extract_entities(title)
        
        for e in entities:
            entity_key = e["entity"].lower()
            if entity_key not in stored["entities"]:
                stored["entities"][entity_key] = {
                    "original": e["entity"],
                    "type": e["type"],
                    "count": 0,
                    "markets": [],
                    "first_seen": datetime.now().isoformat(),
                }
            
            entry = stored["entities"][entity_key]
            entry["count"] += 1
            if market_id not in entry["markets"]:
                entry["markets"].append(market_id)
                entry["markets"] = entry["markets"][-20:]
    
    stored["last_scan"] = datetime.now().isoformat()
    save_json(MARKET_ENTITIES_FILE, stored)


def get_trending_entities(min_count: int = 3) -> List[Dict]:
    """Get entities that appear frequently across markets."""
    stored = load_json(MARKET_ENTITIES_FILE, {"entities": {}})
    
    trending = []
    for key, entry in stored["entities"].items():
        if entry["count"] >= min_count:
            trending.append({
                "entity": entry["original"],
                "type": entry["type"],
                "count": entry["count"],
                "market_count": len(entry.get("markets", [])),
            })
    
    trending.sort(key=lambda x: -x["count"])
    return trending[:30]


# ============================================================================
# Smart Keyword Selection
# ============================================================================

def get_smart_keywords(market_title: str, use_weights: bool = True) -> List[Tuple[str, float]]:
    """
    Get keywords for a market with learned weights.
    Returns [(keyword, weight), ...]
    """
    # Extract terms from title
    terms = extract_searchable_terms(market_title)
    
    if not use_weights:
        return [(t, 1.0) for t in terms]
    
    # Apply learned weights
    weights = get_keyword_weights()
    weighted = []
    
    for term in terms:
        term_lower = term.lower()
        weight = weights.get(term_lower, 1.0)  # Default weight 1.0
        weighted.append((term, weight))
    
    # Sort by weight (highest first)
    weighted.sort(key=lambda x: -x[1])
    
    return weighted


def boost_confidence_by_keywords(base_confidence: float, keywords: List[str]) -> float:
    """
    Boost signal confidence based on keyword performance.
    """
    weights = get_keyword_weights()
    
    if not weights or not keywords:
        return base_confidence
    
    # Average weight of keywords used
    kw_weights = [weights.get(kw.lower(), 1.0) for kw in keywords]
    avg_weight = sum(kw_weights) / len(kw_weights)
    
    # Boost/reduce confidence based on keyword performance
    # avg_weight of 1.2 (60% win rate) → 10% boost
    # avg_weight of 0.8 (40% win rate) → 10% reduction
    adjustment = (avg_weight - 1.0) * 50  # Convert to confidence points
    
    return max(0, min(100, base_confidence + adjustment))


# ============================================================================
# CLI Test
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("KEYWORD LEARNER TEST")
    print("=" * 60)
    
    # Test entity extraction
    test_titles = [
        "Will Elon Musk buy TikTok by March 2026?",
        "Will the SEC approve a Solana ETF?",
        "Will Taylor Swift announce a new album?",
        "Will inflation drop below 3% by December?",
        "Will the FDA approve Neuralink's brain chip?",
    ]
    
    print("\n1. Entity Extraction:")
    print("-" * 60)
    for title in test_titles:
        entities = extract_entities(title)
        terms = extract_searchable_terms(title)
        print(f"Title: {title[:50]}")
        print(f"  Entities: {[e['entity'] for e in entities[:4]]}")
        print(f"  Search terms: {terms}")
        print()
    
    # Test keyword weights
    print("\n2. Simulating Keyword Learning:")
    print("-" * 60)
    
    # Simulate some trades
    record_keyword_usage(["bitcoin", "ETF"], "market1", "win")
    record_keyword_usage(["bitcoin", "SEC"], "market2", "win")
    record_keyword_usage(["bitcoin", "crash"], "market3", "loss")
    record_keyword_usage(["trump", "tariff"], "market4", "win")
    record_keyword_usage(["trump", "impeachment"], "market5", "loss")
    
    print("Top keywords after simulated trades:")
    for kw in get_top_keywords(10):
        print(f"  {kw['keyword']:20} {kw['win_rate']:5.0%} ({kw['wins']}W/{kw['losses']}L)")
    
    # Test smart keywords
    print("\n3. Smart Keyword Selection:")
    print("-" * 60)
    test_market = "Will Bitcoin hit $100,000 by end of 2026?"
    smart_kws = get_smart_keywords(test_market)
    print(f"Market: {test_market}")
    print(f"Smart keywords: {smart_kws}")
    
    # Test confidence boost
    print("\n4. Confidence Boost:")
    print("-" * 60)
    base = 50
    boosted = boost_confidence_by_keywords(base, ["bitcoin", "ETF"])
    print(f"Base confidence: {base}")
    print(f"After keyword boost: {boosted:.1f}")
