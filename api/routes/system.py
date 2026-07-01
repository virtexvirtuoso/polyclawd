"""System routes for health, readiness, and metrics."""
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.deps import get_storage_service
from api.models import HealthResponse, ReadyResponse, MetricsResponse
from api.activity_feed import get_events

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate limiter (will use app.state.limiter at runtime)
limiter = Limiter(key_func=get_remote_address)

# Track startup time for uptime calculation
_startup_time = datetime.now()


@router.get("/health", response_model=HealthResponse)
@limiter.limit("60/minute")
async def health(request: Request) -> HealthResponse:
    """Health check endpoint.

    Returns basic health status for load balancers and monitoring.
    """
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(),
        version="2.0.0"
    )


@router.get("/ready", response_model=ReadyResponse)
@limiter.limit("30/minute")
async def ready(request: Request) -> ReadyResponse:
    """Readiness check endpoint.

    Verifies that required services are available:
    - Storage: Can load balance.json

    Returns 200 if all checks pass, data indicates individual check status.
    """
    checks = {}

    # Check storage availability
    try:
        storage = get_storage_service()
        await storage.load("balance.json", default={"balance": 0})
        checks["storage"] = True
    except Exception:
        checks["storage"] = False

    all_ready = all(checks.values())

    return ReadyResponse(
        ready=all_ready,
        checks=checks
    )


@router.get("/api/source-health")
@limiter.limit("30/minute")
async def source_health(request: Request):
    """Get health metrics for all data sources."""
    try:
        from api.services.source_health import get_all_source_health
        health_data = get_all_source_health()
        return JSONResponse(content={"sources": health_data})
    except Exception as e:
        logger.error(f"Source health endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/activity")
