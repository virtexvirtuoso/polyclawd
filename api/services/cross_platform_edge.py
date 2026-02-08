"""
Cross-Platform Edge Scanner
Compares Polymarket, Kalshi, and Metaculus to find probability discrepancies.
"""

import asyncio
import json
import urllib.request
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

# Topic matching keywords
TOPIC_KEYWORDS = {
    "trump_nobel": ["trump", "nobel", "peace prize"],
    "trump_resign": ["trump", "resign", "leave office"],
    "trump_indictment": ["trump", "indicted", "indictment", "charged", "criminal"],
    "fed_chair": ["fed", "chair", "federal reserve", "warsh", "powell"],
    "fed_rates": ["fed", "rate", "interest rate", "fomc", "cut", "hike"],
    "scotus": ["supreme court", "scotus", "justice", "resign"],
    "bitcoin_pow": ["bitcoin", "proof of work", "pow", "mining"],
    "bitcoin_price": ["bitcoin", "btc", "price", "$"],
    "trump_tariffs": ["trump", "tariff", "trade war", "china"],
    "election_2028": ["2028", "election", "president", "nominee"],
}


@dataclass
class PlatformPrice:
    platform: str
    market_id: str
    title: str
    probability: float
    volume: Optional[float] = None
    forecasters: Optional[int] = None
    url: Optional[str] = None


@dataclass
class EdgeOpportunity:
    topic: str
    markets: list
    spread: float
    edge_type: str
    recommendation: str


