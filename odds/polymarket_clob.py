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

        # Polymarket's /book returns bids ascending and asks descending
        # (worst-to-best). Normalize so bids[0] = best bid (highest) and
        # asks[0] = best ask (lowest) — standard CLOB convention.
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

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


@dataclass
class FillEstimate:
    """Result of walking the order book to size a position.

    Fields:
        ok            : True iff position is tradeable (actual_usd >= min_usd)
        actual_usd    : USD that will actually be spent (<= target_usd)
        shares        : Number of contracts acquired
        avg_price     : Volume-weighted average fill price (decimal 0..1)
        best_price    : Top-of-book price (decimal 0..1)
        slippage_bps  : (avg_price - best_price) / best_price * 10000
        spread        : best_ask - best_bid at fetch time
        reason        : "full" | "resized" | "skip:<why>"
    """
    ok: bool
    actual_usd: float
    shares: float
    avg_price: float
    best_price: float
    slippage_bps: float
    spread: float
    reason: str


def _walk_asks(
    asks: List[OrderBookLevel],
    best_price: float,
    target_usd: float,
    max_slip_bps: float,
) -> tuple[float, float]:
    """Walk asks accumulating fills. Stops at target_usd OR max_slip_bps.
    Returns (cum_usd, cum_shares). Partial walk is fine — caller checks against min_usd.
    """
    cum_usd = 0.0
    cum_shares = 0.0
    for lvl in asks:
        level_usd = lvl.price * lvl.size
        if level_usd <= 0:
            continue

        # Try adding the entire level, see if slippage cap is breached
        next_usd = cum_usd + level_usd
        next_shares = cum_shares + lvl.size
        next_avg = next_usd / next_shares if next_shares > 0 else lvl.price
        next_slip = ((next_avg - best_price) / best_price) * 10000 if best_price > 0 else 0

        # If this full level is within cap AND we'd still be under target, take it all
        if next_slip <= max_slip_bps and next_usd <= target_usd:
            cum_usd = next_usd
            cum_shares = next_shares
            continue

        # Otherwise, we need a partial fill on this level — constrained by either
        # (a) remaining target USD, or (b) slip cap. Take whichever is smaller.

        # (a) Remaining target
        remaining_usd = max(0.0, target_usd - cum_usd)
        remaining_shares_by_target = remaining_usd / lvl.price if lvl.price > 0 else 0

        # (b) Slip cap — solve for max shares at this level such that
        # (cum_usd + p*x) / (cum_shares + x) = best_price * (1 + max_slip_bps/10000)
        cap_avg = best_price * (1 + max_slip_bps / 10000)
        # (cum_usd + p*x) = cap_avg * (cum_shares + x)
        # cum_usd - cap_avg*cum_shares = cap_avg*x - p*x = x*(cap_avg - p)
        denom = cap_avg - lvl.price
        if denom >= 0:
            # Level price is already below the cap — take as much as target allows
            take_by_slip = remaining_shares_by_target
        else:
            # denom negative — solve for x
            lhs = cum_usd - cap_avg * cum_shares
            take_by_slip = max(0.0, lhs / denom) if denom != 0 else 0.0

        take = min(remaining_shares_by_target, take_by_slip, lvl.size)
        if take <= 0:
            break
        cum_usd += lvl.price * take
        cum_shares += take
        if cum_usd >= target_usd - 1e-9:
            break
        # If we hit the slip cap on this level, don't go further
        if take < lvl.size:
            break
    return cum_usd, cum_shares


def size_to_book(
    token_id: Optional[str] = None,
    market_slug: Optional[str] = None,
    side: str = "YES",
    target_usd: float = 100.0,
    max_slip_bps: float = 50.0,
    min_usd: float = 15.0,
    max_spread: float = 0.05,
) -> FillEstimate:
    """Walk the Polymarket CLOB order book to size a position adaptively.

    Either `token_id` or `market_slug` (+side) must be provided. If only
    slug is given, the function resolves the token id via Gamma.

    Policy:
        1. Fetch book for the side's token.
        2. If spread > max_spread: skip (wide market).
        3. Walk asks (for buying) accumulating fills until target_usd or
           slippage cap.
        4. If accumulated usd < min_usd: skip (too thin).
        5. Otherwise return the actual executable size + avg price.

    Returns FillEstimate with ok=False and reason="skip:..." when the
    market is not tradeable at acceptable quality.
    """
    # Resolve token id if only slug given
    if not token_id and market_slug:
        outcome = "Yes" if str(side).upper() == "YES" else "No"
        token_id = get_token_id_for_market(market_slug, outcome)
    if not token_id:
        return FillEstimate(False, 0, 0, 0, 0, 0, 0, "skip:no_token_id")

    book = get_orderbook(token_id)
    if not book:
        return FillEstimate(False, 0, 0, 0, 0, 0, 0, "skip:no_book")

    # Buying always walks asks on the token you want to acquire. For NO we
    # already asked get_token_id_for_market for the NO token above.
    asks = book.asks
    if not asks:
        return FillEstimate(False, 0, 0, 0, 0, 0, book.spread, "skip:empty_asks")

    best_price = asks[0].price
    spread = book.spread
    if spread > max_spread:
        return FillEstimate(False, 0, 0, 0, best_price, 0, spread,
                            f"skip:wide_spread:{spread:.3f}")

    cum_usd, cum_shares = _walk_asks(asks, best_price, target_usd, max_slip_bps)

    if cum_shares <= 0 or cum_usd < min_usd:
        return FillEstimate(False, round(cum_usd, 2), round(cum_shares, 2),
                            best_price, best_price, 0, spread,
                            f"skip:thin_book:${cum_usd:.0f}")

    avg_price = cum_usd / cum_shares
    slip_bps = ((avg_price - best_price) / best_price) * 10000 if best_price > 0 else 0
    reason = "full" if cum_usd >= target_usd - 0.01 else "resized"

    return FillEstimate(
        ok=True,
        actual_usd=round(cum_usd, 2),
        shares=round(cum_shares, 2),
        avg_price=round(avg_price, 4),
        best_price=round(best_price, 4),
        slippage_bps=round(slip_bps, 1),
        spread=round(spread, 4),
        reason=reason,
    )


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
            market = _resilient_urlopen("polymarket_gamma", url, timeout=10)
            slug = market.get("slug", "") if market else ""
            if slug:
                result["market"] = get_market_microstructure(slug)
        except:
            pass
    else:
        # Get top liquid markets
        try:
            url = f"{GAMMA_API}/markets?active=true&closed=false&limit=10&_sort=liquidityNum&_order=desc"
            markets = _resilient_urlopen("polymarket_gamma", url, timeout=15) or []
            
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