@limiter.limit("60/minute")
async def activity(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    type: Optional[str] = Query(None, alias="type"),
    since: Optional[str] = Query(None)
):
    """
    Get recent activity events.
    
    Query params:
    - limit: Max number of events to return (1-500, default 50)
    - type: Filter by event type (signal, trade, resolution, error, system, visitor)
    - since: ISO timestamp to filter events after
    """
    try:
        events = get_events(limit=limit, event_type=type, since=since)
        return JSONResponse(content={"events": events, "count": len(events)})
    except Exception as e:
        logger.error(f"Activity endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})




@router.get("/api/opportunities")
@limiter.limit("60/minute")
async def opportunities(request: Request):
    """Curated opportunities for the portfolio dashboard."""
    import sqlite3
    from pathlib import Path
    
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # Resolving soon: open paper positions, sorted by soonest close
    resolving = []
    try:
        rows = conn.execute("""
            SELECT market_title, side, entry_price, edge_pct, archetype, platform, opened_at, market_id, market_slug
            FROM paper_positions 
            WHERE status='open' 
            ORDER BY opened_at ASC 
            LIMIT 5
        """).fetchall()
        resolving = [dict(r) for r in rows]
    except Exception:
        pass
    
    # Get IDs of open positions to exclude
    held_ids = set()
    try:
        ids = conn.execute("SELECT market_id FROM paper_positions WHERE status='open'").fetchall()
        held_ids = {r[0] for r in ids}
    except Exception:
        pass
    
    # Highest edge: recent shadow trades not in positions, unresolved
    edges = []
    try:
        rows = conn.execute("""
            SELECT market as market_title, side, entry_price, confidence, archetype, platform, timestamp, market_id
            FROM shadow_trades 
            WHERE resolved=0 
              AND (days_to_close IS NULL OR days_to_close <= 30) 
              AND timestamp > datetime('now', '-14 days')
            ORDER BY confidence DESC 
            LIMIT 20
        """).fetchall()
        for r in rows:
            d = dict(r)
            if d['market_id'] not in held_ids:
                edges.append(d)
                if len(edges) >= 5:
                    break
    except Exception:
        pass
    
    # Resolve event slugs with SQLite cache
    conn2 = conn  # reuse connection
    import httpx
    for m in resolving + edges:
        if m.get("platform") != "polymarket" or not m.get("market_id"):
            continue
        mid = m["market_id"]
        # Check cache first
        try:
            cached = conn2.execute("SELECT event_slug FROM event_slug_cache WHERE market_id=?", (mid,)).fetchone()
            if cached:
                m["event_slug"] = cached[0]
                continue
        except Exception:
            pass
        # Cache miss — resolve via CLOB + gamma
        try:
            r = httpx.get(f"https://clob.polymarket.com/markets/{mid}", timeout=5)
            if r.status_code == 200:
                mslug = r.json().get("market_slug", "")
                if mslug:
                    m["market_slug"] = mslug
                    r2 = httpx.get(f"https://gamma-api.polymarket.com/markets?slug={mslug}", timeout=5)
                    if r2.status_code == 200:
                        data = r2.json()
                        if data and data[0].get("events"):
                            eslug = data[0]["events"][0].get("slug", "")
                            m["event_slug"] = eslug
                            conn2.execute("INSERT OR REPLACE INTO event_slug_cache (market_id, event_slug) VALUES (?,?)", (mid, eslug))
                            conn2.commit()
        except Exception:
            pass

    conn2.close()
    return {"resolving_soon": resolving, "highest_edge": edges}


@router.get("/metrics", response_model=MetricsResponse)
@limiter.limit("30/minute")
async def metrics(request: Request) -> MetricsResponse:
    """Basic metrics endpoint.

    Returns uptime and request count placeholder.
    """
    uptime = (datetime.now() - _startup_time).total_seconds()

    return MetricsResponse(
        uptime_seconds=uptime,
        request_count=0,  # Placeholder - would need middleware to track
        version="2.0.0"
    )


@router.get("/api/logs")
@limiter.limit("30/minute")
async def logs(
    request: Request,
    lines: int = Query(100, ge=1, le=1000),
    level: Optional[str] = Query(None),
    module: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Stream recent log lines from the log file.

    Query params:
    - lines: Number of recent lines to return (default 100, max 1000)
    - level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - module: Filter by module name (substring match)
    - search: Free text search across log lines
    """
    from pathlib import Path
    from collections import deque

    log_path = Path(__file__).parent.parent.parent / "logs" / "polyclawd.log"
    if not log_path.exists():
        return JSONResponse(content={"lines": [], "count": 0, "error": "Log file not found"})

    try:
        with open(log_path, "r") as f:
            # Read last N lines efficiently
            all_lines = deque(f, maxlen=lines * 3 if (level or module or search) else lines)

        result = []
        for line in all_lines:
            line = line.rstrip()
            if not line:
                continue
            if level and f"| {level.upper()}" not in line:
                continue
            if module and module.lower() not in line.lower():
                continue
            if search and search.lower() not in line.lower():
                continue
            result.append(line)

        # Trim to requested limit after filtering
        result = result[-lines:]

        return JSONResponse(content={"lines": result, "count": len(result)})
    except Exception as e:
        logger.error(f"Logs endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/strategy-breakdown")
@limiter.limit("30/minute")
async def strategy_breakdown(request: Request):
    """Get win rate breakdown by strategy and archetype."""
    import sqlite3
    from pathlib import Path
    
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        rows = conn.execute("""
            SELECT 
                COALESCE(strategy, 'unknown') as strategy,
                COALESCE(archetype, 'unknown') as archetype,
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl), 2) as avg_pnl
            FROM paper_positions 
            WHERE status != 'open'
            GROUP BY strategy, archetype
            ORDER BY total_pnl DESC
        """).fetchall()
        
        breakdown = []
        for r in rows:
            d = dict(r)
            d['win_rate'] = round((d['wins'] / d['total'] * 100) if d['total'] > 0 else 0, 1)
            breakdown.append(d)
        
        conn.close()
        return JSONResponse(content={"breakdown": breakdown})
    except Exception as e:
        logger.error(f"Strategy breakdown endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/daily-pnl")
@limiter.limit("30/minute")
async def daily_pnl(request: Request):
    """Get daily P&L for calendar heatmap."""
    import sqlite3
    from pathlib import Path
    
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        rows = conn.execute("""
            SELECT 
                DATE(closed_at) as date,
                ROUND(SUM(pnl), 2) as daily_pnl,
                COUNT(*) as trades
            FROM paper_positions 
            WHERE status != 'open' AND closed_at IS NOT NULL
            GROUP BY DATE(closed_at)
            ORDER BY date
        """).fetchall()
        
        daily = [dict(r) for r in rows]
        conn.close()
        
        return JSONResponse(content={"daily": daily})
    except Exception as e:
        logger.error(f"Daily P&L endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@router.get("/api/meta-model")
async def meta_model_stats(request: Request):
    """Meta-labeling model stats and recent gate decisions."""
    import json
    from pathlib import Path
    
    stats_path = Path(__file__).parent.parent.parent / "storage" / "meta_model_stats.json"
    stats = {}
    if stats_path.exists():
        with open(str(stats_path)) as f:
            stats = json.load(f)
    
    # Get recent gate blocks from positions
    import sqlite3
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    recent_scores = []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Show meta scores on open positions
        rows = conn.execute("""
            SELECT market_title, side, entry_price, confidence, edge_pct, archetype
            FROM paper_positions WHERE status='open'
            ORDER BY opened_at DESC LIMIT 10
        """).fetchall()
        
        from signals.paper_portfolio import meta_label_score
        for r in rows:
            score = meta_label_score(r["side"], r["entry_price"], r["confidence"], r["edge_pct"], r["archetype"])
            recent_scores.append({
                "market": r["market_title"][:60] if r["market_title"] else "?",
                "side": r["side"],
                "archetype": r["archetype"],
                "meta_score": score,
            })
        conn.close()
    except Exception:
        pass
    
    return {"model_stats": stats, "open_position_scores": recent_scores}

@router.get("/api/crypto-signals")
async def crypto_signals(request: Request):
    """Live crypto price signal evaluation from VPS infrastructure."""
    import sqlite3
    from pathlib import Path
    from signals.crypto_price_signal import evaluate_crypto_price_market
    
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT DISTINCT market_id, market as title,
               entry_price as price
        FROM shadow_trades 
        WHERE resolved = 0 
        AND (market LIKE '%Bitcoin%' OR market LIKE '%BTC%')
        AND (market LIKE '%reach%' OR market LIKE '%hit%' OR market LIKE '%dip%' 
             OR market LIKE '%above%' OR market LIKE '%below%' OR market LIKE '%price%')
        LIMIT 10
    """).fetchall()
    conn.close()
    
    results = []
    for r in rows:
        price = r["price"] or 0.5
        if price > 1:
            price = price / 100
        
        for side in ["YES", "NO"]:
            ev = evaluate_crypto_price_market(r["title"] or "", price, side)
            if ev and abs(ev["edge"]) > 0.03:
                results.append({
                    "market": r["title"],
                    "market_id": r["market_id"],
                    "side": side,
                    "market_price": price,
                    **ev
                })
    
    results.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return {"signals": results[:10]}



