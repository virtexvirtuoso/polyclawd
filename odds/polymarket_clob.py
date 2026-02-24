"""
Polymarket CLOB (Central Limit Order Book) Integration
Direct access to orderbook depth and price history
"""

import json
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Resilient fetch wrapper
try:
    from api.services.resilient_fetch import resilient_call
    HAS_RESILIENT = True
except ImportError:
    HAS_RESILIENT = False

def _resilient_urlopen(source_name, url, timeout=10):
    """Fetch URL with resilient wrapper if available."""
    import json, urllib.request
    def _do_fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    if HAS_RESILIENT:
        return resilient_call(source_name, _do_fetch, retries=2, backoff_base=2.0)
    return _do_fetch()


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass  
class OrderBook:
    market_id: str
    token_id: str
    outcome: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    spread: float
    mid_price: float
    timestamp: str


def get_token_id_for_market(market_slug: str, outcome: str = "Yes") -> Optional[str]:
    """Get CLOB token ID for a market outcome"""
    try:
        url = f"{GAMMA_API}/markets?slug={market_slug}"
        markets = _resilient_urlopen("polymarket_gamma", url, timeout=10)
        
        if not markets:
            return None
        
        market = markets[0]
        clob_token_ids = market.get("clobTokenIds", "[]")
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)
        
        outcomes = market.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        # Match outcome to token ID
        for i, o in enumerate(outcomes):
            if o.lower() == outcome.lower() and i < len(clob_token_ids):
                return clob_token_ids[i]
        
        return clob_token_ids[0] if clob_token_ids else None
        
    except Exception as e:
        print(f"Error getting token ID: {e}")
        return None


