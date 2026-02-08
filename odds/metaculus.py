"""
Metaculus Forecasting Platform Integration
Free API, no key required - quality forecasts for politics, science, economics
"""

import json
import urllib.request
from datetime import datetime, timezone
from typing import List, Dict, Optional

METACULUS_API = "https://www.metaculus.com/api/posts"

# Categories we care about for Polymarket overlap
RELEVANT_TAGS = [
    "us-politics",
    "elections", 
    "geopolitics",
    "economics",
    "crypto",
    "ai",
    "technology",
]


import time

def _get_question_prediction(question_id: int) -> Optional[float]:
    """Fetch prediction for a single question (detail endpoint has full data)"""
    try:
        time.sleep(0.3)  # Rate limit: ~3 requests/second
        url = f"{METACULUS_API}/{question_id}/"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        question_data = data.get("question", {})
        qtype = question_data.get("type")
        
        if qtype != "binary":
            return None
            
        aggregations = question_data.get("aggregations", {})
        if aggregations:
            recency = aggregations.get("recency_weighted", {})
            if recency:
                latest = recency.get("latest", {})
                if latest and isinstance(latest, dict):
                    centers = latest.get("centers", [])
                    if centers:
                        return centers[0]
        return None
    except Exception as e:
        return None


def fetch_questions(
    limit: int = 50,
    status: str = "open",
    order_by: str = "-activity",
    min_forecasters: int = 10,
    search: str = None,
    fetch_predictions: bool = True
) -> List[Dict]:
    """
    Fetch open questions from Metaculus
    
    Args:
        limit: Max questions to fetch
        status: open, closed, resolved
        order_by: -activity, -publish_time, -close_time, -nr_forecasters
        min_forecasters: Minimum forecasters for quality filter
        search: Optional search term
        fetch_predictions: If True, fetch each question's prediction (slower but complete)
    """
    try:
        url = f"{METACULUS_API}/?limit={limit}&status={status}&type=question&order_by={order_by}&forecast_type=binary"
        if search:
            url += f"&search={urllib.parse.quote(search)}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        
        questions = []
        for q in data.get("results", []):
            forecasters = q.get("nr_forecasters", 0)
            if forecasters < min_forecasters:
                continue
            
            question_data = q.get("question", {})
            qtype = question_data.get("type", "unknown")
            
            if qtype != "binary":
                continue
            
            qid = q.get("id")
            
            # Fetch prediction from detail endpoint (list doesn't include it)
            community_prediction = None
            if fetch_predictions and forecasters >= min_forecasters:
                community_prediction = _get_question_prediction(qid)
            
            questions.append({
                "id": qid,
                "title": q.get("title", ""),
                "short_title": q.get("short_title", ""),
                "url": f"https://www.metaculus.com/questions/{qid}/",
                "status": q.get("status"),
                "type": qtype,
                "forecasters": forecasters,
                "community_prediction": community_prediction,
                "created_at": q.get("created_at"),
                "close_time": question_data.get("scheduled_close_time"),
                "resolve_time": question_data.get("scheduled_resolve_time"),
            })
        
        return questions
        
    except Exception as e:
        print(f"Metaculus fetch error: {e}")
        return []


def search_questions(query: str, limit: int = 20) -> List[Dict]:
    """Search Metaculus for specific topics"""
    try:
        encoded = urllib.parse.quote(query)
        url = f"{METACULUS_API}/?limit={limit}&status=open&type=question&search={encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        
        return [
            {
                "id": q.get("id"),
                "title": q.get("title", ""),
                "url": f"https://www.metaculus.com/questions/{q.get('id')}/",
                "forecasters": q.get("nr_forecasters", 0),
            }
            for q in data.get("results", [])
        ]
    except Exception as e:
        print(f"Metaculus search error: {e}")
        return []


def get_question_detail(question_id: int) -> Optional[Dict]:
    """Get detailed info for a specific question including predictions"""
    try:
        url = f"https://www.metaculus.com/api/posts/{question_id}/"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            q = json.loads(resp.read().decode())
        
        # Extract prediction data
        community_prediction = None
        aggregations = q.get("aggregations", {})
        if aggregations:
            recency = aggregations.get("recency_weighted", {})
            if recency:
                centers = recency.get("centers", [])
                if centers:
                    community_prediction = centers[0]
        
        return {
            "id": q.get("id"),
            "title": q.get("title"),
            "description": q.get("description", "")[:500],
            "url": f"https://www.metaculus.com/questions/{q.get('id')}/",
            "status": q.get("status"),
            "forecasters": q.get("nr_forecasters", 0),
            "community_prediction": community_prediction,
            "close_time": q.get("scheduled_close_time"),
            "resolve_time": q.get("scheduled_resolve_time"),
            "resolution": q.get("resolution"),
        }
    except:
        return None


