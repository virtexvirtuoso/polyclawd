"""
Crypto Price Signal — Uses Virtuoso VPS infrastructure to evaluate
prediction market crypto price targets.

Sources:
  - BTC Wiz (port 8004): On-chain metrics, composite signal, cycle phase,
    technicals, ETF flows, macro, derivatives
  - Derivatives API (port 8003): Funding rates, OI, L/S ratio, CVD, fusion signal
  - Virtuoso MCP: Market overview, regime

For markets like "Will BTC reach $75K in March?" — we combine:
  1. Current price + distance to target (%)
  2. Composite on-chain signal (27 metrics, 0-100 scale)
  3. Cycle phase + conviction
  4. Technical momentum (RSI, MACD, SMAs)
  5. Derivatives positioning (funding, OI trend, L/S ratio)
  6. ETF flow momentum
  7. Macro backdrop

Outputs a directional probability + confidence for the paper portfolio.
"""

import re
import math
from datetime import datetime, timezone
from loguru import logger

try:
    import httpx
except ImportError:
    import urllib.request
    import json
    httpx = None

# ── Config ──────────────────────────────────────────────────────────
BTC_WIZ = "http://localhost:8004"
DERIV_API = "http://localhost:8003"
CACHE_TTL = 300  # 5min
_cache = {}


def _fetch(url, timeout=10):
    """Fetch JSON from local API."""
    cache_key = url
    now = datetime.now(timezone.utc).timestamp()
    if cache_key in _cache and now - _cache[cache_key][0] < CACHE_TTL:
        return _cache[cache_key][1]
    
    try:
        if httpx:
            with httpx.Client(timeout=timeout) as c:
                r = c.get(url)
                r.raise_for_status()
                data = r.json()
        else:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        _cache[cache_key] = (now, data)
        return data
    except Exception as e:
        logger.debug("Crypto signal fetch failed {}: {}", url, e)
        return None


def _get_btc_price():
    """Get current BTC price from technicals."""
    data = _fetch(f"{BTC_WIZ}/metrics/technical")
    if data and data.get("sma_7"):
        return data.get("sma_7")  # 7-day SMA as current proxy
    return None


def _parse_crypto_target(title: str):
    """Parse market title to extract asset, target price, direction, and deadline.
    
    Examples:
      "Will Bitcoin reach $75,000 in March?" → (BTC, 75000, 'above', 'March')
      "Will Bitcoin dip to $55,000 in March?" → (BTC, 55000, 'below', 'March')
      "Will Ethereum hit $5,000 by end of March?" → (ETH, 5000, 'above', 'March')
      "Will BTC be above $80,000 on March 31?" → (BTC, 80000, 'above', 'March 31')
    """
    t = title.lower()
    
    # Asset detection
    asset = None
    if any(w in t for w in ['bitcoin', 'btc']):
        asset = 'BTC'
    elif any(w in t for w in ['ethereum', 'eth']):
        asset = 'ETH'
    elif any(w in t for w in ['solana', 'sol']):
        asset = 'SOL'
    
    if not asset:
        return None
    
    # Price target (handles $1m, $100k abbreviations)
    price_match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)([mkbMKB])?(?![a-zA-Z])", title)
    if not price_match:
        return None
    target = float(price_match.group(1).replace(',', ''))
    suffix = (price_match.group(2) or '').lower()
    if suffix == 'k':
        target *= 1_000
    elif suffix == 'm':
        target *= 1_000_000
    elif suffix == 'b':
        target *= 1_000_000_000
    
    # Direction
    direction = 'above'  # default
    if any(w in t for w in ['dip', 'drop', 'fall', 'below', 'crash', 'under']):
        direction = 'below'
    elif any(w in t for w in ['reach', 'hit', 'above', 'over', 'surpass']):
        direction = 'above'
    
    # Deadline (rough)
    deadline = None
    month_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)', t)
    if month_match:
        deadline = month_match.group(1)
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', t)
    if date_match:
        deadline = date_match.group(1)
    
    return {
        'asset': asset,
        'target': target,
        'direction': direction,
        'deadline': deadline,
    }