@router.get("/api/clv")
async def clv_analysis():
    """Outcome-CLV analysis — compares entry price to final resolution price.
    NOTE: closing_line = final resolution (0 or 1), not a pre-close market price.
    This is outcome CLV (tautological with WR), not true market CLV.
    True CLV would require a pre-resolution market snapshot which is not captured."""
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, market_title, side, entry_price, closing_line, pnl, status,
               strategy, archetype, opened_at, closed_at
        FROM paper_positions
        WHERE status IN ('won', 'lost', 'stopped') AND closing_line IS NOT NULL
        ORDER BY closed_at DESC
    """).fetchall()

    trades = []
    total_clv = 0
    positive_clv = 0

    for r in rows:
        entry = r["entry_price"]
        closing = r["closing_line"]
        side = r["side"]

        if side == "YES":
            clv = closing - entry
        else:
            clv = entry - closing

        clv_pct = round(clv * 100, 1)
        total_clv += clv
        if clv > 0:
            positive_clv += 1

        trades.append({
            "id": r["id"],
            "market": r["market_title"][:80] if r["market_title"] else "",
            "side": side,
            "entry_price": round(entry, 3),
            "closing_line": round(closing, 3),
            "clv": round(clv, 3),
            "clv_pct": clv_pct,
            "pnl": r["pnl"],
            "won": r["status"] == "won",
            "status": r["status"],
            "strategy": r["strategy"],
            "archetype": r["archetype"],
        })

    n = len(trades)
    conn.close()

    # Breakdown by status for transparency
    won_n = sum(1 for t in trades if t["status"] == "won")
    stop_n = sum(1 for t in trades if t["status"] == "stopped")
    lost_n = sum(1 for t in trades if t["status"] == "lost")

    return {
        "total_trades": n,
        "avg_clv": round(total_clv / n * 100, 2) if n else 0,
        "positive_clv_rate": round(positive_clv / n * 100, 1) if n else 0,
        "positive_clv_count": positive_clv,
        "breakdown": {"won": won_n, "stopped": stop_n, "lost": lost_n},
        "warning": "closing_line = final resolution price (0/1), not pre-close market. Positive rate is strongly correlated with WR, not independent edge signal.",
        "trades": trades,
    }


# ── Shadow Performance Endpoint ──────────────────────────────────────
@router.get("/api/shadow-performance")
async def shadow_performance():
    """Archetype-level shadow trade performance for monitoring blocked/all archetypes."""
    import sqlite3
    import os
    db_path = os.path.join(os.path.dirname(__file__), "..", "..", "storage", "shadow_trades.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            COALESCE(archetype, 'unknown') as archetype,
            COUNT(*) as total,
            SUM(resolved) as resolved,
            SUM(CASE WHEN resolved=1 AND outcome=side THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN resolved=1 AND outcome!=side THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END), 2) as total_pnl,
            ROUND(AVG(CASE WHEN resolved=1 THEN confidence END), 1) as avg_confidence,
            COUNT(*) - SUM(resolved) as open
        FROM shadow_trades
        GROUP BY archetype
        ORDER BY total DESC
    """).fetchall()

    # Also get last-30-day stats
    recent = conn.execute("""
        SELECT
            COALESCE(archetype, 'unknown') as archetype,
            COUNT(*) as total,
            SUM(resolved) as resolved,
            SUM(CASE WHEN resolved=1 AND outcome=side THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END), 2) as total_pnl
        FROM shadow_trades
        WHERE timestamp >= datetime('now', '-30 days')
        GROUP BY archetype
        ORDER BY total DESC
    """).fetchall()

    conn.close()

    blocked = {"sports_winner", "deadline_binary", "election"}

    def row_to_dict(r, is_blocked=False):
        resolved = r["resolved"] or 0
        wins = r["wins"] or 0
        return {
            "archetype": r["archetype"],
            "total": r["total"],
            "resolved": resolved,
            "wins": wins,
            "win_rate": round(wins / resolved * 100, 1) if resolved > 0 else None,
            "total_pnl": r["total_pnl"] or 0,
            "blocked": is_blocked,
        }

    all_time = [row_to_dict(r, r["archetype"] in blocked) for r in rows]
    last_30d = [row_to_dict(r, r["archetype"] in blocked) for r in recent]

    return {
        "all_time": all_time,
        "last_30d": last_30d,
        "blocked_archetypes": list(blocked),
        "total_shadow_trades": sum(r["total"] for r in all_time),
        "total_resolved": sum(r["resolved"] or 0 for r in all_time),
    }
