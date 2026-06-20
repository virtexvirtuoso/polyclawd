"""
Social Count API — serve Musk tweet and Trump Truth Social data
Updated: 2026-03-20
"""
from fastapi import APIRouter, HTTPException
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

router = APIRouter(prefix="/social", tags=["social"])

DB_PATH = Path(__file__).parent.parent.parent / "storage" / "shadow_trades.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/counts")
async def get_social_counts():
    """Get latest social count data for Musk and Trump"""
    db = get_db()
    
    # Get latest snapshots
    latest = db.execute("""
        SELECT person, cumulative_count, source, scraped_at
        FROM social_count_history
        WHERE scraped_at = (
            SELECT MAX(scraped_at) 
            FROM social_count_history AS sub 
            WHERE sub.person = social_count_history.person
        )
        ORDER BY person
    """).fetchall()
    
    # Get data points count
    points = db.execute("""
        SELECT person, COUNT(*) as count
        FROM social_count_snapshots
        GROUP BY person
    """).fetchall()
    
    # Get recent trend (last 5 data points)
    trends = db.execute("""
        SELECT person, timestamp, cumulative_count, source
        FROM social_count_snapshots
        WHERE timestamp > datetime('now', '-1 day')
        ORDER BY timestamp DESC
        LIMIT 10
    """).fetchall()
    
    result = {
        "musk": {"total": None, "rate": None, "points": 0, "last_updated": None},
        "trump": {"total": None, "rate": None, "points": 0, "last_updated": None},
        "trend_data": []
    }
    
    # Fill in totals
    for row in latest:
        person = row["person"]
        result[person]["total"] = row["cumulative_count"]
        result[person]["last_updated"] = row["scraped_at"]
        result[person]["source"] = row["source"]
    
    # Fill in data points count
    for row in points:
        result[row["person"]]["points"] = row["count"]
    
    # Calculate rates
    for person in ["musk", "trump"]:
        rows = db.execute("""
            SELECT timestamp, cumulative_count
            FROM social_count_snapshots
            WHERE person = ?
            ORDER BY timestamp DESC
            LIMIT 2
        """, (person,)).fetchall()
        
        if len(rows) >= 2:
            t1 = datetime.fromisoformat(rows[1]["timestamp"].replace('Z', '+00:00'))
            t2 = datetime.fromisoformat(rows[0]["timestamp"].replace('Z', '+00:00'))
            hours = (t2 - t1).total_seconds() / 3600
            
            if hours > 0:
                delta = rows[0]["cumulative_count"] - rows[1]["cumulative_count"]
                daily_rate = (delta / hours) * 24
                result[person]["rate"] = round(daily_rate, 1)
    
    # Fill trend data
    result["trend_data"] = [
        {
            "person": r["person"],
            "timestamp": r["timestamp"],
            "count": r["cumulative_count"],
            "source": r["source"]
        }
        for r in trends
    ]
    
    db.close()
    return result


@router.get("/history/{person}")
async def get_person_history(person: str, days: int = 7):
    """Get history for a specific person (musk or trump)"""
    if person not in ["musk", "trump"]:
        raise HTTPException(status_code=404, detail="Person not found")
    
    db = get_db()
    
    rows = db.execute("""
        SELECT timestamp, cumulative_count, source
        FROM social_count_snapshots
        WHERE person = ? AND timestamp > datetime('now', ?)
        ORDER BY timestamp ASC
    """, (person, f"-{days} days")).fetchall()
    
    db.close()
    
    return {
        "person": person,
        "days": days,
        "data": [
            {"timestamp": r["timestamp"], "count": r["cumulative_count"], "source": r["source"]}
            for r in rows
        ]
    }


@router.get("/snapshots")
async def get_all_snapshots(limit: int = 50):
    """Get raw snapshots with filtering"""
    db = get_db()
    
    rows = db.execute("""
        SELECT person, timestamp, cumulative_count, source
        FROM social_count_snapshots
        ORDER BY timestamp DESC
        LIMIT ?
    """, (limit,)).fetchall()
    
    db.close()
    
    return {
        "count": len(rows),
        "snapshots": [
            {"person": r["person"], "timestamp": r["timestamp"], 
             "count": r["cumulative_count"], "source": r["source"]}
            for r in rows
        ]
    }
