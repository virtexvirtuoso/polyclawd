#!/usr/bin/env python3
"""
UFC shadow-trade resolver + calibration (closed-loop, mirrors soccer_resolver.py).

For each unresolved UFC shadow (strategy ufc_moneyline):
  - Fetch its Polymarket CLOB market
  - Still open → capture current YES mid as closing_yes_mid (running close for CLV)
  - Resolved → score win/loss + PnL, compute CLV vs PM close, write ufc_forecast_log,
    mark shadow resolved

CLV is vs Polymarket CLOSE (our own venue) — measures whether PM corrected toward
the sharp book we faded.
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from loguru import logger

import json as _json
import urllib.parse as _uparse
import urllib.request as _urequest
from pathlib import Path as _Path

DB_PATH = _Path(__file__).parent.parent / "storage" / "shadow_trades.db"
CLOB_API = "https://clob.polymarket.com"
RATE_DELAY = 5.0

UFC_STRATEGIES = ("ufc_moneyline",)


def _fetch_json(url, params=None, timeout=10):
    try:
        u = url + ("?" + _uparse.urlencode(params) if params else "")
        req = _urequest.Request(u, headers={"User-Agent": "Polyclawd/2.0"})
        with _urequest.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())
    except Exception as e:
        logger.debug(f"ufc_resolver fetch failed: {str(url)[:60]} — {e}")
        return None


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ufc_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shadow_trade_id INTEGER,
            strategy TEXT,
            event_title TEXT,
            participant TEXT,
            book_prob REAL,
            poly_entry REAL,
            edge_pct REAL,
            direction TEXT,
            closing_yes_mid REAL,
            clv_pct REAL,
            outcome TEXT,
            predicted_correct INTEGER,
            pnl REAL,
            recorded_at TEXT,
            UNIQUE(shadow_trade_id)
        );
        """
    )
    conn.commit()
    # Idempotent: closing_yes_mid column (may already exist from soccer_resolver)
    for col, decl in (("closing_yes_mid", "REAL"),):
        try:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {decl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _clob_market(market_id: str) -> Optional[Dict]:
    return _fetch_json(f"{CLOB_API}/markets/{market_id}", timeout=10)


def _market_state(data: Dict):
    """(is_closed, winner_idx_or_None, yes_mid_or_None)"""
    if not data:
        return (False, None, None)
    tokens = data.get("tokens", []) or []
    yes_mid = None
    if tokens:
        try:
            yes_mid = float(tokens[0].get("price")) if tokens[0].get("price") is not None else None
        except (TypeError, ValueError):
            yes_mid = None
    closed = bool(data.get("closed") or data.get("resolved"))
    winner_idx = None
    if closed:
        for i, t in enumerate(tokens):
            if t.get("winner") is True:
                winner_idx = i
                break
        if winner_idx is None:
            for i, t in enumerate(tokens):
                try:
                    if float(t.get("price", 0)) > 0.9:
                        winner_idx = i
                        break
                except (TypeError, ValueError):
                    pass
    return (closed, winner_idx, yes_mid)


def _score(side: str, entry_price: float, winner_idx: int):
    is_buy = (side or "YES").upper() == "YES"
    participant_won = winner_idx == 0
    bet_won = participant_won if is_buy else (not participant_won)
    p = entry_price if entry_price is not None else 0.5
    pnl = (1.0 - p) if bet_won else -p
    return (1 if bet_won else 0), round(pnl, 4)


def _clv(side: str, entry_price: float, closing_yes_mid: Optional[float]) -> Optional[float]:
    if closing_yes_mid is None or entry_price is None:
        return None
    held_close = closing_yes_mid if (side or "YES").upper() == "YES" else (1.0 - closing_yes_mid)
    return round((held_close - entry_price) * 100, 1)


def _edge_from_reasoning(reasoning: str) -> float:
    import re
    m = re.search(r"exec edge ([+-]?\d+\.?\d*)%", reasoning or "")
    if m:
        return float(m.group(1))
    m = re.search(r"([+-]?\d+\.?\d*)%\s*edge", reasoning or "")
    return float(m.group(1)) if m else 0.0


def scan_resolved_ufc_trades(batch_size: int = 100) -> Dict[str, Any]:
    """Resolve closed UFC shadows + capture PM closing mid for still-open ones."""
    result = {"resolved": 0, "clv_captured": 0, "skipped": 0, "errors": 0}
    conn = get_db()
    try:
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT id, market_id, side, entry_price, market, reasoning, strategy, "
                "closing_yes_mid FROM shadow_trades "
                "WHERE resolved = 0 AND strategy IN (?) ORDER BY timestamp ASC",
                UFC_STRATEGIES,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        conn.close()
        return {**result, "note": "shadow_trades not ready"}

    if not trades:
        conn.close()
        return {**result, "note": "no unresolved UFC trades"}

    for t in trades[:batch_size]:
        mid = t.get("market_id", "")
        if not mid or not mid.startswith("0x"):
            result["skipped"] += 1
            continue
        data = _clob_market(mid)
        time.sleep(RATE_DELAY)
        closed, winner_idx, yes_mid = _market_state(data)

        if not closed:
            if yes_mid is not None:
                conn.execute("UPDATE shadow_trades SET closing_yes_mid=? WHERE id=?", (yes_mid, t["id"]))
                result["clv_captured"] += 1
            else:
                result["skipped"] += 1
            continue
        if winner_idx is None:
            result["skipped"] += 1
            continue

        side = t.get("side", "YES")
        entry = t.get("entry_price", 0.5)
        correct, pnl = _score(side, entry, winner_idx)
        close_mid = t.get("closing_yes_mid")
        clv = _clv(side, entry, close_mid)
        outcome = "YES" if winner_idx == 0 else "NO"
        mk = t.get("market", "")
        # Parse "UFC 329: Max Holloway vs. Conor McGregor ... — Max Holloway moneyline"
        event_title = mk.split(" — ")[0] if " — " in mk else mk
        participant = ""
        if " — " in mk:
            tail = mk.split(" — ", 1)[1]
            participant = tail.replace(" moneyline", "").strip()

        try:
            conn.execute(
                """INSERT OR IGNORE INTO ufc_forecast_log
                   (shadow_trade_id, strategy, event_title, participant,
                    book_prob, poly_entry, edge_pct, direction, closing_yes_mid, clv_pct,
                    outcome, predicted_correct, pnl, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t["id"],
                    t.get("strategy", "ufc_moneyline"),
                    event_title[:180],
                    participant[:80],
                    None,
                    entry,
                    _edge_from_reasoning(t.get("reasoning", "")),
                    ("BUY" if side == "YES" else "SELL"),
                    close_mid,
                    clv,
                    outcome,
                    correct,
                    pnl,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception as e:
            logger.warning(f"ufc forecast insert failed: {e}")
        try:
            conn.execute(
                "UPDATE shadow_trades SET resolved=1, resolved_at=?, outcome=?, pnl=?, exit_price=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), outcome, pnl, 1.0 if correct else 0.0, t["id"]),
            )
            result["resolved"] += 1
        except Exception as e:
            logger.warning(f"ufc shadow update failed: {e}")
            result["errors"] += 1

    conn.commit()
    conn.close()
    if result["resolved"] or result["clv_captured"]:
        logger.info(f"ufc_resolver: {result}")
    return result


def get_ufc_calibration() -> Dict:
    """Edge-bucket → realized win-rate + avg CLV for UFC shadows."""
    out = {"calibration": [], "clv": {}, "totals": {}}
    try:
        conn = get_db()
        out["calibration"] = [
            {
                "edge_bucket": r["b"],
                "n": r["n"],
                "win_pct": round(r["w"] / r["n"] * 100, 1) if r["n"] else None,
                "avg_clv_pp": round(r["ac"], 1) if r["ac"] is not None else None,
            }
            for r in conn.execute(
                "SELECT CAST(edge_pct/3 AS INT)*3 AS b, COUNT(*) n, SUM(predicted_correct) w, "
                "AVG(clv_pct) ac FROM ufc_forecast_log GROUP BY b ORDER BY b DESC"
            ).fetchall()
        ]
        tot = conn.execute(
            "SELECT COUNT(*) n, SUM(predicted_correct) w, COUNT(clv_pct) cn, AVG(clv_pct) ac, "
            "SUM(CASE WHEN clv_pct>0 THEN 1 ELSE 0 END) cpos FROM ufc_forecast_log"
        ).fetchone()
        out["totals"] = {
            "resolved": tot["n"] or 0,
            "wins": tot["w"] or 0,
            "win_pct": round(tot["w"] / tot["n"] * 100, 1) if tot["n"] else None,
        }
        out["clv"] = {
            "n": tot["cn"] or 0,
            "avg_clv_pp": round(tot["ac"], 1) if tot["ac"] is not None else None,
            "clv_positive_pct": round(tot["cpos"] / tot["cn"] * 100, 1) if tot["cn"] else None,
        }
        conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out


if __name__ == "__main__":
    print("resolve:", scan_resolved_ufc_trades())
    print("calibration:", get_ufc_calibration())
