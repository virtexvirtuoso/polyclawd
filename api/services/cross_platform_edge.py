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
from loguru import logger

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
    "world_cup_fifa": ["world cup", "fifa", "soccer"],
    "world_cup_cricket": ["world cup", "cricket", "t20", "icc"],
    "world_cup_rugby": ["world cup", "rugby"],
    
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
                        logger.info(f"Loaded cache from {cached_at.isoformat()}")
                    else:
                        print(f"Cache expired (from {cached_at.isoformat()})")
        except Exception as e:
            logger.error(f"Cache load error: {e}")
    
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
            logger.info(f"Saved cache at {cache_data['cached_at']}")
        except Exception as e:
            logger.error(f"Cache save error: {e}")
    
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
            logger.error(f"Fetch error for {url}: {e}")
            return None
    
    def fetch_polymarket(self) -> list:
        """Fetch active Polymarket events from Gamma API."""
        prices = []
        try:
            url = "https://gamma-api.polymarket.com/events?active=true&closed=false&limit=500"
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
            logger.error(f"Polymarket fetch error: {e}")
        return prices
    
    def fetch_kalshi(self) -> list:
        """Fetch active Kalshi markets via events endpoint (avoids multi-leg parlays)."""
        prices = []
        try:
            cursor = None
            pages = 0
            while pages < 3:  # Max 3 pages = 600 events
                url = "https://api.elections.kalshi.com/trade-api/v2/events?limit=200&status=open&with_nested_markets=true"
                if cursor:
                    url += f"&cursor={cursor}"
                data = self._fetch_url(url, timeout=45)
                if not data:
                    break

                events = data.get("events", [])
                if not events:
                    break

                for event in events:
                    for market in event.get("markets", []):
                        title = market.get("title", "") or market.get("subtitle", "")
                        if not title:
                            title = event.get("title", "")

                        yes_bid = market.get("yes_bid_dollars") or market.get("yes_bid", 0) or 0
                        yes_ask = market.get("yes_ask_dollars") or market.get("yes_ask", 0) or 0
                        yes_bid = float(yes_bid)
                        yes_ask = float(yes_ask)
                        if yes_bid == 0 and yes_ask == 0:
                            continue

                        # Dollars fields are 0-1 decimals; old fields were 0-100 cents
                        if yes_bid <= 1 and yes_ask <= 1:
                            prob = (yes_bid + yes_ask) / 2
                        else:
                            prob = (yes_bid + yes_ask) / 200
                        volume = market.get("volume_fp") or market.get("volume", 0) or 0
                        volume = float(volume)

                        # Skip zero-volume and micro markets
                        if volume < 100:
                            continue

                        if prob > 0 and title:
                            ticker = market.get("ticker", "")
                            prices.append(PlatformPrice(
                                platform="kalshi",
                                market_id=ticker,
                                title=title,
                                probability=prob,
                                volume=volume,
                                url=f"https://kalshi.com/markets/{ticker}"
                            ))

                cursor = data.get("cursor")
                if not cursor:
                    break
                pages += 1

            logger.info("Kalshi: fetched {} markets from events endpoint", len(prices))
        except Exception as e:
            logger.error(f"Kalshi fetch error: {e}")
            import traceback
            traceback.print_exc()
        return prices
    
    def fetch_metaculus(self) -> list:
        """Fetch Metaculus forecasts (individual fetches required - list API hides predictions)."""
        prices = []
        try:
            # Step 1: Get top question IDs by forecaster count
            list_url = "https://www.metaculus.com/api/posts/?forecast_type=binary&status=open&order_by=-forecasters_count&limit=30"
            # Metaculus requires auth (restricted tier — no community predictions available)
            logger.debug("Metaculus: Skipping (restricted API tier, no community predictions)")
            return prices
            if not list_data:
                logger.info("Metaculus: No list data returned")
                return prices
            
            results = list_data.get("results", [])
            question_ids = []
            for q in results:
                qid = q.get("id")
                forecasters = q.get("nr_forecasters", 0) or 0
                if qid and forecasters >= 100:  # Only high-forecaster questions
                    question_ids.append((qid, q.get("title", ""), forecasters))
            
            logger.debug(f"Metaculus: Fetching {len(question_ids)} questions individually...")
            
            # Step 2: Fetch each question to get predictions (API hides them in list)
            for qid, title, forecasters in question_ids[:20]:  # Limit to 20 to avoid rate limits
                try:
                    detail_url = f"https://www.metaculus.com/api/posts/{qid}/"
                    detail = self._fetch_url(detail_url, timeout=15)
                    if not detail:
                        continue
                    
                    question = detail.get("question", {})
                    if not question or question.get("type") != "binary":
                        continue
                    
                    # Get prediction from individual question
                    aggregations = question.get("aggregations", {})
                    recency = aggregations.get("recency_weighted", {})
                    latest = recency.get("latest", {})
                    centers = latest.get("centers", []) if isinstance(latest, dict) else []
                    prob = centers[0] if centers else None
                    
                    if prob is not None and title:
                        prices.append(PlatformPrice(
                            platform="metaculus",
                            market_id=str(qid),
                            title=title,
                            probability=prob,
                            forecasters=forecasters,
                            url=f"https://metaculus.com/questions/{qid}/"
                        ))
                except Exception as e:
                    logger.error(f"Metaculus question {qid} error: {e}")
                    continue
                    
            logger.info(f"Metaculus: Got {len(prices)} questions with predictions")
        except Exception as e:
            logger.error(f"Metaculus fetch error: {e}")
            import traceback
            traceback.print_exc()
        return prices
    
    def fetch_predictit(self) -> list:
        """Fetch PredictIt markets from cache file (synced from Mac every 30min).
        
        PredictIt blocks datacenter IPs (Cloudflare). Mac cron fetches the API
        and SCPs the JSON to data/predictit_cache.json.
        """
        prices = []
        try:
            cache_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "predictit_cache.json")
            if not os.path.exists(cache_file):
                logger.debug("PredictIt: No cache file")
                return prices
            
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            # Check cache freshness (max 2 hours)
            fetched_at = cache_data.get("fetched_at", "")
            if fetched_at:
                from datetime import datetime, timezone
                try:
                    cache_time = datetime.fromisoformat(fetched_at)
                    if cache_time.tzinfo is None:
                        cache_time = cache_time.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - cache_time
                    if age.total_seconds() > 7200:
                        logger.warning("PredictIt: Cache stale ({})", age)
                        return prices
                except Exception:
                    pass
            
            markets = cache_data.get("markets", [])
            for market in markets:
                market_name = market.get("name", "")
                market_id = market.get("id", "")
                
                for contract in market.get("contracts", []):
                    contract_name = contract.get("name", "")
                    if len(market.get("contracts", [])) == 1:
                        title = market_name
                    else:
                        title = f"{market_name}: {contract_name}"
                    
                    price = contract.get("lastTradePrice")
                    
                    if price is not None and price > 0:
                        prices.append(PlatformPrice(
                            platform="predictit",
                            market_id=str(contract.get("id", "")),
                            title=title,
                            probability=price,
                            volume=None,
                            url=f"https://www.predictit.org/markets/detail/{market_id}"
                        ))
            
            logger.info("PredictIt: {} contracts from {} markets (cache)", len(prices), len(markets))
        except Exception as e:
            logger.error("PredictIt fetch error: {}", e)
        return prices
    
    def fetch_manifold(self) -> list:
        """Fetch active Manifold markets (play money, moves fast)."""
        prices = []
        try:
            # Get top markets by liquidity
            url = "https://api.manifold.markets/v0/markets?limit=200"
            data = self._fetch_url(url, timeout=30)
            if not data:
                logger.info("Manifold: No data returned")
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
                if prob is not None and title and liquidity >= 500:
                    prices.append(PlatformPrice(
                        platform="manifold",
                        market_id=market.get("id", ""),
                        title=title,
                        probability=prob,
                        volume=liquidity,
                        url=market.get("url", f"https://manifold.markets/{market.get('creatorUsername', '')}/{market.get('slug', '')}")
                    ))
        except Exception as e:
            logger.error(f"Manifold fetch error: {e}")
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
            logger.error(f"Smart matcher import failed: {e}")
            return []
        
        # Pre-compute signatures and index by entity (O(n) prefilter)
        logger.info("Building entity index...")
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
        
        logger.info(f"Matching {len(anchor_markets)} anchor markets against {len(other_platforms)} platforms...")
        
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
        
        logger.info(f"Smart matching: {comparisons} comparisons, {len(matched_groups)} matches")
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
        predictit_prices = self.fetch_predictit()
        manifold_prices = self.fetch_manifold()
        meta_prices = self.fetch_metaculus()
        
        all_prices = poly_prices + kalshi_prices + predictit_prices + manifold_prices + meta_prices
        
        # Smart matching: find cross-platform matches using entity extraction
        matched_groups = self.find_cross_platform_matches(all_prices)
        
        # Calculate edges from matched groups
        edges = []
        for group in matched_groups:
            edge = self.calculate_edge(group)
            if edge:
                edges.append(edge)

        # Topic-based matching: group by topic, find cross-platform candidate pairs
        topic_markets: dict = {}
        for price in all_prices:
            topic = self.match_topic(price.title)
            if topic:
                topic_markets.setdefault(topic, []).append(price)

        smart_match_ids = set()
        for group in matched_groups:
            for m in group:
                smart_match_ids.add(m.market_id)

        # Build candidate pairs from topic groups using entity pre-filter
        candidate_pairs = []  # (market_a, market_b, topic)
        try:
            from smart_matcher import create_signature, signatures_match
            has_matcher = True
        except ImportError:
            has_matcher = False

        for topic, markets in topic_markets.items():
            platforms_in_topic = set(m.platform for m in markets)
            if len(platforms_in_topic) < 2:
                continue

            by_plat = {}
            for m in markets:
                if m.market_id not in smart_match_ids:
                    by_plat.setdefault(m.platform, []).append(m)

            plat_list = list(by_plat.keys())
            if len(plat_list) < 2:
                continue

            anchor_plat = "polymarket" if "polymarket" in by_plat else plat_list[0]
            other_plats = [p for p in plat_list if p != anchor_plat]

            for anchor_m in sorted(by_plat[anchor_plat], key=lambda m: m.volume or 0, reverse=True)[:30]:
                for other_plat in other_plats:
                    # Pre-filter: entity overlap if matcher available
                    best_candidate = None
                    best_entity_conf = 0.0

                    if has_matcher:
                        anchor_sig = create_signature(anchor_m.title)
                        if not anchor_sig.entities:
                            continue
                        for cand_m in by_plat[other_plat]:
                            cand_sig = create_signature(cand_m.title)
                            if not cand_sig.entities:
                                continue
                            is_match, conf, _ = signatures_match(anchor_sig, cand_sig, min_entity_overlap=1)
                            if is_match and conf > best_entity_conf and conf >= 0.3:
                                best_candidate = cand_m
                                best_entity_conf = conf
                    else:
                        # Without matcher, pick highest volume candidate on other platform
                        others = sorted(by_plat[other_plat], key=lambda m: m.volume or 0, reverse=True)
                        if others:
                            best_candidate = others[0]

                    if best_candidate:
                        candidate_pairs.append((anchor_m, best_candidate, topic))

        # LLM verification of all candidate pairs (entity pre-filtered + smart matches)
        llm_verified_edges = []
        try:
            from api.services.llm_market_matcher import verify_match, is_ollama_available
            has_llm = is_ollama_available()
        except ImportError:
            has_llm = False

        if has_llm:
            logger.info("LLM verifying %d candidate pairs + %d smart matches...", len(candidate_pairs), len(edges))

            # Verify smart match edges first (may be false positives)
            verified_smart = []
            for edge in edges:
                if len(edge.markets) == 2:
                    result = verify_match(edge.markets[0].title, edge.markets[1].title)
                    if result and result.same and result.confidence >= 0.85:
                        # Handle inverted markets
                        if result.inverted:
                            edge.markets[1] = PlatformPrice(
                                platform=edge.markets[1].platform,
                                market_id=edge.markets[1].market_id,
                                title=edge.markets[1].title,
                                probability=1.0 - edge.markets[1].probability,
                                volume=edge.markets[1].volume,
                                url=edge.markets[1].url,
                            )
                            edge.spread = abs(edge.markets[0].probability - edge.markets[1].probability)
                            edge.recommendation = f"INVERTED: {edge.recommendation}"
                        verified_smart.append(edge)
                    elif result:
                        logger.debug("LLM rejected smart match: %s vs %s (same=%s, conf=%.2f)",
                                     edge.markets[0].title[:40], edge.markets[1].title[:40],
                                     result.same, result.confidence)
                else:
                    verified_smart.append(edge)  # Keep multi-market groups as-is

            # Verify topic candidate pairs
            for market_a, market_b, topic in candidate_pairs:
                result = verify_match(market_a.title, market_b.title)
                if result and result.same and result.confidence >= 0.85:
                    # Build the pair with inversion handling
                    if result.inverted:
                        market_b_adj = PlatformPrice(
                            platform=market_b.platform,
                            market_id=market_b.market_id,
                            title=market_b.title,
                            probability=1.0 - market_b.probability,
                            volume=market_b.volume,
                            url=market_b.url,
                        )
                    else:
                        market_b_adj = market_b

                    edge = self.calculate_edge([market_a, market_b_adj])
                    if edge:
                        edge.topic = topic
                        edge.recommendation = (
                            f"{'[INV] ' if result.inverted else ''}"
                            f"LLM-verified (conf={result.confidence:.0%}): {edge.recommendation}"
                        )
                        llm_verified_edges.append(edge)

            edges = verified_smart + llm_verified_edges
            logger.info("LLM verification: %d smart kept, %d topic pairs verified → %d total edges",
                        len(verified_smart), len(llm_verified_edges), len(edges))
        else:
            logger.warning("Ollama not available — skipping LLM verification, using unverified edges")
            # Fall back to entity-only matches (no LLM)
            for market_a, market_b, topic in candidate_pairs:
                edge = self.calculate_edge([market_a, market_b])
                if edge:
                    edge.topic = topic
                    edge.recommendation = f"[UNVERIFIED] {edge.recommendation}"
                    edges.append(edge)

        edges.sort(key=lambda e: e.spread, reverse=True)
        
        # Filter: require at least one market with volume > $100
        MIN_EDGE_VOLUME = 1000
        edges = [e for e in edges if all((m.volume or 0) >= MIN_EDGE_VOLUME for m in e.markets)]

        results = {
            "scan_time": datetime.utcnow().isoformat(),
            "from_cache": False,
            "platforms": {
                "polymarket": len(poly_prices),
                "kalshi": len(kalshi_prices),
                "predictit": len(predictit_prices),
                "manifold": len(manifold_prices),
                "metaculus": len(meta_prices),
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
                            "market_id": m.market_id,
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
