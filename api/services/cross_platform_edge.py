"""
Cross-Platform Edge Scanner
Compares Polymarket, Kalshi, and Metaculus to find probability discrepancies.

Features:
- 6-hour result caching
- Expanded topic matching (40+ topics)
"""

import asyncio
import json
import os
import urllib.request
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

# Cache file path
CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "edge_cache.json")
CACHE_TTL_HOURS = 6

# Topic matching keywords - expanded coverage
TOPIC_KEYWORDS = {
    # Trump
    "trump_nobel": ["trump", "nobel", "peace prize"],
    "trump_resign": ["trump", "resign", "leave office", "step down"],
    "trump_indictment": ["trump", "indicted", "indictment", "charged", "criminal", "convicted", "guilty"],
    "trump_tariffs": ["trump", "tariff", "trade war", "china", "import", "duties"],
    "trump_impeach": ["trump", "impeach", "impeachment", "removal"],
    "trump_approval": ["trump", "approval", "rating", "poll", "favorability"],
    
    # Fed / Monetary Policy
    "fed_chair": ["fed", "chair", "federal reserve", "warsh", "powell", "yellen"],
    "fed_rates": ["fed", "rate", "interest rate", "fomc", "cut", "hike", "basis points"],
    "inflation": ["inflation", "cpi", "pce", "deflation", "price", "consumer"],
    "recession": ["recession", "gdp", "economic", "downturn", "soft landing"],
    
    # Crypto
    "bitcoin_pow": ["bitcoin", "proof of work", "pow", "mining"],
    "bitcoin_price": ["bitcoin", "btc", "price", "$100k", "$150k", "$200k"],
    "bitcoin_etf": ["bitcoin", "btc", "etf", "spot", "approval", "sec"],
    "ethereum": ["ethereum", "eth", "price", "merge", "staking"],
    "crypto_regulation": ["crypto", "regulation", "sec", "cftc", "gensler"],
    
    # Supreme Court
    "scotus": ["supreme court", "scotus", "justice", "resign", "retire"],
    "scotus_ruling": ["supreme court", "ruling", "decision", "overturn"],
    
    # Elections
    "election_2028": ["2028", "election", "president", "nominee", "primary"],
    "election_2026": ["2026", "midterm", "senate", "house", "congress"],
    "dem_nominee": ["democrat", "democratic", "nominee", "primary", "biden", "harris"],
    "gop_nominee": ["republican", "gop", "nominee", "primary", "trump", "desantis", "haley"],
    
    # Geopolitics
    "ukraine_war": ["ukraine", "russia", "war", "ceasefire", "peace", "zelensky", "putin"],
    "china_taiwan": ["china", "taiwan", "invasion", "strait", "xi", "reunification"],
    "middle_east": ["israel", "gaza", "hamas", "iran", "hezbollah", "ceasefire"],
    "north_korea": ["north korea", "kim", "nuclear", "missile", "test"],
    
    # Tech / AI
    "ai_regulation": ["ai", "artificial intelligence", "regulation", "openai", "anthropic"],
    "tech_antitrust": ["google", "apple", "amazon", "meta", "antitrust", "monopoly", "breakup"],
    
    # Markets / Finance
    "sp500": ["s&p", "spy", "stock", "market", "rally", "crash"],
    "treasury": ["treasury", "bond", "yield", "10-year", "debt ceiling"],
    "dollar": ["dollar", "usd", "dxy", "currency", "forex"],
    
    # Sports (for Kalshi overlap)
    "super_bowl": ["super bowl", "nfl", "champion", "chiefs", "eagles"],
    "world_series": ["world series", "mlb", "baseball", "champion"],
    "nba_finals": ["nba", "finals", "basketball", "champion"],
    "world_cup": ["world cup", "fifa", "soccer", "football"],
    
    # Other
    "pandemic": ["pandemic", "covid", "virus", "outbreak", "who", "lockdown"],
    "climate": ["climate", "carbon", "emissions", "paris", "net zero"],
    "space": ["spacex", "nasa", "mars", "moon", "starship", "artemis"],
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
        self.cache_ttl = timedelta(hours=CACHE_TTL_HOURS)
        self._load_cache()
    
    def _load_cache(self):
        """Load cached results from disk."""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
                    if datetime.utcnow() - cached_at < self.cache_ttl:
                        self.cache = data
                        print(f"Loaded cache from {cached_at.isoformat()}")
                    else:
                        print(f"Cache expired (from {cached_at.isoformat()})")
        except Exception as e:
            print(f"Cache load error: {e}")
    
    def _save_cache(self, results: dict):
        """Save results to disk cache."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            cache_data = {
                "cached_at": datetime.utcnow().isoformat(),
                "results": results
            }
            with open(CACHE_FILE, 'w') as f:
                json.dump(cache_data, f, indent=2)
            self.cache = cache_data
            print(f"Saved cache at {cache_data['cached_at']}")
        except Exception as e:
            print(f"Cache save error: {e}")
    
    def _get_cached_results(self) -> Optional[dict]:
        """Get cached results if still valid."""
        if not self.cache:
            return None
        cached_at = self.cache.get("cached_at")
        if not cached_at:
            return None
        try:
            cached_time = datetime.fromisoformat(cached_at)
            if datetime.utcnow() - cached_time < self.cache_ttl:
                results = self.cache.get("results", {}).copy()
                results["from_cache"] = True
                results["cache_age_minutes"] = int((datetime.utcnow() - cached_time).total_seconds() / 60)
                return results
        except:
            pass
        return None
    
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
    
    def fetch_predictit(self) -> list:
        """Fetch active PredictIt markets from local cache (proxied from Mac)."""
        prices = []
        try:
            # Read from cache file (synced from Mac to bypass Cloudflare)
            cache_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "predictit_cache.json")
            if not os.path.exists(cache_file):
                print("PredictIt: No cache file (run predictit_proxy.py on Mac)")
                return prices
            
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            # Check cache freshness (max 2 hours)
            fetched_at = cache_data.get("fetched_at", "")
            if fetched_at:
                cache_age = datetime.utcnow() - datetime.fromisoformat(fetched_at)
                if cache_age > timedelta(hours=2):
                    print(f"PredictIt: Cache stale ({cache_age})")
                    return prices
            
            markets = cache_data.get("markets", [])
            for market in markets:
                market_name = market.get("name", "")
                
                for contract in market.get("contracts", []):
                    title = f"{market_name}: {contract.get('name', '')}"
                    price = contract.get("lastTradePrice")
                    
                    if price is not None and price > 0:
                        prices.append(PlatformPrice(
                            platform="predictit",
                            market_id=str(contract.get("id", "")),
                            title=title,
                            probability=price,  # PredictIt prices are already 0-1
                            volume=None,
                            url=f"https://www.predictit.org/markets/detail/{market.get('id', '')}"
                        ))
        except Exception as e:
            print(f"PredictIt fetch error: {e}")
            import traceback
            traceback.print_exc()
        return prices
    
    def fetch_manifold(self) -> list:
        """Fetch active Manifold markets (play money, moves fast)."""
        prices = []
        try:
            # Get top markets by liquidity
            url = "https://api.manifold.markets/v0/markets?limit=200"
            data = self._fetch_url(url, timeout=30)
            if not data:
                print("Manifold: No data returned")
                return prices
            
            # API returns array directly
            markets = data if isinstance(data, list) else []
            
            for market in markets:
                # Only binary markets
                if market.get("outcomeType") != "BINARY":
                    continue
                
                # Skip closed/resolved
                if market.get("isResolved") or market.get("closeTime", float('inf')) < datetime.utcnow().timestamp() * 1000:
                    continue
                
                title = market.get("question", "")
                prob = market.get("probability")
                liquidity = market.get("totalLiquidity", 0)
                
                # Only markets with decent liquidity
                if prob is not None and title and liquidity >= 100:
                    prices.append(PlatformPrice(
                        platform="manifold",
                        market_id=market.get("id", ""),
                        title=title,
                        probability=prob,
                        volume=liquidity,
                        url=market.get("url", f"https://manifold.markets/{market.get('creatorUsername', '')}/{market.get('slug', '')}")
                    ))
        except Exception as e:
            print(f"Manifold fetch error: {e}")
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
    
    def find_cross_platform_matches(self, all_prices: list) -> list:
        """Find matching markets across platforms using smart entity matching."""
        # Import smart matcher
        try:
            import sys
            odds_path = os.path.join(os.path.dirname(__file__), "..", "..", "odds")
            if odds_path not in sys.path:
                sys.path.insert(0, odds_path)
            from smart_matcher import create_signature, signatures_match, extract_entities
        except ImportError as e:
            print(f"Smart matcher import failed: {e}")
            return []
        
        # Pre-compute signatures and index by entity (O(n) prefilter)
        print("Building entity index...")
        market_sigs = {}  # market_id -> (market, signature)
        entity_index = {}  # entity -> set of market_ids
        
        for p in all_prices:
            sig = create_signature(p.title)
            if not sig.entities:
                continue
            market_sigs[p.market_id] = (p, sig)
            for entity in sig.entities:
                if entity not in entity_index:
                    entity_index[entity] = set()
                entity_index[entity].add(p.market_id)
        
        # Group by platform
        by_platform = {}
        for p in all_prices:
            if p.market_id not in market_sigs:
                continue
            if p.platform not in by_platform:
                by_platform[p.platform] = []
            by_platform[p.platform].append(p)
        
        platforms = list(by_platform.keys())
        if len(platforms) < 2:
            return []
        
        # Use Polymarket as anchor (largest, most liquid)
        anchor_platform = "polymarket" if "polymarket" in by_platform else platforms[0]
        anchor_markets = by_platform.get(anchor_platform, [])
        other_platforms = [p for p in platforms if p != anchor_platform]
        
        # Limit anchor markets to top 200 by volume for speed
        anchor_markets = sorted(anchor_markets, key=lambda m: m.volume or 0, reverse=True)[:200]
        
        matched_groups = []
        seen_ids = set()
        comparisons = 0
        
        print(f"Matching {len(anchor_markets)} anchor markets against {len(other_platforms)} platforms...")
        
        for anchor in anchor_markets:
            if anchor.market_id in seen_ids:
                continue
            
            anchor_market, anchor_sig = market_sigs.get(anchor.market_id, (None, None))
            if not anchor_sig:
                continue
            
            group = [anchor]
            seen_ids.add(anchor.market_id)
            
            # Find candidate markets that share at least one entity (prefilter)
            candidate_ids = set()
            for entity in anchor_sig.entities:
                candidate_ids.update(entity_index.get(entity, set()))
            candidate_ids.discard(anchor.market_id)
            
            # Find matches on other platforms
            for other_plat in other_platforms:
                best_match = None
                best_conf = 0.0
                
                for candidate in by_platform[other_plat]:
                    if candidate.market_id not in candidate_ids:
                        continue  # Skip - no entity overlap (prefiltered)
                    if candidate.market_id in seen_ids:
                        continue
                    
                    cand_market, cand_sig = market_sigs.get(candidate.market_id, (None, None))
                    if not cand_sig:
                        continue
                    
                    comparisons += 1
                    # Require 2 entity overlap if anchor has 2+ entities
                    min_overlap = 2 if len(anchor_sig.entities) >= 2 else 1
                    is_match, confidence, reason = signatures_match(anchor_sig, cand_sig, min_entity_overlap=min_overlap)
                    
                    # Require high confidence (0.6+) for real matches
                    if is_match and confidence > best_conf and confidence >= 0.6:
                        best_match = candidate
                        best_conf = confidence
                
                if best_match:
                    group.append(best_match)
                    seen_ids.add(best_match.market_id)
            
            # Only keep groups with 2+ platforms
            if len(group) >= 2:
                matched_groups.append(group)
        
        print(f"Smart matching: {comparisons} comparisons, {len(matched_groups)} matches")
        return matched_groups
    
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
    
    def scan(self, force_refresh: bool = False) -> dict:
        """Run full cross-platform scan (sync version).
        
        Args:
            force_refresh: If True, bypass cache and fetch fresh data
        """
        # Check cache first
        if not force_refresh:
            cached = self._get_cached_results()
            if cached:
                return cached
        
        # Fetch all platforms
        poly_prices = self.fetch_polymarket()
        kalshi_prices = self.fetch_kalshi()
        meta_prices = self.fetch_metaculus()
        predictit_prices = self.fetch_predictit()
        manifold_prices = self.fetch_manifold()
        
        all_prices = poly_prices + kalshi_prices + meta_prices + predictit_prices + manifold_prices
        
        # Smart matching: find cross-platform matches using entity extraction
        matched_groups = self.find_cross_platform_matches(all_prices)
        
        # Calculate edges from matched groups
        edges = []
        for group in matched_groups:
            edge = self.calculate_edge(group)
            if edge:
                edges.append(edge)
        
        # Also do topic-based matching as fallback (lower priority)
        topic_markets: dict = {}
        for price in all_prices:
            topic = self.match_topic(price.title)
            if topic:
                if topic not in topic_markets:
                    topic_markets[topic] = []
                topic_markets[topic].append(price)
        
        edges.sort(key=lambda e: e.spread, reverse=True)
        
        results = {
            "scan_time": datetime.utcnow().isoformat(),
            "from_cache": False,
            "platforms": {
                "polymarket": len(poly_prices),
                "kalshi": len(kalshi_prices),
                "metaculus": len(meta_prices),
                "predictit": len(predictit_prices),
                "manifold": len(manifold_prices),
            },
            "smart_matches": len(matched_groups),
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
        
        # Save to cache
        self._save_cache(results)
        
        return results


# Singleton
scanner = CrossPlatformEdgeScanner()


async def scan_edges(force_refresh: bool = False) -> dict:
    """Async wrapper for edge scanning."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: scanner.scan(force_refresh=force_refresh))
