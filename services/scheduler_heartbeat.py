"""
Scheduler Heartbeat — Self-checking task verification

Each critical task writes a heartbeat after successful execution.
On startup and periodically, scheduler verifies all heartbeats are fresh.
Alerts via Discord if any critical task is stale.
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"

# Critical tasks that MUST run
CRITICAL_TASKS = {
    "stop_evaluator": 300,      # 5 min
    "paper_resolution": 300,    # 5 min
    "shadow_resolution": 300,   # 5 min
    "weather_reeval": 300,      # 5 min
}

# Non-critical tasks (log warning only)
MONITORED_TASKS = {
    "signal_scan": 1800,        # 30 min
    "edge_alerts": 1800,        # 30 min
    "hf_signals": 30,           # 30 sec
    "calibration_check": 300,   # 5 min
}


def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def update_heartbeat(task_name: str) -> None:
    """Update heartbeat timestamp for a task. Call after successful execution."""
    conn = _db()
    try:
        conn.execute("""
            INSERT INTO scheduler_heartbeat (task_name, last_run, interval_seconds, critical)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_name) DO UPDATE SET last_run = excluded.last_run
        """, (
            task_name,
            datetime.now(timezone.utc).isoformat(),
            CRITICAL_TASKS.get(task_name) or MONITORED_TASKS.get(task_name, 300),
            1 if task_name in CRITICAL_TASKS else 0
        ))
        conn.commit()
    except Exception as e:
        logger.warning("Failed to update heartbeat for {}: {}", task_name, e)
    finally:
        conn.close()


def check_heartbeat(task_name: str) -> dict:
    """Check if a task's heartbeat is fresh. Returns {fresh: bool, stale_seconds: int}."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT last_run, interval_seconds FROM scheduler_heartbeat WHERE task_name = ?",
            (task_name,)
        ).fetchone()
        
        if not row:
            return {"fresh": False, "stale_seconds": 999999, "error": "no_heartbeat"}
        
        last_run = datetime.fromisoformat(row["last_run"].replace("Z", "+00:00"))
        interval = row["interval_seconds"]
        age = (datetime.now(timezone.utc) - last_run).total_seconds()
        
        # Stale if age > 2x interval
        stale = age > interval * 2
        
        return {
            "fresh": not stale,
            "stale_seconds": int(age),
            "interval": interval,
            "last_run": row["last_run"]
        }
    except Exception as e:
        return {"fresh": False, "stale_seconds": 999999, "error": str(e)}
    finally:
        conn.close()


def check_all_heartbeats() -> dict:
    """Check all critical tasks. Returns {healthy: bool, stale: [task_names], details: {...}}."""
    stale = []
    details = {}
    
    for task_name in CRITICAL_TASKS:
        result = check_heartbeat(task_name)
        details[task_name] = result
        if not result.get("fresh"):
            stale.append(task_name)
    
    return {
        "healthy": len(stale) == 0,
        "stale": stale,
        "details": details
    }


def verify_startup() -> bool:
    """Verify all critical tasks are registered. Call on scheduler startup.
    
    Returns True if all critical tasks are present in tick functions.
    Sends Discord alert if any are missing.
    """
    # This is called from scheduler.py after registering all tasks
    # It checks that all CRITICAL_TASKS have heartbeats in the DB
    conn = _db()
    try:
        registered = conn.execute(
            "SELECT task_name FROM scheduler_heartbeat WHERE critical = 1"
        ).fetchall()
        registered = [r["task_name"] for r in registered]
        
        missing = [t for t in CRITICAL_TASKS if t not in registered]
        
        if missing:
            logger.error("CRITICAL: Missing tasks in scheduler: {}", missing)
            _send_missing_task_alert(missing)
            return False
        
        logger.info("Scheduler startup verified: {} critical tasks registered", len(registered))
        return True
    finally:
        conn.close()


def _send_missing_task_alert(missing_tasks: list) -> None:
    """Send Discord alert for missing critical tasks."""
    try:
        from signals.discord_alerts import _send, COLOR_RED
        from datetime import datetime, timezone
        
        _send([{
            "title": "🚨 SCHEDULER ERROR — Missing Critical Tasks",
            "description": f"The scheduler is running without critical tasks!",
            "color": COLOR_RED,
            "fields": [
                {"name": "Missing Tasks", "value": "```\\n" + "\\n".join(missing_tasks) + "```", "inline": False},
                {"name": "Impact", "value": "Stop-losses, resolutions, or other critical operations will not run.", "inline": False},
                {"name": "Action", "value": "Check scheduler.py tick_5min() and ensure all tasks are registered.", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }], alert_type="scheduler_error")
    except Exception as e:
        logger.warning("Failed to send missing task alert: {}", e)


def send_stale_heartbeat_alert(stale_tasks: list, details: dict) -> None:
    """Send Discord alert for stale heartbeats."""
    try:
        from signals.discord_alerts import _send, COLOR_ORANGE
        
        task_lines = []
        for task in stale_tasks:
            d = details.get(task, {})
            age = d.get("stale_seconds", 0)
            interval = d.get("interval", 0)
            task_lines.append(f"**{task}**: {age}s stale (interval: {interval}s)")
        
        _send([{
            "title": "⚠️ STALE HEARTBEAT — Tasks Not Running",
            "description": f"Critical tasks have not run in 2x their interval.",
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Stale Tasks", "value": "\\n".join(task_lines), "inline": False},
                {"name": "Action", "value": "Check scheduler logs and restart if needed.", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }], alert_type="scheduler_stale")
    except Exception as e:
        logger.warning("Failed to send stale heartbeat alert: {}", e)


# Periodic check function to call from scheduler
def periodic_heartbeat_check() -> dict:
    """Call this from tick_30min to verify all heartbeats are fresh."""
    result = check_all_heartbeats()
    
    if not result["healthy"]:
        logger.warning("Stale heartbeats detected: {}", result["stale"])
        send_stale_heartbeat_alert(result["stale"], result["details"])
    else:
        logger.debug("All heartbeats fresh")
    
    return result


if __name__ == "__main__":
    # Test
    print("Checking heartbeats...")
    result = check_all_heartbeats()
    print(f"Healthy: {result['healthy']}")
    print(f"Stale: {result['stale']}")
    for task, detail in result['details'].items():
        status = "✓" if detail.get('fresh') else "✗"
        print(f"  {status} {task}: {detail}")