def _get_eth_price():
    """Get current ETH price from Bybit public API."""
    cache_key = "eth_price"
    if cache_key in _cache:
        ts, val = _cache[cache_key]
        if datetime.now(timezone.utc).timestamp() - ts < CACHE_TTL:
            return val
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
        price = float(data["result"]["list"][0]["lastPrice"])
        _cache[cache_key] = (datetime.now(timezone.utc).timestamp(), price)
        return price
    except Exception as e:
        logger.debug("Failed to get ETH price: {}", e)
        return None



def _get_sol_price():
    """Get current SOL price from Bybit public API."""
    cache_key = "sol_price"
    if cache_key in _cache:
        ts, val = _cache[cache_key]
        if datetime.now(timezone.utc).timestamp() - ts < CACHE_TTL:
            return val
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            "https://api.bybit.com/v5/market/tickers?category=linear&symbol=SOLUSDT",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
        price = float(data["result"]["list"][0]["lastPrice"])
        _cache[cache_key] = (datetime.now(timezone.utc).timestamp(), price)
        return price
    except Exception as e:
        logger.debug("Failed to get SOL price: {}", e)
        return None


def evaluate_crypto_price_market(title: str, market_price: float, side: str = "NO"):
    """
    Evaluate a crypto price prediction market using VPS infrastructure.
    
    Returns:
        dict with 'confidence', 'edge', 'signals_used', 'reasoning' or None if not applicable.
    """
    parsed = _parse_crypto_target(title)
    if not parsed:
        return None
    
    asset = parsed['asset']
    target = parsed['target']
    direction = parsed['direction']
    
    # BTC has full infrastructure (BTC Wiz + Derivatives), ETH has Derivatives only
    if asset == 'BTC':
        current_price = _get_btc_price()
    elif asset == 'ETH':
        current_price = _get_eth_price()
    elif asset == 'SOL':
        current_price = _get_sol_price()
    else:
        logger.debug("Crypto signal: {} not supported (BTC/ETH/SOL only)", asset)
        return None
    if not current_price or current_price <= 0:
        return None
    
    # ── Distance to target ──────────────────────────────────────────
    distance_pct = (target - current_price) / current_price * 100
    # For "above" markets: positive distance = target is above current price
    # For "below" markets: negative distance = target is below current price
    
    signals = {}
    reasoning = []
    
    # ── 1. Price Distance (strongest signal) ────────────────────────
    abs_distance = abs(distance_pct)
    if direction == 'above':
        # Probability of reaching target (higher = YES more likely)
        if distance_pct <= 0:
            # Already above target
            distance_prob = 0.90
            reasoning.append(f"Price ${current_price:,.0f} already above ${target:,.0f}")
        elif abs_distance < 5:
            distance_prob = 0.65
            reasoning.append(f"Target ${target:,.0f} is {abs_distance:.1f}% away (close)")
        elif abs_distance < 15:
            distance_prob = 0.40
            reasoning.append(f"Target ${target:,.0f} is {abs_distance:.1f}% away (moderate)")
        elif abs_distance < 30:
            distance_prob = 0.20
            reasoning.append(f"Target ${target:,.0f} is {abs_distance:.1f}% away (far)")
        else:
            distance_prob = 0.08
            reasoning.append(f"Target ${target:,.0f} is {abs_distance:.1f}% away (very far)")
    else:  # below/dip
        if distance_pct >= 0:
            # Already below target
            distance_prob = 0.90
            reasoning.append(f"Price ${current_price:,.0f} already below ${target:,.0f}")
        elif abs_distance < 5:
            distance_prob = 0.55
            reasoning.append(f"Dip target ${target:,.0f} is {abs_distance:.1f}% away (close)")
        elif abs_distance < 15:
            distance_prob = 0.30
            reasoning.append(f"Dip target ${target:,.0f} is {abs_distance:.1f}% away (moderate)")
        elif abs_distance < 30:
            distance_prob = 0.15
            reasoning.append(f"Dip target ${target:,.0f} is {abs_distance:.1f}% away (far)")
        else:
            distance_prob = 0.05
            reasoning.append(f"Dip target ${target:,.0f} is {abs_distance:.1f}% away (very far)")
    
    signals['distance'] = distance_prob
    
    # ── 2. On-Chain Composite (BTC Wiz — BTC only) ─────────────────
    composite = _fetch(f"{BTC_WIZ}/signals/composite") if asset == 'BTC' else None
    if composite and composite.get("data"):
        cd = composite["data"]
        score = cd.get("score", 50)  # 0-100, >60 = bullish
        conviction = cd.get("conviction_score", 0.5)
        
        # Map composite to directional probability adjustment
        # Score 80+ = strongly bullish, 20- = strongly bearish
        bullish_factor = (score - 50) / 50  # -1 to +1
        
        if direction == 'above':
            composite_adj = bullish_factor * 0.15  # ±15% adjustment
        else:
            composite_adj = -bullish_factor * 0.15  # bearish = more likely to dip
        
        signals['composite'] = {'score': score, 'conviction': conviction, 'adj': composite_adj}
        signal_label = cd.get("signal_type", "NEUTRAL")
        reasoning.append(f"On-chain composite: {score:.0f}/100 ({signal_label}, conviction {conviction:.0%})")
    else:
        composite_adj = 0
    
    # ── 3. Cycle Phase (BTC Wiz — BTC only) ────────────────────────
    cycle = _fetch(f"{BTC_WIZ}/cycle/phase") if asset == 'BTC' else None
    if cycle and cycle.get("data"):
        phase = cycle["data"].get("phase", "UNKNOWN")
        phase_conf = cycle["data"].get("phase_confidence", 0)
        
        phase_multipliers = {
            'ACCUMULATION': -0.05,  # less likely to reach highs
            'MARKUP': 0.08,         # trending up
            'DISTRIBUTION': -0.03,  # topping
            'MARKDOWN': -0.10,      # trending down
        }
        cycle_adj = phase_multipliers.get(phase, 0)
        if direction == 'below':
            cycle_adj = -cycle_adj  # flip for dip targets
        
        signals['cycle'] = {'phase': phase, 'confidence': phase_conf, 'adj': cycle_adj}
        reasoning.append(f"Cycle: {phase} ({phase_conf:.0f}% conf)")
    else:
        cycle_adj = 0
    
    # ── 4. Technical Momentum (BTC Wiz — BTC only) ────────────────
    tech = _fetch(f"{BTC_WIZ}/metrics/technical") if asset == 'BTC' else None
    if tech:
        rsi = tech.get("rsi", 50)
        macd_hist = tech.get("macd_histogram", 0)
        sma_50 = tech.get("sma_50", current_price)
        sma_200 = tech.get("sma_200", current_price)
        
        # RSI momentum
        if rsi > 70:
            tech_adj = 0.05 if direction == 'above' else -0.03
            reasoning.append(f"RSI {rsi:.0f} (overbought, momentum UP)")
        elif rsi < 30:
            tech_adj = -0.05 if direction == 'above' else 0.05
            reasoning.append(f"RSI {rsi:.0f} (oversold, momentum DOWN)")
        else:
            tech_adj = 0
            reasoning.append(f"RSI {rsi:.0f} (neutral)")
        
        # MACD direction
        if macd_hist > 0:
            tech_adj += 0.03 if direction == 'above' else -0.02
        elif macd_hist < 0:
            tech_adj += -0.03 if direction == 'above' else 0.02
        
        # Price vs SMAs (trend)
        above_50 = current_price > sma_50
        above_200 = current_price > sma_200
        if above_50 and above_200:
            tech_adj += 0.05 if direction == 'above' else -0.03
            reasoning.append("Above 50 & 200 SMA (bullish trend)")
        elif not above_50 and not above_200:
            tech_adj += -0.05 if direction == 'above' else 0.05
            reasoning.append("Below 50 & 200 SMA (bearish trend)")
        else:
            reasoning.append("Mixed SMA signals")
        
        signals['technical'] = {'rsi': rsi, 'macd_hist': macd_hist, 'adj': tech_adj}
    else:
        tech_adj = 0
    
    # ── 5. Derivatives Positioning ──────────────────────────────────
    if asset == 'BTC':
        derivs = _fetch(f"{BTC_WIZ}/derivatives")
    else:
        # ETH/SOL: use Derivatives API (port 8003) directly
        derivs = None
        _symbol = "ETHUSDT" if asset == "ETH" else "SOLUSDT"
        _eth_derivs = _fetch(f"{DERIV_API}/signals/fusion/{_symbol}")
        if _eth_derivs and _eth_derivs.get("signal"):
            sig = _eth_derivs["signal"]
            _lsr = _fetch(f"{DERIV_API}/signals/long-short-ratio/{_symbol}")
            _fr = _fetch(f"{DERIV_API}/signals/funding-rate/{_symbol}")
            long_pct = 0.5
            current_rate = None
            if _lsr and _lsr.get("signal"):
                ratio = _lsr["signal"].get("value", 1.0)
                long_pct = ratio / (1 + ratio) if ratio else 0.5
            if _fr and _fr.get("signal"):
                current_rate = _fr["signal"].get("value")
            derivs = {"data": {
                "long_short_ratio": {"long_pct": long_pct},
                "funding_rates": {"current": current_rate},
                "open_interest": {"trend": "stable"},
            }}
    deriv_adj = 0
    if derivs and derivs.get("data"):
        dd = derivs["data"]
        ls = dd.get("long_short_ratio", {})
        long_pct = ls.get("long_pct", 0.5)
        
        funding = dd.get("funding_rates", {})
        current_rate = None
        if isinstance(funding, dict):
            current_rate = funding.get("current")
        
        oi_trend = dd.get("open_interest", {}).get("trend", "stable")
        
        # High longs + positive funding = crowded long (contrarian bearish)
        if long_pct > 0.55 and current_rate and current_rate > 0.01:
            deriv_adj = -0.05 if direction == 'above' else 0.03
            reasoning.append(f"Crowded long ({long_pct:.0%}), funding {current_rate:.4f}")
        elif long_pct < 0.45:
            deriv_adj = 0.03 if direction == 'above' else -0.02
            reasoning.append(f"Shorts dominant ({1-long_pct:.0%})")
        else:
            reasoning.append(f"L/S balanced ({long_pct:.0%} long)")
        
        signals['derivatives'] = {'long_pct': long_pct, 'funding': current_rate, 'oi_trend': oi_trend, 'adj': deriv_adj}
    
    # ── 6. ETF Flows (BTC only — no ETH ETF data yet) ─────────────
    etf = _fetch(f"{BTC_WIZ}/etf/flows") if asset == 'BTC' else None
    etf_adj = 0
    if etf:
        flow_7d = etf.get("flow_7d", 0)
        if isinstance(flow_7d, str):
            flow_7d = float(flow_7d)
        
        if flow_7d > 500_000_000:  # >$500M inflows
            etf_adj = 0.05 if direction == 'above' else -0.03
            reasoning.append(f"ETF 7d inflows: ${flow_7d/1e6:.0f}M (bullish)")
        elif flow_7d < -500_000_000:
            etf_adj = -0.05 if direction == 'above' else 0.03
            reasoning.append(f"ETF 7d outflows: ${flow_7d/1e6:.0f}M (bearish)")
        else:
            reasoning.append(f"ETF 7d flows: ${flow_7d/1e6:.0f}M (neutral)")
        
        signals['etf'] = {'flow_7d': flow_7d, 'adj': etf_adj}
    
    # ── 7. Macro Backdrop (BTC Wiz — shared macro, BTC only endpoint)
    macro = _fetch(f"{BTC_WIZ}/macro") if asset == 'BTC' else None
    macro_adj = 0
    if macro:
        macro_signal = macro.get("composite_signal", 50)
        if macro_signal > 60:
            macro_adj = 0.03 if direction == 'above' else -0.02
            reasoning.append(f"Macro: {macro_signal:.0f}/100 (supportive)")
        elif macro_signal < 40:
            macro_adj = -0.03 if direction == 'above' else 0.02
            reasoning.append(f"Macro: {macro_signal:.0f}/100 (headwind)")
        else:
            reasoning.append(f"Macro: {macro_signal:.0f}/100 (neutral)")
        
        signals['macro'] = {'signal': macro_signal, 'adj': macro_adj}
    
    # ── Combine: P(YES) ─────────────────────────────────────────────
    # Start with distance-based probability, adjust with signals
    p_yes = distance_prob + composite_adj + cycle_adj + tech_adj + deriv_adj + etf_adj + macro_adj
    p_yes = max(0.03, min(0.97, p_yes))  # clamp
    
    # Our confidence for the given side
    if side == "YES":
        confidence = p_yes
        edge = confidence - market_price
    else:
        confidence = 1 - p_yes  # P(NO) = 1 - P(YES)
        edge = confidence - (1 - market_price)
    
    n_signals = sum(1 for k in ['composite', 'cycle', 'technical', 'derivatives', 'etf', 'macro'] if k in signals)
    
    result = {
        'confidence': round(confidence, 4),
        'edge': round(edge, 4),
        'p_yes': round(p_yes, 4),
        'current_price': round(current_price, 2),
        'target': target,
        'distance_pct': round(distance_pct, 2),
        'direction': direction,
        'asset': parsed['asset'],
        'signals_used': n_signals + 1,  # +1 for distance
        'reasoning': reasoning,
        'signal_details': {k: v for k, v in signals.items() if k != 'distance'},
    }
    
    logger.info("Crypto signal: {} | side={} p_yes={:.0%} conf={:.0%} edge={:.1%} | {} signals | ${:,.0f} → ${:,.0f} ({:+.1f}%)",
                title[:50], side, p_yes, confidence, edge, n_signals + 1,
                current_price, target, distance_pct)
    
    return result


