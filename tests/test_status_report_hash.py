"""Task 4.2 — status-report change detection (persisted state hash in kv)."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import scripts.sports_pulse as sp

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 7, 16, 12, 5, tzinfo=ET)
EIGHT_PM = datetime(2026, 7, 16, 20, 5, tzinfo=ET)

REPORT_A = "Polymarket Sports Pulse · Jul 16, 2026 · 12:05 PM ET\n────\nBTC UP 0.62"
REPORT_A_LATER = "Polymarket Sports Pulse · Jul 16, 2026 · 4:05 PM ET\n────\nBTC UP 0.62"
REPORT_B = "Polymarket Sports Pulse · Jul 16, 2026 · 4:05 PM ET\n────\nBTC DOWN 0.31"


def test_first_send_allowed(tmp_path):
    db = tmp_path / "s.db"
    assert sp.should_send_status(REPORT_A, db_path=db, now=NOON) is True


def test_unchanged_report_skipped(tmp_path):
    db = tmp_path / "s.db"
    sp.record_status_sent(REPORT_A, db_path=db)
    assert sp.should_send_status(REPORT_A, db_path=db, now=NOON) is False


def test_timestamp_only_change_still_skipped(tmp_path):
    db = tmp_path / "s.db"
    sp.record_status_sent(REPORT_A, db_path=db)
    assert sp.should_send_status(REPORT_A_LATER, db_path=db, now=NOON) is False


def test_changed_report_sends(tmp_path):
    db = tmp_path / "s.db"
    sp.record_status_sent(REPORT_A, db_path=db)
    assert sp.should_send_status(REPORT_B, db_path=db, now=NOON) is True


def test_20_et_slot_always_sends(tmp_path):
    db = tmp_path / "s.db"
    sp.record_status_sent(REPORT_A, db_path=db)
    assert sp.should_send_status(REPORT_A, db_path=db, now=EIGHT_PM) is True


def test_hash_persisted_in_kv_table(tmp_path):
    db = tmp_path / "s.db"
    sp.record_status_sent(REPORT_A, db_path=db)
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT v FROM kv WHERE k='status_report_hash'").fetchone()
    conn.close()
    assert row and len(row[0]) == 64  # sha256 hexdigest
