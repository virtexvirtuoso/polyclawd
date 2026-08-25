#!/usr/bin/env python3
"""
Soccer / World Cup shadow-trade resolver + calibration (closed-loop, mirrors
signals/baseball_resolver.py). Activates the dormant soccer pipeline:

  - For each unresolved soccer shadow (strategy soccer_match_3way / soccer_futures),
    fetch its OWN Polymarket market via CLOB ONCE per cycle:
      * still open  -> capture the current YES mid into closing_yes_mid (the value
                       at the last pre-resolution cycle becomes the close -> CLV).
      * resolved    -> score win/loss + pnl, compute CLV vs the captured PM close,
                       write soccer_forecast_log, mark the shadow resolved.
  - get_soccer_calibration(): edge-bucket -> realized win-rate + avg CLV, per
    strategy. This is the kill-rule / "is the edge real" data the dashboard needs.

Free (Polymarket CLOB/Gamma). Rate-limited 1 call / 5s like the baseball resolver.
CLV here is vs the Polymarket CLOSE (our own venue) — it measures whether PM
corrected toward the sharp book we faded; labelled as such so it is not confused
with an independent-close CLV.
"""

import sqlite3
import time
from datetime import datetime, timezone, date
from typing import Dict, Any, List, Optional

from loguru import logger

# Self-contained Polymarket plumbing. NOTE: do NOT import _fetch_json from
# baseball_resolver — the deployed copy of that module has carried a urllib-scoping
# bug in its _fetch_json (caught by /qa 2026-06-10), which would silently break
# every CLOB call here. Own the fetch so this resolver can't inherit that rot.
import json as _json
import urllib.parse as _uparse
import urllib.request as _urequest
from pathlib import Path as _Path
from config.polymarket_urls import CLOB_API  # polyproxy: central URL config

DB_PATH = _Path(__file__).parent.parent / "storage" / "shadow_trades.db"

RATE_DELAY = 5.0

def _fetch_json(url, params=None, timeout=10):
    try:
        u = url + ("?" + _uparse.urlencode(params) if params else "")
        req = _urequest.Request(u, headers={"User-Agent": "Polyclawd/2.0"})
        with _urequest.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())
    except Exception as e:  # pragma: no cover
        logger.debug(f"soccer_resolver fetch failed: {str(url)[:60]} — {e}")
        return None

