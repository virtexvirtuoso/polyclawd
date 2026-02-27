"""
HF Data Collector — Phase 4A

Collects and stores:
1. 5/15-min market resolutions from Polymarket (actual outcomes)
2. Periodic snapshots of Binance vs Chainlink divergence
3. Virtuoso signal snapshots at market open time

This data feeds the Monte Carlo backtester.

Runs as a background task inside the HF engine or standalone.
"""

import json
import logging
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("hf_collector")

GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = os.getenv("HF_DB_PATH",
    str(Path(__file__).parent.parent / "storage" / "shadow_trades.db"))


# ============================================================================
# Database Setup
# ============================================================================

def _get_db() -> sqlite3.Connection:
    """Get SQLite connection with tables created."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    
    # Market resolutions — the ground truth for backtesting
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hf_market_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            condition_id TEXT,
            question TEXT,
            asset TEXT,
            duration TEXT,
            outcome TEXT,  -- 'Up' or 'Down'
            yes_price_at_open REAL,
            no_price_at_open REAL,
            volume REAL,
            liquidity REAL,
            start_time TEXT,
            end_time TEXT,
            resolved_at TEXT,
            collected_at TEXT,
            UNIQUE(market_id)
        )
    """)
    
    # Divergence snapshots — periodic Binance vs Oracle state captures
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hf_divergence_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            binance_price REAL,
            oracle_price REAL,
            divergence_pct REAL,
            oracle_age_seconds REAL,
            snapshot_at TEXT
        )
    """)
    
    # Signal snapshots — what Virtuoso said at each market window
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hf_signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            fusion_direction TEXT,
            fusion_score REAL,
            fusion_confidence INTEGER,
            regime_bias TEXT,
            regime_volatility TEXT,
            kill_switch_active INTEGER,
            manipulation_detected INTEGER,
            should_trade INTEGER,
            snapshot_at TEXT
        )
    """)
    
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hf_resolutions_asset_time 
        ON hf_market_resolutions(asset, end_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hf_div_snapshots 
        ON hf_divergence_snapshots(asset, snapshot_at)
    """)
    
    conn.commit()
    return conn


# ============================================================================
# Market Resolution Collector
# ============================================================================

def collect_resolved_markets() -> Dict:
    """
    Fetch recently resolved 5/15-min crypto markets from Polymarket.
    
    Checks Gamma API for closed markets with outcomes, stores resolutions.
    """
    db = _get_db()
    collected = 0
    errors = 0
    
    try:
        # Get recently closed events
        url = (f"{GAMMA_API}/events?closed=true&limit=100"
               f"&order=endDate&ascending=false")
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-Collector/1.0"})
        
        with urllib.request.urlopen(req, timeout=20) as resp:
            events = json.loads(resp.read().decode())
        
        for event in events:
            for market in event.get("markets", []):
                question = market.get("question", "")
                q_lower = question.lower()
                
                # Filter to crypto short-duration markets
                is_crypto = any(w in q_lower for w in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol"])
                is_short = any(w in q_lower for w in ["minute", "min", "am-", "pm-", "am ", "pm "])
                
                # Also check via time range pattern
                import re
                has_time_range = bool(re.search(r'\d{1,2}:\d{2}\s*[AP]M\s*[-–]\s*\d{1,2}:\d{2}\s*[AP]M', question, re.IGNORECASE))
                
                if not (is_crypto and (is_short or has_time_range)):
                    continue
                
                market_id = market.get("id", "")
                
                # Check if already collected
                existing = db.execute(
                    "SELECT id FROM hf_market_resolutions WHERE market_id = ?",
                    (market_id,)
                ).fetchone()
                if existing:
                    continue
                
                # Determine outcome
                outcome = None
                try:
                    outcomes_raw = market.get("outcomes", "[]")
                    if isinstance(outcomes_raw, str):
                        outcomes_list = json.loads(outcomes_raw)
                    else:
                        outcomes_list = outcomes_raw
                    
                    prices_raw = market.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        prices_list = json.loads(prices_raw)
                    else:
                        prices_list = prices_raw
                    
                    # Resolved market: winning outcome has price ~1.0
                    for i, p in enumerate(prices_list):
                        if float(p) > 0.95 and i < len(outcomes_list):
                            outcome = outcomes_list[i]
                            break
                except Exception:
                    pass
                
                # Detect asset
                asset = "BTC" if any(w in q_lower for w in ["bitcoin", "btc"]) else \
                        "ETH" if any(w in q_lower for w in ["ethereum", "eth"]) else \
                        "SOL" if any(w in q_lower for w in ["solana", "sol"]) else "OTHER"
                
                # Detect duration
                from odds.hf_scanner import _detect_duration
                duration = _detect_duration(question) or "unknown"
                
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO hf_market_resolutions 
                           (market_id, condition_id, question, asset, duration, outcome,
                            yes_price_at_open, no_price_at_open, volume, liquidity,
                            start_time, end_time, resolved_at, collected_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            market_id,
                            market.get("conditionId", ""),
                            question,
                            asset,
                            duration,
                            outcome,
                            0.5,  # We don't have open price — use 0.5 as baseline
                            0.5,
                            float(market.get("volumeNum", 0) or 0),
                            float(market.get("liquidityNum", 0) or 0),
                            market.get("createdAt", ""),
                            market.get("endDate", ""),
                            market.get("endDate", ""),
                            datetime.now(timezone.utc).isoformat(),
                        )
                    )
                    collected += 1
                except Exception as e:
                    errors += 1
                    logger.error(f"DB insert error: {e}")
        
        db.commit()
    
    except Exception as e:
        logger.error(f"Collection error: {e}")
        errors += 1
    
    finally:
        db.close()
    
    return {
        "collected": collected,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Divergence Snapshot
# ============================================================================

def snapshot_divergence() -> Dict:
    """Capture current Binance vs Oracle divergence state."""
    db = _get_db()
    captured = 0
    
    try:
        req = urllib.request.Request("http://127.0.0.1:8422/state",
                                    headers={"User-Agent": "Polyclawd-Collector"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            state = json.loads(resp.read().decode())
        
        now = datetime.now(timezone.utc).isoformat()
        
        for asset, data in state.get("prices", {}).items():
            oracle_age = time.time() - data.get("oracle_updated_at", 0) \
                if data.get("oracle_updated_at", 0) > 0 else -1
            
            db.execute(
                """INSERT INTO hf_divergence_snapshots
                   (asset, binance_price, oracle_price, divergence_pct, 
                    oracle_age_seconds, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (asset, data.get("binance_price", 0), data.get("oracle_price", 0),
                 data.get("divergence_pct", 0), round(oracle_age, 1), now)
            )
            captured += 1
        
        db.commit()
    except Exception as e:
        logger.warning(f"Divergence snapshot error: {e}")
    finally:
        db.close()
    
    return {"captured": captured}


# ============================================================================
# Signal Snapshot
# ============================================================================

def snapshot_signals() -> Dict:
    """Capture current Virtuoso signal state for BTC/ETH."""
    db = _get_db()
    captured = 0
    
    try:
        from services.virtuoso_bridge import get_directional_signal
        
        now = datetime.now(timezone.utc).isoformat()
        
        for asset in ["BTC", "ETH"]:
            try:
                sig = get_directional_signal(asset)
                db.execute(
                    """INSERT INTO hf_signal_snapshots
                       (asset, fusion_direction, fusion_score, fusion_confidence,
                        regime_bias, regime_volatility, kill_switch_active,
                        manipulation_detected, should_trade, snapshot_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (asset, sig.fusion_direction, sig.fusion_score,
                     sig.fusion_confidence, sig.regime_bias, sig.regime_volatility,
                     1 if sig.kill_switch_active else 0,
                     1 if sig.manipulation_detected else 0,
                     1 if sig.should_trade else 0, now)
                )
                captured += 1
            except Exception as e:
                logger.warning(f"Signal snapshot error ({asset}): {e}")
        
        db.commit()
    except ImportError:
        logger.warning("virtuoso_bridge not available — skipping signal snapshot")
    except Exception as e:
        logger.warning(f"Signal snapshot error: {e}")
    finally:
        db.close()
    
    return {"captured": captured}


# ============================================================================
# Collection Summary
# ============================================================================

def get_collection_stats() -> Dict:
    """Get stats on collected data."""
    db = _get_db()
    try:
        resolutions = db.execute(
            "SELECT asset, duration, outcome, COUNT(*) FROM hf_market_resolutions GROUP BY asset, duration, outcome"
        ).fetchall()
        
        div_count = db.execute("SELECT COUNT(*) FROM hf_divergence_snapshots").fetchone()[0]
        sig_count = db.execute("SELECT COUNT(*) FROM hf_signal_snapshots").fetchone()[0]
        events_count = db.execute("SELECT COUNT(*) FROM hf_latency_events").fetchone()[0]
        
        # Resolution breakdown
        resolution_breakdown = {}
        for asset, duration, outcome, count in resolutions:
            key = f"{asset}_{duration}"
            if key not in resolution_breakdown:
                resolution_breakdown[key] = {"Up": 0, "Down": 0, "unknown": 0, "total": 0}
            resolution_breakdown[key][outcome or "unknown"] += count
            resolution_breakdown[key]["total"] += count
        
        return {
            "market_resolutions": sum(r[3] for r in resolutions),
            "resolution_breakdown": resolution_breakdown,
            "divergence_snapshots": div_count,
            "signal_snapshots": sig_count,
            "latency_events": events_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        db.close()


# ============================================================================
# Run All Collectors
# ============================================================================

def run_collection_cycle() -> Dict:
    """Run one full collection cycle (resolutions + divergence + signals)."""
    results = {
        "resolutions": collect_resolved_markets(),
        "divergence": snapshot_divergence(),
        "signals": snapshot_signals(),
        "stats": get_collection_stats(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Running HF data collection cycle...")
    result = run_collection_cycle()
    print(json.dumps(result, indent=2))
