"""
Cross-Platform Edge → Paper Portfolio Signal Bridge

Converts LLM-verified cross-platform edges into signals
compatible with paper_portfolio.process_signals().

Strategy: Buy on the platform where the market is cheapest
relative to cross-platform consensus.
"""

from loguru import logger


def edges_to_signals(edges: list, min_spread: float = 0.05, max_signals: int = 5) -> list:
    """
    Convert edge scan results to paper portfolio signals.

    For each verified edge with 2 markets:
    - Take the cheaper side (buy YES where prob is lowest)
    - Use cross-platform average as confidence estimate
    - Only produce signals for Polymarket or Kalshi markets (resolvable)

    Args:
        edges: List of edge dicts from cross_platform_edge.scan()
        min_spread: Minimum spread to consider (default 5%)
        max_signals: Max signals to return

    Returns:
        List of signal dicts ready for process_signals()
    """
    signals = []

    for edge in edges:
        spread_str = edge.get("spread", "0%")
        if isinstance(spread_str, str):
            spread = float(spread_str.replace("%", "")) / 100
        else:
            spread = float(spread_str)

        if spread < min_spread:
            continue

        markets = edge.get("markets", [])
        if len(markets) != 2:
            continue

        # Determine which market to buy on (cheapest YES)
        m_a, m_b = markets[0], markets[1]

        def _prob(m):
            p = m.get("prob_raw") or m.get("probability", 0)
            if isinstance(p, str):
                p = float(p.replace("%", "")) / 100
            return float(p)

        prob_a = _prob(m_a)
        prob_b = _prob(m_b)

        # Cross-platform consensus (average)
        consensus = (prob_a + prob_b) / 2

        # Buy on the platform where YES is cheapest
        if prob_a <= prob_b:
            buy_market = m_a
            buy_price = prob_a
            ref_market = m_b
            ref_price = prob_b
        else:
            buy_market = m_b
            buy_price = prob_b
            ref_market = m_a
            ref_price = prob_a

        platform = buy_market.get("platform", "").lower()
        # Only trade on platforms we can resolve
        if platform not in ("polymarket", "kalshi"):
            # Try the other side
            if ref_market.get("platform", "").lower() in ("polymarket", "kalshi"):
                # Buy NO on the expensive side instead
                buy_market, ref_market = ref_market, buy_market
                buy_price, ref_price = ref_price, buy_price
                platform = buy_market.get("platform", "").lower()
                # Flip: buy NO on expensive side
                side = "NO"
                entry_price = buy_price  # YES price (NO cost = 1 - this)
            else:
                continue
        else:
            side = "YES"
            entry_price = buy_price

        # Skip near-zero or near-one prices
        effective_price = entry_price if side == "YES" else (1 - entry_price)
        if effective_price < 0.03 or effective_price > 0.97:
            continue

        market_id = buy_market.get("market_id", "")
        if not market_id:
            continue

        topic = edge.get("topic", "cross_platform")
        rec = edge.get("recommendation", "")
        is_inverted = "[INV]" in rec

        # For cross-platform edges, confidence = the OTHER platform's price
        # (we trust the more liquid/established platform's price as "true" probability)
        # This makes the edge = |confidence - entry_price| = the spread itself
        ref_vol = ref_market.get("volume") or 0
        buy_vol = buy_market.get("volume") or 0
        # Use the higher-volume platform's price as confidence
        if ref_vol >= buy_vol:
            confidence = ref_price
        else:
            confidence = buy_price + spread  # adjust up by spread

        # Clamp confidence to valid range
        confidence = max(0.05, min(0.95, confidence))

        # Parse days_to_close from endDate if available
        end_date_raw = buy_market.get("endDate") or buy_market.get("end_date") or ""
        days_to_close = 999  # default unknown = very far out
        if end_date_raw:
            try:
                from datetime import datetime, timezone
                edt = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
                days_to_close = max(0.1, (edt - datetime.now(timezone.utc)).total_seconds() / 86400)
            except Exception:
                pass

        signal = {
            "market_id": market_id,
            "market_title": buy_market.get("title", "")[:120],
            "market": buy_market.get("title", "")[:120],
            "title": buy_market.get("title", "")[:120],
            "side": side,
            "confidence": round(confidence, 4),
            "entry_price": round(entry_price, 4),
            "price": round(entry_price, 4),
            "platform": platform,
            "strategy": "cross_platform_edge",
            "volume": buy_market.get("volume") or 0,
            "archetype": "other",
            "source": "cross_platform_edge",
            "edge_spread": round(spread, 4),
            "edge_topic": topic,
            "ref_platform": ref_market.get("platform", ""),
            "ref_price": round(ref_price, 4),
            "inverted": is_inverted,
            "days_to_close": round(days_to_close, 1),
            "end_date": end_date_raw,
        }

        logger.info(
            "🔀 Edge signal: %s %s on %s @ %.1f%% (vs %s @ %.1f%%, spread=%.1f%%)",
            side, market_id[:30], platform, entry_price * 100,
            ref_market.get("platform", ""), ref_price * 100, spread * 100,
        )

        signals.append(signal)

        if len(signals) >= max_signals:
            break

    return signals


def scan_and_generate_signals(force_refresh: bool = False, min_spread: float = 0.05) -> list:
    """
    Run edge scan and return paper-portfolio-ready signals.
    Uses cached scan results if available (6h TTL).
    """
    try:
        from api.services.cross_platform_edge import scanner
        result = scanner.scan(force_refresh=force_refresh)
        edges = result.get("edges", [])
        if not edges:
            logger.debug("Cross-platform edge scan: no edges found")
            return []
        return edges_to_signals(edges, min_spread=min_spread)
    except Exception as e:
        logger.error("Cross-platform edge scan failed: {}", e)
        return []
