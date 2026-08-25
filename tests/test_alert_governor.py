#!/usr/bin/env python3
"""Unit tests for signals/alert_governor.py (G1 gate)."""
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.alert_governor import Leg, govern, purge_stale, _connect, _ensure_tables

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def tmpdb():
    return Path(tempfile.mktemp(suffix=".db"))


def test_replay_orioles_spam():
    """Tonight's 3x Orioles/Royals drift spam -> exactly 1 send."""
    db = tmpdb()
    t0 = time.time()
    legs = lambda m: [Leg("Orioles", m, "down")]
    v1 = govern("mlb_line_drift", "g1", legs(59.0), db, _now=t0)
    v2 = govern("mlb_line_drift", "g1", legs(58.0), db, _now=t0 + 105)   # 01:16:39-ish
    v3 = govern("mlb_line_drift", "g1", legs(61.0), db, _now=t0 + 578)   # 01:24:32-ish
    check("first fires", v1.action == "fire" and v1.should_send)
    check("repeat suppressed", v2.action == "suppress" and not v2.should_send, v2.reasons)
    check("third suppressed (within ±5)", v3.action == "suppress" and not v3.should_send, v3.reasons)


def test_upgrade_fires_with_delta_line():
    db = tmpdb()
    t0 = time.time()
    v1 = govern("mlb_run", "g2", [Leg("Yankees", 6.0, "BUY")], db, _now=t0)
    v2 = govern("mlb_run", "g2", [Leg("Yankees", 12.0, "BUY")], db, _now=t0 + 120)
    check("baseline fires", v1.action == "fire")
    check("upgrade fires", v2.action == "fire_upgrade" and v2.should_send, v2.reasons)
    check("delta line present", "6" in v2.delta_line and "12" in v2.delta_line, v2.delta_line)
    check("decorate prepends", v2.decorate("body").startswith("⬆"))


def test_direction_flip_fires():
    db = tmpdb()
    t0 = time.time()
    govern("mlb_run", "g3", [Leg("Mets", 7.0, "BUY")], db, _now=t0)
    v = govern("mlb_run", "g3", [Leg("Mets", 7.0, "SELL")], db, _now=t0 + 60)
    check("flip fires", v.action == "fire" and any("flip" in r for r in v.reasons), v.reasons)


def test_new_leg_fires():
    db = tmpdb()
    t0 = time.time()
    govern("mlb_run", "g4", [Leg("Reds", 8.0, "BUY")], db, _now=t0)
    v = govern("mlb_run", "g4", [Leg("Reds", 8.0, "BUY"), Leg("Cubs", 6.0, "SELL")], db, _now=t0 + 60)
    check("new leg fires", v.action == "fire" and any("new:Cubs" in r for r in v.reasons), v.reasons)
    # C5: the pre-existing leg's state must survive the multi-leg write
    v2 = govern("mlb_run", "g4", [Leg("Reds", 8.0, "BUY")], db, _now=t0 + 120)
    check("old leg still suppressed", v2.action == "suppress", v2.reasons)


def test_rearm_capped():
    db = tmpdb()
    t0 = time.time()
    govern("mlb_run", "g5", [Leg("Twins", 7.0, "BUY")], db, _now=t0)
    v1 = govern("mlb_run", "g5", [Leg("Twins", 7.0, "BUY")], db, _now=t0 + 46 * 60)
    v2 = govern("mlb_run", "g5", [Leg("Twins", 7.0, "BUY")], db, _now=t0 + 92 * 60)
    v3 = govern("mlb_run", "g5", [Leg("Twins", 7.0, "BUY")], db, _now=t0 + 140 * 60)
    check("rearm 1 fires", v1.action == "fire" and "rearm:Twins" in v1.reasons, v1.reasons)
    check("rearm 2 fires", v2.action == "fire" and "rearm:Twins" in v2.reasons, v2.reasons)
    check("rearm 3 capped (C7)", v3.action == "suppress", v3.reasons)


def test_seed_migration():
    """C4: live mlb_run_alert_state cooldowns survive the cutover."""
    db = tmpdb()
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE mlb_run_alert_state (game_id TEXT PRIMARY KEY, last_sent_ts TEXT, last_signature TEXT)")
    from datetime import datetime, timezone
    con.execute("INSERT INTO mlb_run_alert_state VALUES (?,?,?)",
                ("g6", datetime.now(timezone.utc).isoformat(), "BUY:Yankees|SELL:Nationals"))
    con.commit()
    con.close()
    t0 = time.time()
    v = govern("mlb_run", "g6", [Leg("Yankees", 6.0, "BUY"), Leg("Nationals", 6.0, "SELL")], db, _now=t0)
    check("seeded cooldown suppresses", v.action == "suppress", v.reasons)
    v2 = govern("mlb_run", "g6", [Leg("Yankees", 6.0, "SELL")], db, _now=t0 + 60)
    check("seeded row still fires on flip", v2.action == "fire", v2.reasons)


def test_lock_contention_suppresses():
    """C1: persistent lock -> suppress, not fail-open double-send."""
    db = tmpdb()
    govern("mlb_run", "g7", [Leg("A", 6.0, "BUY")], db)  # create db/tables
    blocker = sqlite3.connect(str(db))
    blocker.execute("BEGIN IMMEDIATE")
    v = govern("mlb_run", "g7", [Leg("B", 9.0, "BUY")], db)
    blocker.rollback()
    blocker.close()
    check("lock contention suppresses", v.action == "suppress" and "lock-contention" in v.reasons, v.reasons)


def test_fail_open_on_bad_db():
    v = govern("mlb_run", "g8", [Leg("A", 6.0, "BUY")], Path("/nonexistent/dir/x.db"))
    check("fail-open on unexpected error", v.action == "fire" and v.should_send, v.reasons)


def test_shadow_mode():
    from signals import alert_governor
    alert_governor.CONFIG["test_shadow"] = {"delta": 5.0, "rearm_min": 45, "max_rearms": 2, "mode": "shadow"}
    db = tmpdb()
    t0 = time.time()
    govern("test_shadow", "g9", [Leg("X", 6.0, "BUY")], db, _now=t0)
    v = govern("test_shadow", "g9", [Leg("X", 6.0, "BUY")], db, _now=t0 + 60)
    check("shadow logs suppress but still sends", v.action == "suppress" and v.should_send, (v.action, v.should_send))


def test_purge():
    db = tmpdb()
    govern("mlb_run", "g10", [Leg("A", 6.0, "BUY")], db, _now=time.time() - 10 * 86400)
    n = purge_stale(db, max_age_days=7)
    check("purge drops stale", n == 1, n)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{'='*40}\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