def get_crypto_price_signals(markets: list = None):
    """
    Scan prediction markets for crypto price targets and evaluate them.
    Returns list of signal dicts compatible with paper_portfolio.process_signals().
    """
    if not markets:
        return []
    
    results = []
    for m in markets:
        title = m.get("title") or m.get("market_title") or ""
        market_price = m.get("price") or m.get("yes_price") or 0.5
        market_id = m.get("market_id") or m.get("id") or ""
        
        # Try both sides
        for side in ["YES", "NO"]:
            eval_result = evaluate_crypto_price_market(title, market_price, side)
            if eval_result and eval_result['edge'] > 0.08:  # 8% min edge
                results.append({
                    'market_id': market_id,
                    'market': title,
                    'market_title': title,
                    'side': side,
                    'price': market_price,
                    'confidence': eval_result['confidence'],
                    'edge': eval_result['edge'],
                    'edge_pct': eval_result['edge'],
                    'archetype': 'crypto_price',
                    'strategy': 'crypto_price',
                    'signals_used': eval_result['signals_used'],
                    'reasoning': eval_result['reasoning'],
                    'days_to_close': m.get('days_to_close', 30),
                    'end_date': m.get('end_date'),
                })
        
    # Sort by edge, return top signals
    results.sort(key=lambda x: x['edge'], reverse=True)
    return results[:6]


if __name__ == "__main__":
    # Quick test
    test_markets = [
        {"title": "Will Bitcoin reach $75,000 in March?", "price": 0.15, "market_id": "test1"},
        {"title": "Will Bitcoin dip to $55,000 in March?", "price": 0.08, "market_id": "test2"},
        {"title": "Will Bitcoin reach $100,000 in March?", "price": 0.03, "market_id": "test3"},
    ]
    
    for m in test_markets:
        print(f"\n{'='*60}")
        print(f"Market: {m['title']} (YES @ {m['price']})")
        for side in ['YES', 'NO']:
            r = evaluate_crypto_price_market(m['title'], m['price'], side)
            if r:
                print(f"  {side}: conf={r['confidence']:.0%}, edge={r['edge']:+.1%}, p_yes={r['p_yes']:.0%}")
                for line in r['reasoning']:
                    print(f"    • {line}")