def find_polymarket_overlaps(poly_events: List[Dict], min_forecasters: int = 20) -> List[Dict]:
    """
    Find Metaculus questions that match Polymarket events
    Returns potential edge opportunities
    """
    try:
        from smart_matcher import match_markets
    except ImportError:
        from odds.smart_matcher import match_markets
    
    # Get active Metaculus questions
    metaculus_questions = fetch_questions(limit=100, min_forecasters=min_forecasters)
    
    overlaps = []
    
    for poly in poly_events:
        poly_title = poly.get("title", "")
        
        # Build candidate list
        candidates = [
            {
                "title": q["title"],
                "probability": q.get("community_prediction"),
                "forecasters": q["forecasters"],
                "url": q["url"],
                "id": q["id"],
            }
            for q in metaculus_questions
            if q.get("community_prediction") is not None
        ]
        
        if not candidates:
            continue
        
        # Find matches
        matches = match_markets(
            source_title=poly_title,
            candidates=candidates,
            title_key="title",
            min_entity_overlap=1,
            min_confidence=0.5,
            max_matches=2
        )
        
        for match in matches:
            # Get Polymarket price
            poly_price = None
            for mkt in poly.get("markets", []):
                outcome_prices = mkt.get("outcomePrices", {})
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        outcome_prices = {}
                poly_price = outcome_prices.get("Yes") if isinstance(outcome_prices, dict) else None
                if poly_price:
                    poly_price = float(poly_price)
                break
            
            if poly_price and match.get("probability"):
                metaculus_prob = match["probability"]
                edge = (metaculus_prob - poly_price) * 100
                
                overlaps.append({
                    "polymarket_title": poly_title,
                    "polymarket_price": round(poly_price * 100, 1),
                    "metaculus_title": match["title"],
                    "metaculus_prob": round(metaculus_prob * 100, 1),
                    "metaculus_forecasters": match["forecasters"],
                    "metaculus_url": match["url"],
                    "edge_pct": round(edge, 1),
                    "match_confidence": match.get("_match_confidence", 0),
                    "direction": "BUY" if edge > 0 else "SELL",
                })
    
    # Sort by absolute edge
    overlaps.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    return overlaps


async def get_metaculus_edges(min_edge: float = 5.0, min_forecasters: int = 20) -> Dict:
    """Main entry point for Metaculus edge detection"""
    import urllib.request
    
    # Fetch Polymarket events
    try:
        req = urllib.request.Request(
            "https://gamma-api.polymarket.com/events?closed=false&limit=200",
            headers={"User-Agent": "Polyclawd/1.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            poly_events = json.loads(resp.read().decode())
    except:
        poly_events = []
    
    overlaps = find_polymarket_overlaps(poly_events, min_forecasters)
    
    # Filter by minimum edge
    edges = [o for o in overlaps if abs(o["edge_pct"]) >= min_edge]
    
    return {
        "source": "Metaculus",
        "description": "Forecasting platform with crowd-sourced predictions",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_overlaps": len(overlaps),
        "edges_found": len(edges),
        "min_edge_filter": min_edge,
        "min_forecasters": min_forecasters,
        "edges": edges[:20],
    }


def get_metaculus_summary(min_forecasters: int = 10) -> Dict:
    """Get summary of active Metaculus questions"""
    questions = fetch_questions(limit=50, min_forecasters=min_forecasters)
    
    # Categorize by topic (simple keyword matching)
    categories = {
        "politics": [],
        "economics": [],
        "technology": [],
        "science": [],
        "other": [],
    }
    
    for q in questions:
        title_lower = q["title"].lower()
        if any(kw in title_lower for kw in ["trump", "biden", "election", "congress", "president", "vote"]):
            categories["politics"].append(q)
        elif any(kw in title_lower for kw in ["gdp", "inflation", "fed", "recession", "economy", "market"]):
            categories["economics"].append(q)
        elif any(kw in title_lower for kw in ["ai", "gpt", "crypto", "bitcoin", "tech", "openai", "google"]):
            categories["technology"].append(q)
        elif any(kw in title_lower for kw in ["climate", "covid", "vaccine", "science", "space"]):
            categories["science"].append(q)
        else:
            categories["other"].append(q)
    
    return {
        "source": "Metaculus",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_questions": len(questions),
        "min_forecasters_filter": min_forecasters,
        "by_category": {
            cat: {
                "count": len(qs),
                "top_questions": [
                    {
                        "title": q["title"][:80],
                        "forecasters": q["forecasters"],
                        "prediction": f"{q['community_prediction']*100:.0f}%" if q.get("community_prediction") else "N/A",
                        "url": q["url"],
                    }
                    for q in sorted(qs, key=lambda x: x["forecasters"], reverse=True)[:5]
                ]
            }
            for cat, qs in categories.items()
            if qs
        },
    }


if __name__ == "__main__":
    import asyncio
    
    print("Testing Metaculus integration...")
    print()
    
    # Test summary
    summary = get_metaculus_summary()
    print(f"Total questions: {summary['total_questions']}")
    print("\nBy category:")
    for cat, data in summary["by_category"].items():
        print(f"  {cat}: {data['count']} questions")
        for q in data["top_questions"][:2]:
            print(f"    - {q['prediction']} ({q['forecasters']} forecasters): {q['title'][:50]}...")
    
    print("\n" + "="*50)
    print("\nTesting edge detection...")
    result = asyncio.run(get_metaculus_edges(min_edge=3.0))
    print(f"Overlaps found: {result['total_overlaps']}")
    print(f"Edges (>3%): {result['edges_found']}")
    for e in result["edges"][:3]:
        print(f"  {e['metaculus_title'][:40]}...")
        print(f"    Metaculus: {e['metaculus_prob']}% vs Poly: {e['polymarket_price']}% = {e['edge_pct']:+.1f}%")
