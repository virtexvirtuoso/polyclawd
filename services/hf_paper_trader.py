"""
HF Paper Trader — Bridges trigger signals to the paper portfolio.

Reads signals from:
1. HF engine trigger evaluations (memcached: polymarket:edge:*)
2. Virtuoso bridge directional signals
3. Negative vig scanner

Converts to paper_portfolio.open_position() format and tracks HF-specific
positions alongside the existing portfolio on portfolio.html.

All positions are tagged with archetype="hf_crypto" and strategy="hf_<trigger_type>".
"""

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger("hf_paper_trader")

# Add signals path for paper_portfolio import
SIGNALS_PATH = str(Path(__file__).parent.parent / "signals")
if SIGNALS_PATH not in sys.path:
    sys.path.insert(0, SIGNALS_PATH)

DB_PATH = os.getenv("HF_DB_PATH",
    str(Path(__file__).parent.parent / "storage" / "shadow_trades.db"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
HF_ENGINE_URL = "http://127.0.0.1:8422"
VIRTUOSO_EDGE_URL = "http://localhost:8002/api/polymarket/edge"


# ============================================================================
# Signal Sources
# ============================================================================

def _fetch_json(url: str, timeout: int = 5) -> Optional[Dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF-Paper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Fetch error {url}: {e}")
        return None


def get_trigger_signal() -> Optional[Dict]:
    """Read the current trigger signal from Virtuoso polymarket edge endpoint."""
    data = _fetch_json(VIRTUOSO_EDGE_URL)
    if not data:
        return None
    if data.get("action") != "TRADE":
        return None
    return data


def get_hf_engine_state() -> Optional[Dict]:
    """Read current HF engine state (Binance/Oracle prices)."""
    return _fetch_json(f"{HF_ENGINE_URL}/state")


def get_neg_vig_opportunities() -> List[Dict]:
    """Check for negative vig opportunities."""
    data = _fetch_json("http://127.0.0.1:8420/api/hf/negvig?threshold=0.995")
    if not data:
        return []
    return data.get("opportunities", [])


# ============================================================================
# Market Matching
# ============================================================================

def find_tradeable_market(asset: str, direction: str) -> Optional[Dict]:
    """Find the best 5/15-min market to trade for an asset+direction.
    
    Picks the market closest to resolution (most liquid, soonest expiry).
    """
    try:
        url = (f"{GAMMA_API}/events?active=true&closed=false&limit=50"
               f"&order=startDate&ascending=false")
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode())
        
        import re
        candidates = []
        
        for event in events:
            for market in event.get("markets", []):
                question = market.get("question", "")
                q_lower = question.lower()
                
                # Must match asset
                if asset == "BTC" and not any(w in q_lower for w in ["bitcoin", "btc"]):
                    continue
                if asset == "ETH" and not any(w in q_lower for w in ["ethereum", "eth"]):
                    continue
                
                # Must be short-duration (has time range in title)
                if not re.search(r'\d{1,2}:\d{2}\s*[AP]M', question, re.IGNORECASE):
                    continue
                
                # Must be "Up or Down" style
                if "up or down" not in q_lower and "up" not in q_lower:
                    continue
                
                # Parse prices
                try:
                    prices = json.loads(market.get("outcomePrices", "[0,0]"))
                    yes_price = float(prices[0]) if prices[0] else 0.5
                    no_price = float(prices[1]) if len(prices) > 1 and prices[1] else 0.5
                except:
                    yes_price, no_price = 0.5, 0.5
                
                # Parse token IDs
                try:
                    token_ids = json.loads(market.get("clobTokenIds", "[]"))
                except:
                    token_ids = []
                
                end_date = market.get("endDate", "")
                liquidity = float(market.get("liquidityNum", 0) or 0)
                
                candidates.append({
                    "market_id": market.get("conditionId", market.get("id", "")),
                    "question": question,
                    "slug": market.get("slug", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "liquidity": liquidity,
                    "end_date": end_date,
                    "clob_token_ids": token_ids,
                    "asset": asset,
                })
        
        if not candidates:
            return None
        
        # Pick: highest liquidity among soonest-expiring
        # Sort by end_date ascending (soonest first), then liquidity desc
        candidates.sort(key=lambda m: (m["end_date"], -m["liquidity"]))
        
        return candidates[0]
    
    except Exception as e:
        logger.error(f"Market matching error: {e}")
        return None


# ============================================================================
# Paper Position Opening
# ============================================================================

def open_hf_paper_position(
    market: Dict,
    direction: str,  # "UP" or "DOWN"
    trigger_type: str,
    confidence: float,
    edge_pct: float,
    kelly_fraction: float,
    strength: str = "medium",
) -> Dict:
    """Open a paper position via the existing paper_portfolio system.
    
    Converts HF signal format to paper_portfolio.open_position() format.
    """
    try:
        from paper_portfolio import open_position, get_portfolio_status
        
        status = get_portfolio_status()
        bankroll = status.get("bankroll", 10000)
        
        # Calculate bet size from Kelly fraction
        bet_size = bankroll * kelly_fraction
        bet_size = max(5.0, min(bet_size, bankroll * 0.15))  # Floor $5, cap 15% of bankroll
        
        # Map direction to side
        # "UP" → buy Yes side (price goes up)
        # "DOWN" → buy No side (or equivalently, bet on Down)
        if direction == "UP":
            side = "YES"
            entry_price = market["yes_price"]
        else:
            side = "NO"
            entry_price = market["no_price"]
        
        # Build signal dict in paper_portfolio format
        signal = {
            "market_id": market["market_id"],
            "market_title": market["question"],
            "title": market["question"],
            "platform": "polymarket",
            "side": side,
            "entry_price": entry_price,
            "confidence": confidence,
            "edge_pct": edge_pct,
            "bet_size": bet_size,
            "archetype": "hf_crypto",
            "strategy": f"hf_{trigger_type}",
            "market_slug": market.get("slug", ""),
        }
        
        result = open_position(signal)
        
        if result.get("opened"):
            logger.info(
                f"📈 HF PAPER TRADE: {direction} {market['asset']} "
                f"via {trigger_type} | ${bet_size:.2f} at {entry_price:.3f} "
                f"| conf:{confidence:.0%} edge:{edge_pct:.1%}"
            )
            
            # Also log to HF-specific table
            _log_hf_trade(market, direction, trigger_type, confidence,
                         edge_pct, bet_size, entry_price, strength)
        
        return result
    
    except Exception as e:
        logger.error(f"Paper position error: {e}")
        return {"opened": False, "reason": str(e)}


def _log_hf_trade(market, direction, trigger_type, confidence,
                  edge_pct, bet_size, entry_price, strength):
    """Log HF trade to dedicated table for performance tracking."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hf_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                asset TEXT,
                direction TEXT,
                trigger_type TEXT,
                strength TEXT,
                confidence REAL,
                edge_pct REAL,
                bet_size REAL,
                entry_price REAL,
                market_question TEXT,
                market_end_time TEXT,
                outcome TEXT,
                pnl REAL,
                opened_at TEXT,
                resolved_at TEXT
            )
        """)
        conn.execute(
            """INSERT INTO hf_paper_trades 
               (market_id, asset, direction, trigger_type, strength, confidence,
                edge_pct, bet_size, entry_price, market_question, market_end_time, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (market["market_id"], market["asset"], direction, trigger_type,
             strength, confidence, edge_pct, bet_size, entry_price,
             market["question"], market.get("end_date", ""),
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"HF trade log error: {e}")


# ============================================================================
# Main Processing Pipeline
# ============================================================================

def process_hf_signals() -> Dict:
    """
    Main pipeline: read all HF signal sources → match to markets → open paper positions.
    
    Called by the API endpoint or cron.
    """
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signals_checked": 0,
        "positions_opened": 0,
        "positions_skipped": 0,
        "details": [],
    }
    
    # ---- Source 1: Virtuoso trigger signal ----
    trigger = get_trigger_signal()
    if trigger:
        results["signals_checked"] += 1
        
        symbol = trigger.get("symbol", "BTCUSDT")
        asset = "BTC" if "BTC" in symbol else "ETH" if "ETH" in symbol else symbol[:3]
        direction = trigger.get("direction", "").upper()
        
        if direction in ("LONG", "UP"):
            direction = "UP"
        elif direction in ("SHORT", "DOWN"):
            direction = "DOWN"
        else:
            direction = None
        
        if direction:
            market = find_tradeable_market(asset, direction)
            if market:
                result = open_hf_paper_position(
                    market=market,
                    direction=direction,
                    trigger_type=trigger.get("trigger_type", "virtuoso_trigger"),
                    confidence=trigger.get("confidence", 0.6),
                    edge_pct=trigger.get("confidence", 0.6) - 0.5,  # Simple edge estimate
                    kelly_fraction=trigger.get("sizing", {}).get("kelly_fraction", 0.05),
                    strength=trigger.get("trigger_details", {}).get("strength", "medium"),
                )
                
                if result.get("opened"):
                    results["positions_opened"] += 1
                else:
                    results["positions_skipped"] += 1
                
                results["details"].append({
                    "source": "virtuoso_trigger",
                    "asset": asset,
                    "direction": direction,
                    "trigger_type": trigger.get("trigger_type"),
                    "result": "opened" if result.get("opened") else result.get("reason", "skipped"),
                })
            else:
                results["details"].append({
                    "source": "virtuoso_trigger",
                    "asset": asset,
                    "direction": direction,
                    "result": "no_market_found",
                })
    
    # ---- Source 2: Virtuoso bridge directional signals ----
    for asset in ["BTC", "ETH"]:
        try:
            bridge_data = _fetch_json(f"http://127.0.0.1:8420/api/hf/signal/{asset}")
            if bridge_data and bridge_data.get("should_trade"):
                results["signals_checked"] += 1
                direction = bridge_data.get("direction")
                
                if direction in ("UP", "DOWN"):
                    market = find_tradeable_market(asset, direction)
                    if market:
                        result = open_hf_paper_position(
                            market=market,
                            direction=direction,
                            trigger_type="virtuoso_bridge",
                            confidence=bridge_data.get("confidence", 50) / 100,
                            edge_pct=(bridge_data.get("confidence", 50) - 50) / 100,
                            kelly_fraction=bridge_data.get("suggested_kelly_fraction", 0.05),
                            strength=bridge_data.get("strength", "weak"),
                        )
                        
                        if result.get("opened"):
                            results["positions_opened"] += 1
                        else:
                            results["positions_skipped"] += 1
                        
                        results["details"].append({
                            "source": "virtuoso_bridge",
                            "asset": asset,
                            "direction": direction,
                            "confidence": bridge_data.get("confidence"),
                            "result": "opened" if result.get("opened") else result.get("reason", "skipped"),
                        })
        except Exception as e:
            logger.debug(f"Bridge signal check error ({asset}): {e}")
    
    # ---- Source 3: Negative vig (risk-free) ----
    neg_vig_opps = get_neg_vig_opportunities()
    for opp in neg_vig_opps[:3]:  # Max 3 neg vig positions
        results["signals_checked"] += 1
        
        # Neg vig: buy BOTH sides for < $1.00
        # For paper tracking, we log it as a YES position with guaranteed edge
        result = open_hf_paper_position(
            market={
                "market_id": opp.get("market_id", "unknown"),
                "question": opp.get("question", "Neg Vig Opportunity"),
                "slug": "",
                "yes_price": opp.get("yes_best_ask", 0.5),
                "no_price": opp.get("no_best_ask", 0.5),
                "liquidity": 0,
                "end_date": "",
                "asset": opp.get("asset", "BTC"),
            },
            direction="UP",  # Doesn't matter for neg vig — we profit either way
            trigger_type="neg_vig",
            confidence=0.99,
            edge_pct=opp.get("free_edge_pct", 1.0) / 100,
            kelly_fraction=0.15,  # Aggressive — it's risk-free
            strength="high",
        )
        
        if result.get("opened"):
            results["positions_opened"] += 1
        else:
            results["positions_skipped"] += 1
        
        results["details"].append({
            "source": "neg_vig",
            "edge_pct": opp.get("free_edge_pct"),
            "result": "opened" if result.get("opened") else result.get("reason", "skipped"),
        })
    
    return results


# ============================================================================
# HF Performance Stats
# ============================================================================

def get_hf_performance() -> Dict:
    """Get HF-specific paper trading performance."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Ensure table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hf_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT, asset TEXT, direction TEXT,
                trigger_type TEXT, strength TEXT, confidence REAL,
                edge_pct REAL, bet_size REAL, entry_price REAL,
                market_question TEXT, market_end_time TEXT,
                outcome TEXT, pnl REAL,
                opened_at TEXT, resolved_at TEXT
            )
        """)
        
        total = conn.execute("SELECT COUNT(*) FROM hf_paper_trades").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM hf_paper_trades WHERE outcome IS NOT NULL").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM hf_paper_trades WHERE pnl > 0").fetchone()[0]
        total_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM hf_paper_trades WHERE pnl IS NOT NULL").fetchone()[0]
        
        # By trigger type
        by_trigger = conn.execute("""
            SELECT trigger_type, COUNT(*) as cnt,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl), 0) as pnl
            FROM hf_paper_trades
            WHERE outcome IS NOT NULL
            GROUP BY trigger_type
        """).fetchall()
        
        # By asset
        by_asset = conn.execute("""
            SELECT asset, COUNT(*) as cnt,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl), 0) as pnl
            FROM hf_paper_trades
            WHERE outcome IS NOT NULL
            GROUP BY asset
        """).fetchall()
        
        # Recent trades
        recent = conn.execute("""
            SELECT * FROM hf_paper_trades ORDER BY opened_at DESC LIMIT 20
        """).fetchall()
        
        conn.close()
        
        return {
            "total_trades": total,
            "resolved": resolved,
            "open": total - resolved,
            "wins": wins,
            "win_rate_pct": round(wins / resolved * 100, 1) if resolved > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "by_trigger": [
                {"trigger": r["trigger_type"], "trades": r["cnt"],
                 "wins": r["wins"], "pnl": round(r["pnl"], 2),
                 "win_rate": round(r["wins"] / r["cnt"] * 100, 1) if r["cnt"] > 0 else 0}
                for r in by_trigger
            ],
            "by_asset": [
                {"asset": r["asset"], "trades": r["cnt"],
                 "wins": r["wins"], "pnl": round(r["pnl"], 2)}
                for r in by_asset
            ],
            "recent_trades": [dict(r) for r in recent],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    except Exception as e:
        logger.error(f"HF performance error: {e}")
        return {"error": str(e)}


# ============================================================================
# Auto-resolve HF positions
# ============================================================================

def resolve_hf_positions() -> Dict:
    """Check resolved markets and update HF paper trades with outcomes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Get unresolved HF trades
        open_trades = conn.execute(
            "SELECT * FROM hf_paper_trades WHERE outcome IS NULL"
        ).fetchall()
        
        resolved = 0
        
        for trade in open_trades:
            market_id = trade["market_id"]
            
            # Check if market has resolved via Gamma API
            try:
                url = f"{GAMMA_API}/markets?conditionId={market_id}&closed=true"
                req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    markets = json.loads(resp.read().decode())
                
                if not markets:
                    continue
                
                market = markets[0]
                prices = json.loads(market.get("outcomePrices", "[0,0]"))
                outcomes = json.loads(market.get("outcomes", '["Yes","No"]'))
                
                # Determine winner
                winner = None
                for i, p in enumerate(prices):
                    if float(p) > 0.95:
                        winner = outcomes[i] if i < len(outcomes) else None
                        break
                
                if winner is None:
                    continue
                
                # Map to Up/Down
                actual_direction = "UP" if winner.lower() in ("yes", "up") else "DOWN"
                
                # Calculate P&L
                trade_direction = trade["direction"]
                bet_size = trade["bet_size"]
                entry_price = trade["entry_price"]
                
                if trade_direction == actual_direction:
                    # Win: payout = bet_size * (1/entry_price - 1)
                    pnl = bet_size * (1.0 / entry_price - 1) if entry_price > 0 else 0
                else:
                    # Loss: lose bet
                    pnl = -bet_size
                
                conn.execute(
                    """UPDATE hf_paper_trades 
                       SET outcome = ?, pnl = ?, resolved_at = ?
                       WHERE id = ?""",
                    (actual_direction, round(pnl, 2),
                     datetime.now(timezone.utc).isoformat(), trade["id"])
                )
                resolved += 1
                
                logger.info(
                    f"{'✅' if pnl > 0 else '❌'} HF RESOLVED: {trade['asset']} "
                    f"{trade_direction} → {actual_direction} | "
                    f"P&L: ${pnl:+.2f} | {trade['trigger_type']}"
                )
            
            except Exception as e:
                logger.debug(f"Resolve check error for {market_id[:20]}: {e}")
                continue
        
        conn.commit()
        conn.close()
        
        return {
            "checked": len(open_trades),
            "resolved": resolved,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    except Exception as e:
        logger.error(f"HF resolve error: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Processing HF signals...")
    result = process_hf_signals()
    print(json.dumps(result, indent=2))
