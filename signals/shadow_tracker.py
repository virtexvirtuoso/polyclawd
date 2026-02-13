#!/usr/bin/env python3
"""
Shadow Trade Tracker — persistent paper trade tracking with SQLite.

Features:
- SQLite storage (no more JSON cap / pruning)
- Daily signal snapshots
- Daily P&L summary with cumulative stats (win rate, Sharpe, drawdown)
- Batch market resolution (rate-limit aware)
- CLI: snapshot / resolve / summary / export

Integrates with mispriced_category_signal.py and watchdog cron.
"""

import json
import logging
import math
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "shadow_trades.db"
SNAPSHOTS_DIR = STORAGE_DIR / "shadow_snapshots"
PERFORMANCE_FILE = STORAGE_DIR / "shadow_performance.json"
LEGACY_JSON = STORAGE_DIR / "shadow_trades.json"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


# ============================================================================
# Database
# ============================================================================

def get_db() -> sqlite3.Connection:
    """Get SQLite connection with WAL mode for concurrent reads."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            market_id TEXT NOT NULL,
            market TEXT,
            category TEXT,
            category_tier TEXT,
            platform TEXT DEFAULT 'kalshi',
            side TEXT,
            entry_price REAL,
            confidence REAL,
            confirmations INTEGER,
            days_to_close REAL,
            volume INTEGER,
            reasoning TEXT,
            resolved INTEGER DEFAULT 0,
            resolved_at TEXT,
            outcome TEXT,
            exit_price REAL,
            pnl REAL,
            snapshot_date TEXT,
            UNIQUE(market_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS daily_summaries (
            date TEXT PRIMARY KEY,
            total_signals INTEGER,
            trades_logged INTEGER,
            trades_resolved INTEGER,
            wins INTEGER,
            losses INTEGER,
            win_rate REAL,
            total_pnl REAL,
            cumulative_pnl REAL,
            avg_confidence REAL,
            avg_edge REAL,
            max_drawdown REAL,
            sharpe REAL,
            best_trade TEXT,
            worst_trade TEXT,
            sources TEXT,
            generated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            source TEXT,
            platform TEXT,
            market_id TEXT,
            market TEXT,
            category TEXT,
            side TEXT,
            price REAL,
            confidence REAL,
            volume INTEGER,
            days_to_close REAL,
            confirmations INTEGER,
            reasoning TEXT,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_resolved ON shadow_trades(resolved);
        CREATE INDEX IF NOT EXISTS idx_trades_date ON shadow_trades(snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_trades_market ON shadow_trades(market_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date ON signal_snapshots(snapshot_date);
    """)
    conn.commit()


def _migrate_legacy_json(conn: sqlite3.Connection):
    """Import trades from legacy JSON file into SQLite."""
    if not LEGACY_JSON.exists():
        return 0

    try:
        with open(LEGACY_JSON) as f:
            trades = json.load(f)
    except Exception:
        return 0

    imported = 0
    for t in trades:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO shadow_trades
                (timestamp, market_id, market, category, side, entry_price,
                 confidence, confirmations, days_to_close, volume,
                 resolved, outcome, pnl, snapshot_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t.get("timestamp", ""),
                t.get("market_id", ""),
                t.get("market", ""),
                t.get("category", ""),
                t.get("side", ""),
                t.get("entry_price"),
                t.get("confidence"),
                t.get("confirmations"),
                t.get("days_to_close"),
                t.get("volume"),
                1 if t.get("resolved") else 0,
                t.get("outcome"),
                t.get("pnl"),
                t.get("timestamp", "")[:10],
            ))
            imported += 1
        except Exception:
            continue

    conn.commit()

    # Rename legacy file
    LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.migrated"))
    logger.info(f"Migrated {imported} trades from legacy JSON to SQLite")
    return imported


# ============================================================================
# Signal Snapshot
# ============================================================================