class CrossPlatformEdgeScanner:
    def __init__(self):
        self.cache = {}
        self.cache_ttl = timedelta(hours=6)
    
    def _fetch_url(self, url: str, timeout: int = 30) -> Optional[dict]:
        """Sync URL fetch with error handling."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"Fetch error for {url}: {e}")
            return None
    
    def fetch_polymarket(self) -> list:
        """Fetch active Polymarket events from Gamma API."""
        prices = []
        try:
            url = "https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100"
            data = self._fetch_url(url)
            if not data:
                return prices
            
            for event in data:
                markets = event.get("markets", [])
                for market in markets:
                    title = market.get("question", event.get("title", ""))
                    try:
                        prices_str = market.get("outcomePrices", "[]")
                        price_list = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                        prob = float(price_list[0]) if price_list else None
                    except:
                        prob = None
                    
                    if prob is not None and title:
                        prices.append(PlatformPrice(
                            platform="polymarket",
                            market_id=market.get("conditionId", ""),
                            title=title,
                            probability=prob,
                            volume=market.get("volumeNum", 0),
                            url=f"https://polymarket.com/event/{event.get('slug', '')}"
                        ))
        except Exception as e:
            print(f"Polymarket fetch error: {e}")
        return prices
    
    def fetch_kalshi(self) -> list:
        """Fetch active Kalshi markets."""
        prices = []
        try:
            url = "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open"
            data = self._fetch_url(url, timeout=45)
            if not data:
                print("Kalshi: No data returned")
                return prices
            
            markets = data.get("markets", [])
            for market in markets:
                title = market.get("title", "")
                # Kalshi prices are in cents (0-100)
                yes_bid = market.get("yes_bid", 0) or 0
                yes_ask = market.get("yes_ask", 0) or 0
                
                # Skip markets with no real prices
                if yes_bid == 0 and yes_ask == 0:
                    continue
                    
                # Convert from cents to probability
                prob = (yes_bid + yes_ask) / 200 if (yes_bid or yes_ask) else None
                
                if prob is not None and prob > 0 and title:
                    prices.append(PlatformPrice(
                        platform="kalshi",
                        market_id=market.get("ticker", ""),
                        title=title,
                        probability=prob,
                        volume=market.get("volume", 0),
                        url=f"https://kalshi.com/markets/{market.get('ticker', '')}"
                    ))
        except Exception as e:
            print(f"Kalshi fetch error: {e}")
            import traceback
            traceback.print_exc()
        return prices
    
    def fetch_metaculus(self) -> list:
        """Fetch Metaculus forecasts (limited due to rate limiting)."""
        prices = []
        try:
            # Fetch top binary questions with good forecast coverage
            url = "https://www.metaculus.com/api/posts/?forecast_type=binary&order_by=-activity&limit=50"
            data = self._fetch_url(url, timeout=45)
            if not data:
                print("Metaculus: No data returned")
                return prices
            
            results = data.get("results", [])
            for q in results:
                question = q.get("question", {})
                if not question or question.get("type") != "binary":
                    continue
                
                title = q.get("title", "")
                forecasters = q.get("nr_forecasters", 0) or 0
                
                # Get prediction from aggregations
                aggregations = question.get("aggregations", {})
                recency = aggregations.get("recency_weighted", {})
                latest = recency.get("latest", {})
                centers = latest.get("centers", []) if isinstance(latest, dict) else []
                prob = centers[0] if centers else None
                
                if prob is not None and title and forecasters >= 30:
                    prices.append(PlatformPrice(
                        platform="metaculus",
                        market_id=str(q.get("id", "")),
                        title=title,
                        probability=prob,
                        forecasters=forecasters,
                        url=f"https://metaculus.com/questions/{q.get('id')}/"
                    ))
        except Exception as e:
            print(f"Metaculus fetch error: {e}")
            import traceback
            traceback.print_exc()
        return prices
    
    def match_topic(self, title: str) -> Optional[str]:
        """Match a market title to a topic category."""
        title_lower = title.lower()
        for topic, keywords in TOPIC_KEYWORDS.items():
            matches = sum(1 for kw in keywords if kw in title_lower)
            if matches >= 2:
                return topic
        return None
    
    def calculate_edge(self, markets: list) -> Optional[EdgeOpportunity]:
        """Calculate edge opportunity from matched markets."""
        if len(markets) < 2:
            return None
        
        probs = [m.probability for m in markets]
        min_prob = min(probs)
        max_prob = max(probs)
        spread = max_prob - min_prob
        
        if spread < 0.05:
            return None
        
        if spread >= 0.15:
            edge_type = "arbitrage"
            buy_market = min(markets, key=lambda m: m.probability)
            sell_market = max(markets, key=lambda m: m.probability)
            recommendation = (
                f"BUY YES on {buy_market.platform} @ {buy_market.probability:.1%}, "
                f"SELL YES on {sell_market.platform} @ {sell_market.probability:.1%} "
                f"(+{spread:.1%} spread)"
            )
        else:
            edge_type = "disagreement"
            avg_prob = sum(probs) / len(probs)
            outlier = max(markets, key=lambda m: abs(m.probability - avg_prob))
            direction = "higher" if outlier.probability > avg_prob else "lower"
            recommendation = (
                f"{outlier.platform} is {direction} than consensus "
                f"({outlier.probability:.1%} vs avg {avg_prob:.1%})"
            )
        
        topic = self.match_topic(markets[0].title) or "unknown"
        
        return EdgeOpportunity(
            topic=topic,
            markets=markets,
            spread=spread,
            edge_type=edge_type,
            recommendation=recommendation
        )
    
    def scan(self) -> dict:
        """Run full cross-platform scan (sync version)."""
        # Fetch all platforms
        poly_prices = self.fetch_polymarket()
        kalshi_prices = self.fetch_kalshi()
        meta_prices = self.fetch_metaculus()
        
        all_prices = poly_prices + kalshi_prices + meta_prices
        
        # Group by topic
        topic_markets: dict = {}
        for price in all_prices:
            topic = self.match_topic(price.title)
            if topic:
                if topic not in topic_markets:
                    topic_markets[topic] = []
                topic_markets[topic].append(price)
        
        # Find edges
        edges = []
        for topic, markets in topic_markets.items():
            # Dedupe by platform
            platform_best: dict = {}
            for m in markets:
                key = m.platform
                if key not in platform_best or (m.volume or 0) > (platform_best[key].volume or 0):
                    platform_best[key] = m
            
            deduped = list(platform_best.values())
            if len(deduped) >= 2:
                edge = self.calculate_edge(deduped)
                if edge:
                    edges.append(edge)
        
        edges.sort(key=lambda e: e.spread, reverse=True)
        
        return {
            "scan_time": datetime.utcnow().isoformat(),
            "platforms": {
                "polymarket": len(poly_prices),
                "kalshi": len(kalshi_prices),
                "metaculus": len(meta_prices),
            },
            "topics_found": len(topic_markets),
            "edges": [
                {
                    "topic": e.topic,
                    "spread": f"{e.spread:.1%}",
                    "spread_pct": round(e.spread * 100, 1),
                    "type": e.edge_type,
                    "recommendation": e.recommendation,
                    "markets": [
                        {
                            "platform": m.platform,
                            "title": m.title[:80],
                            "probability": f"{m.probability:.1%}",
                            "prob_raw": round(m.probability, 4),
                            "volume": m.volume,
                            "url": m.url,
                        }
                        for m in e.markets
                    ]
                }
                for e in edges[:20]
            ],
            "topic_coverage": {
                topic: {
                    "platforms": list(set(m.platform for m in markets)),
                    "count": len(markets)
                }
                for topic, markets in topic_markets.items()
            }
        }


# Singleton
scanner = CrossPlatformEdgeScanner()


async def scan_edges() -> dict:
    """Async wrapper for edge scanning."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, scanner.scan)
