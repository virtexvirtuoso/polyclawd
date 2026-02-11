"""
Edge Signal Cache
Caches edge signals from Vegas/Soccer/Betfair/Kalshi with background refresh
"""

import json
import logging
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)

# Cache file
CACHE_FILE = Path.home() / ".openclaw" / "edge_cache.json"
CACHE_TTL = 300  # 5 minutes

# Lock for thread safety
_cache_lock = threading.Lock()
_last_refresh = 0
_cached_signals = []

def load_cache() -> List[Dict]:
    """Load cached edge signals"""
    global _cached_signals, _last_refresh
    
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
                _cached_signals = data.get("signals", [])
                _last_refresh = data.get("timestamp", 0)
                return _cached_signals
        except (json.JSONDecodeError, IOError, KeyError) as e:
            # Log cache load failure but don't crash - just use empty cache
            logger.warning(f"Failed to load edge cache: {e}")
    return []

def save_cache(signals: List[Dict]):
    """Save edge signals to cache"""
    global _cached_signals, _last_refresh
    
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _cached_signals = signals
    _last_refresh = time.time()
    
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "signals": signals,
            "timestamp": _last_refresh,
            "updated_at": datetime.now().isoformat()
        }, f)

def fetch_vegas_edges() -> List[Dict]:
    """Fetch Vegas edge signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/vegas/edge?min_edge=0.08",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=20
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            if edge.get("edge_pct", 0) >= 8:
                side = edge.get("direction", "YES").upper()
                signals.append({
                    "source": "vegas_edge",
                    "platform": "polymarket",
                    "market": edge.get("poly_market", ""),
                    "market_id": edge.get("poly_market_id"),
                    "side": side,
                    "confidence": min(70, edge.get("edge_pct", 0) * 3),
                    "value": edge.get("kelly_bet", 0),
                    "reasoning": f"Vegas {edge.get('vegas_prob', 0):.0f}% vs Poly {edge.get('poly_price', 0)*100:.0f}% ({edge.get('edge_pct', 0):+.1f}% edge)",
                    "price": edge.get("poly_price", 0.5),
                    "url": edge.get("poly_url")
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Vegas edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching Vegas edges: {e}")
    return signals

def fetch_soccer_edges() -> List[Dict]:
    """Fetch Soccer edge signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/vegas/soccer",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=25
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            edge_pct = edge.get("edge_pct", 0)
            if abs(edge_pct) >= 5:
                side = "YES" if edge_pct > 0 else "NO"
                signals.append({
                    "source": "soccer_edge",
                    "platform": "polymarket",
                    "market": f"{edge.get('team', '')} - {edge.get('competition', '')}",
                    "side": side,
                    "confidence": min(60, abs(edge_pct) * 2),
                    "value": abs(edge_pct),
                    "reasoning": f"Vegas {edge.get('vegas_prob', 0):.0f}% vs Poly {edge.get('poly_prob', 0):.0f}% ({edge_pct:+.1f}% edge)",
                    "price": edge.get("poly_prob", 50) / 100,
                    "url": edge.get("poly_url")
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Soccer edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching soccer edges: {e}")
    return signals

def fetch_betfair_edges() -> List[Dict]:
    """Fetch Betfair edge signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/betfair/edge?min_edge=0.05",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=20
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            edge_pct = edge.get("edge_pct", 0)
            if abs(edge_pct) >= 5:
                side = "YES" if edge_pct > 0 else "NO"
                signals.append({
                    "source": "betfair_edge",
                    "platform": "polymarket",
                    "market": edge.get("poly_market", ""),
                    "market_id": edge.get("poly_market_id"),
                    "side": side,
                    "confidence": min(65, abs(edge_pct) * 2.5),
                    "value": abs(edge_pct),
                    "reasoning": f"Betfair {edge.get('betfair_prob', 0):.0f}% vs Poly {edge.get('poly_prob', 0):.0f}% ({edge_pct:+.1f}% edge)",
                    "price": edge.get("poly_prob", 50) / 100,
                    "url": edge.get("poly_url")
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Betfair edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching Betfair edges: {e}")
    return signals

def fetch_kalshi_overlaps() -> List[Dict]:
    """Fetch Kalshi overlap signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/kalshi/markets",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=35
        )
        data = json.loads(resp.read().decode())
        for overlap in data.get("overlaps", [])[:5]:
            match_conf = overlap.get("match_confidence", 0)
            if match_conf >= 0.7:
                poly_price = overlap.get("polymarket_price", 50)
                signals.append({
                    "source": "kalshi_overlap",
                    "platform": "polymarket",
                    "market": overlap.get("polymarket_event", ""),
                    "market_id": overlap.get("polymarket_id"),
                    "side": "RESEARCH",
                    "confidence": match_conf * 30,
                    "value": match_conf,
                    "reasoning": f"Kalshi match: {overlap.get('kalshi_title', '')[:40]} (conf: {match_conf:.0%})",
                    "price": poly_price / 100 if poly_price else 0.5
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Kalshi overlap fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching Kalshi overlaps: {e}")
    return signals

def fetch_manifold_edges() -> List[Dict]:
    """Fetch Manifold edge signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/manifold/edge?min_edge=5",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=30
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            edge_pct = edge.get("edge_pct", 0)
            if abs(edge_pct) >= 5:
                signals.append({
                    "source": "manifold_edge",
                    "platform": "polymarket",
                    "market": edge.get("polymarket_title", ""),
                    "side": edge.get("direction", "YES"),
                    "confidence": min(50, abs(edge_pct) * 2),  # Play money = lower weight
                    "value": abs(edge_pct),
                    "reasoning": f"Manifold {edge.get('manifold_prob', 0):.0f}% vs Poly {edge.get('polymarket_price', 0):.0f}% ({edge_pct:+.1f}%)",
                    "price": edge.get("polymarket_price", 50) / 100,
                    "url": edge.get("manifold_url")
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Manifold edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching Manifold edges: {e}")
    return signals

def fetch_predictit_edges() -> List[Dict]:
    """Fetch PredictIt edge signals"""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:8420/api/predictit/edge?min_edge=5",
                                   headers={"User-Agent": "EdgeCache/1.0"}),
            timeout=30
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            edge_pct = edge.get("edge_pct", 0)
            if abs(edge_pct) >= 5:
                signals.append({
                    "source": "predictit_edge",
                    "platform": "polymarket",
                    "market": edge.get("polymarket_title", ""),
                    "side": edge.get("direction", "YES"),
                    "confidence": min(55, abs(edge_pct) * 2.2),  # Real money = higher weight
                    "value": abs(edge_pct),
                    "reasoning": f"PredictIt {edge.get('predictit_price', 0):.0f}% vs Poly {edge.get('polymarket_price', 0):.0f}% ({edge_pct:+.1f}%)",
                    "price": edge.get("polymarket_price", 50) / 100,
                    "url": edge.get("predictit_url")
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"PredictIt edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching PredictIt edges: {e}")
    return signals

def refresh_edge_cache() -> List[Dict]:
    """Refresh all edge signals (called in background)"""
    all_signals = []

    # Fetch all sources (order by speed)
    all_signals.extend(fetch_vegas_edges())
    all_signals.extend(fetch_betfair_edges())
    all_signals.extend(fetch_soccer_edges())
    all_signals.extend(fetch_manifold_edges())
    all_signals.extend(fetch_predictit_edges())
    all_signals.extend(fetch_kalshi_overlaps())

    # Save to cache
    with _cache_lock:
        save_cache(all_signals)

    return all_signals


async def refresh_edge_cache_async() -> List[Dict]:
    """Enhancement #1: Parallel async refresh of all edge sources.

    Fires all 6 edge fetchers concurrently in a thread pool.
    Total time = max(individual latencies) instead of sum.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    _pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="edge")
    loop = asyncio.get_event_loop()

    futures = [
        loop.run_in_executor(_pool, fetch_vegas_edges),
        loop.run_in_executor(_pool, fetch_betfair_edges),
        loop.run_in_executor(_pool, fetch_soccer_edges),
        loop.run_in_executor(_pool, fetch_manifold_edges),
        loop.run_in_executor(_pool, fetch_predictit_edges),
        loop.run_in_executor(_pool, fetch_kalshi_overlaps),
    ]

    results = await asyncio.gather(*futures, return_exceptions=True)

    all_signals = []
    for result in results:
        if isinstance(result, list):
            all_signals.extend(result)
        elif isinstance(result, Exception):
            logger.debug(f"Edge source failed during parallel refresh: {result}")

    with _cache_lock:
        save_cache(all_signals)

    _pool.shutdown(wait=False)
    return all_signals

def get_edge_signals(force_refresh: bool = False) -> List[Dict]:
    """
    Get edge signals from cache.
    Returns cached signals immediately, refreshes in background if stale.
    """
    global _cached_signals, _last_refresh
    
    # Load from file if empty
    if not _cached_signals:
        load_cache()
    
    # Check if cache is stale
    cache_age = time.time() - _last_refresh
    
    if force_refresh or cache_age > CACHE_TTL:
        # Refresh in background thread
        thread = threading.Thread(target=refresh_edge_cache, daemon=True)
        thread.start()
        
        # If cache is very old (>15 min), wait for refresh
        if cache_age > 900:
            thread.join(timeout=60)
    
    with _cache_lock:
        return list(_cached_signals)

def get_cache_status() -> Dict:
    """Get cache status info"""
    global _last_refresh
    
    if not _cached_signals:
        load_cache()
    
    cache_age = time.time() - _last_refresh if _last_refresh else 99999
    
    return {
        "cached_signals": len(_cached_signals),
        "cache_age_seconds": int(cache_age) if cache_age < 99999 else None,
        "cache_stale": cache_age > CACHE_TTL,
        "last_refresh": datetime.fromtimestamp(_last_refresh).isoformat() if _last_refresh else None,
        "sources": list(set(s.get("source") for s in _cached_signals))
    }