def save_signal_snapshot(signals: List[Dict], source: str = "all"):
    """Save all current signals to daily snapshot table + file."""
    conn = get_db()
    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    # Save to SQLite
    for sig in signals:
        try:
            conn.execute("""
                INSERT INTO signal_snapshots
                (snapshot_date, snapshot_time, source, platform, market_id,
                 market, category, side, price, confidence, volume,
                 days_to_close, confirmations, reasoning, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today, now,
                sig.get("source", source),
                sig.get("platform", ""),
                sig.get("market_id", ""),
                sig.get("market", "")[:200],
                sig.get("category", ""),
                sig.get("side", ""),
                sig.get("price"),
                sig.get("confidence"),
                sig.get("volume"),
                sig.get("days_to_close"),
                sig.get("confirmations"),
                sig.get("reasoning", "")[:500],
                json.dumps(sig)[:2000],
            ))
        except Exception as e:
            logger.debug(f"Snapshot insert error: {e}")

    conn.commit()

    # Also save to daily JSON file
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_file = SNAPSHOTS_DIR / f"{today}.json"

    existing = []
    if snapshot_file.exists():
        try:
            with open(snapshot_file) as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing.append({
        "time": now,
        "source": source,
        "signal_count": len(signals),
        "signals": signals,
    })

    with open(snapshot_file, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    conn.close()
    return len(signals)


# ============================================================================
# Trade Logging
# ============================================================================

def log_shadow_trade(signal: Dict) -> bool:
    """Log a signal as a shadow trade in SQLite.
    
    Dedup: only one open (unresolved) trade per market_id (regardless of side).
    If an open trade exists for this market, update it (including side if changed).
    This prevents conflicting YES/NO trades on the same market.
    """
    conn = get_db()
    today = date.today().isoformat()
    market_id = signal.get("market_id", "")
    side = signal.get("side", "")

    try:
        # Check for existing open trade on same market (ANY side)
        existing = conn.execute(
            "SELECT id, side, confidence FROM shadow_trades WHERE market_id = ? AND resolved = 0",
            (market_id,)
        ).fetchone()

        if existing:
            existing_side = existing[1]
            if existing_side != side:
                # Side flipped — this means the signal is unstable, skip
                logger.warning(
                    f"Shadow trade side conflict: {market_id} was {existing_side}, "
                    f"now {side} — keeping original, skipping new signal"
                )
                conn.close()
                return False
            # Update existing trade with latest data (price, confidence, volume)
            conn.execute("""
                UPDATE shadow_trades
                SET entry_price = ?, confidence = ?, confirmations = ?,
                    days_to_close = ?, volume = ?, reasoning = ?,
                    snapshot_date = ?
                WHERE id = ?
            """, (
                signal.get("price"),
                signal.get("confidence"),
                signal.get("confirmations"),
                signal.get("days_to_close"),
                signal.get("volume"),
                signal.get("reasoning", "")[:500],
                today,
                existing[0],
            ))
            conn.commit()
            conn.close()
            logger.info(f"Shadow trade updated (dedup): {market_id} {side}")
            return True

        conn.execute("""
            INSERT INTO shadow_trades
            (timestamp, market_id, market, category, category_tier, platform,
             side, entry_price, confidence, confirmations, days_to_close,
             volume, reasoning, snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            market_id,
            signal.get("market", "")[:200],
            signal.get("category", ""),
            signal.get("category_tier", ""),
            signal.get("platform", "kalshi"),
            side,
            signal.get("price"),
            signal.get("confidence"),
            signal.get("confirmations"),
            signal.get("days_to_close"),
            signal.get("volume"),
            signal.get("reasoning", "")[:500],
            today,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"Shadow trade log failed: {e}")
        conn.close()
        return False


# ============================================================================
# Resolution
# ============================================================================

def _fetch_json(url: str, timeout: int = 8) -> Any:
    """Fetch JSON with timeout."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Fetch failed: {e}")
        return None


def resolve_trades(batch_size: int = 10, delay: float = 0.5) -> Dict[str, Any]:
    """Resolve unresolved shadow trades against Kalshi API.
    
    Rate-limit aware: processes batch_size per run with delay between calls.
    """
    conn = get_db()

    # Migrate legacy JSON if exists
    _migrate_legacy_json(conn)

    # Get unresolved trades (oldest first, limit batch)
    rows = conn.execute("""
        SELECT id, market_id, side, entry_price, market
        FROM shadow_trades
        WHERE resolved = 0
        ORDER BY timestamp ASC
        LIMIT ?
    """, (batch_size,)).fetchall()

    if not rows:
        conn.close()
        return {"resolved": 0, "pending": 0, "note": "No unresolved trades"}

    resolved_count = 0
    total_pnl = 0.0
    errors = 0

    for row in rows:
        market_id = row["market_id"]
        if not market_id:
            continue

        data = _fetch_json(f"{KALSHI_API}/markets/{market_id}", timeout=8)
        if not data:
            errors += 1
            time.sleep(delay)
            continue

        market = data.get("market", data)
        result = market.get("result", "")
        if not result:
            time.sleep(delay * 0.5)  # Not resolved yet, shorter delay
            continue

        entry_price = row["entry_price"] or 0.5
        side = row["side"] or "YES"

        # P&L calculation (Kalshi binary: win = 1.00, lose = 0.00)
        if result.upper() == "YES":
            pnl = (1.0 - entry_price) if side == "YES" else -entry_price
        elif result.upper() == "NO":
            pnl = -entry_price if side == "YES" else (entry_price)
        else:
            pnl = 0

        conn.execute("""
            UPDATE shadow_trades
            SET resolved = 1, resolved_at = ?, outcome = ?, pnl = ?, exit_price = ?
            WHERE id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            result.upper(),
            round(pnl, 4),
            1.0 if result.upper() == side else 0.0,
            row["id"],
        ))

        total_pnl += pnl
        resolved_count += 1
        time.sleep(delay)

    conn.commit()

    # Get overall stats
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved_total,
            SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 AND resolved = 1 THEN 1 ELSE 0 END) as losses,
            SUM(COALESCE(pnl, 0)) as cumulative_pnl,
            AVG(CASE WHEN resolved = 1 THEN pnl END) as avg_pnl
        FROM shadow_trades
    """).fetchone()

    conn.close()

    total_resolved = stats["resolved_total"] or 0
    wins = stats["wins"] or 0

    return {
        "resolved_this_run": resolved_count,
        "errors": errors,
        "pending": stats["pending"] or 0,
        "total_trades": stats["total"] or 0,
        "total_resolved": total_resolved,
        "wins": wins,
        "losses": stats["losses"] or 0,
        "win_rate": round(wins / total_resolved * 100, 1) if total_resolved > 0 else 0,
        "pnl_this_run": round(total_pnl, 4),
        "cumulative_pnl": round(stats["cumulative_pnl"] or 0, 4),
        "avg_pnl_per_trade": round(stats["avg_pnl"] or 0, 4),
    }


# ============================================================================
# Daily Summary
# ============================================================================

def generate_daily_summary(target_date: Optional[str] = None) -> Dict[str, Any]:
    """Generate and store daily performance summary."""
    conn = get_db()
    today = target_date or date.today().isoformat()

    # Today's trades
    day_trades = conn.execute("""
        SELECT * FROM shadow_trades WHERE snapshot_date = ?
    """, (today,)).fetchall()

    day_resolved = [t for t in day_trades if t["resolved"]]
    day_wins = sum(1 for t in day_resolved if (t["pnl"] or 0) > 0)
    day_pnl = sum(t["pnl"] or 0 for t in day_resolved)

    # Today's signals
    day_signals = conn.execute("""
        SELECT COUNT(*) as cnt FROM signal_snapshots WHERE snapshot_date = ?
    """, (today,)).fetchone()["cnt"]

    # All-time cumulative
    all_resolved = conn.execute("""
        SELECT pnl, snapshot_date, market FROM shadow_trades
        WHERE resolved = 1 ORDER BY resolved_at
    """).fetchall()

    cumulative_pnl = sum(t["pnl"] or 0 for t in all_resolved)
    total_wins = sum(1 for t in all_resolved if (t["pnl"] or 0) > 0)
    total_resolved = len(all_resolved)

    # Sharpe ratio (annualized, assuming daily returns)
    daily_pnls = {}
    for t in all_resolved:
        d = t["snapshot_date"] or "unknown"
        daily_pnls.setdefault(d, 0)
        daily_pnls[d] += (t["pnl"] or 0)

    pnl_values = list(daily_pnls.values())
    if len(pnl_values) >= 2:
        mean_pnl = sum(pnl_values) / len(pnl_values)
        std_pnl = (sum((p - mean_pnl) ** 2 for p in pnl_values) / (len(pnl_values) - 1)) ** 0.5
        sharpe = (mean_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0
    else:
        sharpe = 0

    # Max drawdown
    running_pnl = 0
    peak = 0
    max_dd = 0
    for t in all_resolved:
        running_pnl += (t["pnl"] or 0)
        peak = max(peak, running_pnl)
        dd = peak - running_pnl
        max_dd = max(max_dd, dd)

    # Best/worst trades today
    best = max(day_resolved, key=lambda t: t["pnl"] or 0) if day_resolved else None
    worst = min(day_resolved, key=lambda t: t["pnl"] or 0) if day_resolved else None

    # Average confidence today
    avg_conf = (
        sum(t["confidence"] or 0 for t in day_trades) / len(day_trades)
        if day_trades else 0
    )

    summary = {
        "date": today,
        "total_signals": day_signals,
        "trades_logged": len(day_trades),
        "trades_resolved": len(day_resolved),
        "wins": day_wins,
        "losses": len(day_resolved) - day_wins,
        "win_rate": round(day_wins / len(day_resolved) * 100, 1) if day_resolved else 0,
        "day_pnl": round(day_pnl, 4),
        "cumulative_pnl": round(cumulative_pnl, 4),
        "total_resolved_all_time": total_resolved,
        "total_wins_all_time": total_wins,
        "all_time_win_rate": round(total_wins / total_resolved * 100, 1) if total_resolved > 0 else 0,
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 4),
        "avg_confidence": round(avg_conf, 1),
        "best_trade": f"{best['market'][:40]} +{best['pnl']:.4f}" if best else None,
        "worst_trade": f"{worst['market'][:40]} {worst['pnl']:.4f}" if worst else None,
    }

    # Save to SQLite
    conn.execute("""
        INSERT OR REPLACE INTO daily_summaries
        (date, total_signals, trades_logged, trades_resolved, wins, losses,
         win_rate, total_pnl, cumulative_pnl, avg_confidence, avg_edge,
         max_drawdown, sharpe, best_trade, worst_trade, sources, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today, day_signals, len(day_trades), len(day_resolved),
        day_wins, len(day_resolved) - day_wins,
        summary["win_rate"], round(day_pnl, 4), round(cumulative_pnl, 4),
        round(avg_conf, 1), 0,
        round(max_dd, 4), round(sharpe, 2),
        summary["best_trade"], summary["worst_trade"],
        json.dumps({"mispriced_category": len(day_trades)}),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()

    # Also append to JSON performance file
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    perf_data = []
    if PERFORMANCE_FILE.exists():
        try:
            with open(PERFORMANCE_FILE) as f:
                perf_data = json.load(f)
        except Exception:
            perf_data = []

    # Replace today's entry if exists
    perf_data = [p for p in perf_data if p.get("date") != today]
    perf_data.append(summary)
    perf_data.sort(key=lambda x: x.get("date", ""))

    with open(PERFORMANCE_FILE, "w") as f:
        json.dump(perf_data, f, indent=2)

    conn.close()
    return summary


# ============================================================================
# Query Helpers
# ============================================================================

def get_performance_history(days: int = 30) -> List[Dict]:
    """Get daily performance summaries for the last N days."""
    conn = get_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT * FROM daily_summaries
        WHERE date >= ?
        ORDER BY date DESC
    """, (cutoff,)).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_open_trades() -> List[Dict]:
    """Get all unresolved shadow trades."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM shadow_trades
        WHERE resolved = 0
        ORDER BY timestamp DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trade_stats() -> Dict[str, Any]:
    """Get overall shadow trading statistics."""
    conn = get_db()

    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 AND resolved = 1 THEN 1 ELSE 0 END) as losses,
            SUM(COALESCE(pnl, 0)) as total_pnl,
            AVG(CASE WHEN resolved = 1 THEN pnl END) as avg_pnl,
            MAX(pnl) as best_pnl,
            MIN(CASE WHEN resolved = 1 THEN pnl END) as worst_pnl,
            AVG(confidence) as avg_confidence,
            COUNT(DISTINCT snapshot_date) as active_days,
            COUNT(DISTINCT category) as categories_traded
        FROM shadow_trades
    """).fetchone()

    # Category breakdown
    cats = conn.execute("""
        SELECT category,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(COALESCE(pnl, 0)) as pnl
        FROM shadow_trades
        WHERE resolved = 1
        GROUP BY category
        ORDER BY pnl DESC
    """).fetchall()

    conn.close()

    resolved = stats["resolved"] or 0
    wins = stats["wins"] or 0

    return {
        "total_trades": stats["total"] or 0,
        "resolved": resolved,
        "pending": stats["pending"] or 0,
        "wins": wins,
        "losses": stats["losses"] or 0,
        "win_rate": round(wins / resolved * 100, 1) if resolved > 0 else 0,
        "total_pnl": round(stats["total_pnl"] or 0, 4),
        "avg_pnl": round(stats["avg_pnl"] or 0, 4),
        "best_trade_pnl": round(stats["best_pnl"] or 0, 4),
        "worst_trade_pnl": round(stats["worst_pnl"] or 0, 4),
        "avg_confidence": round(stats["avg_confidence"] or 0, 1),
        "active_days": stats["active_days"] or 0,
        "categories_traded": stats["categories_traded"] or 0,
        "category_breakdown": [
            {
                "category": c["category"],
                "trades": c["trades"],
                "wins": c["wins"],
                "pnl": round(c["pnl"] or 0, 4),
            }
            for c in cats
        ],
    }


def export_trades(format: str = "json") -> str:
    """Export all trades to JSON file. Returns path."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM shadow_trades ORDER BY timestamp").fetchall()
    conn.close()

    trades = [dict(r) for r in rows]
    out_path = STORAGE_DIR / f"shadow_export_{date.today().isoformat()}.json"
    with open(out_path, "w") as f:
        json.dump(trades, f, indent=2, default=str)

    return str(out_path)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "snapshot":
        # Snapshot current signals
        try:
            from mispriced_category_signal import get_mispriced_category_signals
            result = get_mispriced_category_signals()
            count = save_signal_snapshot(result.get("signals", []), "mispriced_category")
            print(f"Snapshot saved: {count} signals")
        except Exception as e:
            print(f"Snapshot failed: {e}")

    elif cmd == "resolve":
        result = resolve_trades(batch_size=15, delay=0.3)
        print(json.dumps(result, indent=2))

    elif cmd == "summary":
        result = generate_daily_summary()
        print(json.dumps(result, indent=2))

    elif cmd == "stats":
        result = get_trade_stats()
        print(json.dumps(result, indent=2))

    elif cmd == "open":
        trades = get_open_trades()
        print(f"Open trades: {len(trades)}")
        for t in trades[:10]:
            print(f"  {t['market_id']} | {t['side']} @ {t['entry_price']:.2f} | {t['category']} | {t['days_to_close']:.0f}d")

    elif cmd == "history":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        history = get_performance_history(days)
        for h in history:
            print(f"  {h['date']} | Signals: {h['total_signals']} | W/L: {h['wins']}/{h['losses']} | WR: {h['win_rate']}% | PnL: {h['total_pnl']:+.4f} | Cum: {h['cumulative_pnl']:+.4f}")

    elif cmd == "export":
        path = export_trades()
        print(f"Exported to: {path}")

    elif cmd == "migrate":
        conn = get_db()
        count = _migrate_legacy_json(conn)
        conn.close()
        print(f"Migrated {count} trades from legacy JSON")

    else:
        print("Usage: shadow_tracker.py [snapshot|resolve|summary|stats|open|history [days]|export|migrate]")