SOCCER_STRATEGIES = ("soccer_match_3way", "soccer_futures")

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
        CREATE TABLE IF NOT EXISTS soccer_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shadow_trade_id INTEGER,
            strategy TEXT,
            market_model TEXT,        -- 3way | outright
            event_title TEXT,
            participant TEXT,
            book_prob REAL,
            poly_entry REAL,          -- executable entry price of the held token
            edge_pct REAL,
            direction TEXT,           -- BUY | SELL
            closing_yes_mid REAL,     -- PM YES mid at the last pre-resolution cycle
            clv_pct REAL,             -- (held-token close - entry), pp; vs PM close
            outcome TEXT,             -- YES | NO (which side won)
            predicted_correct INTEGER,
            pnl REAL,
            recorded_at TEXT,
            UNIQUE(shadow_trade_id)
        );
        """
    )
    conn.commit()
    # Idempotent: closing-mid capture column on the shared shadow_trades table.
    for col, decl in (("closing_yes_mid", "REAL"),):
        try:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {decl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

def _clob_market(market_id: str) -> Optional[Dict]:
    """Fetch one CLOB market. Returns the raw dict or None."""
    return _fetch_json(f"{CLOB_API}/markets/{market_id}", timeout=10)

def _market_state(data: Dict):
    """(is_closed, winner_idx_or_None, yes_mid_or_None) from a CLOB market payload.
    yes_mid is tokens[0] price (the YES/participant token)."""
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
        if winner_idx is None:  # price-based fallback
            for i, t in enumerate(tokens):
                try:
                    if float(t.get("price", 0)) > 0.9:
                        winner_idx = i
                        break
                except (TypeError, ValueError):
                    pass
    return (closed, winner_idx, yes_mid)

def _score(side: str, entry_price: float, winner_idx: int):
    """Binary 'Will X win?' market. side YES=BUY participant, NO=SELL. Held token
    is YES (BUY) or NO (SELL), bought at entry_price. Returns (correct, pnl)."""
    is_buy = (side or "YES").upper() == "YES"
    participant_won = winner_idx == 0
    bet_won = participant_won if is_buy else (not participant_won)
    p = entry_price if entry_price is not None else 0.5
    pnl = (1.0 - p) if bet_won else -p
    return (1 if bet_won else 0), round(pnl, 4)

def _clv(side: str, entry_price: float, closing_yes_mid: Optional[float]) -> Optional[float]:
    """CLV in pp of the held token vs the PM close. None if no close captured."""
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

def scan_resolved_soccer_trades(batch_size: int = 200) -> Dict[str, Any]:
    """Resolve closed soccer shadows + capture PM closing mid for still-open ones."""
    result = {"resolved": 0, "clv_captured": 0, "skipped": 0, "errors": 0}
    conn = get_db()
    try:
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT id, market_id, side, entry_price, market, reasoning, strategy, "
                "closing_yes_mid FROM shadow_trades "
                "WHERE resolved = 0 AND strategy IN (?, ?) ORDER BY timestamp ASC",
                SOCCER_STRATEGIES,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        conn.close()
        return {**result, "note": "shadow_trades not ready"}

    if not trades:
        conn.close()
        return {**result, "note": "no unresolved soccer trades"}

    for t in trades[:batch_size]:
        mid = t.get("market_id", "")
        if not mid or not mid.startswith("0x"):
            result["skipped"] += 1
            continue
        data = _clob_market(mid)
        time.sleep(RATE_DELAY)
        closed, winner_idx, yes_mid = _market_state(data)

        if not closed:
            # capture the latest PM mid as the running close
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
        close_mid = t.get("closing_yes_mid")  # last captured before resolution
        clv = _clv(side, entry, close_mid)
        outcome = "YES" if winner_idx == 0 else "NO"
        strat = t.get("strategy", "")
        model = "outright" if strat == "soccer_futures" else "3way"
        mk = t.get("market", "")
        event_title = mk.split(" — ")[0] if " — " in mk else mk
        participant = ""
        if " — " in mk:
            tail = mk.split(" — ", 1)[1]
            participant = tail.rsplit(" ", 1)[0] if " " in tail else tail

        try:
            conn.execute(
                """INSERT OR IGNORE INTO soccer_forecast_log
                   (shadow_trade_id, strategy, market_model, event_title, participant,
                    book_prob, poly_entry, edge_pct, direction, closing_yes_mid, clv_pct,
                    outcome, predicted_correct, pnl, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t["id"],
                    strat,
                    model,
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
            logger.warning(f"soccer forecast insert failed: {e}")
        try:
            conn.execute(
                "UPDATE shadow_trades SET resolved=1, resolved_at=?, outcome=?, pnl=?, exit_price=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), outcome, pnl, 1.0 if correct else 0.0, t["id"]),
            )
            result["resolved"] += 1
        except Exception as e:
            logger.warning(f"soccer shadow update failed: {e}")
            result["errors"] += 1

    conn.commit()
    conn.close()
    if result["resolved"] or result["clv_captured"]:
        logger.info(f"soccer_resolver: {result}")
    return result

def get_soccer_calibration() -> Dict:
    """Edge-bucket -> realized win-rate + avg CLV, per strategy + overall. For the
    dashboard / kill rules. Degrades to empty pre-data."""
    out = {"by_strategy": [], "calibration": [], "clv": {}, "totals": {}}
    try:
        conn = get_db()
        out["by_strategy"] = [
            {
                "strategy": r["strategy"],
                "n": r["n"],
                "wins": r["w"],
                "win_pct": round(r["w"] / r["n"] * 100, 1) if r["n"] else None,
                "avg_pnl": round(r["ap"], 4) if r["ap"] is not None else None,
                "clv_n": r["cn"],
                "avg_clv_pp": round(r["ac"], 1) if r["ac"] is not None else None,
            }
            for r in conn.execute(
                "SELECT strategy, COUNT(*) n, SUM(predicted_correct) w, AVG(pnl) ap, "
                "COUNT(clv_pct) cn, AVG(clv_pct) ac FROM soccer_forecast_log GROUP BY strategy"
            ).fetchall()
        ]
        out["calibration"] = [
            {
                "edge_bucket": r["b"],
                "n": r["n"],
                "win_pct": round(r["w"] / r["n"] * 100, 1) if r["n"] else None,
                "avg_clv_pp": round(r["ac"], 1) if r["ac"] is not None else None,
            }
            for r in conn.execute(
                "SELECT CAST(edge_pct/3 AS INT)*3 AS b, COUNT(*) n, SUM(predicted_correct) w, "
                "AVG(clv_pct) ac FROM soccer_forecast_log GROUP BY b ORDER BY b DESC"
            ).fetchall()
        ]
        tot = conn.execute(
            "SELECT COUNT(*) n, SUM(predicted_correct) w, COUNT(clv_pct) cn, AVG(clv_pct) ac, "
            "SUM(CASE WHEN clv_pct>0 THEN 1 ELSE 0 END) cpos FROM soccer_forecast_log"
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
    except Exception as e:  # pragma: no cover
        out["error"] = str(e)
    return out

if __name__ == "__main__":
    print("resolve:", scan_resolved_soccer_trades())
    print("calibration:", get_soccer_calibration())