def get_orderbook(token_id: str) -> Optional[OrderBook]:
    """
    Fetch live orderbook for a token.
    
    Args:
        token_id: The CLOB token ID (from clobTokenIds field)
    
    Returns:
        OrderBook with bids, asks, spread, and mid price
    """
    try:
        url = f"{CLOB_API}/book?token_id={token_id}"
        data = _resilient_urlopen("polymarket_clob", url, timeout=10)
        
        if "error" in data:
            return None
        
        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]
        
        # Calculate spread and mid
        best_bid = bids[0].price if bids else 0
        best_ask = asks[0].price if asks else 1
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2 if bids and asks else 0.5
        
        return OrderBook(
            market_id=data.get("market", ""),
            token_id=token_id,
            outcome=data.get("outcome", ""),
            bids=bids[:10],  # Top 10 levels
            asks=asks[:10],
            spread=round(spread, 4),
            mid_price=round(mid_price, 4),
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        print(f"Orderbook fetch error: {e}")
        return None


def get_orderbook_for_market(market_slug: str, outcome: str = "Yes") -> Optional[OrderBook]:
    """Convenience function to get orderbook by market slug"""
    token_id = get_token_id_for_market(market_slug, outcome)
    if not token_id:
        return None
    return get_orderbook(token_id)


def get_price_history(
    token_id: str,
    interval: str = "1h",
    fidelity: int = 60,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None
) -> List[Dict]:
    """
    Fetch OHLC price history for a token.
    
    Args:
        token_id: CLOB token ID
        interval: Time range - "1h", "1d", "1w", "1m", "all"
        fidelity: Candle width in minutes (1, 5, 15, 60, 1440)
        start_ts: Unix timestamp start (optional)
        end_ts: Unix timestamp end (optional)
    
    Returns:
        List of OHLC candles
    """
    try:
        url = f"{CLOB_API}/prices-history?market={token_id}&interval={interval}&fidelity={fidelity}"
        if start_ts:
            url += f"&startTs={start_ts}"
        if end_ts:
            url += f"&endTs={end_ts}"
        
        data = _resilient_urlopen("polymarket_clob", url, timeout=15)
        
        if not data or "history" not in data:
            return []
        
        return [
            {
                "timestamp": h.get("t"),
                "open": float(h.get("o", 0)),
                "high": float(h.get("h", 0)),
                "low": float(h.get("l", 0)),
                "close": float(h.get("c", 0)),
            }
            for h in data["history"]
        ]
        
    except Exception as e:
        print(f"Price history error: {e}")
        return []


def analyze_orderbook_depth(orderbook: OrderBook) -> Dict:
    """
    Analyze orderbook for trading signals.
    
    Returns:
        Analysis including liquidity, imbalance, and wall detection
    """
    if not orderbook:
        return {}
    
    bid_liquidity = sum(b.size for b in orderbook.bids)
    ask_liquidity = sum(a.size for a in orderbook.asks)
    total_liquidity = bid_liquidity + ask_liquidity
    
    # Order imbalance (-1 to 1, positive = more bids)
    imbalance = (bid_liquidity - ask_liquidity) / total_liquidity if total_liquidity > 0 else 0
    
    # Detect walls (unusually large orders)
    bid_sizes = [b.size for b in orderbook.bids]
    ask_sizes = [a.size for a in orderbook.asks]
    
    avg_bid = sum(bid_sizes) / len(bid_sizes) if bid_sizes else 0
    avg_ask = sum(ask_sizes) / len(ask_sizes) if ask_sizes else 0
    
    # Wall = order 3x average size
    bid_walls = [b for b in orderbook.bids if b.size > avg_bid * 3]
    ask_walls = [a for a in orderbook.asks if a.size > avg_ask * 3]
    
    return {
        "spread_cents": round(orderbook.spread * 100, 2),
        "mid_price": orderbook.mid_price,
        "bid_liquidity": round(bid_liquidity, 2),
        "ask_liquidity": round(ask_liquidity, 2),
        "total_liquidity": round(total_liquidity, 2),
        "imbalance": round(imbalance, 3),
        "imbalance_signal": "BUY" if imbalance > 0.2 else "SELL" if imbalance < -0.2 else "NEUTRAL",
        "bid_walls": [{"price": w.price, "size": w.size} for w in bid_walls],
        "ask_walls": [{"price": w.price, "size": w.size} for w in ask_walls],
        "tight_spread": orderbook.spread < 0.02,  # <2 cents is tight
    }


def get_market_microstructure(market_slug: str) -> Dict:
    """
    Get complete market microstructure analysis for a market.
    
    Returns bid/ask depth, spread analysis, and trading signals.
    """
    # Get both Yes and No orderbooks
    yes_book = get_orderbook_for_market(market_slug, "Yes")
    no_book = get_orderbook_for_market(market_slug, "No")
    
    result = {
        "market": market_slug,
        "timestamp": datetime.utcnow().isoformat(),
    }
    
    if yes_book:
        result["yes"] = {
            "mid_price": yes_book.mid_price,
            "spread": yes_book.spread,
            "analysis": analyze_orderbook_depth(yes_book),
        }
    
    if no_book:
        result["no"] = {
            "mid_price": no_book.mid_price,
            "spread": no_book.spread,
            "analysis": analyze_orderbook_depth(no_book),
        }
    
    # Cross-check prices
    if yes_book and no_book:
        implied_total = yes_book.mid_price + no_book.mid_price
        result["price_consistency"] = {
            "yes_mid": yes_book.mid_price,
            "no_mid": no_book.mid_price,
            "total": round(implied_total, 4),
            "arbitrage_exists": abs(implied_total - 1.0) > 0.02,
        }
    
    return result


async def get_clob_summary(market_id: str = None) -> Dict:
    """Get CLOB orderbook summary for trading signals"""
    from datetime import datetime
    
    result = {
        "source": "Polymarket CLOB",
        "timestamp": datetime.utcnow().isoformat(),
        "description": "Live orderbook depth and liquidity analysis"
    }
    
    if market_id:
        # Get specific market
        try:
            url = f"{GAMMA_API}/markets/{market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                market = json.loads(resp.read().decode())
            
            slug = market.get("slug", "")
            if slug:
                result["market"] = get_market_microstructure(slug)
        except:
            pass
    else:
        # Get top liquid markets
        try:
            url = f"{GAMMA_API}/markets?active=true&closed=false&limit=10&_sort=liquidityNum&_order=desc"
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                markets = json.loads(resp.read().decode())
            
            result["top_markets"] = []
            for m in markets[:5]:
                slug = m.get("slug", "")
                if slug:
                    analysis = get_market_microstructure(slug)
                    if analysis.get("yes"):
                        result["top_markets"].append({
                            "question": m.get("question", "")[:60],
                            "liquidity": m.get("liquidityNum", 0),
                            "spread": analysis.get("yes", {}).get("spread", 0),
                            "imbalance": analysis.get("yes", {}).get("analysis", {}).get("imbalance", 0),
                        })
        except Exception as e:
            print(f"Error: {e}")
    
    return result


if __name__ == "__main__":
    import asyncio
    
    print("Testing Polymarket CLOB integration...")
    
    # Test with a known active market
    test_slug = "will-donald-trump-be-convicted-in-a-criminal-trial-in-2025"
    
    print(f"\nGetting orderbook for: {test_slug}")
    book = get_orderbook_for_market(test_slug, "Yes")
    
    if book:
        print(f"Mid price: {book.mid_price}")
        print(f"Spread: {book.spread}")
        print(f"Top bids: {[(b.price, b.size) for b in book.bids[:3]]}")
        print(f"Top asks: {[(a.price, a.size) for a in book.asks[:3]]}")
        
        analysis = analyze_orderbook_depth(book)
        print(f"\nAnalysis:")
        print(f"  Imbalance: {analysis['imbalance']} ({analysis['imbalance_signal']})")
        print(f"  Total liquidity: ${analysis['total_liquidity']:,.0f}")
        print(f"  Tight spread: {analysis['tight_spread']}")
    else:
        print("Could not fetch orderbook")
    
    print("\n" + "="*50)
    print("\nGetting CLOB summary...")
    summary = asyncio.run(get_clob_summary())
    print(f"Top markets by liquidity: {len(summary.get('top_markets', []))}")
