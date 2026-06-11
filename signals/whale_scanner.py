#!/usr/bin/env python3
"""
Whale Shark Scanner — detect anomalous order book activity across Kalshi + Polymarket.

Sweeps all available markets, flags abnormal depth/imbalance, scores severity.
Designed to run every 5min via scheduler tick_5min() or as standalone CLI.

Usage:
    python3 signals/whale_scanner.py                          # full scan, all markets
    python3 signals/whale_scanner.py --platform kalshi        # Kalshi only
    python3 signals/whale_scanner.py --platform polymarket    # Polymarket only
    python3 signals/whale_scanner.py --min-score 5            # only HIGH/CRITICAL
    python3 signals/whale_scanner.py --json                   # JSON output for scheduling
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Kalshi ──────────────────────────────────────────────────────────────────
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

def kalshi_fetch(path: str) -> Optional[dict]:
    url = f"{KALSHI_API}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None

def get_kalshi_weather_series() -> list:
    """All open weather series."""
    data = kalshi_fetch("/series?limit=200")
    if not data: return []
    return [s for s in data.get("series", []) 
            if s["ticker"].startswith("KXHIGH") or s["ticker"].startswith("KXLOW")]

def get_kalshi_open_markets(series: str) -> list:
    data = kalshi_fetch(f"/markets?series_ticker={series}&status=open&limit=50")
    return data.get("markets", []) if data else []

def get_kalshi_orderbook(ticker: str) -> Optional[dict]:
    data = kalshi_fetch(f"/markets/{ticker}/orderbook")
    return data.get("orderbook_fp") if data else None

# ── Polymarket ──────────────────────────────────────────────────────────────
def get_pm_client():
    try:
        from polymarket_us import PolymarketUS
        return PolymarketUS()
    except ImportError:
        return None

def get_pm_prop_slugs(client) -> list:
    """Get all MLB prop market slugs for today."""
    try:
        resp = client.search.query({"query": "mlb 2026"})
        events = resp.get("events", [])
        slugs = []
        for ev in events:
            for m in ev.get("markets", []):
                slug = m.get("slug", "")
                if slug and ("strikeouts" in slug or "home_runs" in slug or "hits" in slug):
                    slugs.append(slug)
        return slugs
    except:
        return []

def get_pm_book(client, slug: str) -> Optional[dict]:
    try:
        return client.markets.book(slug)
    except:
        return None

# ── Whale Detection ────────────────────────────────────────────────────────

WHALE_LEVEL_JUMP = 1000    # shares — single level > 1K = whale
IMBALANCE_RATIO  = 3.0     # bid/ask ratio > 3:1
OI_SPIKE         = 500     # OI jump
CRITICAL_SCORE   = 8
HIGH_SCORE       = 5

def analyze_orderbook(book: dict, oi_history: Optional[dict] = None) -> dict:
    """
    Analyze order book for whale signals.
    Returns anomaly dict or None.
    """
    md = book.get("marketData", {})
    bids = md.get("bids", [])
    asks = md.get("asks", [])
    
    if not bids and not asks:
        return {"whale": False, "reason": "no_depth", "score": 0}
    
    # Convert
    bid_levels = [(float(b["px"]["value"]), float(b["qty"])) for b in bids]
    ask_levels = [(float(a["px"]["value"]), float(a["qty"])) for a in asks]
    
    total_bid_shares = sum(q for _, q in bid_levels)
    total_ask_shares = sum(q for _, q in ask_levels)
    
    # Find max single-level depth
    max_bid_level = max(q for _, q in bid_levels) if bid_levels else 0
    max_ask_level = max(q for _, q in ask_levels) if ask_levels else 0
    max_level = max(max_bid_level, max_ask_level)
    
    # Bid/ask ratio
    ratio = total_bid_shares / total_ask_shares if total_ask_shares > 0 else float('inf')
    
    # Score components
    score = 0
    reasons = []
    
    # 1. Whale-sized single level
    if max_level >= WHALE_LEVEL_JUMP:
        score += 4
        reasons.append(f"whale_level_{max_level:.0f}sh")
    elif max_level >= WHALE_LEVEL_JUMP // 2:
        score += 2
        reasons.append(f"big_level_{max_level:.0f}sh")
    
    # 2. Extreme imbalance
    if ratio > IMBALANCE_RATIO or ratio < 1/IMBALANCE_RATIO:
        score += 3
        side = "BID-heavy" if ratio > IMBALANCE_RATIO else "ASK-heavy"
        reasons.append(f"{side}_{ratio:.1f}x")
    
    # 3. One-sided market (no asks at all)
    if total_ask_shares == 0 and total_bid_shares > 0:
        score += 3
        reasons.append("zero_asks")
    elif total_bid_shares == 0 and total_ask_shares > 0:
        score += 3
        reasons.append("zero_bids")
    
    # 4. Tiny total depth (thin market anomaly is more significant)
    total_dollars = total_bid_shares + total_ask_shares
    if total_dollars < 500 and max_level > 200:
        score += 2  # whale in an empty market
        reasons.append("thin_market_whale")
    
    # 5. OI spike (if we have history)
    slug = md.get("marketSlug", "")
    if oi_history and slug in oi_history:
        last_oi = oi_history[slug]
        current_oi = float(md.get("openInterest", {}).get("value", 0))
        oi_delta = current_oi - last_oi
        if oi_delta > OI_SPIKE:
            score += 3
            reasons.append(f"oi_spike_{oi_delta:.0f}")
        elif oi_delta > OI_SPIKE // 2:
            score += 1
            reasons.append(f"oi_move_{oi_delta:.0f}")
    
    return {
        "whale": score >= 3,
        "score": score,
        "severity": "CRITICAL" if score >= CRITICAL_SCORE else "HIGH" if score >= HIGH_SCORE else "LOW",
        "reasons": reasons,
        "bid_depth": total_bid_shares,
        "ask_depth": total_ask_shares,
        "bid_dollars": sum(px * q for px, q in bid_levels),
        "ask_dollars": sum(px * q for px, q in ask_levels),
        "max_level_shares": max_level,
        "ratio": ratio,
        "total_shares": total_dollars,
    }

def scan_kalshi_weather() -> list:
    """Scan Kalshi weather markets for whale activity."""
    alerts = []
    series = get_kalshi_weather_series()
    
    for s in series[:30]:  # limit to stay within rate limits
        ticker = s["ticker"]
        title = s.get("title", ticker)[:40]
        markets = get_kalshi_open_markets(ticker)
        
        for m in markets:
            mt = m["ticker"]
            ob = get_kalshi_orderbook(mt)
            if not ob:
                continue
            
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            
            total_bid = sum(float(b[1]) for b in bids)
            total_ask = sum(float(a[1]) for a in asks)
            max_bid = max((float(b[1]) for b in bids), default=0)
            max_ask = max((float(a[1]) for a in asks), default=0)
            max_level = max(max_bid, max_ask)
            ratio = total_bid / total_ask if total_ask > 0 else float('inf')
            
            score = 0
            reasons = []
            
            if max_level >= 500:
                score += 3
                reasons.append(f"level_{max_level:.0f}")
            if ratio > 3 or ratio < 0.33:
                score += 2
                reasons.append(f"imbalance_{ratio:.1f}x")
            if total_bid == 0 and total_ask > 0:
                score += 2
                reasons.append("no_bids")
            if total_ask == 0 and total_bid > 0:
                score += 2
                reasons.append("no_asks")
            
            if score >= 3:
                # Get prices
                best_bid = float(bids[0][0]) if bids else 0
                best_ask = float(asks[0][0]) if asks else 0
                alerts.append({
                    "platform": "kalshi",
                    "market": mt,
                    "title": title,
                    "score": score,
                    "severity": "CRITICAL" if score >= 8 else "HIGH" if score >= 5 else "LOW",
                    "reasons": ",".join(reasons),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "mid": (best_bid + best_ask) / 2 if best_bid or best_ask else 0,
                    "bid_depth": total_bid,
                    "ask_depth": total_ask,
                    "max_level": max_level,
                    "ratio": round(ratio, 2),
                    "scan_time": datetime.now(timezone.utc).isoformat(),
                })
    
    return alerts

def scan_polymarket_props(oi_state: dict = None) -> list:
    """Scan Polymarket MLB props for whale activity."""
    alerts = []
    client = get_pm_client()
    if not client:
        return []
    
    slugs = get_pm_prop_slugs(client)
    for slug in slugs[:30]:  # limit
        book = get_pm_book(client, slug)
        if not book:
            continue
        
        result = analyze_orderbook(book, oi_state)
        if result.get("whale"):
            md = book.get("marketData", {})
            cur = md.get("currentPx", {}).get("value", "0")
            oi = md.get("openInterest", {}).get("value", "0")
            alerts.append({
                "platform": "polymarket",
                "market": slug,
                "score": result["score"],
                "severity": result["severity"],
                "reasons": ",".join(result["reasons"]),
                "current_price": float(cur),
                "open_interest": oi,
                "bid_depth": result["bid_depth"],
                "ask_depth": result["ask_depth"],
                "ratio": result["ratio"],
                "max_level": result["max_level_shares"],
                "scan_time": datetime.now(timezone.utc).isoformat(),
            })
    
    return alerts

def format_alert(a: dict) -> str:
    """Format a whale alert for Telegram."""
    sev_icon = "🚨" if a["severity"] == "CRITICAL" else "⚠️" if a["severity"] == "HIGH" else "🐟"
    plat_icon = "📊" if a["platform"] == "kalshi" else "🔵"
    
    lines = [
        f"{sev_icon} {a['severity']} | {plat_icon} {a['platform'].upper()}",
        f"Market: {a['market']}",
        f"Score: {a['score']}/10 | {a['reasons']}",
    ]
    
    if "best_bid" in a:
        lines.append(f"Bid/Ask: {a['best_bid']:.3f} / {a['best_ask']:.3f} (mid={a['mid']:.3f})")
    if "current_price" in a:
        lines.append(f"Price: {a['current_price']:.3f} | OI: {a.get('open_interest', '?')}")
    if "bid_depth" in a:
        lines.append(f"Depth: {a['bid_depth']:.0f}B / {a['ask_depth']:.0f}A ({a['ratio']:.1f}x)")
    
    if a["platform"] == "kalshi":
        dt = a.get("title", "")
        lines.append(f"Link: https://kalshi.com/markets/{a['market'].split('-')[0]}")
    else:
        lines.append(f"Link: https://polymarket.com/event/{a['market']}")
    
    return "\n".join(lines)

def run_scan() -> list:
    """Run full scan across all platforms. Returns sorted alerts."""
    alerts = []
    
    print("Scanning Kalshi weather...", flush=True)
    alerts.extend(scan_kalshi_weather())
    
    print(f"Scanning Polymarket props...", flush=True)
    alerts.extend(scan_polymarket_props())
    
    # Sort by score descending
    alerts.sort(key=lambda a: a["score"], reverse=True)
    return alerts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=int, default=3, help="Minimum anomaly score")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--platform", choices=["kalshi", "polymarket", "all"], default="all")
    args = parser.parse_args()
    
    alerts = run_scan()
    filtered = [a for a in alerts if a["score"] >= args.min_score]
    
    if args.json:
        print(json.dumps(filtered, indent=2))
    else:
        print(f"\n=== WHALE SCAN: {len(filtered)} alerts (score >= {args.min_score}) ===\n")
        for a in filtered:
            print(format_alert(a))
            print()