#!/usr/bin/env python3
"""sport_edge_alerts.py — Telegram alerts for tradeable sports edge scans.

Generic across sports (NFL, NBA, MLB, soccer, UFC, ...). Fires a Telegram
alert when an edge clears the tradeable threshold (executable edge ≥ 3% AND
depth ≥ $10K), with the team-strength + situational overlay tag included when
present. Persistent dedup so the same edge doesn't re-fire every scan
(matches the MLB prop_alert_dedup pattern — see 2026-07-07 audit rec 1).

Dedup key: (sport, game_id, participant). Cooldown: 4h, re-alert if the
executable edge improves by ≥ 10pp (like MLB's 10pp re-alert).

Generalized from signals/nfl_edges_alerts.py (2026-08-22). The dedup table
gained a `sport` column so game_ids from different sports never collide.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# ── Thresholds ───────────────────────────────────────────────────
MIN_EXEC_EDGE = 0.03        # 3% executable edge
MIN_DEPTH_USD = 10_000      # $10K fillable depth
COOLDOWN_HOURS = 4          # dedup cooldown per (sport, game, participant)
REALERT_PP = 0.10           # re-alert if exec edge improves by ≥10pp

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# Sport → emoji for alert headers
SPORT_EMOJI: Dict[str, str] = {
    "NFL": "🏈", "NBA": "🏀", "MLB": "⚾", "soccer": "⚽",
    "UFC": "🥊", "NHL": "🏒", "CFB": "🏈", "default": "🎯",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sport_edge_alert_dedup (
            sport TEXT NOT NULL,
            game_id TEXT NOT NULL,
            participant TEXT NOT NULL,
            alerted_at REAL NOT NULL,
            exec_edge REAL,
            PRIMARY KEY (sport, game_id, participant)
        )
    """)
    # Migrate legacy nfl table (idempotent) — copy rows into the generic table
    try:
        conn.execute("""
            INSERT OR IGNORE INTO sport_edge_alert_dedup (sport, game_id, participant, alerted_at, exec_edge)
            SELECT 'NFL', game_id, participant, alerted_at, exec_edge FROM nfl_edge_alert_dedup
        """)
        conn.commit()
    except Exception:
        pass
    return conn


def _load_dedup() -> Dict[str, Dict]:
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT sport, game_id, participant, alerted_at, exec_edge FROM sport_edge_alert_dedup"
        ).fetchall()
        conn.close()
        return {f"{s}|{g}|{p}": {"ts": t, "edge": e} for s, g, p, t, e in rows}
    except Exception as e:
        logger.debug(f"sport alert dedup load failed: {e}")
        return {}


def _save_dedup(sport: str, game_id: str, participant: str, ts: float, edge: float) -> None:
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO sport_edge_alert_dedup (sport, game_id, participant, alerted_at, exec_edge) "
            "VALUES (?,?,?,?,?)",
            (sport, game_id, participant, ts, edge),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"sport alert dedup save failed: {e}")


def _should_alert(prev: Optional[Dict], exec_edge: float) -> bool:
    """True if no prior alert, past cooldown, or edge improved ≥10pp."""
    if not prev:
        return True
    if (time.time() - prev["ts"]) > COOLDOWN_HOURS * 3600:
        return True
    if prev["edge"] is not None and exec_edge >= prev["edge"] + REALERT_PP - 1e-9:
        return True
    return False


def _overlay_tag(edge) -> str:
    """Build the overlay tag for the alert line (NFL strength/situational)."""
    parts = []
    if getattr(edge, "strength_agree", None) is not None:
        parts.append("STR:" + ("agree" if edge.strength_agree else "conflict"))
    if getattr(edge, "situational_edge_pct", None) is not None:
        parts.append(f"SITU:{edge.situational_edge_pct * 100:+.1f}%")
    if getattr(edge, "home_qb", None):
        parts.append(f"QB:{edge.home_qb.get('status','?')}")
    if getattr(edge, "away_qb", None):
        parts.append(f"QB:{edge.away_qb.get('status','?')}")
    return " · ".join(parts) if parts else ""


def run_sport_edge_alerts(edges: list, sport: str = "NFL",
                          min_exec_edge: float = MIN_EXEC_EDGE,
                          min_depth: float = MIN_DEPTH_USD) -> Dict:
    """Scan edges, alert on tradeable ones above threshold with dedup.

    sport is used for the dedup key + alert header emoji. Returns
    {"alerted": n, "scanned": n, "skipped": n}.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SMART_WALLET_ALERT_SEND") == "0":
        return {"alerted": 0, "scanned": len(edges), "skipped": len(edges)}

    prev_state = _load_dedup()
    to_alert = []
    for e in edges:
        exec_edge = getattr(e, "executable_edge", None)
        depth = getattr(e, "fillable_usd", None) or 0
        if exec_edge is None or exec_edge < min_exec_edge:
            continue
        if depth < min_depth:
            continue
        game_id = getattr(e, "event_id", None) or getattr(e, "poly_event_id", None) or e.event_title
        key = f"{sport}|{game_id}|{e.participant}"
        if _should_alert(prev_state.get(key), exec_edge):
            to_alert.append(e)

    if not to_alert:
        return {"alerted": 0, "scanned": len(edges), "skipped": len(edges)}

    # Send
    emoji = SPORT_EMOJI.get(sport, "🎯")
    try:
        from scripts.openclaw_alerts import alert_openclaw
        lines = [f"{emoji} {sport} Edge Alerts"]
        for e in to_alert[:8]:
            tag = _overlay_tag(e)
            lines.append(
                f"• {e.event_title} — {e.participant} "
                f"({e.direction} @ {e.executable_price * 100:.0f}¢)\n"
                f"   exec edge {e.executable_edge * 100:+.1f}% · depth ${e.fillable_usd:.0f}"
                + (f" · {tag}" if tag else "")
            )
        alert_openclaw("\n".join(lines))
    except Exception as ex:
        logger.debug(f"{sport} edge alert send failed: {ex}")

    # Record dedup for sent alerts
    now = time.time()
    for e in to_alert:
        game_id = getattr(e, "poly_event_id", None) or e.event_title
        _save_dedup(sport, game_id, e.participant, now, e.executable_edge)

    return {"alerted": len(to_alert), "scanned": len(edges), "skipped": len(edges) - len(to_alert)}


# ── Backward-compat alias (NFL callers) ─────────────────────────
def run_nfl_edge_alerts(edges: list, **kwargs) -> Dict:
    """Alias for run_sport_edge_alerts with sport='NFL' (legacy callers)."""
    return run_sport_edge_alerts(edges, sport="NFL", **kwargs)
