#!/usr/bin/env python3
"""
whale_gate_check.py — Fire one-time TG alerts when resolved CRITICAL whale count
crosses model upgrade gates (200, 500).

Run after whale_resolution_tracker.py (daily 8am ET).
State file prevents repeat fires.
"""

import sqlite3
import json
import os
import sys
import requests
import logging
from datetime import datetime, timezone

# Ensure project root is on sys.path regardless of how this script is invoked
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from scripts.alert_formatter import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("whale_gate_check")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "storage", "whale_meta.db")
STATE_FILE = os.path.join(BASE_DIR, "storage", "whale_gate_state.json")

GATES = [
    {
        "name": "week2",
        "threshold": 200,
        "message": (
            "🎯 *Whale Model — Week 2 Gate Reached*\n\n"
            "✅ {count} resolved CRITICAL alerts in DB\n"
            "Precision: {precision:.1%} | Required: 200\n\n"
            "Next: Run `/model-upgrade` assessment — auto-weight tuning eligible.\n"
            "Vault: `02-Projects/Polyclawd/05-Decisions/2026-06-14-QA-Whale-Autolearning.md`"
        ),
    },
    {
        "name": "week5",
        "threshold": 500,
        "message": (
            "🎯 *Whale Model — Week 5 Gate Reached*\n\n"
            "✅ {count} resolved CRITICAL alerts in DB\n"
            "Precision: {precision:.1%} | Required: 500\n\n"
            "Next: Full precision band assessment + weight lock-in.\n"
            "Brier baseline calibration ready."
        ),
    },
]

def get_stats(db_path: str):
    if not os.path.exists(db_path):
        logger.error("DB not found: %s", db_path)
        return 0, 0.0
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(correct_res) AS wins
            FROM whale_outcomes
            WHERE severity = 'CRITICAL'
              AND correct_res IS NOT NULL
        """).fetchone()
    if not row or row["total"] == 0:
        return 0, 0.0
    precision = (row["wins"] or 0) / row["total"]
    return row["total"], precision


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    count, precision = get_stats(DB_PATH)
    logger.info("Resolved CRITICAL: %d | Precision: %.1f%%", count, precision * 100)

    state = load_state()
    fired_any = False

    for gate in GATES:
        name = gate["name"]
        threshold = gate["threshold"]

        if count < threshold:
            logger.info("Gate %s: %d/%d — not crossed", name, count, threshold)
            continue

        if state.get(name, {}).get("fired"):
            logger.info("Gate %s already fired on %s — skip", name, state[name].get("fired_at"))
            continue

        msg = gate["message"].format(count=count, precision=precision)
        logger.info("Firing gate alert: %s", name)

        if send_telegram(msg):
            state[name] = {
                "fired": True,
                "fired_at": datetime.now(timezone.utc).isoformat(),
                "count_at_fire": count,
                "precision_at_fire": round(precision, 4),
            }
            save_state(state)
            fired_any = True
            logger.info("Gate %s alert sent and state saved", name)
        else:
            logger.error("Gate %s alert FAILED to send", name)
            sys.exit(1)

    if not fired_any:
        logger.info("No gates crossed or all already fired. Current: %d resolved CRITICAL", count)


if __name__ == "__main__":
    main()
