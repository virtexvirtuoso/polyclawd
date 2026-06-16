"""
Activity feed with in-memory ring buffer and SQLite persistence.
Provides a real-time event log for the Polyclawd API.
"""
import json
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger


# In-memory ring buffer (per-process)
_activity_buffer = deque(maxlen=500)
_buffer_lock = threading.Lock()

# Database path
DB_PATH = Path("/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db")


def _init_db():
    """Initialize the activity_log table if it doesn't exist."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    data_json TEXT DEFAULT NULL
                )
            """)
            # Create index for faster queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_activity_timestamp 
                ON activity_log(timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_activity_type 
                ON activity_log(event_type)
            """)
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to initialize activity_log table: {e}")


# Initialize on module load
_init_db()


def emit_event(
    event_type: str,
    severity: str,
    title: str,
    detail: str = "",
    data: Optional[Dict[str, Any]] = None
):
    """
    Emit an activity event to both the ring buffer and SQLite.
    
    Args:
        event_type: Type of event (signal, trade, resolution, error, system, visitor)
        severity: Severity level (info, warning, critical)
        title: Short title of the event
        detail: Optional detailed description
        data: Optional structured data (will be JSON-serialized)
    """
    timestamp = datetime.utcnow().isoformat() + "Z"
    data_json = json.dumps(data) if data else None
    
    event = {
        "timestamp": timestamp,
        "event_type": event_type,
        "severity": severity,
        "title": title,
        "detail": detail,
        "data": data
    }
    
    # Add to ring buffer
    with _buffer_lock:
        _activity_buffer.append(event)
    
    # Persist to SQLite (non-blocking approach)
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.execute(
                """
                INSERT INTO activity_log (timestamp, event_type, severity, title, detail, data_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, event_type, severity, title, detail, data_json)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to persist activity event to database: {e}")
    
    # Also log via loguru at appropriate level
    log_message = f"[{event_type.upper()}] {title}"
    if detail:
        log_message += f": {detail}"
    
    if severity == "critical":
        logger.error(log_message)
    elif severity == "warning":
        logger.warning(log_message)
    else:
        logger.info(log_message)


def get_events(
    limit: int = 50,
    event_type: Optional[str] = None,
    since: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Retrieve activity events from SQLite with optional filters.
    
    Args:
        limit: Maximum number of events to return (default 50)
        event_type: Filter by event type (optional)
        since: Filter events after this ISO timestamp (optional)
    
    Returns:
        List of event dictionaries
    """
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            
            query = "SELECT * FROM activity_log WHERE 1=1"
            params = []
            
            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            
            if since:
                query += " AND timestamp >= ?"
                params.append(since)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            events = []
            for row in rows:
                event = {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "event_type": row["event_type"],
                    "severity": row["severity"],
                    "title": row["title"],
                    "detail": row["detail"],
                    "data": json.loads(row["data_json"]) if row["data_json"] else None
                }
                events.append(event)
            
            return events
    
    except Exception as e:
        logger.error(f"Failed to retrieve activity events: {e}")
        return []


def get_buffer_snapshot() -> List[Dict[str, Any]]:
    """
    Get a snapshot of the in-memory ring buffer.
    Useful for very recent events without hitting the database.
    """
    with _buffer_lock:
        return list(_activity_buffer)
