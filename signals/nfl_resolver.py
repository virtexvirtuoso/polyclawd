#!/usr/bin/env python3
"""nfl_resolver.py — Resolve NFL shadow trades + capture CLV.

Same pattern as ufc_resolver.py:
  - Still open → capture current YES mid as closing_yes_mid (running close for CLV)
  - Resolved   → record outcome + PnL + final CLV

Runs on the 30min scheduler tick during NFL season.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"
CLOB_API = "https://clob.polymarket.com"


def _fetch_json(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Ensure closing_yes_mid column exists
    for col, decl in (("closing_yes_mid", "REAL"),):
        try:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {decl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def _clv(side: str, entry_price: float, closing_yes_mid: Optional[float]) -> Optional[float]:
    if closing_yes_mid is None or entry_price is None:
        return None
    held_close = closing_yes_mid if (side or "YES").upper() == "YES" else (1.0 - closing_yes_mid)
    held_entry = entry_price if (side or "YES").upper() == "YES" else (1.0 - entry_price)
    return round(held_close - held_entry, 4) if held_entry > 0 else None


def scan_resolved_nfl_trades() -> dict:
    """Resolve NFL shadow trades via CLOB + capture CLV."""
    conn = _get_conn()
    result = {"resolved": 0, "clv_captured": 0, "skipped": 0}

    trades = conn.execute("""
        SELECT id, market_id, side, entry_price, market, reasoning,
               closing_yes_mid FROM shadow_trades
        WHERE resolved=0 AND strategy LIKE 'nfl%'
        ORDER BY timestamp ASC
    """).fetchall()

    if not trades:
        conn.close()
        return {**result, "note": "No unresolved NFL trades"}

    for t in trades:
        mid = t["market_id"]
        if not mid or not mid.startswith("0x"):
            result["skipped"] += 1
            continue

        data = _fetch_json(f"{CLOB_API}/markets/{mid}")
        if not data:
            result["skipped"] += 1
            continue

        # Still open → capture CLV mid
        if not (data.get("closed") or data.get("resolved")):
            tokens = data.get("tokens", [])
            if tokens:
                try:
                    yes_mid = float(tokens[0].get("price", 0))
                    if 0.02 < yes_mid < 0.98:
                        conn.execute(
                            "UPDATE shadow_trades SET closing_yes_mid=? WHERE id=?",
                            (yes_mid, t["id"]),
                        )
                        result["clv_captured"] += 1
                except (ValueError, TypeError):
                    pass
            result["skipped"] += 1
            continue

        # Resolved — find winner
        tokens = data.get("tokens", [])
        winner_idx = None
        for i, tok in enumerate(tokens):
            if tok.get("winner") is True:
                winner_idx = i
                break
        if winner_idx is None:
            for i, tok in enumerate(tokens):
                try:
                    if float(tok.get("price", 0)) > 0.9:
                        winner_idx = i
                        break
                except (ValueError, TypeError):
                    pass

        if winner_idx is None:
            result["skipped"] += 1
            continue

        outcome = "YES" if winner_idx == 0 else "NO"
        side = (t["side"] or "YES").upper()
        entry = t["entry_price"] or 0.5
        won = (side == outcome)
        eff_entry = entry if side == "YES" else (1.0 - entry)
        pnl = (1.0 - eff_entry) if won else -eff_entry
        close_mid = t["closing_yes_mid"]
        clv = _clv(side, entry, close_mid)

        conn.execute("""
            UPDATE shadow_trades
            SET resolved=1, resolved_at=?, outcome=?, pnl=?, exit_price=?
            WHERE id=?
        """, (datetime.now(timezone.utc).isoformat(), outcome,
              round(pnl, 4), 1.0 if won else 0.0, t["id"]))
        result["resolved"] += 1

        if clv is not None:
            logger.info(f"NFL resolved: {t['market'][:60]} → {outcome} "
                        f"(won={won}, CLV={clv:+.3f})")

    conn.commit()
    conn.close()
    return result


if __name__ == "__main__":
    r = scan_resolved_nfl_trades()
    print(f"NFL resolver: {r}")
